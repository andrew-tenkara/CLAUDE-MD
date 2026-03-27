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

# ── Headroom proxy (context compression for all pilots) ──────────────
HEADROOM_PORT=8787
HEADROOM_PID_FILE="/tmp/uss-tenkara/headroom.pid"
HEADROOM_LOG="/tmp/uss-tenkara/headroom.log"
mkdir -p /tmp/uss-tenkara

# Kill stale headroom if running — use pgrep as authoritative source (PID file may be stale)
STALE_PID=$(pgrep -f "headroom proxy" | head -1)
if [ -n "$STALE_PID" ]; then
  kill "$STALE_PID" 2>/dev/null || true
fi
rm -f "$HEADROOM_PID_FILE"

if command -v headroom &>/dev/null; then
  headroom proxy --port "$HEADROOM_PORT" > "$HEADROOM_LOG" 2>&1 &
  # Wait for proxy to be ready (Kompress ML model preload takes 10-15s)
  for i in $(seq 1 20); do
    if curl -sf "http://localhost:${HEADROOM_PORT}/health" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  if curl -sf "http://localhost:${HEADROOM_PORT}/health" >/dev/null 2>&1; then
    # headroom forks a child — use pgrep to get the real worker PID
    REAL_PID=$(pgrep -f "headroom proxy" | head -1)
    echo "${REAL_PID:-unknown}" > "$HEADROOM_PID_FILE"
    echo "HEADROOM:running on port ${HEADROOM_PORT} (pid ${REAL_PID:-?})"
  else
    echo "HEADROOM:WARNING — proxy failed to start, pilots will connect directly" >&2
    rm -f "$HEADROOM_PID_FILE"
  fi
else
  echo "HEADROOM:not installed — skipping (pip install 'headroom-ai[all]' to enable)"
fi

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

  -- Small delay to ensure sessions are ready, then write TUI command
  delay 0.3
  tell tuiSess
    write text "${CMD}"
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
