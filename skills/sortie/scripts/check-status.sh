#!/usr/bin/env bash
# check-status.sh — Read progress and lifecycle markers for all active sorties
# Usage: ./check-status.sh [ticket-id]
#
# With no args: scans all worktrees under .claude/worktrees/
# With ticket-id: scans just that one
#
# Output format (machine-parseable lines):
#   AGENT:<ticket-id>:<status>:<model>:<context-%>:<last-progress-line>
#   METRICS:<ticket-id>:<total-calls>:<error-count>:<agent-spawns>:<top-tools>
#   SUB:<parent-ticket-id>:<sub-name>:<status>:<model>:<context-%>:<last-progress-line>
#   METRICS:<sub-ticket-id>:<total-calls>:<error-count>:<agent-spawns>:<top-tools>
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

get_context_pct() {
  local sortie_dir="$1"
  if [ ! -f "$sortie_dir/context.json" ]; then
    echo "--"
    return
  fi
  if ! command -v jq >/dev/null 2>&1; then
    # Fallback: grep the percentage from JSON without jq
    local pct
    pct=$(grep -o '"used_percentage":[[:space:]]*[0-9]*' "$sortie_dir/context.json" 2>/dev/null | grep -o '[0-9]*$')
    echo "${pct:---}"
    return
  fi
  local pct
  pct=$(jq -r '.used_percentage // empty' "$sortie_dir/context.json" 2>/dev/null)
  echo "${pct:---}"
}

# Emit a METRICS line for a worktree path by parsing its JSONL session log.
# Output: METRICS:<ticket-id>:<total-calls>:<error-count>:<agent-spawns>:<top-tools>
# Requires python3. Uses the shared parse_jsonl_metrics lib module.
emit_jsonl_metrics() {
  local ticket_id="$1"
  local worktree_path="$2"

  command -v python3 >/dev/null || return 0

  local lib_dir
  lib_dir="$(cd "$(dirname "$0")/../lib" && pwd)"

  python3 - "$ticket_id" "$worktree_path" "$lib_dir" 2>/dev/null <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[3])
from parse_jsonl_metrics import parse_jsonl_metrics

ticket_id, worktree_path = sys.argv[1], sys.argv[2]
m = parse_jsonl_metrics(worktree_path)
if m is None:
    sys.exit(0)

top = sorted(m.tool_call_counts.items(), key=lambda x: x[1], reverse=True)[:3]
top_tools = ",".join(f"{name}:{cnt}" for name, cnt in top)
print(f"METRICS:{ticket_id}:{m.total_tool_calls}:{m.error_count}:{m.agent_spawns}:{top_tools}")
PYEOF
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

  local status model ctx_pct last_progress
  status=$(detect_status "$sortie_dir")
  model=$(get_model "$sortie_dir")
  ctx_pct=$(get_context_pct "$sortie_dir")
  last_progress=$(get_last_progress "$sortie_dir")

  echo "AGENT:${ticket_id}:${status}:${model}:${ctx_pct}:${last_progress}"
  emit_jsonl_metrics "${ticket_id}" "${dir}"

  # Check for sub-agent worktrees
  for sub_dir in "$dir"/sub-*/; do
    if [ -d "$sub_dir/.sortie" ]; then
      local sub_name
      sub_name=$(basename "$sub_dir" | sed 's/^sub-//')
      local sub_status sub_model sub_ctx_pct sub_progress
      sub_status=$(detect_status "$sub_dir/.sortie")
      sub_model=$(get_model "$sub_dir/.sortie")
      sub_ctx_pct=$(get_context_pct "$sub_dir/.sortie")
      sub_progress=$(get_last_progress "$sub_dir/.sortie")
      echo "SUB:${ticket_id}:${sub_name}:${sub_status}:${sub_model}:${sub_ctx_pct}:${sub_progress}"
      emit_jsonl_metrics "${ticket_id}/${sub_name}" "${sub_dir}"
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
