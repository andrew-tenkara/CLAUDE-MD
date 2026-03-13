#!/usr/bin/env bash
# carrier-status.sh — Quick carrier-formatted status output (non-TUI)
# Usage: ./carrier-status.sh [--project-dir /path/to/project]
#
# Reads .sortie/ state and outputs a carrier-themed status summary.

set -euo pipefail

PROJECT_DIR="${SORTIE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || echo .)}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-dir) PROJECT_DIR="$2"; shift 2 ;;
    *) shift ;;
  esac
done

WORKTREES_DIR="${PROJECT_DIR}/.claude/worktrees"

if [[ ! -d "$WORKTREES_DIR" ]]; then
  echo "⚓ USS TENKARA ━━━ CIC"
  echo "CONDITION: GREEN │ No sorties active │ $(date '+%H:%M LOCAL')"
  exit 0
fi

echo "⚓ USS TENKARA ━━━ CIC ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

AIRBORNE=0
ON_APPROACH=0
RECOVERED=0
MAYDAY=0

for ENTRY in "${WORKTREES_DIR}"/*/; do
  [[ -d "$ENTRY" ]] || continue
  SORTIE_DIR="${ENTRY}.sortie"
  [[ -d "$SORTIE_DIR" ]] || continue

  TICKET="Unknown"
  if [[ -f "${SORTIE_DIR}/directive.md" ]]; then
    TICKET=$(grep -oP '\*\*ID\*\*:\s*\K.+' "${SORTIE_DIR}/directive.md" 2>/dev/null || echo "Unknown")
  fi

  MODEL="unknown"
  [[ -f "${SORTIE_DIR}/model.txt" ]] && MODEL=$(cat "${SORTIE_DIR}/model.txt" 2>/dev/null || echo "unknown")

  # Determine CIC status
  STATUS="AIRBORNE"
  if [[ -f "${SORTIE_DIR}/post-review.done" ]]; then
    STATUS="RECOVERED"
    ((RECOVERED++))
  elif [[ -f "${SORTIE_DIR}/pre-review.done" ]]; then
    STATUS="ON APPROACH"
    ((ON_APPROACH++))
  else
    ((AIRBORNE++))
  fi

  # Squadron callsign
  case "${MODEL,,}" in
    opus*)    SQUADRON="Viper" ;;
    sonnet*)  SQUADRON="Iceman" ;;
    haiku*)   SQUADRON="Maverick" ;;
    *)        SQUADRON="Ghost" ;;
  esac

  # Context %
  FUEL="N/A"
  if [[ -f "${SORTIE_DIR}/context.json" ]]; then
    FUEL=$(python3 -c "import json; d=json.load(open('${SORTIE_DIR}/context.json')); print(f\"{d.get('used_percentage', '?')}%\")" 2>/dev/null || echo "N/A")
  fi

  printf "  %-12s %-10s %-12s FUEL: %-6s %s\n" "${SQUADRON} (${TICKET})" "${MODEL^}" "${STATUS}" "${FUEL}" ""
done

echo ""
TOTAL=$((AIRBORNE + ON_APPROACH + RECOVERED + MAYDAY))
COND="GREEN"
[[ $MAYDAY -gt 0 ]] && COND="RED"

echo "CONDITION: ${COND} │ AIRBORNE: ${AIRBORNE} │ ON DECK: ${ON_APPROACH} │ RECOVERED: ${RECOVERED} │ $(date '+%H:%M LOCAL')"
