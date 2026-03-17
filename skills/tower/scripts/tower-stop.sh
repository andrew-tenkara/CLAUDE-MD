#!/usr/bin/env bash
# ── USS Tenkara — Clean Shutdown ─────────────────────────────────────
#
# Kills all Tower-related processes and cleans state files.
# Run after quitting the TUI, or standalone to nuke everything.
#
# Usage: bash tower-stop.sh [--force]
#   --force: skip confirmation prompt

set -euo pipefail

FORCE=false
[ "${1:-}" = "--force" ] && FORCE=true

STATE_DIR="/tmp/uss-tenkara/_prifly"
PROJECT_DIR="${SORTIE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
SORTIE_DIR="$PROJECT_DIR/.sortie"

# ── Gather what's running ────────────────────────────────────────────

dashboard_pids=$(pgrep -f "commander-dashboard" 2>/dev/null || true)
sentinel_pids=$(pgrep -f "sentinel.py" 2>/dev/null || true)
miniboss_pid=""
if [ -f "$STATE_DIR/miniboss-session" ]; then
    miniboss_pid=$(cat "$STATE_DIR/miniboss-session" 2>/dev/null || true)
fi

# Count dev servers from managed-servers.json
server_pids=""
if [ -f "$SORTIE_DIR/managed-servers.json" ]; then
    server_pids=$(python3 -c "
import json
try:
    entries = json.load(open('$SORTIE_DIR/managed-servers.json'))
    for e in entries:
        pid = e.get('pid')
        if pid:
            print(pid)
except: pass
" 2>/dev/null || true)
fi

echo "═══ USS TENKARA — SHUTDOWN ═══"
echo ""
[ -n "$dashboard_pids" ] && echo "  Dashboard: PIDs $dashboard_pids" || echo "  Dashboard: not running"
[ -n "$sentinel_pids" ] && echo "  Sentinel:  PIDs $sentinel_pids" || echo "  Sentinel:  not running"
[ -n "$server_pids" ] && echo "  Servers:   PIDs $server_pids" || echo "  Servers:   none"
echo "  State dir: $STATE_DIR"
echo ""

if [ -z "$dashboard_pids" ] && [ -z "$sentinel_pids" ] && [ -z "$server_pids" ] && [ ! -d "$STATE_DIR" ]; then
    echo "  Nothing to clean. Already stopped."
    exit 0
fi

if [ "$FORCE" = false ]; then
    read -p "  Kill all and clean state? [Y/n] " confirm
    case "$confirm" in
        [nN]*) echo "  Aborted."; exit 0 ;;
    esac
fi

# ── Kill processes ───────────────────────────────────────────────────

if [ -n "$dashboard_pids" ]; then
    echo "$dashboard_pids" | xargs kill 2>/dev/null || true
    echo "  ✓ Dashboard killed"
fi

if [ -n "$sentinel_pids" ]; then
    echo "$sentinel_pids" | xargs kill 2>/dev/null || true
    echo "  ✓ Sentinel killed"
fi

if [ -n "$server_pids" ]; then
    echo "$server_pids" | while read pid; do
        kill "$pid" 2>/dev/null || true
    done
    echo "  ✓ Dev servers killed"
    # Clean the registry
    echo "[]" > "$SORTIE_DIR/managed-servers.json"
fi

# ── Clean state files ────────────────────────────────────────────────

if [ -d "$STATE_DIR" ]; then
    rm -rf "$STATE_DIR"
    echo "  ✓ State dir cleaned"
fi

if [ -f "$SORTIE_DIR/sentinel-heartbeat.json" ]; then
    rm -f "$SORTIE_DIR/sentinel-heartbeat.json"
    echo "  ✓ Sentinel heartbeat cleaned"
fi

# ── Verify ───────────────────────────────────────────────────────────

echo ""
remaining_sentinels=$(pgrep -f "sentinel.py" 2>/dev/null || true)
if [ -n "$remaining_sentinels" ]; then
    echo "  ⚠ Stubborn sentinels still alive: $remaining_sentinels"
    echo "    Force kill with: kill -9 $remaining_sentinels"
else
    echo "  ✓ ALL STATIONS SECURED"
fi
