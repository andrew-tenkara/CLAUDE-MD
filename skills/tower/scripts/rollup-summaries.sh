#!/usr/bin/env bash
# rollup-summaries.sh — Compress all ticket summaries into a project-level rollup.
#
# Usage: rollup-summaries.sh <project-dir>
#
# Called by XO (Mini Boss) when the summaries table gets large, or manually.
# Output: writes a level-2 summary to the summaries table.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="${1:-}"

if [ -z "$PROJECT_DIR" ]; then
  echo "Usage: rollup-summaries.sh <project-dir>" >&2
  exit 1
fi

STORAGE_DB="${SCRIPT_DIR}/storage-db.py"

if ! command -v claude >/dev/null 2>&1; then
  echo "ROLLUP:skipped — claude not in PATH" >&2
  exit 0
fi

# Get all ticket summaries
SUMMARIES_JSON=$(python3 "$STORAGE_DB" get-summaries-for-rollup "$PROJECT_DIR" 2>/dev/null)

if [ "$SUMMARIES_JSON" = "SUMMARIES:none" ]; then
  echo "ROLLUP:no ticket summaries to roll up"
  exit 0
fi

SUMMARIES_TEXT=$(echo "$SUMMARIES_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['text'])")
SUMMARIES_COUNT=$(echo "$SUMMARIES_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['count'])")
SUMMARY_IDS=$(echo "$SUMMARIES_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d['summary_ids']))")

if [ "$SUMMARIES_COUNT" -lt 5 ]; then
  echo "ROLLUP:only $SUMMARIES_COUNT summaries — not worth rolling up yet (need 5+)"
  exit 0
fi

PROMPT="You are synthesizing $SUMMARIES_COUNT completed ticket summaries into a single project-level intelligence rollup (max 400 words).
Cover: recurring patterns and architecture decisions across tickets, common gotchas, which areas of the codebase get touched most, any conventions that emerged, and outstanding work threads.
Write for a new pilot joining this project cold. Be specific and dense. No intro sentence."

ROLLUP=$(echo "$SUMMARIES_TEXT" | claude --model sonnet --print --no-markdown "$PROMPT" 2>/dev/null || true)

if [ -z "$ROLLUP" ]; then
  echo "ROLLUP:claude summarization failed — summaries preserved" >&2
  exit 1
fi

python3 -c "
import json, sys
print(json.dumps({
    'content': sys.argv[1],
    'summary_type': 'project_rollup',
    'level': 2,
    'debrief_count': int(sys.argv[2]),
    'source_ids': json.loads(sys.argv[3]),
    'model': 'sonnet',
}))" "$ROLLUP" "$SUMMARIES_COUNT" "$SUMMARY_IDS" \
  | python3 "$STORAGE_DB" write-summary "$PROJECT_DIR" -

echo "ROLLUP:done — $SUMMARIES_COUNT ticket summaries → project rollup"
