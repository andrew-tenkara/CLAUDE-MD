#!/usr/bin/env bash
# cleanup.sh — Remove a sortie's worktree and local branch
# Usage: ./cleanup.sh <ticket-id> [--branch <branch-name>]
#        ./cleanup.sh --all   (clean all completed sorties)
#
# Only cleans sorties that have post-review.done (i.e., completed).
# For parallel tickets, cleans sub-worktrees first, then parent.

set -euo pipefail

GIT_ROOT=$(git rev-parse --show-toplevel)
WORKTREES_DIR="$GIT_ROOT/.claude/worktrees"

TICKET_ID="${1:?Usage: cleanup.sh <ticket-id> [--branch <branch-name>] OR cleanup.sh --all}"
BRANCH_NAME=""

shift
while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch) BRANCH_NAME="$2"; shift 2 ;;
    *) shift ;;
  esac
done

cleanup_worktree() {
  local worktree_path="$1"
  local branch="$2"

  if [ ! -d "$worktree_path" ]; then
    echo "SKIP: $worktree_path does not exist"
    return
  fi

  # Remove worktree (no --force — fail loudly if uncommitted changes remain)
  echo "Removing worktree: $worktree_path"
  if ! git worktree remove "$worktree_path" 2>/dev/null; then
    echo "WARN: Could not remove worktree $worktree_path"
    echo "  It may have uncommitted changes. Inspect manually, then run:"
    echo "  git worktree remove --force $worktree_path"
    return
  fi

  # Delete local branch if provided
  if [ -n "$branch" ]; then
    if git branch --list "$branch" | grep -q "$branch"; then
      echo "Deleting local branch: $branch"
      git branch -d "$branch" 2>/dev/null || {
        echo "WARN: Branch $branch has unmerged changes. Use git branch -D to force delete."
      }
    fi
  fi

  echo "CLEANED:$(basename "$worktree_path")"
}

if [ "$TICKET_ID" = "--all" ]; then
  # Clean all completed sorties
  CLEANED=0
  for dir in "$WORKTREES_DIR"/*/; do
    [ -d "$dir" ] || continue
    tid=$(basename "$dir")

    # Check if completed
    if [ -f "$dir/.sortie/post-review.done" ]; then
      # Clean sub-worktrees first
      for sub_dir in "$dir"/sub-*/; do
        if [ -d "$sub_dir" ]; then
          sub_name=$(basename "$sub_dir")
          # Try to find the sub-branch
          sub_branch=$(git -C "$sub_dir" branch --show-current 2>/dev/null || echo "")
          cleanup_worktree "$sub_dir" "$sub_branch"
        fi
      done

      # Clean parent
      parent_branch=$(git -C "$dir" branch --show-current 2>/dev/null || echo "")
      cleanup_worktree "$dir" "$parent_branch"
      CLEANED=$((CLEANED + 1))
    else
      echo "SKIP: $tid — not completed (no post-review.done)"
    fi
  done
  echo "TOTAL_CLEANED:$CLEANED"
else
  # Clean specific ticket
  WORKTREE_PATH="$WORKTREES_DIR/$TICKET_ID"

  if [ ! -d "$WORKTREE_PATH" ]; then
    echo "ERROR: No worktree found for $TICKET_ID at $WORKTREE_PATH"
    exit 1
  fi

  # Clean sub-worktrees first
  for sub_dir in "$WORKTREE_PATH"/sub-*/; do
    if [ -d "$sub_dir" ]; then
      sub_branch=$(git -C "$sub_dir" branch --show-current 2>/dev/null || echo "")
      cleanup_worktree "$sub_dir" "$sub_branch"
    fi
  done

  # If no branch name provided, try to detect it
  if [ -z "$BRANCH_NAME" ]; then
    BRANCH_NAME=$(git -C "$WORKTREE_PATH" branch --show-current 2>/dev/null || echo "")
  fi

  cleanup_worktree "$WORKTREE_PATH" "$BRANCH_NAME"
fi
