#!/usr/bin/env bash
# create-worktree.sh — Create a git worktree with .sortie/ protocol directory
# Usage: ./create-worktree.sh <ticket-id> <branch-name> [base-branch]
# For sub-agents: ./create-worktree.sh <ticket-id> <branch-name> [base-branch] --sub <sub-name> --parent-worktree <path>
#
# Creates:
#   .claude/worktrees/<ticket-id>/
#   .claude/worktrees/<ticket-id>/.sortie/
#   .claude/worktrees/<ticket-id>/.sortie/progress.md
#   .claude/worktrees/<ticket-id>/.sortie/model.txt (if --model provided)

set -euo pipefail

TICKET_ID="${1:?Usage: create-worktree.sh <ticket-id> <branch-name> [base-branch]}"
BRANCH_NAME="${2:?Usage: create-worktree.sh <ticket-id> <branch-name> [base-branch]}"
shift 2

# Parse optional flags
SUB_NAME=""
PARENT_WORKTREE=""
MODEL=""
RESUME=false
BASE_BRANCH="dev"

# Consume base-branch only if the next arg is a positional (not a flag)
if [[ $# -gt 0 && "${1}" != --* ]]; then
  BASE_BRANCH="$1"
  shift
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sub)
      SUB_NAME="$2"
      shift 2
      ;;
    --parent-worktree)
      PARENT_WORKTREE="$2"
      shift 2
      ;;
    --model)
      MODEL="$2"
      shift 2
      ;;
    --resume)
      RESUME=true
      shift
      ;;
    *)
      shift
      ;;
  esac
done

GIT_ROOT=$(git rev-parse --show-toplevel)

if [ -n "$SUB_NAME" ]; then
  # Sub-agent worktree
  WORKTREE_PATH="$GIT_ROOT/.claude/worktrees/${TICKET_ID}/sub-${SUB_NAME}"
  ACTUAL_BRANCH="${BRANCH_NAME}-${SUB_NAME}"
else
  # Standard worktree
  WORKTREE_PATH="$GIT_ROOT/.claude/worktrees/${TICKET_ID}"
  ACTUAL_BRANCH="$BRANCH_NAME"
fi

# Check if worktree already exists
if [ -d "$WORKTREE_PATH" ]; then
  echo "WORKTREE_EXISTS:$WORKTREE_PATH"
  echo "Worktree already exists at $WORKTREE_PATH"
  echo "The agent may have crashed — work is likely preserved."
  exit 2
fi

# Check if branch already exists
BRANCH_EXISTS_LOCALLY=false
BRANCH_EXISTS_REMOTE=false
git show-ref --verify --quiet "refs/heads/$ACTUAL_BRANCH" && BRANCH_EXISTS_LOCALLY=true || true
git show-ref --verify --quiet "refs/remotes/origin/$ACTUAL_BRANCH" && BRANCH_EXISTS_REMOTE=true || true

if [ "$BRANCH_EXISTS_LOCALLY" = true ] || [ "$BRANCH_EXISTS_REMOTE" = true ]; then
  if [ "$RESUME" = false ]; then
    echo "BRANCH_EXISTS:$ACTUAL_BRANCH" >&2
    echo "Branch '$ACTUAL_BRANCH' already exists. This ticket is already being worked on." >&2
    exit 2
  fi

  # Resume mode — check out the existing branch into the worktree
  echo "RESUMING:$ACTUAL_BRANCH"
  if [ "$BRANCH_EXISTS_LOCALLY" = true ]; then
    git worktree add "$WORKTREE_PATH" "$ACTUAL_BRANCH"
  else
    git fetch origin "$ACTUAL_BRANCH"
    git worktree add "$WORKTREE_PATH" --track -b "$ACTUAL_BRANCH" "origin/$ACTUAL_BRANCH"
  fi
else
  # Fresh branch — create the worktree
  echo "Creating worktree: $WORKTREE_PATH (branch: $ACTUAL_BRANCH, base: $BASE_BRANCH)"
  git worktree add "$WORKTREE_PATH" -b "$ACTUAL_BRANCH" "$BASE_BRANCH"
fi

# Create .sortie/ protocol directory
mkdir -p "$WORKTREE_PATH/.sortie"
touch "$WORKTREE_PATH/.sortie/progress.md"

# Write model if provided
if [ -n "$MODEL" ]; then
  echo "$MODEL" > "$WORKTREE_PATH/.sortie/model.txt"
fi

echo "WORKTREE_CREATED:$WORKTREE_PATH"
echo "BRANCH:$ACTUAL_BRANCH"
