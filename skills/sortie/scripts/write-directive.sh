#!/usr/bin/env bash
# write-directive.sh — Template a directive.md into a worktree's .sortie/ directory
# Usage: ./write-directive.sh <worktree-path> \
#   --ticket-id <id> --title <title> --description <desc> \
#   --branch-name <branch> --model <model> \
#   [--labels <labels>] [--priority <priority>] \
#   [--scope <scope>] [--requirements <reqs>] [--acceptance-criteria <ac>]

set -euo pipefail

WORKTREE_PATH="${1:?Usage: write-directive.sh <worktree-path> --ticket-id ... }"
shift

SKILLS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$SKILLS_DIR/templates/directive.md"

# Defaults
TICKET_ID=""
TITLE=""
DESCRIPTION=""
BRANCH_NAME=""
MODEL="sonnet"
LABELS=""
PRIORITY=""
SCOPE=""
REQUIREMENTS=""
ACCEPTANCE_CRITERIA=""
PRIOR_WORK=""
CALLSIGN=""

# Parse flags
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ticket-id)      TICKET_ID="$2"; shift 2 ;;
    --title)          TITLE="$2"; shift 2 ;;
    --description)    DESCRIPTION="$2"; shift 2 ;;
    --branch-name)    BRANCH_NAME="$2"; shift 2 ;;
    --model)          MODEL="$2"; shift 2 ;;
    --labels)         LABELS="$2"; shift 2 ;;
    --priority)       PRIORITY="$2"; shift 2 ;;
    --scope)          SCOPE="$2"; shift 2 ;;
    --requirements)   REQUIREMENTS="$2"; shift 2 ;;
    --acceptance-criteria) ACCEPTANCE_CRITERIA="$2"; shift 2 ;;
    --prior-work)          PRIOR_WORK="$2"; shift 2 ;;
    --callsign)            CALLSIGN="$2"; shift 2 ;;
    *) shift ;;
  esac
done

# Validate required fields
if [ -z "$TICKET_ID" ] || [ -z "$TITLE" ] || [ -z "$BRANCH_NAME" ]; then
  echo "ERROR: --ticket-id, --title, and --branch-name are required" >&2
  exit 1
fi

# Ensure .sortie/ exists
mkdir -p "$WORKTREE_PATH/.sortie"

# Write model file
echo "$MODEL" > "$WORKTREE_PATH/.sortie/model.txt"

if [ ! -f "$TEMPLATE" ]; then
  echo "ERROR: Template not found at $TEMPLATE" >&2
  exit 1
fi

# Use Python3 for substitution — sed can't handle multi-line replacement values
python3 - "$TEMPLATE" "$WORKTREE_PATH/.sortie/directive.md" <<PYEOF
import sys

template_path, output_path = sys.argv[1], sys.argv[2]

replacements = {
    "{{TICKET_ID}}":           """${TICKET_ID}""",
    "{{TITLE}}":               """${TITLE}""",
    "{{DESCRIPTION}}":         """${DESCRIPTION}""",
    "{{BRANCH_NAME}}":         """${BRANCH_NAME}""",
    "{{MODEL}}":               """${MODEL}""",
    "{{LABELS}}":              """${LABELS:-None}""",
    "{{PRIORITY}}":            """${PRIORITY:-Unset}""",
    "{{SCOPE}}":               """${SCOPE:-To be assessed by agent}""",
    "{{REQUIREMENTS}}":        """${REQUIREMENTS:-See ticket description above}""",
    "{{ACCEPTANCE_CRITERIA}}": """${ACCEPTANCE_CRITERIA:-See ticket description above}""",
    "{{PRIOR_WORK}}":          """${PRIOR_WORK:-}""",
    "{{CALLSIGN}}":            """${CALLSIGN:-Pilot}""",
}

with open(template_path, "r") as f:
    content = f.read()

for placeholder, value in replacements.items():
    content = content.replace(placeholder, value)

with open(output_path, "w") as f:
    f.write(content)
PYEOF

echo "Directive written to $WORKTREE_PATH/.sortie/directive.md"
echo "Model: $MODEL"
