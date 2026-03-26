#!/usr/bin/env bash
# write-settings.sh — Write scoped .claude/settings.json to a worktree
# Usage: ./write-settings.sh <branch-name> <worktree-path> [<project-dir>]
#
# Placeholders replaced in the template:
#   <BRANCH-NAME>     — Linear gitBranchName (scopes push permissions)
#   <SORTIE_SCRIPTS>  — path to sortie/scripts/
#   <TOWER_SCRIPTS>   — path to tower/scripts/
#   <PROJECT_DIR>     — project root (for storage-db.py)

set -euo pipefail

BRANCH_NAME="${1:?Usage: write-settings.sh <branch-name> <worktree-path> [<project-dir>]}"
WORKTREE_PATH="${2:?Usage: write-settings.sh <branch-name> <worktree-path> [<project-dir>]}"
PROJECT_DIR="${3:-$(git -C "$WORKTREE_PATH" rev-parse --show-toplevel 2>/dev/null || echo "")}"
SKILLS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TOWER_SCRIPTS="$(cd "$(dirname "$0")/../../tower/scripts" 2>/dev/null && pwd || echo "")"
TEMPLATE="$SKILLS_DIR/templates/settings.json"

if [ ! -f "$TEMPLATE" ]; then
  echo "ERROR: Template not found at $TEMPLATE" >&2
  exit 1
fi

mkdir -p "$WORKTREE_PATH/.claude"

sed -e "s|<BRANCH-NAME>|${BRANCH_NAME}|g" \
    -e "s|<SORTIE_SCRIPTS>|${SKILLS_DIR}/scripts|g" \
    -e "s|<TOWER_SCRIPTS>|${TOWER_SCRIPTS}|g" \
    -e "s|<PROJECT_DIR>|${PROJECT_DIR}|g" \
    "$TEMPLATE" > "$WORKTREE_PATH/.claude/settings.json"

echo "Settings written to $WORKTREE_PATH/.claude/settings.json (push scoped to ${BRANCH_NAME})"
