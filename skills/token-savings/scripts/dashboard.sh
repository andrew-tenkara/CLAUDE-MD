#!/usr/bin/env bash
# Token savings dashboard — formatted CLI report.
# Pulls from RTK gain + Headroom /stats endpoint.

set -euo pipefail

# ── Constants ────────────────────────────────────────────────────────

readonly BOLD='\033[1m'
readonly DIM='\033[2m'
readonly GREEN='\033[0;32m'
readonly CYAN='\033[0;36m'
readonly YELLOW='\033[0;33m'
readonly NC='\033[0m'

readonly HEADROOM_URL="http://localhost:8787"

# ── Dependency check ─────────────────────────────────────────────────

HAS_PYTHON=true
if ! command -v python3 &>/dev/null; then
  echo -e "${YELLOW}python3 required for Headroom stats parsing${NC}" >&2
  echo -e "Showing RTK stats only." >&2
  HAS_PYTHON=false
fi

# ── Temp file with trap cleanup ──────────────────────────────────────

STATS_FILE="$(mktemp 2>/dev/null || echo "/tmp/headroom-stats-$$")"
cleanup() { rm -f "$STATS_FILE" 2>/dev/null; }
trap cleanup EXIT

# ── Header ───────────────────────────────────────────────────────────

echo -e "${BOLD}═══ Token Savings Dashboard ═══${NC}"
echo ""

# ── RTK Stats ────────────────────────────────────────────────────────

echo -e "${BOLD}RTK (Command Output Filtering)${NC}"

if command -v rtk &>/dev/null; then
  RTK_VERSION="$(rtk --version 2>/dev/null | head -1)"
  echo -e "  Status:       ${GREEN}● ACTIVE${NC} (${RTK_VERSION})"

  RTK_OUTPUT="$(rtk gain 2>/dev/null || echo "")"
  if [[ -n "$RTK_OUTPUT" ]]; then
    TOTAL_CMDS="$(echo "$RTK_OUTPUT" | grep "Total commands" | awk '{print $NF}')"
    SAVED_LINE="$(echo "$RTK_OUTPUT" | grep "Tokens saved" | head -1)"
    SAVED_TOKENS="$(echo "$SAVED_LINE" | awk '{print $3}')"
    SAVED_PCT="$(echo "$SAVED_LINE" | grep -oE '[0-9]+\.[0-9]+%' || echo "?%")"

    echo -e "  Total saved:  ${CYAN}${SAVED_TOKENS:-0} tokens (${SAVED_PCT})${NC}"
    echo -e "  Commands:     ${TOTAL_CMDS:-0}"

    # Top 3 savers via python for reliable parsing
    if [[ "$HAS_PYTHON" == true ]]; then
      TOP_OUTPUT="$(echo "$RTK_OUTPUT" | python3 -c "
import sys
lines = [l.strip() for l in sys.stdin if l.strip() and l.strip()[0].isdigit() and '.' in l.strip()[:4]]
for line in lines[:3]:
    parts = line.split()
    if len(parts) >= 6:
        cmd = ' '.join(parts[1:3])
        saved = parts[4]
        pct = parts[5]
        print(f'    {cmd}: {saved} ({pct})')
" 2>/dev/null || echo "")"

      if [[ -n "$TOP_OUTPUT" ]]; then
        echo -e "  Top savers:"
        while IFS= read -r line; do
          echo -e "  ${DIM}${line}${NC}"
        done <<< "$TOP_OUTPUT"
      fi
    fi
  fi
else
  echo -e "  Status:       ${YELLOW}○ NOT INSTALLED${NC}"
fi

echo ""

# ── Headroom Stats ───────────────────────────────────────────────────

echo -e "${BOLD}Headroom (Session Compression)${NC}"

STATS_OK=false
if curl -sf "${HEADROOM_URL}/stats" > "$STATS_FILE" 2>/dev/null && [[ -s "$STATS_FILE" ]]; then
  STATS_OK=true
fi

if [[ "$STATS_OK" == true && "$HAS_PYTHON" == true ]]; then
  VERSION="$(curl -sf "${HEADROOM_URL}/health" 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('version','?'))" 2>/dev/null \
    || echo "?")"
  echo -e "  Status:       ${GREEN}● LIVE on :8787${NC} (v${VERSION})"

  # Parse stats from file (no shell interpolation of JSON)
  python3 - "$STATS_FILE" << 'PYEOF'
import json, sys

with open(sys.argv[1]) as f:
    stats = json.load(f)

def fmt(n):
    if n is None: return "—"
    if n >= 1_000_000_000: return f'{n/1_000_000_000:.2f}B'
    if n >= 1_000_000: return f'{n/1_000_000:.1f}M'
    if n >= 1_000: return f'{n/1_000:.1f}K'
    return str(int(n))

summary = stats.get('summary', {})
compression = summary.get('compression', {})
avg_pct = compression.get('avg_compression_pct', 0)
requests_compressed = compression.get('requests_compressed', 0)
api_requests = summary.get('api_requests', 0)

cache = stats.get('prefix_cache', {}).get('totals', {})
cache_rate_raw = cache.get('hit_rate', 0)
cache_rate = cache_rate_raw / 100 if cache_rate_raw > 1 else cache_rate_raw
cache_reads = cache.get('cache_read_tokens', 0)
cache_savings = cache.get('savings_usd', 0)

overhead = stats.get('overhead', {})
avg_ms = overhead.get('average_ms', 0)

layers = stats.get('savings', {}).get('by_layer', {})
comp_tokens = layers.get('compression', {}).get('tokens', 0)

print(f'  Compression:  {fmt(comp_tokens)} tokens removed ({avg_pct:.1f}% avg)')
print(f'  Prefix cache: {cache_rate:.0%} hit rate ({fmt(cache_reads)} read tokens, ${cache_savings:.2f} saved)')
print(f'                (Anthropic-side cache; Headroom keeps prefix stable for hits)')
print(f'  Overhead:     {avg_ms:.0f}ms avg')
print(f'  Requests:     {api_requests} API calls ({requests_compressed} compressed)')
print()

per_model = stats.get('cost', {}).get('per_model', {})
if per_model:
    print('  Per-Model Breakdown')
    for model, data in sorted(per_model.items()):
        short = model.replace('claude-', '')
        for suffix in ['-20250514', '-20250929', '-20251001', '-20251101', '-20260101']:
            short = short.replace(suffix, '')
        sent = fmt(data.get('tokens_sent', 0))
        saved = fmt(data.get('tokens_saved', 0))
        pct = data.get('reduction_pct', 0)
        reqs = data.get('requests', 0)
        print(f'    {short:<20} {sent:>8} sent  {saved:>8} saved  {pct:>5.1f}%  ({reqs} reqs)')
    print()

cost = stats.get('cost', {})
total_saved_usd = cost.get('savings_usd', 0)
with_headroom = cost.get('cost_with_headroom_usd', 0)
without_headroom = cost.get('cost_without_headroom_usd', 0)
if total_saved_usd > 0:
    print(f'  Cost savings: ${total_saved_usd:.2f} (would have been ${without_headroom:.2f}, now ${with_headroom:.2f})')

total_tokens = stats.get('savings', {}).get('total_tokens', 0)
if total_tokens > 0:
    print()
    print(f'  COMBINED:     {total_tokens:,} tokens saved')
PYEOF

elif [[ "$STATS_OK" == true ]]; then
  echo -e "  Status:       ${GREEN}● LIVE on :8787${NC}"
  echo -e "  ${YELLOW}python3 required for detailed stats${NC}"
else
  echo -e "  Status:       ${YELLOW}○ NOT RUNNING${NC}"
  if command -v headroom &>/dev/null; then
    echo -e "  ${DIM}Start with: headroom proxy --port 8787${NC}"
  else
    echo -e "  ${DIM}Install with: pip install \"headroom-ai[proxy]\"${NC}"
  fi
fi

echo ""
echo -e "${BOLD}═══════════════════════════════════${NC}"
