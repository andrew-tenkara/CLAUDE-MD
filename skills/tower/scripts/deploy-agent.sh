#!/usr/bin/env bash
# deploy-agent.sh — Deploy a sortie agent to a worktree
#
# Usage:
#   deploy-agent.sh <ticket-id> [--model sonnet|opus|haiku] [--directive "text"]
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
NO_LAUNCH=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)       MODEL="$2"; shift 2 ;;
    --directive)   DIRECTIVE="$2"; shift 2 ;;
    --project-dir) PROJECT_DIR="$2"; shift 2 ;;
    --branch)      BRANCH_OVERRIDE="$2"; shift 2 ;;
    --no-launch)   NO_LAUNCH=true; shift ;;
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

# Validate model
case "$MODEL" in
  sonnet|opus|haiku) ;;
  *) echo "ERROR: Invalid model '$MODEL'. Must be sonnet, opus, or haiku." >&2; exit 1 ;;
esac

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
  OUTPUT=$(bash "${SORTIE_SCRIPTS}/create-worktree.sh" "$TICKET_ID" "$BRANCH_NAME" dev --model "$MODEL" --resume 2>&1) || CREATE_EXIT=$?

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
  if [ ! -d "$WORKTREE_PATH" ]; then
    if ! git -C "$PROJECT_DIR" worktree add "$WORKTREE_PATH" -b "$BRANCH_NAME" dev 2>/dev/null; then
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

# ── Write .sortie/ protocol files ────────────────────────────────────
SORTIE_DIR="${WORKTREE_PATH}/.sortie"
mkdir -p "$SORTIE_DIR"

# ── Init storage DB + fetch briefing ─────────────────────────────────
STORAGE_DB="${SCRIPT_DIR}/storage-db.py"
python3 "$STORAGE_DB" init "$PROJECT_DIR" 2>/dev/null || true
BRIEFING=$(python3 "$STORAGE_DB" get-briefing "$PROJECT_DIR" "$TICKET_ID" 2>/dev/null || true)
if [ "$BRIEFING" = "BRIEFING:none" ]; then
  BRIEFING=""
fi

# Directive
# NOTE: Static role content FIRST, dynamic ticket content LAST.
# This keeps the cacheable prefix stable across turns (~30% better cache reuse).
if [ -n "$DIRECTIVE" ]; then
  cat > "${SORTIE_DIR}/directive.md" << DIRECTIVE_EOF
## Role: PILOT (individual contributor)
YOUR JOB:
- Execute the directive below — implement, fix, test, PR
- Write code, run tests, commit changes, open PRs
- Read and understand the codebase in your worktree
- Before implementing any new function, use find_symbol to check if it already exists. Do not duplicate existing implementations.
- Track progress in .sortie/progress.md

NOT YOUR JOB (redirect to Mini Boss or Air Boss):
- Deploying other agents or managing other pilots
- Triaging tickets or deciding what to work on next
- Fetching Linear tickets or managing the mission queue
- Coordinating multi-agent work or splitting tasks
- Making architectural decisions that affect other tickets

If asked to do something outside your role, say:
"That's Mini Boss territory — I'm a pilot, not an orchestrator. Talk to Mini Boss for coordination/triage, or handle it from Pri-Fly."
Stay in your lane. Do your mission. Do it well.

## Sibling Coordination (pull-parent protocol)
If you see a file at .sortie/pull-parent.json, a sibling agent has merged their work
into the parent branch. Read the file for details, then:
1. Run: git pull origin <branch from the file>
2. Resolve any merge conflicts
3. Delete .sortie/pull-parent.json
4. Continue your work with the updated code

## Project Intelligence DB
All pilots share a SQLite database at: ${PROJECT_DIR}/.sortie/storage.db
Use it to read prior intel, log discoveries, communicate with coordinators, and leave debriefs.

### Read prior intel on this ticket (run on startup)

    python3 '${SCRIPT_DIR}/storage-db.py' get-briefing '${PROJECT_DIR}' '${TICKET_ID}'

### Check for messages (from coordinator or siblings)

    python3 '${SCRIPT_DIR}/storage-db.py' get-messages '${PROJECT_DIR}' '${TICKET_ID}'

### Send a message (signal coordinator, ask for help, broadcast a discovery)

    python3 '${SCRIPT_DIR}/storage-db.py' send-message '${PROJECT_DIR}' - << 'MSG'
    {"from_agent": "${TICKET_ID}", "to_agent": "<ticket-id or null for broadcast>", "type": "<done|blocked|progress|info>", "payload": "<message>"}
    MSG

Types: done (work complete), blocked (need help), progress (status update), info (broadcast finding)

### Log a discovery other pilots should know

    python3 '${SCRIPT_DIR}/storage-db.py' write-insight '${PROJECT_DIR}' '${TICKET_ID}' '<category>' '<detail>'

Categories: gotcha, architecture, pattern, convention — only log things not obvious from the code.

### Retrieve a cached tool result (after compaction or dedup hook blocks re-read)

    python3 '${SCRIPT_DIR}/storage-db.py' get-cached-tool '${PROJECT_DIR}' "$CLAUDE_SESSION_ID" <tool_name> <tool_key>

tool_key: file path for Read, first 200 chars of command for Bash, "pattern:path" for Grep.
Tool results >2KB are cached automatically. Retrieve instead of re-running when possible.

### Session debrief (MANDATORY before stopping)
Write for the next pilot — what you did, what's left, decisions made, landmines.

    python3 '${SCRIPT_DIR}/storage-db.py' write-debrief '${PROJECT_DIR}' - << 'DEBRIEF'
    {
      "ticket_id": "${TICKET_ID}",
      "branch": "<branch>",
      "model": "<model>",
      "what_done": "<1-2 sentences>",
      "whats_left": "<1-2 sentences or empty>",
      "decisions": "<key decisions made>",
      "gotchas": "<landmines for the next pilot>",
      "files_touched": "<key files>",
      "pr_url": "<url or empty>",
      "pr_status": "<open|merged|draft|empty>",
      "branch_status": "<clean|needs-rebase|empty>"
    }
    DEBRIEF

---
## Mission Directive

${DIRECTIVE}
${BRIEFING:+
---
## Prior Intelligence

${BRIEFING}}
DIRECTIVE_EOF
fi

# Write worktree CLAUDE.md with !command injection (survives compaction)
if [ ! -f "${WORKTREE_PATH}/CLAUDE.md" ]; then
  cat > "${WORKTREE_PATH}/CLAUDE.md" << 'CLAUDE_MD_EOF'
<!-- Auto-generated by deploy-agent.sh — do not edit manually -->
## Live Pilot State (injected every turn — survives compaction)
!cat .sortie/progress.md 2>/dev/null || echo "(no progress recorded yet)"
!cat .sortie/context-anchor.md 2>/dev/null
CLAUDE_MD_EOF
  echo "CLAUDE_MD:written"
fi

# Model
echo "$MODEL" > "${SORTIE_DIR}/model.txt"

# Progress (create if missing)
touch "${SORTIE_DIR}/progress.md"

# Stub context.json so fuel gauge never crashes on missing file
if [ ! -f "${SORTIE_DIR}/context.json" ]; then
  python3 -c "import json; open('${SORTIE_DIR}/context.json','w').write(json.dumps({'used_percentage':None,'context_window_size':None,'stale':True,'timestamp':0}))"
fi

# Set PREFLIGHT status — agent is on deck, not yet airborne
python3 -c "import json,time; open('${SORTIE_DIR}/flight-status.json','w').write(json.dumps({'status':'PREFLIGHT','phase':'on deck - pre-launch checks','timestamp':int(time.time())}))"

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

# ── Build disallowed tools list ──────────────────────────────────────
# Centralized list file takes precedence over inline fallback
DISALLOWED_FILE="${SCRIPT_DIR}/disallowed-tools.txt"
if [ -f "$DISALLOWED_FILE" ]; then
  DISALLOWED=$(tr '\n' ' ' < "$DISALLOWED_FILE")
else
  DISALLOWED="'Bash(git push --force*)' 'Bash(git push -f *)' 'Bash(git push *--force*)' 'Bash(git push *-f *)' 'Bash(git branch -D:*)' 'Bash(git branch -d:*)' 'Bash(git branch --delete:*)' 'Bash(git clean:*)' 'Bash(git reset --hard:*)' 'Bash(git checkout -- :*)' 'Bash(git restore:*)' 'Bash(rm:*)' 'Bash(rm )' 'Bash(rmdir:*)' 'Bash(unlink:*)' 'Bash(trash:*)' 'Bash(sudo:*)' 'Bash(chmod:*)' 'Bash(chown:*)' 'mcp__linear__*'"
fi

# ── Build kickoff ────────────────────────────────────────────────────
KICKOFF="Read ${SORTIE_DIR}/directive.md and follow all instructions. Track progress in ${SORTIE_DIR}/progress.md. Check for prior intel: python3 '${SCRIPT_DIR}/storage-db.py' get-briefing '${PROJECT_DIR}' '${TICKET_ID}'"

# ── Write launch script ─────────────────────────────────────────────
LAUNCH_SCRIPT="${SORTIE_DIR}/launch.sh"
cat > "${LAUNCH_SCRIPT}" << 'LAUNCH_EOF'
#!/usr/bin/env bash
LAUNCH_EOF
cat >> "${LAUNCH_SCRIPT}" << LAUNCH_EOF2
cd '${WORKTREE_PATH}'

# Cleanup on exit — signal session ended so dashboard sets RECOVERED
# Also runs auto-debrief in case pane was killed without a graceful /exit
cleanup_flight() {
  touch .sortie/session-ended
  python3 '${SCRIPT_DIR}/hooks/stop-auto-debrief.py' '${PROJECT_DIR}' <<< '{}' 2>/dev/null || true
}
trap cleanup_flight EXIT

# Route through Headroom proxy if running (context compression)
# Health check instead of PID: headroom forks a child, so $! captures a dead parent PID
if curl -sf "http://localhost:8787/health" >/dev/null 2>&1; then
  export ANTHROPIC_BASE_URL="http://localhost:8787"
fi

claude --model ${MODEL} '${KICKOFF}' --disallowedTools ${DISALLOWED}
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
