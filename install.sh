#!/usr/bin/env bash
#
# USS Tenkara — Installation & Preflight Check
#
# Verifies all dependencies are present, installs Python packages,
# and symlinks skills into ~/.claude/skills/.
#
# Usage:
#   bash install.sh
#
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILLS_DIR="${HOME}/.claude/skills"
FAILED=0

echo ""
echo -e "${BOLD}${CYAN}  ★ ★ ★  USS TENKARA — PREFLIGHT CHECK  ★ ★ ★${NC}"
echo ""

# ── Helper ────────────────────────────────────────────────────────────

check() {
  local name="$1"
  local cmd="$2"
  local help="$3"

  printf "  %-28s" "$name"
  if eval "$cmd" > /dev/null 2>&1; then
    echo -e "${GREEN}✓ READY${NC}"
  else
    echo -e "${RED}✗ MISSING${NC}"
    echo -e "    ${DIM}→ ${help}${NC}"
    FAILED=1
  fi
}

# ── Required ──────────────────────────────────────────────────────────

echo -e "${BOLD}Required:${NC}"
check "Python 3.10+"         "python3 -c 'import sys; assert sys.version_info >= (3, 10)'" \
                             "Install Python 3.10+: brew install python@3.12"
check "Claude CLI"           "which claude"  \
                             "Install: npm install -g @anthropic-ai/claude-code"
check "iTerm2"               "osascript -e 'tell application \"System Events\" to get name of every process' 2>/dev/null | grep -qi iterm || [ -d /Applications/iTerm.app ]" \
                             "Install from https://iterm2.com or: brew install --cask iterm2"
check "Git"                  "which git"  \
                             "Install: brew install git"

# ── Python packages ───────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Python packages:${NC}"

MISSING_PKGS=""
for pkg in textual watchdog rich; do
  printf "  %-28s" "$pkg"
  if python3 -c "import $pkg" 2>/dev/null; then
    echo -e "${GREEN}✓ installed${NC}"
  else
    echo -e "${YELLOW}○ not found${NC}"
    MISSING_PKGS="$MISSING_PKGS $pkg"
  fi
done

if [ -n "$MISSING_PKGS" ]; then
  echo ""
  echo -e "  ${CYAN}Installing missing packages...${NC}"
  pip3 install -r "$SCRIPT_DIR/requirements.txt"
  echo -e "  ${GREEN}✓ Packages installed${NC}"
fi

# ── Optional (nice to have) ───────────────────────────────────────────

echo ""
echo -e "${BOLD}Optional:${NC}"
check "Linear MCP"           "claude mcp list 2>/dev/null | grep -qi linear" \
                             "Add Linear MCP for ticket fetching: claude mcp add linear"
check "GitHub CLI (gh)"      "which gh"  \
                             "Install: brew install gh (for PR creation from agents)"
check "terminal-notifier"    "which terminal-notifier"  \
                             "Install: brew install terminal-notifier (for macOS notifications)"
check "RTK token optimizer"  "which rtk && rtk --version 2>/dev/null | grep -q rtk" \
                             "Optional token savings: https://github.com/anthropics/rtk"

# ── Symlink skills ────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Installing skills to ~/.claude/skills/:${NC}"

mkdir -p "$SKILLS_DIR"

for skill_dir in "$SCRIPT_DIR"/skills/*/; do
  skill_name=$(basename "$skill_dir")
  target="$SKILLS_DIR/$skill_name"

  printf "  %-28s" "$skill_name"

  if [ -L "$target" ]; then
    # Already a symlink — check if it points to us
    existing=$(readlink "$target")
    if [ "$existing" = "$skill_dir" ] || [ "$existing" = "${skill_dir%/}" ]; then
      echo -e "${GREEN}✓ linked${NC}"
    else
      echo -e "${YELLOW}○ exists (points elsewhere)${NC}"
      echo -e "    ${DIM}→ Current: $existing${NC}"
      echo -e "    ${DIM}→ To update: rm '$target' && ln -s '$skill_dir' '$target'${NC}"
    fi
  elif [ -d "$target" ]; then
    echo -e "${YELLOW}○ exists (real directory)${NC}"
    echo -e "    ${DIM}→ Skills already installed directly. Symlink skipped.${NC}"
  else
    ln -s "${skill_dir%/}" "$target"
    echo -e "${GREEN}✓ linked${NC}"
  fi
done

# ── Config ────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}Configuration:${NC}"

CONFIG="$SCRIPT_DIR/tower.config.json"
if [ -f "$CONFIG" ]; then
  echo -e "  Config file: ${CYAN}${CONFIG}${NC}"
  echo -e "  ${DIM}Edit tower.config.json to set your base branch, package manager, etc.${NC}"
  echo ""
  echo -e "  ${DIM}Defaults:${NC}"
  echo -e "    base_branch:      $(python3 -c "import json; print(json.load(open('$CONFIG'))['base_branch'])")"
  echo -e "    package_manager:  $(python3 -c "import json; print(json.load(open('$CONFIG'))['package_manager'])")"
  echo -e "    mini_boss_model:  $(python3 -c "import json; print(json.load(open('$CONFIG'))['mini_boss_model'])")"
  echo -e "    default_model:    $(python3 -c "import json; print(json.load(open('$CONFIG'))['default_pilot_model'])")"
fi

# ── Summary ───────────────────────────────────────────────────────────

echo ""
if [ "$FAILED" -eq 0 ]; then
  echo -e "${GREEN}${BOLD}  ALL STATIONS MANNED AND READY${NC}"
  echo ""
  echo -e "  Launch Tower:   ${CYAN}/tower <project-dir>${NC}"
  echo -e "  Queue a sortie: ${CYAN}/tq ENG-123${NC}"
  echo -e "  Interactive:    ${CYAN}/sortie${NC}"
  echo ""
else
  echo -e "${RED}${BOLD}  PREFLIGHT FAILED — fix the items above and re-run${NC}"
  echo ""
  exit 1
fi
