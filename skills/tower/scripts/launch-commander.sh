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

# Preflight — patch existing worktrees with .claudeignore / .mcp.json
bash "${SCRIPT_DIR}/preflight-worktrees.sh" "$PROJECT_DIR" 2>&1 | while IFS= read -r line; do echo "$line"; done


# State dir for IPC
STATE_DIR="/tmp/uss-tenkara/_prifly"
mkdir -p "$STATE_DIR"
rm -f "$STATE_DIR"/agents_window_id "$STATE_DIR"/agents_last_session_id

# Build the command to run inside the TUI window
CMD="python3 '${DASHBOARD}' --project-dir '${PROJECT_DIR}'"

# Run iTerm2 setup fully detached to avoid disrupting Headroom proxy connections.
# The osascript writes state files directly instead of returning values.
(
osascript <<EOF
tell application "iTerm2"
  activate

  -- Window 1: TUI — capture the window and session explicitly
  set tuiWindow to (create window with default profile)
  set tuiSess to current session of current tab of tuiWindow
  tell tuiSess
    set name to "USS Tenkara PRI-FLY"
  end tell

  -- Window 2: Pit Boss
  set pitBossWindow to (create window with default profile)
  set pitBossSess to current session of current tab of pitBossWindow
  tell pitBossSess
    set name to "PIT BOSS"
    write text "echo '⚓ USS TENKARA — PIT BOSS'; echo 'Mini Boss + agent panes will appear here.'; echo ''"
  end tell

  -- Write state files directly (detached execution can't return values to shell)
  do shell script "echo " & (id of pitBossWindow as text) & " > ${STATE_DIR}/agents_window_id"
  do shell script "echo " & (unique id of pitBossSess) & " > ${STATE_DIR}/agents_last_session_id"
  do shell script "echo " & (id of pitBossWindow as text) & " > ${STATE_DIR}/window_id"
  do shell script "echo running > ${STATE_DIR}/tower_running"

  -- Small delay to ensure sessions are ready, then write TUI command
  delay 0.3
  tell tuiSess
    write text "${CMD}"
  end tell
end tell
EOF
) &
disown

echo "Pri-Fly commander launching in new iTerm2 window..."
echo "Pit Boss window ready for agent panes"
