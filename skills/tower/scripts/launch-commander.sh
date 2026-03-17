#!/usr/bin/env bash
# launch-commander.sh — Open USS Tenkara Pri-Fly + Pit Boss windows
#
# Window 1: TUI (commander-dashboard) — full screen
# Window 2: Pit Boss — agent panes get added here as they spawn
#
# Usage: ./launch-commander.sh --project-dir /path/to/project

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DASHBOARD="${SCRIPT_DIR}/commander-dashboard.py"
PROJECT_DIR=""
LINEAR_ORG=""
CONFIG_FILE="${SCRIPT_DIR}/../config.json"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-dir) PROJECT_DIR="$2"; shift 2 ;;
    --linear-org) LINEAR_ORG="$2"; shift 2 ;;
    *) shift ;;
  esac
done

if [[ -z "$PROJECT_DIR" ]]; then
  PROJECT_DIR="${SORTIE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
fi

# Save linear org to config if provided
if [[ -n "$LINEAR_ORG" ]]; then
  python3 -c "
import json, pathlib
p = pathlib.Path('$CONFIG_FILE')
cfg = json.loads(p.read_text()) if p.exists() else {}
cfg['linear_org'] = '$LINEAR_ORG'
p.write_text(json.dumps(cfg, indent=2) + '\n')
"
fi

# Ensure dependencies (fast path: skip pip if already importable)
python3 -c "import textual; import watchdog" 2>/dev/null || pip3 install -q textual watchdog 2>/dev/null || true

# State dir for IPC
STATE_DIR="/tmp/uss-tenkara/_prifly"
mkdir -p "$STATE_DIR"
rm -f "$STATE_DIR"/agents_window_id "$STATE_DIR"/agents_last_session_id

# Build the command to run inside the TUI window
CMD="python3 '${DASHBOARD}' --project-dir '${PROJECT_DIR}'"

# Create TUI window + Pit Boss window, save Pit Boss window/session IDs
RESULT=$(osascript <<EOF
tell application "iTerm2"
  activate

  -- Window 1: TUI
  create window with default profile
  tell current session of current tab of current window
    set name to "USS Tenkara PRI-FLY"
    write text "${CMD}"
  end tell

  -- Window 2: Pit Boss (agents will be paned in here)
  set pitBossWindow to (create window with default profile)
  set pitBossSess to current session of current tab of pitBossWindow
  tell pitBossSess
    set name to "PIT BOSS"
    write text "echo '⚓ USS TENKARA — PIT BOSS'; echo 'Mini Boss + agent panes will appear here.'; echo ''"
  end tell

  return (id of pitBossWindow as text) & "," & (unique id of pitBossSess)
end tell
EOF
)

PB_WINDOW_ID=$(echo "$RESULT" | cut -d',' -f1)
PB_SESSION_ID=$(echo "$RESULT" | cut -d',' -f2)

echo "$PB_WINDOW_ID" > "$STATE_DIR/agents_window_id"
echo "$PB_SESSION_ID" > "$STATE_DIR/agents_last_session_id"

# Marker files for Tower detection (XO and scripts check these)
echo "$PB_WINDOW_ID" > "$STATE_DIR/window_id"
echo "running" > "$STATE_DIR/tower_running"

echo "Pri-Fly commander launched in new iTerm2 window"
echo "Pit Boss window ready for agent panes"
