#!/usr/bin/env bash
# headroom-gain.sh — Show Headroom token savings (akin to rtk gain)

PORT="${HEADROOM_PORT:-8787}"
URL="http://localhost:${PORT}/stats"

if ! curl -sf "$URL" >/dev/null 2>&1; then
  echo "Headroom is not running on port ${PORT}"
  exit 1
fi

curl -s "$URL" | python3 - << 'EOF'
import json, sys

raw = sys.stdin.read()
# Headroom returns pseudo-JSON with unquoted keys — parse via ast
import ast, re

# Convert to valid JSON: quote keys, fix structure
def to_json(s):
    # Already valid JSON if it starts clean
    try:
        return json.loads(s)
    except Exception:
        pass
    raise ValueError("Could not parse stats")

try:
    data = json.loads(raw)
except Exception:
    print("  No stats yet — no pilot traffic has flowed through Headroom.")
    print("  Deploy a pilot to start accumulating savings.")
    sys.exit(0)

s = data.get("summary", {})
savings = data.get("savings", {})
cost = data.get("cost", {})
comp = s.get("compression", {})
cost_s = s.get("cost", {})

total_tokens_saved = savings.get("total_tokens", 0)
total_reqs = s.get("api_requests", 0)
compressed_reqs = comp.get("requests_compressed", 0)
avg_pct = comp.get("avg_compression_pct", 0)
best_pct = comp.get("best_compression_pct", 0)
saved_usd = cost_s.get("total_saved_usd", 0)
with_usd = cost_s.get("with_headroom_usd", 0)
without_usd = cost_s.get("without_headroom_usd", 0)
savings_pct = cost_s.get("savings_pct", 0)

by_layer = savings.get("by_layer", {})
comp_tokens = by_layer.get("compression", {}).get("tokens", 0)
cache_tokens = by_layer.get("prefix_cache", {})
cli_tokens = by_layer.get("cli_filtering", {}).get("tokens", 0)

print()
print("  ┌─────────────────────────────────────────┐")
print("  │         HEADROOM TOKEN SAVINGS           │")
print("  └─────────────────────────────────────────┘")
print()
print(f"  Total tokens saved:   {total_tokens_saved:>12,}")
print(f"  Estimated savings:    ${saved_usd:>11.4f}")
print(f"  Cost with headroom:   ${with_usd:>11.4f}")
print(f"  Cost without:         ${without_usd:>11.4f}")
print(f"  Overall savings:      {savings_pct:>11.1f}%")
print()
print(f"  ── By layer ──────────────────────────────")
print(f"  Compression:          {comp_tokens:>12,} tokens")
print(f"  CLI filtering:        {cli_tokens:>12,} tokens")
print()
print(f"  ── Compression ───────────────────────────")
print(f"  Requests compressed:  {compressed_reqs:>12,} / {total_reqs}")
print(f"  Avg compression:      {avg_pct:>11.1f}%")
print(f"  Best compression:     {best_pct:>11.1f}%")
if comp.get("best_detail"):
    print(f"  Best case:            {comp['best_detail'][:40]}")
print()
EOF
