#!/usr/bin/env bash
# Token savings preflight — detect RTK and Headroom status.
# Exit 0 with status report. Used by SKILL.md to decide setup vs dashboard flow.

set -euo pipefail

# ── Constants ────────────────────────────────────────────────────────

readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[0;33m'
readonly CYAN='\033[0;36m'
readonly BOLD='\033[1m'
readonly NC='\033[0m'

readonly SETTINGS="$HOME/.claude/settings.json"
readonly RTK_HOOK_SCRIPT="$HOME/.claude/hooks/rtk-rewrite.sh"
readonly RTK_WRAPPER_SCRIPT="$HOME/.claude/hooks/rtk-wrapper.sh"

# ── State ────────────────────────────────────────────────────────────

RTK_OK=false
RTK_VERSION=""
RTK_HOOK=false
RTK_PATH_FIX=false
HEADROOM_OK=false
HEADROOM_VERSION=""
HEADROOM_RUNNING=false
HEADROOM_HOOK=false

# ── RTK checks ───────────────────────────────────────────────────────

if command -v rtk &>/dev/null; then
  RTK_OK=true
  RTK_VERSION="$(rtk --version 2>/dev/null | head -1)"
fi

if [[ -f "$SETTINGS" ]] && grep -qE "rtk-rewrite|rtk-wrapper" "$SETTINGS" 2>/dev/null; then
  RTK_HOOK=true
fi

# Check PATH fix (wrapper preferred, direct as fallback)
if [[ -f "$RTK_WRAPPER_SCRIPT" ]] && grep -q '/opt/homebrew/bin\|\.cargo/bin' "$RTK_WRAPPER_SCRIPT" 2>/dev/null; then
  RTK_PATH_FIX=true
elif [[ -f "$RTK_HOOK_SCRIPT" ]] && grep -q '/opt/homebrew/bin\|\.cargo/bin' "$RTK_HOOK_SCRIPT" 2>/dev/null; then
  RTK_PATH_FIX=true
fi

# ── Headroom checks ─────────────────────────────────────────────────

if command -v headroom &>/dev/null; then
  HEADROOM_OK=true
  HEADROOM_VERSION="$(headroom --version 2>/dev/null | head -1)"
fi

# Single health request, cache the response
HEALTH_RESPONSE="$(curl -sf http://localhost:8787/health 2>/dev/null || echo "")"
if [[ -n "$HEALTH_RESPONSE" ]]; then
  HEADROOM_RUNNING=true
  if [[ -z "$HEADROOM_VERSION" ]]; then
    HEADROOM_VERSION="$(echo "$HEALTH_RESPONSE" \
      | python3 -c "import json,sys; print(json.load(sys.stdin).get('version','unknown'))" 2>/dev/null \
      || echo "unknown")"
  fi
fi

if [[ -f "$SETTINGS" ]] && grep -q "headroom" "$SETTINGS" 2>/dev/null; then
  HEADROOM_HOOK=true
fi

# ── Report ───────────────────────────────────────────────────────────

echo -e "${BOLD}═══ Token Savings Preflight ═══${NC}"
echo ""

# RTK status
echo -e "${BOLD}RTK (Command Output Filtering)${NC}"
if [[ "$RTK_OK" == true ]]; then
  echo -e "  ${GREEN}✓${NC} Installed (${RTK_VERSION})"
else
  echo -e "  ${RED}✗${NC} Not installed"
fi

if [[ "$RTK_HOOK" == true ]]; then
  echo -e "  ${GREEN}✓${NC} Hook wired (PreToolUse → Bash)"
else
  echo -e "  ${RED}✗${NC} Hook not wired"
fi

if [[ "$RTK_HOOK" == true && "$RTK_PATH_FIX" == false ]]; then
  echo -e "  ${YELLOW}⚠${NC} Hook missing PATH fix — will silently fail in Claude Code"
  echo -e "    Create a wrapper at ${RTK_WRAPPER_SCRIPT}:"
  echo -e "    #!/usr/bin/env bash"
  echo -e "    export PATH=\"/opt/homebrew/bin:/usr/local/bin:\$HOME/.cargo/bin:\$PATH\""
  echo -e "    exec bash \"\$(dirname \"\$0\")/rtk-rewrite.sh\" \"\$@\""
  echo -e "    Then point the hook in settings.json at rtk-wrapper.sh"
elif [[ "$RTK_PATH_FIX" == true ]]; then
  if [[ -f "$RTK_WRAPPER_SCRIPT" ]]; then
    echo -e "  ${GREEN}✓${NC} PATH fix applied (via wrapper — survives rtk init -g)"
  else
    echo -e "  ${GREEN}✓${NC} PATH fix applied (direct — will be lost on rtk init -g)"
  fi
fi

if [[ "$RTK_OK" == true ]]; then
  SAVED="$(rtk gain 2>/dev/null | grep "Tokens saved" | head -1)"
  if [[ -n "$SAVED" ]]; then
    echo -e "  ${CYAN}${SAVED}${NC}"
  fi
fi

echo ""

# Headroom status
echo -e "${BOLD}Headroom (Session Compression)${NC}"
if [[ "$HEADROOM_OK" == true ]]; then
  echo -e "  ${GREEN}✓${NC} Installed (${HEADROOM_VERSION})"
else
  echo -e "  ${RED}✗${NC} Not installed"
fi

if [[ "$HEADROOM_RUNNING" == true ]]; then
  echo -e "  ${GREEN}✓${NC} Proxy running on :8787"
else
  echo -e "  ${YELLOW}○${NC} Proxy not running"
fi

if [[ "$HEADROOM_HOOK" == true ]]; then
  echo -e "  ${GREEN}✓${NC} Hook wired (SessionStart)"
else
  echo -e "  ${RED}✗${NC} Hook not wired"
fi

echo ""

# Overall
ISSUES=0
[[ "$RTK_OK" == true ]]       || ISSUES=$((ISSUES + 1))
[[ "$RTK_HOOK" == true ]]     || ISSUES=$((ISSUES + 1))
[[ "$RTK_PATH_FIX" == true ]] || ISSUES=$((ISSUES + 1))
[[ "$HEADROOM_OK" == true ]]  || ISSUES=$((ISSUES + 1))
[[ "$HEADROOM_HOOK" == true ]] || ISSUES=$((ISSUES + 1))

if [[ "$ISSUES" -eq 0 ]]; then
  echo -e "${GREEN}${BOLD}═══ ALL SYSTEMS GO ═══${NC}"
  echo "PREFLIGHT:PASS"
else
  echo -e "${YELLOW}${BOLD}═══ ${ISSUES} ISSUE(S) TO FIX ═══${NC}"
  echo "PREFLIGHT:NEEDS_SETUP"
fi
