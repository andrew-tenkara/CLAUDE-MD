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
if [ -n "$DIRECTIVE" ]; then
  cat > "${SORTIE_DIR}/directive.md" << DIRECTIVE_EOF
${DIRECTIVE}

---
## Role: PILOT (individual contributor)
YOUR JOB:
- Execute the directive above — implement, fix, test, PR
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

## Session Debrief (MANDATORY before stopping)
Before your session ends, write a debrief to the project database:
\`\`\`bash
python3 '${SCRIPT_DIR}/storage-db.py' write-debrief '${PROJECT_DIR}' '<json>'
\`\`\`
The JSON must include: ticket_id, branch, model, what_done, whats_left,
decisions, gotchas, files_touched, pr_url, pr_status, branch_status.
Keep each field to 1-2 concise sentences. This debrief is read by the next
pilot deployed on this ticket — write for them, not for yourself.

## Logging Insights
If you discover something that other pilots on this project should know
(a gotcha, an architecture pattern, a convention, a landmine), log it:
\`\`\`bash
python3 '${SCRIPT_DIR}/storage-db.py' write-insight '${PROJECT_DIR}' '${TICKET_ID}' '<category>' '<detail>'
\`\`\`
Categories: gotcha, architecture, pattern, convention
Only log things that aren't obvious from the code itself.
${BRIEFING:+
---
${BRIEFING}}
DIRECTIVE_EOF
fi

# Model
echo "$MODEL" > "${SORTIE_DIR}/model.txt"

# Progress (create if missing)
touch "${SORTIE_DIR}/progress.md"

# Set PREFLIGHT status — agent is on deck, not yet airborne
echo "{\"status\": \"PREFLIGHT\", \"phase\": \"on deck — pre-launch checks\", \"timestamp\": $(date +%s)}" > "${SORTIE_DIR}/flight-status.json"

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
cleanup_flight() {
  touch .sortie/session-ended
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
  set targetWindow to (windows whose id is ${PB_WINDOW_ID})'s item 1
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
