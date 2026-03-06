#!/usr/bin/env bash
# setup.sh — Verify all sortie prerequisites are met
# Usage: ./setup.sh
# Exit 0 if all good, exit 1 with details if something is missing

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ERRORS=()
WARNINGS=()

# 1. Claude CLI
if command -v claude &>/dev/null; then
  echo -e "${GREEN}✓${NC} claude CLI found: $(which claude)"
else
  ERRORS+=("claude CLI not found. Install: https://docs.anthropic.com/en/docs/claude-code")
fi

# 2. Git with worktree support
if command -v git &>/dev/null; then
  if git worktree list &>/dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} git with worktree support"
  else
    ERRORS+=("git worktree not supported — update git to 2.5+")
  fi
else
  ERRORS+=("git not found")
fi

# 3. osascript (macOS)
if command -v osascript &>/dev/null; then
  echo -e "${GREEN}✓${NC} osascript available"
else
  ERRORS+=("osascript not found — this skill requires macOS for iTerm2 control")
fi

# 4. iTerm2
if osascript -e 'tell application "System Events" to (name of processes) contains "iTerm2"' 2>/dev/null | grep -q "true"; then
  echo -e "${GREEN}✓${NC} iTerm2 is running"
else
  if [ -d "/Applications/iTerm.app" ]; then
    WARNINGS+=("iTerm2 is installed but not running. Launch it before spawning agents.")
    echo -e "${YELLOW}!${NC} iTerm2 installed but not running"
  else
    ERRORS+=("iTerm2 not found in /Applications. Install from https://iterm2.com")
  fi
fi

# 5. Linear MCP — check if available in global MCP config
LINEAR_MCP=false
if [ -f "$HOME/.claude/.mcp.json" ]; then
  if grep -q '"linear"' "$HOME/.claude/.mcp.json" 2>/dev/null; then
    LINEAR_MCP=true
    echo -e "${GREEN}✓${NC} Linear MCP configured globally"
  fi
fi
if [ "$LINEAR_MCP" = false ] && [ -f ".mcp.json" ]; then
  if grep -q '"linear"' ".mcp.json" 2>/dev/null; then
    LINEAR_MCP=true
    echo -e "${GREEN}✓${NC} Linear MCP configured for this project"
  fi
fi
if [ "$LINEAR_MCP" = false ]; then
  WARNINGS+=("Linear MCP not detected. The orchestrator will ask you to paste ticket details manually. To set up: add Linear to ~/.claude/.mcp.json")
  echo -e "${YELLOW}!${NC} Linear MCP not configured (optional — manual fallback available)"
fi

# 6. Check we're in a git repo
if git rev-parse --is-inside-work-tree &>/dev/null; then
  echo -e "${GREEN}✓${NC} Inside git repository: $(git rev-parse --show-toplevel)"
else
  ERRORS+=("Not inside a git repository. Sortie requires a git repo to create worktrees.")
fi

# Summary
echo ""
if [ ${#ERRORS[@]} -gt 0 ]; then
  echo -e "${RED}BLOCKED — ${#ERRORS[@]} issue(s) must be resolved:${NC}"
  for err in "${ERRORS[@]}"; do
    echo -e "  ${RED}✗${NC} $err"
  done
  echo ""
fi

if [ ${#WARNINGS[@]} -gt 0 ]; then
  echo -e "${YELLOW}WARNINGS:${NC}"
  for warn in "${WARNINGS[@]}"; do
    echo -e "  ${YELLOW}!${NC} $warn"
  done
  echo ""
fi

if [ ${#ERRORS[@]} -eq 0 ]; then
  echo -e "${GREEN}All prerequisites met. Sortie is ready.${NC}"
  exit 0
else
  exit 1
fi
