#!/usr/bin/env bash
# queue-to-tower.sh — Write a mission to Tower's queue and poke Mini Boss
#
# Usage:
#   queue-to-tower.sh --id <ticket-id> --title <title> --project-dir <dir> \
#     [--source linear|file|adhoc] [--directive <text>] [--priority 1-3] \
#     [--branch <branch-name>]
#
# Writes <project>/.sortie/mission-queue/<id>.json and sends a message
# to Mini Boss's iTerm pane (if Tower is running) to triage + deploy.

set -euo pipefail

# ── Parse args ────────────────────────────────────────────────────────
ID="" TITLE="" SOURCE="linear" DIRECTIVE="" PROJECT_DIR="" PRIORITY="2" BRANCH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --id)          ID="$2"; shift 2 ;;
    --title)       TITLE="$2"; shift 2 ;;
    --source)      SOURCE="$2"; shift 2 ;;
    --directive)   DIRECTIVE="$2"; shift 2 ;;
    --project-dir) PROJECT_DIR="$2"; shift 2 ;;
    --priority)    PRIORITY="$2"; shift 2 ;;
    --branch)      BRANCH="$2"; shift 2 ;;
    *)             echo "Unknown: $1" >&2; exit 1 ;;
  esac
done

if [ -z "$ID" ] || [ -z "$PROJECT_DIR" ]; then
  echo "ERROR: --id and --project-dir are required" >&2
  exit 1
fi

TITLE="${TITLE:-$ID}"

# ── Write mission JSON ───────────────────────────────────────────────
QUEUE_DIR="${PROJECT_DIR}/.sortie/mission-queue"
mkdir -p "$QUEUE_DIR"

MISSION_FILE="${QUEUE_DIR}/${ID}.json"

# Use python3 for proper JSON encoding (handles quotes, newlines in directives)
python3 - "$ID" "$TITLE" "$SOURCE" "$PRIORITY" "$BRANCH" "$DIRECTIVE" "$MISSION_FILE" << 'PYEOF'
import json, sys, time

_, mid, title, source, priority, branch, directive, outpath = sys.argv
mission = {
    "id": mid,
    "title": title,
    "source": source,
    "priority": int(priority),
    "agent_count": 1,
    "directive": directive,
    "created_at": int(time.time()),
}
if branch:
    mission["branch_name"] = branch
with open(outpath, "w") as f:
    json.dump(mission, f, indent=2)
PYEOF

echo "QUEUED: ${ID} → ${MISSION_FILE}"

# ── Poke Mini Boss ───────────────────────────────────────────────────
STATE_DIR="/tmp/uss-tenkara/_prifly"
MB_ITERM="${STATE_DIR}/miniboss-iterm-session"
PB_WINDOW="${STATE_DIR}/agents_window_id"
MB_STATUS="${STATE_DIR}/miniboss-status"

if [ ! -f "$MB_ITERM" ] || [ ! -f "$PB_WINDOW" ]; then
  echo "SKIP: No Mini Boss session found — mission queued for manual deploy"
  exit 0
fi

MB_STATUS_VAL=$(cat "$MB_STATUS" 2>/dev/null || echo "UNKNOWN")
if [ "$MB_STATUS_VAL" != "ACTIVE" ]; then
  echo "SKIP: Mini Boss not active (status: ${MB_STATUS_VAL}) — mission queued"
  exit 0
fi

MB_SID=$(cat "$MB_ITERM")
PB_WID=$(cat "$PB_WINDOW")

# Write poke message to temp file to avoid shell/AppleScript escaping hell
POKE_FILE=$(mktemp)
cat > "$POKE_FILE" << MSGEOF
Incoming sortie queued: ${ID} — ${TITLE}. Check .sortie/mission-queue/${ID}.json, triage model + priority, write a directive, and deploy when ready.
MSGEOF

osascript << APPLESCRIPT 2>/dev/null || true
tell application "iTerm2"
  set targetWindow to (windows whose id is ${PB_WID})'s item 1
  set msg to read POSIX file "${POKE_FILE}"
  -- Trim trailing newline
  if msg ends with (ASCII character 10) then
    set msg to text 1 thru -2 of msg
  end if
  repeat with s in sessions of current tab of targetWindow
    if unique id of s is "${MB_SID}" then
      tell s to write text msg
      exit repeat
    end if
  end repeat
end tell
APPLESCRIPT

rm -f "$POKE_FILE"
echo "POKED: Mini Boss notified"
