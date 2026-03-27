#!/usr/bin/env bash
# headroom-gain.sh — Show Headroom token savings (akin to rtk gain)

PORT="${HEADROOM_PORT:-8787}"
URL="http://localhost:${PORT}/stats"

if ! curl -sf "$URL" >/dev/null 2>&1; then
  echo "Headroom is not running on port ${PORT}"
  exit 1
fi

# Requires Accept: application/json — without it the endpoint returns a type schema
# Capture to temp file: piping into `python3 - << 'EOF'` conflicts (heredoc overrides pipe stdin)
TMPJSON=$(mktemp)
trap "rm -f '$TMPJSON'" EXIT
curl -s -H "Accept: application/json" "$URL" > "$TMPJSON"

python3 << EOF
import json, sys

try:
    with open('${TMPJSON}') as f:
        data = json.load(f)
except Exception as e:
    print(f"  Could not parse stats response: {e}")
    sys.exit(1)

s        = data.get("summary", {})
savings  = data.get("savings", {})
comp     = s.get("compression", {})
cost_s   = s.get("cost", {})
cli      = data.get("cli_filtering", {})
tokens   = data.get("tokens", {})

total_tokens_saved = savings.get("total_tokens", 0)
total_reqs         = s.get("api_requests", 0)
compressed_reqs    = comp.get("requests_compressed", 0)
avg_pct            = comp.get("avg_compression_pct", 0)
best_pct           = comp.get("best_compression_pct", 0)
saved_usd          = cost_s.get("total_saved_usd", 0)
with_usd           = cost_s.get("with_headroom_usd", 0)
without_usd        = cost_s.get("without_headroom_usd", 0)
savings_pct        = cost_s.get("savings_pct", 0)

by_layer   = savings.get("by_layer", {})
comp_tokens = by_layer.get("compression", {}).get("tokens", 0)
cli_tokens  = by_layer.get("cli_filtering", {}).get("tokens", 0)
cli_avg_pct = cli.get("avg_savings_pct", 0)
cli_cmds    = cli.get("total_commands", 0)

print()
print("  ┌─────────────────────────────────────────┐")
print("  │         HEADROOM TOKEN SAVINGS           │")
print("  └─────────────────────────────────────────┘")
print()
print(f"  Total tokens saved:   {total_tokens_saved:>12,}")
if savings_pct:
    print(f"  Overall savings:      {savings_pct:>11.1f}%")
if saved_usd or without_usd:
    print(f"  Cost saved:           ${saved_usd:>11.4f}")
    print(f"  Cost with headroom:   ${with_usd:>11.4f}")
    print(f"  Cost without:         ${without_usd:>11.4f}")
print()
print(f"  ── By layer ──────────────────────────────")
print(f"  CLI filtering (RTK):  {cli_tokens:>12,} tokens  ({cli_avg_pct:.1f}% avg, {cli_cmds:,} cmds)")
print(f"  Proxy compression:    {comp_tokens:>12,} tokens")
print()
if total_reqs > 0:
    print(f"  ── Proxy compression ─────────────────────")
    print(f"  Requests compressed:  {compressed_reqs:>12,} / {total_reqs}")
    print(f"  Avg compression:      {avg_pct:>11.1f}%")
    print(f"  Best compression:     {best_pct:>11.1f}%")
    if comp.get("best_detail"):
        print(f"  Best case:            {comp['best_detail'][:40]}")
else:
    print("  ── Proxy compression ─────────────────────")
    print("  No pilot traffic yet — deploy a pilot to start compressing context.")
print()
EOF
