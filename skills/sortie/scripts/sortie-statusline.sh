#!/usr/bin/env bash
# sortie-statusline.sh — Claude Code statusline that writes context data to .sortie/context.json
#
# Configured per-agent in the worktree's .claude/settings.json.
# Receives JSON session data from Claude Code via stdin.
# Writes context snapshot to .sortie/context.json for external monitoring.
# Outputs a minimal status line to stdout for the agent's own terminal.

input=$(cat)

# Bail early if no input (avoids jq errors)
if [ -z "$input" ]; then
  echo "[--] no data"
  exit 0
fi

# Extract fields — all with safe defaults
MODEL=$(echo "$input" | jq -r '.model.display_name // "unknown"' 2>/dev/null || echo "unknown")
PCT=$(echo "$input" | jq -r '.context_window.used_percentage // 0' 2>/dev/null | cut -d. -f1)
PCT=${PCT:-0}
CTX_SIZE=$(echo "$input" | jq -r '.context_window.context_window_size // 200000' 2>/dev/null || echo 200000)
INPUT_TOKENS=$(echo "$input" | jq -r '.context_window.total_input_tokens // 0' 2>/dev/null || echo 0)
OUTPUT_TOKENS=$(echo "$input" | jq -r '.context_window.total_output_tokens // 0' 2>/dev/null || echo 0)
PROJECT_DIR=$(echo "$input" | jq -r '.workspace.project_dir // empty' 2>/dev/null || echo "")

# Write context snapshot for sortie monitoring (atomic via tmp+mv)
SORTIE_DIR="${PROJECT_DIR}/.sortie"
if [ -d "$SORTIE_DIR" ] && command -v jq >/dev/null 2>&1; then
  TMP_FILE="$SORTIE_DIR/context.json.tmp.$$"
  if jq -n --argjson pct "${PCT:-0}" \
    --argjson ctx_size "${CTX_SIZE}" \
    --argjson in_tok "${INPUT_TOKENS}" \
    --argjson out_tok "${OUTPUT_TOKENS}" \
    --arg model "${MODEL}" \
    --argjson ts "$(date +%s)" \
    '{used_percentage:$pct, context_window_size:$ctx_size, total_input_tokens:$in_tok, total_output_tokens:$out_tok, model:$model, timestamp:$ts}' \
    > "$TMP_FILE" 2>/dev/null; then
    mv "$TMP_FILE" "$SORTIE_DIR/context.json"
  else
    rm -f "$TMP_FILE"
  fi
fi

# Color coding for the agent's own terminal
if [ "$PCT" -ge 80 ]; then
  COLOR="\033[1;31m"  # bright red
elif [ "$PCT" -ge 50 ]; then
  COLOR="\033[1;33m"  # yellow
else
  COLOR="\033[32m"    # green
fi
RESET="\033[0m"

# Build a small bar (10 chars wide)
FILLED=$(( PCT / 10 ))
EMPTY=$(( 10 - FILLED ))
BAR=""
for (( i=0; i<FILLED; i++ )); do BAR+="█"; done
for (( i=0; i<EMPTY; i++ )); do BAR+="░"; done

echo -e "[${MODEL}] ${COLOR}${BAR} ${PCT}%${RESET} | ${INPUT_TOKENS}in/${OUTPUT_TOKENS}out"
