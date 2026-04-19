#!/usr/bin/env bash
# Token savings preflight — detect RTK and Headroom status
# Exit 0 with status report. Used by SKILL.md to decide setup vs dashboard flow.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

RTK_OK=false
RTK_VERSION=""
RTK_HOOK=false
RTK_PATH_FIX=false
HEADROOM_OK=false
HEADROOM_VERSION=""
HEADROOM_RUNNING=false
HEADROOM_HOOK=false

# ── RTK ──────────────────────────────────────────────────────────────

if command -v rtk &>/dev/null; then
  RTK_OK=true
  RTK_VERSION=$(rtk --version 2>/dev/null | head -1)
fi

# Check hook in ~/.claude/settings.json
SETTINGS="$HOME/.claude/settings.json"
if [ -f "$SETTINGS" ] && grep -q "rtk-rewrite" "$SETTINGS" 2>/dev/null; then
  RTK_HOOK=true
fi

# Check PATH fix in hook script
RTK_HOOK_SCRIPT="$HOME/.claude/hooks/rtk-rewrite.sh"
if [ -f "$RTK_HOOK_SCRIPT" ] && grep -q '/opt/homebrew/bin\|\.cargo/bin' "$RTK_HOOK_SCRIPT" 2>/dev/null; then
  RTK_PATH_FIX=true
fi

# ── Headroom ─────────────────────────────────────────────────────────

if command -v headroom &>/dev/null; then
  HEADROOM_OK=true
  HEADROOM_VERSION=$(headroom --version 2>/dev/null | head -1)
fi

# Check if running (single request, cache response)
HEALTH_RESPONSE=$(curl -sf http://localhost:8787/health 2>/dev/null || echo "")
if [ -n "$HEALTH_RESPONSE" ]; then
  HEADROOM_RUNNING=true
  if [ -z "$HEADROOM_VERSION" ]; then
    HEADROOM_VERSION=$(echo "$HEALTH_RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('version','unknown'))" 2>/dev/null || echo "unknown")
  fi
fi

# Check hook
if [ -f "$SETTINGS" ] && grep -q "headroom" "$SETTINGS" 2>/dev/null; then
  HEADROOM_HOOK=true
fi

# ── Report ───────────────────────────────────────────────────────────

echo -e "${BOLD}═══ Token Savings Preflight ═══${NC}"
echo ""

# RTK status
echo -e "${BOLD}RTK (Command Output Filtering)${NC}"
if $RTK_OK; then
  echo -e "  ${GREEN}✓${NC} Installed (${RTK_VERSION})"
else
  echo -e "  ${RED}✗${NC} Not installed"
fi
if $RTK_HOOK; then
  echo -e "  ${GREEN}✓${NC} Hook wired (PreToolUse → Bash)"
else
  echo -e "  ${RED}✗${NC} Hook not wired"
fi
if $RTK_HOOK && ! $RTK_PATH_FIX; then
  echo -e "  ${YELLOW}⚠${NC} Hook missing PATH fix — will silently fail in Claude Code"
  echo -e "    Add to top of ${RTK_HOOK_SCRIPT}:"
  echo -e "    export PATH=\"/opt/homebrew/bin:/usr/local/bin:\$HOME/.cargo/bin:\$PATH\""
elif $RTK_PATH_FIX; then
  echo -e "  ${GREEN}✓${NC} PATH fix applied"
fi

# Show savings if available
if $RTK_OK; then
  SAVED=$(rtk gain 2>/dev/null | grep "Tokens saved" | head -1)
  if [ -n "$SAVED" ]; then
    echo -e "  ${CYAN}$SAVED${NC}"
  fi
fi

echo ""

# Headroom status
echo -e "${BOLD}Headroom (Session Compression)${NC}"
if $HEADROOM_OK; then
  echo -e "  ${GREEN}✓${NC} Installed (${HEADROOM_VERSION})"
else
  echo -e "  ${RED}✗${NC} Not installed"
fi
if $HEADROOM_RUNNING; then
  echo -e "  ${GREEN}✓${NC} Proxy running on :8787"
else
  echo -e "  ${YELLOW}○${NC} Proxy not running"
fi
if $HEADROOM_HOOK; then
  echo -e "  ${GREEN}✓${NC} Hook wired (SessionStart)"
else
  echo -e "  ${RED}✗${NC} Hook not wired"
fi

echo ""

# Overall — check each condition explicitly
ISSUES=0
$RTK_OK       || ISSUES=$((ISSUES + 1))
$RTK_HOOK     || ISSUES=$((ISSUES + 1))
$RTK_PATH_FIX || ISSUES=$((ISSUES + 1))
$HEADROOM_OK  || ISSUES=$((ISSUES + 1))
$HEADROOM_HOOK || ISSUES=$((ISSUES + 1))

if [ "$ISSUES" -eq 0 ]; then
  echo -e "${GREEN}${BOLD}═══ ALL SYSTEMS GO ═══${NC}"
  echo "PREFLIGHT:PASS"
else
  echo -e "${YELLOW}${BOLD}═══ ${ISSUES} ISSUE(S) TO FIX ═══${NC}"
  echo "PREFLIGHT:NEEDS_SETUP"
fi
