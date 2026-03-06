#!/usr/bin/env bash
# check-status.sh — Read progress and lifecycle markers for all active sorties
# Usage: ./check-status.sh [ticket-id]
#
# With no args: scans all worktrees under .claude/worktrees/
# With ticket-id: scans just that one
#
# Output format (machine-parseable lines):
#   AGENT:<ticket-id>:<status>:<model>:<last-progress-line>
#   SUB:<parent-ticket-id>:<sub-name>:<status>:<model>:<last-progress-line>
#
# Status values: working | pre-review | reviewing | post-review | done | unknown

set -euo pipefail

GIT_ROOT=$(git rev-parse --show-toplevel)
WORKTREES_DIR="$GIT_ROOT/.claude/worktrees"

if [ ! -d "$WORKTREES_DIR" ]; then
  echo "NO_WORKTREES"
  exit 0
fi

TARGET="${1:-}"

detect_status() {
  local sortie_dir="$1"

  if [ ! -d "$sortie_dir" ]; then
    echo "unknown"
    return
  fi

  if [ -f "$sortie_dir/post-review.done" ]; then
    echo "done"
  elif [ -f "$sortie_dir/review-feedback.md" ]; then
    echo "reviewing"
  elif [ -f "$sortie_dir/pre-review.done" ]; then
    echo "pre-review"
  elif [ -f "$sortie_dir/progress.md" ] && [ -s "$sortie_dir/progress.md" ]; then
    echo "working"
  else
    echo "working"
  fi
}

get_model() {
  local sortie_dir="$1"
  if [ -f "$sortie_dir/model.txt" ]; then
    cat "$sortie_dir/model.txt" | tr -d '[:space:]'
  else
    echo "unknown"
  fi
}

get_last_progress() {
  local sortie_dir="$1"
  if [ -f "$sortie_dir/progress.md" ] && [ -s "$sortie_dir/progress.md" ]; then
    tail -1 "$sortie_dir/progress.md" | tr -d '\n'
  else
    echo "(no progress yet)"
  fi
}

scan_worktree() {
  local dir="$1"
  local ticket_id
  ticket_id=$(basename "$dir")
  local sortie_dir="$dir/.sortie"

  # Check if this is a sortie worktree
  if [ ! -d "$sortie_dir" ]; then
    return
  fi

  local status model last_progress
  status=$(detect_status "$sortie_dir")
  model=$(get_model "$sortie_dir")
  last_progress=$(get_last_progress "$sortie_dir")

  echo "AGENT:${ticket_id}:${status}:${model}:${last_progress}"

  # Check for sub-agent worktrees
  for sub_dir in "$dir"/sub-*/; do
    if [ -d "$sub_dir/.sortie" ]; then
      local sub_name
      sub_name=$(basename "$sub_dir" | sed 's/^sub-//')
      local sub_status sub_model sub_progress
      sub_status=$(detect_status "$sub_dir/.sortie")
      sub_model=$(get_model "$sub_dir/.sortie")
      sub_progress=$(get_last_progress "$sub_dir/.sortie")
      echo "SUB:${ticket_id}:${sub_name}:${sub_status}:${sub_model}:${sub_progress}"
    fi
  done
}

if [ -n "$TARGET" ]; then
  if [ -d "$WORKTREES_DIR/$TARGET" ]; then
    scan_worktree "$WORKTREES_DIR/$TARGET"
  else
    echo "NOT_FOUND:$TARGET"
    exit 1
  fi
else
  for dir in "$WORKTREES_DIR"/*/; do
    [ -d "$dir" ] && scan_worktree "$dir"
  done
fi
