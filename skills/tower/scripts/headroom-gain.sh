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
cc       = data.get("compression_cache", {})
pc       = data.get("prefix_cache", {})
pc_tot   = pc.get("totals", {})
req      = data.get("requests", {})
cost     = data.get("cost", {})

total_tokens_saved = savings.get("total_tokens", 0)
total_reqs         = req.get("total", s.get("api_requests", 0))

by_layer    = savings.get("by_layer", {})
comp_tokens = by_layer.get("compression", {}).get("tokens", 0)
cli_tokens  = by_layer.get("cli_filtering", {}).get("tokens", 0)
cli_avg_pct = cli.get("avg_savings_pct", 0)
cli_cmds    = cli.get("total_commands", 0)

# Prefix cache
pc_hit_reqs    = pc_tot.get("hit_requests", 0)
pc_total_reqs  = pc_tot.get("requests", 0)
pc_hit_rate    = pc_tot.get("hit_rate", 0)
pc_read_tokens = pc_tot.get("cache_read_tokens", 0)

# Compression cache (per-block dedup cache — more accurate than requests_compressed)
cc_entries  = cc.get("total_entries", 0)
cc_hits     = cc.get("total_hits", 0)
cc_misses   = cc.get("total_misses", 0)
cc_hit_rate = cc.get("hit_rate", 0)
avg_pct     = comp.get("avg_compression_pct", 0)

# Per-session proxy stats (tokens actually sent vs saved through headroom this session)
session_sent  = cost.get("total_input_tokens", 0)
per_model     = cost.get("per_model", {})
session_saved = sum(m.get("tokens_saved", 0) for m in per_model.values())
session_total = session_sent + session_saved
session_pct   = (session_saved / session_total * 100) if session_total > 0 else 0

print()
print("  ┌─────────────────────────────────────────┐")
print("  │         HEADROOM TOKEN SAVINGS           │")
print("  └─────────────────────────────────────────┘")
print()
print(f"  Total tokens saved:   {total_tokens_saved:>12,}")
print()
print(f"  ── By layer ──────────────────────────────")
print(f"  CLI filtering (RTK):  {cli_tokens:>12,} tokens  ({cli_avg_pct:.1f}% avg, {cli_cmds:,} cmds)")
if pc_read_tokens:
    print(f"  Prefix cache:         {pc_read_tokens:>12,} tokens  ({pc_hit_rate:.1f}% hit rate, {pc_hit_reqs}/{pc_total_reqs} reqs)")
print(f"  ML compression:       {comp_tokens:>12,} tokens")
print()
if total_reqs > 0:
    print(f"  ── Prefix cache ──────────────────────────")
    print(f"  Hit rate:             {pc_hit_rate:>11.1f}%  ({pc_hit_reqs} / {pc_total_reqs} requests)")
    print()
    print(f"  ── ML compression ────────────────────────")
    if cc_entries > 0:
        cc_events = cc_hits + cc_misses
        print(f"  Content blocks cached:{cc_entries:>12,}")
        print(f"  Cache hits:           {cc_hits:>12,} / {cc_events:,} events  ({cc_hit_rate:.1f}%)")
        if avg_pct:
            print(f"  Avg block reduction:  {avg_pct:>11.1f}%")
    else:
        print("  No pilot traffic yet — deploy a pilot to start compressing context.")
else:
    print("  ── ML compression ────────────────────────")
    print("  No pilot traffic yet — deploy a pilot to start compressing context.")

if session_sent > 0:
    print()
    print(f"  ── This session ──────────────────────────")
    print(f"  Tokens sent:          {session_sent:>12,}")
    print(f"  Tokens saved:         {session_saved:>12,}  ({session_pct:.1f}% reduction)")
    if per_model:
        print()
        col = 10
        for model, m in sorted(per_model.items()):
            label = model.replace("claude-", "").replace("-20251001", "")
            sent  = m.get("tokens_sent", 0)
            saved = m.get("tokens_saved", 0)
            pct   = m.get("reduction_pct", 0)
            reqs  = m.get("requests", 0)
            print(f"  {label:<{col}}  {sent:>8,} sent  {saved:>8,} saved  ({pct:.1f}%)  {reqs} reqs")
print()
EOF
