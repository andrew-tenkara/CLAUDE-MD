#!/usr/bin/env bash
# preflight-worktrees.sh — Ensure all existing worktrees have required config files
#
# Runs on Tower startup to retroactively provision worktree config that
# deploy-agent.sh normally writes at creation time. Covers:
#   - .claudeignore (token hygiene)
#   - .mcp.json (per-worktree Serena + inherited MCP servers)
#   - .env.local symlink (env vars from project root)
#   - .claude/settings.json (branch-scoped push permissions)
#   - .sortie/progress.md (dashboard expects it)
#
# Usage: preflight-worktrees.sh <project-dir>

set -euo pipefail

PROJECT_DIR="${1:?Usage: preflight-worktrees.sh <project-dir>}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SORTIE_SCRIPTS="${HOME}/.claude/skills/sortie/scripts"

if [ ! -d "$PROJECT_DIR" ]; then
  echo "PREFLIGHT:ERROR — project dir not found: $PROJECT_DIR" >&2
  exit 1
fi

PATCHED=0

# Iterate worktrees via git porcelain format
while IFS= read -r line; do
  [[ "$line" == worktree\ * ]] || continue
  WT_PATH="${line#worktree }"

  # Skip main worktree
  [ "$WT_PATH" = "$PROJECT_DIR" ] && continue

  # Skip if no .sortie dir (not a Tower-managed worktree)
  [ -d "$WT_PATH/.sortie" ] || continue

  CHANGED=false

  # ── .claudeignore ──────────────────────────────────────────────────
  if [ -f "${PROJECT_DIR}/.claudeignore" ] && [ ! -f "${WT_PATH}/.claudeignore" ]; then
    cp "${PROJECT_DIR}/.claudeignore" "${WT_PATH}/.claudeignore"
    CHANGED=true
  fi

  # ── .mcp.json (with per-worktree Serena) ───────────────────────────
  if [ ! -f "${WT_PATH}/.mcp.json" ]; then
    if [ -f "${PROJECT_DIR}/.mcp.json" ]; then
      cp "${PROJECT_DIR}/.mcp.json" "${WT_PATH}/.mcp.json"
    else
      echo '{"mcpServers":{}}' > "${WT_PATH}/.mcp.json"
    fi
    python3 -c "
import json
with open('${WT_PATH}/.mcp.json') as f: cfg = json.load(f)
cfg.setdefault('mcpServers', {})['serena'] = {
    'command': 'uvx',
    'args': ['--from', 'git+https://github.com/oraios/serena',
             'serena', 'start-mcp-server',
             '--context', 'ide-assistant',
             '--project', '${WT_PATH}']
}
with open('${WT_PATH}/.mcp.json', 'w') as f: json.dump(cfg, f, indent=2)
"
    CHANGED=true
  fi

  # ── .env.local symlink ───────────────────────────────────────────────
  if [ ! -f "${WT_PATH}/.env.local" ] && [ -f "${PROJECT_DIR}/.env.local" ]; then
    ln -sf "${PROJECT_DIR}/.env.local" "${WT_PATH}/.env.local"
    CHANGED=true
  fi

  # ── .sortie/progress.md ─────────────────────────────────────────────
  if [ ! -f "${WT_PATH}/.sortie/progress.md" ]; then
    touch "${WT_PATH}/.sortie/progress.md"
    CHANGED=true
  fi

  # ── .claude/settings.json (branch-scoped push permissions) ──────────
  if [ ! -f "${WT_PATH}/.claude/settings.json" ] && [ -x "${SORTIE_SCRIPTS}/write-settings.sh" ]; then
    # Extract branch name from git worktree
    WT_BRANCH=$(git -C "$WT_PATH" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
    if [ -n "$WT_BRANCH" ]; then
      bash "${SORTIE_SCRIPTS}/write-settings.sh" "$WT_BRANCH" "$WT_PATH" "$PROJECT_DIR" 2>/dev/null || true
      CHANGED=true
    fi
  fi

  if [ "$CHANGED" = true ]; then
    PATCHED=$((PATCHED + 1))
    echo "PREFLIGHT:patched $(basename "$WT_PATH")"
  fi

done < <(git -C "$PROJECT_DIR" worktree list --porcelain 2>/dev/null)

if [ "$PATCHED" -gt 0 ]; then
  echo "PREFLIGHT:done — patched $PATCHED worktree(s)"
else
  echo "PREFLIGHT:done — all worktrees up to date"
fi
