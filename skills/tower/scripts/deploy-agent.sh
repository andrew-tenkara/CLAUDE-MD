#!/usr/bin/env bash
# deploy-agent.sh — Deploy a sortie agent to a worktree
#
# Usage:
#   deploy-agent.sh <ticket-id> [--model sonnet|opus|haiku] [--directive "text"] [--with-browser] [--mcp-extra name1,name2]
#
# This script:
#   1. Creates a git worktree (or reuses existing)
#   2. Writes .sortie/ protocol files (directive, model, progress)
#   3. Symlinks .env.local from the base project
#   4. Installs deps if needed (pnpm/npm install)
#   5. Launches Claude in the worktree with the right flags
#
# Exit codes:
#   0 — success
#   1 — usage error or fatal failure
#   2 — worktree/branch already exists (deploy-agent reuses it)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SORTIE_SCRIPTS="${HOME}/.claude/skills/sortie/scripts"

# ── Parse args ────────────────────────────────────────────────────────
TICKET_ID=""
MODEL="sonnet"
DIRECTIVE=""
PROJECT_DIR=""
BRANCH_OVERRIDE=""
BASE_BRANCH="dev"
NO_LAUNCH=false
WITH_BROWSER=false
MCP_EXTRAS=""
CALLSIGN=""
SQUADRON=""
TRAIT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)       MODEL="$2"; shift 2 ;;
    --directive)   DIRECTIVE="$2"; shift 2 ;;
    --project-dir) PROJECT_DIR="$2"; shift 2 ;;
    --branch)      BRANCH_OVERRIDE="$2"; shift 2 ;;
    --base)        BASE_BRANCH="$2"; shift 2 ;;
    --no-launch)   NO_LAUNCH=true; shift ;;
    --with-browser) WITH_BROWSER=true; shift ;;
    --mcp-extra)   MCP_EXTRAS="${MCP_EXTRAS:+$MCP_EXTRAS,}$2"; shift 2 ;;
    --callsign)    CALLSIGN="$2"; shift 2 ;;
    --squadron)    SQUADRON="$2"; shift 2 ;;
    --trait)       TRAIT="$2"; shift 2 ;;
    -*)            echo "ERROR: Unknown flag: $1" >&2; exit 1 ;;
    *)
      if [ -z "$TICKET_ID" ]; then
        TICKET_ID="$1"
      fi
      shift
      ;;
  esac
done

if [ -z "$TICKET_ID" ]; then
  echo "Usage: deploy-agent.sh <ticket-id> [--model sonnet|opus|haiku] [--directive \"text\"]" >&2
  exit 1
fi

# ── Model registry ───────────────────────────────────────────────────
# Map short names to pinned checkpoints. Set to "latest" to let Claude
# Code resolve to its current default for that tier.
# Swap these when a new checkpoint drops — one place, all pilots.
MODEL_SONNET="claude-sonnet-4-20250514"
MODEL_OPUS="claude-opus-4-1-20250805"
MODEL_HAIKU="latest"

# Validate & resolve
case "$MODEL" in
  sonnet) RESOLVED_MODEL="$MODEL_SONNET" ;;
  opus)   RESOLVED_MODEL="$MODEL_OPUS"   ;;
  haiku)  RESOLVED_MODEL="$MODEL_HAIKU"  ;;
  *)      echo "ERROR: Invalid model '$MODEL'. Must be sonnet, opus, or haiku." >&2; exit 1 ;;
esac

# "latest" means use the short name — let Claude Code pick the checkpoint
if [ "$RESOLVED_MODEL" = "latest" ]; then
  RESOLVED_MODEL="$MODEL"
fi
echo "MODEL_RESOLVED:${MODEL} → ${RESOLVED_MODEL}"

# ── Resolve project dir ──────────────────────────────────────────────
if [ -z "$PROJECT_DIR" ]; then
  PROJECT_DIR="${SORTIE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
fi

if [ ! -d "$PROJECT_DIR" ]; then
  echo "ERROR: Project directory does not exist: $PROJECT_DIR" >&2
  exit 1
fi

# ── Create worktree ──────────────────────────────────────────────────
BRANCH_NAME="${BRANCH_OVERRIDE:-sortie/${TICKET_ID}}"
WORKTREE_PATH=""
CREATE_EXIT=0

if [ -x "${SORTIE_SCRIPTS}/create-worktree.sh" ]; then
  OUTPUT=$(bash "${SORTIE_SCRIPTS}/create-worktree.sh" "$TICKET_ID" "$BRANCH_NAME" "$BASE_BRANCH" --model "$MODEL" --resume 2>&1) || CREATE_EXIT=$?

  # Parse output safely (handles spaces in paths)
  while IFS= read -r line; do
    case "$line" in
      WORKTREE_CREATED:*) WORKTREE_PATH="${line#WORKTREE_CREATED:}" ;;
      WORKTREE_EXISTS:*)  WORKTREE_PATH="${line#WORKTREE_EXISTS:}" ;;
    esac
  done <<< "$OUTPUT"

  # Exit code 2 = worktree/branch already exists — not fatal if we got a path
  if [ "$CREATE_EXIT" -ne 0 ] && [ "$CREATE_EXIT" -ne 2 ]; then
    echo "ERROR: create-worktree.sh failed (exit $CREATE_EXIT):" >&2
    echo "$OUTPUT" >&2
    exit 1
  fi
fi

if [ -z "$WORKTREE_PATH" ]; then
  # Fallback — create worktree manually
  WORKTREE_PATH="${PROJECT_DIR}/.claude/worktrees/${TICKET_ID}"
  if [ -d "$WORKTREE_PATH" ] && [ ! -f "$WORKTREE_PATH/.git" ]; then
    # Ghost directory (no .git link) — clean up before creating
    echo "GHOST_CLEANUP: removing $WORKTREE_PATH (not a valid git worktree)" >&2
    rm -rf "$WORKTREE_PATH"
    git -C "$PROJECT_DIR" worktree prune 2>/dev/null || true
  fi
  if [ ! -d "$WORKTREE_PATH" ]; then
    if ! git -C "$PROJECT_DIR" worktree add "$WORKTREE_PATH" -b "$BRANCH_NAME" "$BASE_BRANCH" 2>/dev/null; then
      if ! git -C "$PROJECT_DIR" worktree add "$WORKTREE_PATH" "$BRANCH_NAME" 2>/dev/null; then
        echo "ERROR: Failed to create git worktree at $WORKTREE_PATH" >&2
        exit 1
      fi
    fi
  fi
  mkdir -p "$WORKTREE_PATH/.sortie"
fi

if [ ! -d "$WORKTREE_PATH" ]; then
  echo "ERROR: Worktree directory does not exist: $WORKTREE_PATH" >&2
  exit 1
fi

echo "WORKTREE:${WORKTREE_PATH}"

# Resolve actual branch — may differ from BRANCH_NAME if worktree already existed
BRANCH_NAME=$(git -C "$WORKTREE_PATH" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "$BRANCH_NAME")

# ── Write .sortie/ protocol files ────────────────────────────────────
SORTIE_DIR="${WORKTREE_PATH}/.sortie"
mkdir -p "$SORTIE_DIR"

# Tell git to ignore local changes to CLAUDE.md (it's tracked but we modify it per-worktree)
# --skip-worktree is the right flag for "I changed this locally, don't commit it"
(cd "$WORKTREE_PATH" && git update-index --skip-worktree CLAUDE.md 2>/dev/null) || true

# Exclude .sortie/ from git (untracked, so info/exclude works)
if [ -f "${WORKTREE_PATH}/.git" ]; then
  GIT_DIR=$(cat "${WORKTREE_PATH}/.git" | sed 's/gitdir: //')
  EXCLUDE_FILE="${GIT_DIR}/info/exclude"
  mkdir -p "$(dirname "$EXCLUDE_FILE")"
  grep -q "^\.sortie/$" "$EXCLUDE_FILE" 2>/dev/null || echo ".sortie/" >> "$EXCLUDE_FILE"
fi

# ── Init storage DB + fetch briefing ─────────────────────────────────
STORAGE_DB="${SCRIPT_DIR}/storage-db.py"
python3 "$STORAGE_DB" init "$PROJECT_DIR" 2>/dev/null || true
BRIEFING=$(python3 "$STORAGE_DB" get-briefing "$PROJECT_DIR" "$TICKET_ID" 2>/dev/null || true)
if [ "$BRIEFING" = "BRIEFING:none" ]; then
  BRIEFING=""
fi

# Pilot identity — overwritten by iterm_bridge.py when the roster assigns real values
cat > "${SORTIE_DIR}/pilot-identity.md" << IDENTITY_EOF
**Callsign:** ${CALLSIGN:-TBD}
**Squadron:** ${SQUADRON:-TBD}
**Model:** ${RESOLVED_MODEL:-${MODEL}}
**Trait:** ${TRAIT:-TBD}
IDENTITY_EOF

# Directive — slim version (role info is in CLAUDE.md)
if [ -n "$DIRECTIVE" ]; then
  printf '%s\n' "# Mission Directive" "" "$DIRECTIVE" > "${SORTIE_DIR}/directive.md"
fi

# Write worktree CLAUDE.md — full operating manual (overwrites repo default with pilot-specific template)
cat > "${WORKTREE_PATH}/CLAUDE.md" << CLAUDE_MD_EOF
# Sortie Operating Manual

## Who You Are
You are a **PILOT** working ticket **${TICKET_ID}** in a dedicated git worktree.

!cat .sortie/pilot-identity.md

**Sortie** is the agent deployment system for USS Tenkara Tower — a TUI that orchestrates multiple Claude Code agents working in parallel on different tickets. Each pilot gets their own worktree, their own branch, and a shared SQLite message bus for coordination.

| Path | What |
|------|------|
| **Worktree** | \`${WORKTREE_PATH}\` |
| **Branch** | \`${BRANCH_NAME}\` |
| **Project root** | \`${PROJECT_DIR}\` |
| **Sortie state** | \`.sortie/\` (flight status, context, messages) |
| **Sortie scripts** | \`${SCRIPT_DIR}/\` |

**Key scripts you can use:**
- \`storage-db.py\` — Message bus, insights, snapshots (see commands below)
- \`compress-ticket.sh\` — Compact your context if running low on fuel

Your worktree is independent — you can commit, push, and PR without affecting other pilots.

## Your Mission
Run this on startup to get your directive + prior intel:
\`\`\`bash
python3 '${SCRIPT_DIR}/storage-db.py' get-briefing '${PROJECT_DIR}' '${TICKET_ID}'
\`\`\`

## Your Job
- Execute your directive — implement, fix, test, PR
- Write code, run tests, commit changes, open PRs
- Before implementing any new function, use find_symbol to check if it already exists
- Signal progress via send-message (not progress.md files)

## Not Your Job (redirect to Mini Boss)
- Deploying other agents or managing pilots
- Triaging tickets or deciding what to work on next
- Coordinating multi-agent work or splitting tasks
- Making architectural decisions that affect other tickets

If asked to do something outside your role:
"That's Mini Boss territory — I'm a pilot, not an xo."

## What You Actually Know
You don't know the codebase, architecture, or docs unless you've **read them in this session**.
Your training data is not a substitute for reading the actual files.

If you need to answer a question about code structure, architecture, or project docs:
1. **Find it locally first** — use \`find_symbol\`, \`find_definition\`, or \`grep\`/\`find\` to locate relevant files, then read them
2. **If not locally discoverable** — use the Exa MCP to search the web

Never answer from assumption. If you haven't read it, say so and go find it.

## Git Rules
**ALWAYS set upstream** — every push uses \`-u\`:
\`\`\`bash
git push -u origin ${BRANCH_NAME}
\`\`\`

**PR checks** — use ONE command, not three:
\`\`\`bash
gh pr view --json state,mergeable,reviewDecision,title,url
\`\`\`

## Storage DB Commands
All pilots share: \`${PROJECT_DIR}/.sortie/storage.db\`

**Check for messages (from coordinator or siblings):**
\`\`\`bash
python3 '${SCRIPT_DIR}/storage-db.py' get-messages '${PROJECT_DIR}' '${TICKET_ID}'
\`\`\`

**Signal progress/completion/blocked:**
\`\`\`bash
python3 '${SCRIPT_DIR}/storage-db.py' send-message '${PROJECT_DIR}' - << 'MSG'
{"from_agent": "${TICKET_ID}", "to_agent": null, "type": "done", "payload": "PR ready for review. Summary: <2 sentences>"}
MSG
\`\`\`
Types: \`done\` (work complete), \`blocked\` (need help), \`progress\` (status update), \`info\` (broadcast finding)

**Log a discovery for other pilots:**
\`\`\`bash
python3 '${SCRIPT_DIR}/storage-db.py' write-insight '${PROJECT_DIR}' '${TICKET_ID}' '<category>' '<detail>'
\`\`\`
Categories: gotcha, architecture, pattern, convention

**Write context snapshot (survives compaction):**
\`\`\`bash
python3 '${SCRIPT_DIR}/storage-db.py' write-snapshot '${PROJECT_DIR}' - << 'SNAP'
{"session_id": "\$CLAUDE_SESSION_ID", "ticket_id": "${TICKET_ID}", "remaining_pct": "75", "snapshot": "Phase: implementation. Done: X. Next: Y. Blockers: none."}
SNAP
\`\`\`

## Sibling Coordination
If you see \`.sortie/pull-parent.json\`, a sibling merged work. Read it, then:
1. \`git pull origin <branch from file>\`
2. Resolve conflicts
3. Delete the file
4. Continue

## Live State (injected every turn)
!cat .sortie/context-anchor.md 2>/dev/null
CLAUDE_MD_EOF
echo "CLAUDE_MD:written"

# Model — short name on line 1, resolved checkpoint on line 2
echo -e "${MODEL}\n${RESOLVED_MODEL}" > "${SORTIE_DIR}/model.txt"

# Progress (create if missing)
# Progress tracked via send-message broadcasts, not files

# Stub context.json so fuel gauge never crashes on missing file
if [ ! -f "${SORTIE_DIR}/context.json" ]; then
  python3 -c "import json; open('${SORTIE_DIR}/context.json','w').write(json.dumps({'used_percentage':None,'context_window_size':None,'stale':True,'timestamp':0}))"
fi

# Set PREFLIGHT status — agent is on deck, not yet airborne
python3 -c "import json,time; open('${SORTIE_DIR}/flight-status.json','w').write(json.dumps({'status':'PREFLIGHT','phase':'on deck - pre-launch checks','timestamp':int(time.time())}))"

# ── Port allocation ──────────────────────────────────────────────────
# Assign a unique dev server port to avoid collisions between agents.
# Scans managed-servers.json + existing .sortie/port files to find the next free port.
MANAGED_SERVERS="${PROJECT_DIR}/.sortie/managed-servers.json"
BASE_PORT=3001  # 3000 reserved for the main project dev server

# Collect all ports already in use
USED_PORTS=""
if [ -f "$MANAGED_SERVERS" ]; then
  USED_PORTS=$(python3 -c "
import json
try:
    with open('${MANAGED_SERVERS}') as f:
        servers = json.load(f)
    for s in servers:
        url = s.get('url', '')
        if ':' in url:
            print(url.split(':')[-1])
except: pass
" 2>/dev/null)
fi

# Also check port files from other worktrees (in case managed-servers.json is stale)
# Use find to avoid zsh nomatch errors when no port files exist
while IFS= read -r PORT_FILE; do
  [ -n "$PORT_FILE" ] && USED_PORTS="${USED_PORTS}
$(cat "$PORT_FILE")"
done < <(find "${PROJECT_DIR}/.claude/worktrees" -path "*/.sortie/port" -type f 2>/dev/null)

# Find the next available port starting from BASE_PORT
ASSIGNED_PORT=$BASE_PORT
while echo "$USED_PORTS" | grep -q "^${ASSIGNED_PORT}$"; do
  ASSIGNED_PORT=$((ASSIGNED_PORT + 1))
done

# Write the port file so the agent and other deployments know this port is taken
echo "$ASSIGNED_PORT" > "${SORTIE_DIR}/port"
echo "PORT:${ASSIGNED_PORT}"

# ── Env setup ─────────────────────────────────────────────────────────
cd "$WORKTREE_PATH"

# Symlink .env.local
if [ ! -f .env.local ] && [ -f "${PROJECT_DIR}/.env.local" ]; then
  ln -sf "${PROJECT_DIR}/.env.local" .env.local
  echo "ENV:symlinked .env.local"
elif [ ! -f .env.local ]; then
  echo "ENV:WARNING — no .env.local found in project root (${PROJECT_DIR})" >&2
fi

# Install deps — try pnpm first, then npm
if [ -f pnpm-lock.yaml ] && [ ! -d node_modules ]; then
  echo "DEPS:installing (pnpm)..."
  if ! pnpm install --frozen-lockfile 2>/dev/null; then
    pnpm install 2>/dev/null || echo "DEPS:WARNING — pnpm install failed" >&2
  fi
  echo "DEPS:done"
elif [ -f package-lock.json ] && [ ! -d node_modules ]; then
  echo "DEPS:installing (npm)..."
  npm ci 2>/dev/null || npm install 2>/dev/null || echo "DEPS:WARNING — npm install failed" >&2
  echo "DEPS:done"
fi

# ── Write settings (branch-scoped push permission) ───────────────────
if [ -x "${SORTIE_SCRIPTS}/write-settings.sh" ]; then
  bash "${SORTIE_SCRIPTS}/write-settings.sh" "$BRANCH_NAME" "$WORKTREE_PATH" "$PROJECT_DIR" 2>/dev/null || true
fi

# ── Generate scoped MCP config for pilot ────────────────────────────
# Whitelist approach: pilots get only the MCPs they need, not the full global set.
# Saves ~20-40K tokens of tool definitions at startup.
PILOT_MCP="${SORTIE_DIR}/pilot-mcp.json"
WITH_BROWSER="${WITH_BROWSER}" MCP_EXTRAS="${MCP_EXTRAS}" PROJECT_DIR="${PROJECT_DIR}" PILOT_MCP="${PILOT_MCP}" python3 - <<'PYEOF' || echo "MCP:WARNING — pilot-mcp.json generation failed" >&2
import json, os
proj_path = os.path.join(os.environ["PROJECT_DIR"], ".mcp.json")
glob_path = os.path.expanduser("~/.claude.json")
proj = json.load(open(proj_path)) if os.path.exists(proj_path) else {}
glob = json.load(open(glob_path)) if os.path.exists(glob_path) else {}
proj_servers = proj.get("mcpServers", {})
glob_servers = glob.get("mcpServers", {})
allow = ["serena", "CodeGraphContext", "exa"]
if os.environ.get("WITH_BROWSER") == "true":
    allow += ["playwright", "stagehand-local"]
extras_str = os.environ.get("MCP_EXTRAS", "").strip()
if extras_str:
    extras = [x.strip() for x in extras_str.split(",") if x.strip()]
    allow += [e for e in extras if e not in allow]
out = {"mcpServers": {}}
for name in allow:
    if name in proj_servers:
        out["mcpServers"][name] = proj_servers[name]
    elif name in glob_servers:
        out["mcpServers"][name] = glob_servers[name]
json.dump(out, open(os.environ["PILOT_MCP"], "w"), indent=2)
print(f"MCP:scoped to {len(out['mcpServers'])} servers: {','.join(out['mcpServers'].keys())}")
PYEOF

# ── Build disallowed tools list ──────────────────────────────────────
# Centralized list file takes precedence over inline fallback
DISALLOWED_FILE="${SCRIPT_DIR}/disallowed-tools.txt"
if [ -f "$DISALLOWED_FILE" ]; then
  DISALLOWED=$(tr '\n' ' ' < "$DISALLOWED_FILE")
else
  DISALLOWED="'Bash(git push --force*)' 'Bash(git push -f *)' 'Bash(git push *--force*)' 'Bash(git push *-f *)' 'Bash(git branch -D:*)' 'Bash(git branch -d:*)' 'Bash(git branch --delete:*)' 'Bash(git clean:*)' 'Bash(git reset --hard:*)' 'Bash(git checkout -- :*)' 'Bash(git restore:*)' 'Bash(rm:*)' 'Bash(rm )' 'Bash(rmdir:*)' 'Bash(unlink:*)' 'Bash(trash:*)' 'Bash(sudo:*)' 'Bash(chmod:*)' 'Bash(chown:*)' 'mcp__linear__*'"
fi

# ── Build kickoff ────────────────────────────────────────────────────
KICKOFF="Get your mission: python3 '${SCRIPT_DIR}/storage-db.py' get-briefing '${PROJECT_DIR}' '${TICKET_ID}'. See CLAUDE.md for commands reference."

# ── Write launch script ─────────────────────────────────────────────
LAUNCH_SCRIPT="${SORTIE_DIR}/launch.sh"
cat > "${LAUNCH_SCRIPT}" << 'LAUNCH_EOF'
#!/usr/bin/env bash
LAUNCH_EOF
cat >> "${LAUNCH_SCRIPT}" << LAUNCH_EOF2
cd '${WORKTREE_PATH}'
export PORT=${ASSIGNED_PORT}

# Cleanup on exit — signal session ended so dashboard sets RECOVERED
# Also runs auto-debrief in case pane was killed without a graceful /exit
cleanup_flight() {
  touch .sortie/session-ended
  python3 '${SCRIPT_DIR}/hooks/stop-auto-debrief.py' '${PROJECT_DIR}' <<< '{}' 2>/dev/null || true
}
trap cleanup_flight EXIT

claude --model ${RESOLVED_MODEL} '${KICKOFF}' --strict-mcp-config --mcp-config '${PILOT_MCP}' --disallowedTools ${DISALLOWED}
LAUNCH_EOF2
chmod +x "${LAUNCH_SCRIPT}"

echo "LAUNCH_SCRIPT:${LAUNCH_SCRIPT}"
echo "READY: Run: bash '${LAUNCH_SCRIPT}'"

# ── No-launch mode — exit here ───────────────────────────────────────
if [ "$NO_LAUNCH" = true ]; then
  echo "PREPPED:${TICKET_ID} (no-launch mode — deploy from TUI with D/R)"
  exit 0
fi

# ── Launch in iTerm2 pane (Pit Boss window) ──────────────────────────
STATE_DIR="/tmp/uss-tenkara/_prifly"
AGENTS_WINDOW_FILE="${STATE_DIR}/agents_window_id"
AGENTS_SESSION_FILE="${STATE_DIR}/agents_last_session_id"

if [ -f "$AGENTS_WINDOW_FILE" ] && [ -f "$AGENTS_SESSION_FILE" ]; then
  PB_WINDOW_ID=$(cat "$AGENTS_WINDOW_FILE")
  PB_SESSION_ID=$(cat "$AGENTS_SESSION_FILE")

  NEW_SESSION_ID=$(osascript << APPLESCRIPT_EOF
tell application "iTerm2"
  set targetWindow to item 1 of (windows whose id is ${PB_WINDOW_ID})
  set targetSession to missing value
  repeat with s in sessions of current tab of targetWindow
    if unique id of s is "${PB_SESSION_ID}" then
      set targetSession to s
      exit repeat
    end if
  end repeat
  tell targetSession
    set newSession to (split vertically with default profile)
    tell newSession
      set name to "${TICKET_ID}"
      write text "bash '${LAUNCH_SCRIPT}'"
    end tell
    return unique id of newSession
  end tell
end tell
APPLESCRIPT_EOF
  )

  echo "$NEW_SESSION_ID" > "$AGENTS_SESSION_FILE"
  echo "DEPLOYED:${TICKET_ID} in Pit Boss window"
else
  osascript << APPLESCRIPT_EOF
tell application "iTerm2"
  create window with default profile
  tell current session of current tab of current window
    set name to "${TICKET_ID}"
    write text "bash '${LAUNCH_SCRIPT}'"
  end tell
end tell
APPLESCRIPT_EOF
  echo "DEPLOYED:${TICKET_ID} in new window (no Pit Boss found)"
fi
