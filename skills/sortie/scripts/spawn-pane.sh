#!/usr/bin/env bash
# spawn-pane.sh — Spawn a Claude Code agent in an iTerm2 pane
# Usage: ./spawn-pane.sh <worktree-path> <model> <ticket-id> [--new-window] [--pane-index <n>]
#
# Pane layout rules:
#   - Max 8 panes per window
#   - Panes 1-4: top row (split vertically)
#   - Panes 5-8: bottom row (split horizontally from top, then vertically)
#   - Pane 9+: new window
#
# --new-window: force a new iTerm2 window
# --pane-index: which pane number this is (1-based) — controls split direction

set -euo pipefail

WORKTREE_PATH="${1:?Usage: spawn-pane.sh <worktree-path> <model> <ticket-id>}"
MODEL="${2:?Usage: spawn-pane.sh <worktree-path> <model> <ticket-id>}"
TICKET_ID="${3:?Usage: spawn-pane.sh <worktree-path> <model> <ticket-id>}"

NEW_WINDOW=false
PANE_INDEX=1

shift 3
while [[ $# -gt 0 ]]; do
  case "$1" in
    --new-window)  NEW_WINDOW=true; shift ;;
    --pane-index)  PANE_INDEX="$2"; shift 2 ;;
    *) shift ;;
  esac
done

# Build the disallowedTools string
DISALLOWED="'Bash(git push --force*)' 'Bash(git push -f *)' 'Bash(git push *--force*)' 'Bash(git push *-f *)' 'Bash(git branch -D:*)' 'Bash(git branch -d:*)' 'Bash(git branch --delete:*)' 'Bash(git clean:*)' 'Bash(git reset --hard:*)' 'Bash(git checkout -- :*)' 'Bash(git restore:*)' 'Bash(rm:*)' 'Bash(rm )' 'Bash(rmdir:*)' 'Bash(unlink:*)' 'Bash(trash:*)' 'Bash(sudo:*)' 'Bash(chmod:*)' 'Bash(chown:*)'"

# Write a launch script into the worktree — keeps AppleScript's write text call short
# and avoids quoting/truncation issues with the long disallowedTools list
KICKOFF="Read ${WORKTREE_PATH}/.sortie/directive.md and follow all instructions. Track progress in ${WORKTREE_PATH}/.sortie/progress.md"
LAUNCH_SCRIPT="${WORKTREE_PATH}/.sortie/launch.sh"

cat > "${LAUNCH_SCRIPT}" << LAUNCH_EOF
#!/usr/bin/env bash
cd '${WORKTREE_PATH}'
exec claude --model ${MODEL} '${KICKOFF}' --disallowedTools ${DISALLOWED}
LAUNCH_EOF
chmod +x "${LAUNCH_SCRIPT}"

CLAUDE_CMD="bash '${WORKTREE_PATH}/.sortie/launch.sh'"

WINDOW_ID_FILE="/tmp/sortie-active-window-id"
LAST_TOP_FILE="/tmp/sortie-last-top-pane-id"
LAST_BOTTOM_FILE="/tmp/sortie-last-bottom-pane-id"

# Helper: find session by unique ID and split it vertically; writes new ID to a file
split_session_vertically() {
  local window_id="$1"
  local session_id="$2"
  local ticket="$3"
  local cmd="$4"
  osascript <<EOF
tell application "iTerm2"
  set targetWindow to (windows whose id is ${window_id})'s item 1
  set targetSession to missing value
  repeat with s in sessions of current tab of targetWindow
    if unique id of s is "${session_id}" then
      set targetSession to s
      exit repeat
    end if
  end repeat
  tell targetSession
    set newSession to (split vertically with default profile)
    tell newSession
      set name to "${ticket}"
      write text "${cmd}"
    end tell
    return unique id of newSession
  end tell
end tell
EOF
}

if [ "$NEW_WINDOW" = true ] || [ "$PANE_INDEX" -eq 1 ]; then
  # Create window, immediately split horizontally to reserve the bottom row.
  # Agent goes in the top-left; bottom row is pre-created empty.
  RESULT=$(osascript <<EOF
tell application "iTerm2"
  create window with default profile
  set w to current window
  set topSess to current session of current tab of w
  tell topSess
    set bottomSess to (split horizontally with default profile)
    set name to "${TICKET_ID}"
    write text "${CLAUDE_CMD}"
  end tell
  return (id of w as text) & "," & (unique id of topSess) & "," & (unique id of bottomSess)
end tell
EOF
)
  echo "${RESULT}" | cut -d',' -f1 > "$WINDOW_ID_FILE"
  echo "${RESULT}" | cut -d',' -f2 > "$LAST_TOP_FILE"
  echo "${RESULT}" | cut -d',' -f3 > "$LAST_BOTTOM_FILE"
  echo "SPAWNED:new-window:pane-1:${TICKET_ID}"

elif [ "$PANE_INDEX" -le 4 ]; then
  # Split last top-row pane vertically; track the new pane as the last top
  WINDOW_ID=$(cat "$WINDOW_ID_FILE")
  LAST_TOP_ID=$(cat "$LAST_TOP_FILE")
  NEW_ID=$(split_session_vertically "$WINDOW_ID" "$LAST_TOP_ID" "${TICKET_ID}" "${CLAUDE_CMD}")
  echo "$NEW_ID" > "$LAST_TOP_FILE"
  echo "SPAWNED:split-vertical:pane-${PANE_INDEX}:${TICKET_ID}"

elif [ "$PANE_INDEX" -eq 5 ]; then
  # Use the pre-created empty bottom pane from pane 1's horizontal split
  WINDOW_ID=$(cat "$WINDOW_ID_FILE")
  BOTTOM_ID=$(cat "$LAST_BOTTOM_FILE")
  osascript <<EOF
tell application "iTerm2"
  set targetWindow to (windows whose id is ${WINDOW_ID})'s item 1
  set targetSession to missing value
  repeat with s in sessions of current tab of targetWindow
    if unique id of s is "${BOTTOM_ID}" then
      set targetSession to s
      exit repeat
    end if
  end repeat
  tell targetSession
    set name to "${TICKET_ID}"
    write text "${CLAUDE_CMD}"
  end tell
end tell
EOF
  echo "SPAWNED:bottom-row:pane-5:${TICKET_ID}"

elif [ "$PANE_INDEX" -le 8 ]; then
  # Split last bottom-row pane vertically; track the new pane as the last bottom
  WINDOW_ID=$(cat "$WINDOW_ID_FILE")
  LAST_BOTTOM_ID=$(cat "$LAST_BOTTOM_FILE")
  NEW_ID=$(split_session_vertically "$WINDOW_ID" "$LAST_BOTTOM_ID" "${TICKET_ID}" "${CLAUDE_CMD}")
  echo "$NEW_ID" > "$LAST_BOTTOM_FILE"
  echo "SPAWNED:split-vertical:pane-${PANE_INDEX}:${TICKET_ID}"

else
  osascript <<EOF
tell application "iTerm2"
  create window with default profile
  tell current session of current tab of current window
    set name to "${TICKET_ID}"
    write text "${CLAUDE_CMD}"
  end tell
end tell
EOF
  echo "SPAWNED:new-window:pane-${PANE_INDEX}:${TICKET_ID}"
fi
