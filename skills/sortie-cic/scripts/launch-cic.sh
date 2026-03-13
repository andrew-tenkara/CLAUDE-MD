#!/usr/bin/env bash
# launch-cic.sh — Open the USS Tenkara CIC dashboard in a new iTerm2 window
#
# Usage: ./launch-cic.sh --project-dir /path/to/project

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DASHBOARD="${SCRIPT_DIR}/carrier-dashboard.py"
PROJECT_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-dir) PROJECT_DIR="$2"; shift 2 ;;
    *) shift ;;
  esac
done

if [[ -z "$PROJECT_DIR" ]]; then
  PROJECT_DIR="${SORTIE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
fi

# Build the command to run inside the new window
CIC_CMD="python3 '${DASHBOARD}' --project-dir '${PROJECT_DIR}'"

osascript <<EOF
tell application "iTerm2"
  activate
  create window with default profile
  tell current session of current tab of current window
    set name to "USS Tenkara CIC"
    write text "${CIC_CMD}"
  end tell
end tell
EOF

echo "CIC dashboard launched in new iTerm2 window"
