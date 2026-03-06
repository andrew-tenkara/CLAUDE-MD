#!/usr/bin/env bash
# write-settings.sh — Write scoped .claude/settings.json to a worktree
# Usage: ./write-settings.sh <branch-name> <worktree-path>
#
# The <BRANCH-NAME> placeholder in the template's push allow rule is replaced
# with the actual Linear gitBranchName so the agent can ONLY push to that branch.
# All other pushes (main, dev, master, force) remain hard-blocked.

set -euo pipefail

BRANCH_NAME="${1:?Usage: write-settings.sh <branch-name> <worktree-path>}"
WORKTREE_PATH="${2:?Usage: write-settings.sh <branch-name> <worktree-path>}"
SKILLS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$SKILLS_DIR/templates/settings.json"

if [ ! -f "$TEMPLATE" ]; then
  echo "ERROR: Template not found at $TEMPLATE" >&2
  exit 1
fi

mkdir -p "$WORKTREE_PATH/.claude"

sed "s|<BRANCH-NAME>|${BRANCH_NAME}|g" "$TEMPLATE" > "$WORKTREE_PATH/.claude/settings.json"

echo "Settings written to $WORKTREE_PATH/.claude/settings.json (push scoped to ${BRANCH_NAME})"
