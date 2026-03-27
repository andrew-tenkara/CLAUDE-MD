#!/usr/bin/env bash
# compress-ticket.sh — Compress pilot debriefs into a ticket summary on sortie dismiss.
#
# Usage: compress-ticket.sh <project-dir> <ticket-id>
#
# Called automatically by Tower when Z (dismiss) is pressed on a RECOVERED sortie.
# Requires: claude CLI in PATH

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="${1:-}"
TICKET_ID="${2:-}"

if [ -z "$PROJECT_DIR" ] || [ -z "$TICKET_ID" ]; then
  echo "Usage: compress-ticket.sh <project-dir> <ticket-id>" >&2
  exit 1
fi

STORAGE_DB="${SCRIPT_DIR}/storage-db.py"

# Check claude is available
if ! command -v claude >/dev/null 2>&1; then
  echo "COMPRESS:skipped — claude not in PATH" >&2
  exit 0
fi

# Get raw debriefs
DEBRIEF_JSON=$(python3 "$STORAGE_DB" get-for-compression "$PROJECT_DIR" "$TICKET_ID" 2>/dev/null)

if [ "$DEBRIEF_JSON" = "DEBRIEFS:none" ]; then
  echo "COMPRESS:no debriefs for $TICKET_ID — nothing to compress"
  exit 0
fi

DEBRIEF_TEXT=$(echo "$DEBRIEF_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['text'])")
DEBRIEF_COUNT=$(echo "$DEBRIEF_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d['debrief_ids']))")
DEBRIEF_IDS=$(echo "$DEBRIEF_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d['debrief_ids']))")

# Skip if only 1 debrief and already small
if [ "$DEBRIEF_COUNT" -lt 2 ]; then
  echo "COMPRESS:only $DEBRIEF_COUNT debrief(s) for $TICKET_ID — skipping (not worth compressing)"
  exit 0
fi

# Summarize with Claude haiku (cheap + fast)
PROMPT="Summarize these pilot session debriefs for ticket $TICKET_ID into a single concise block (max 250 words).
Cover: what was accomplished, what is still outstanding, key decisions made, gotchas/landmines for the next pilot, and key files touched.
Write for the next engineer picking this ticket up. Be specific and terse. No intro sentence."

SUMMARY=$(echo "$DEBRIEF_TEXT" | claude --model haiku --print --no-markdown "$PROMPT" 2>/dev/null || true)

if [ -z "$SUMMARY" ]; then
  echo "COMPRESS:claude summarization failed for $TICKET_ID — raw debriefs preserved" >&2
  exit 1
fi

# Write summary to DB
python3 -c "
import json, sys
print(json.dumps({
    'content': sys.argv[1],
    'summary_type': 'ticket',
    'level': 1,
    'debrief_count': int(sys.argv[2]),
    'source_ids': json.loads(sys.argv[3]),
    'model': 'haiku',
}))" "$SUMMARY" "$DEBRIEF_COUNT" "$DEBRIEF_IDS" \
  | python3 "$STORAGE_DB" write-summary "$PROJECT_DIR" "$TICKET_ID"

echo "COMPRESS:done — $TICKET_ID ($DEBRIEF_COUNT debriefs → summary)"
