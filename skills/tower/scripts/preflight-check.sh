#!/usr/bin/env bash
# ── USS Tenkara — Preflight Environment Check ────────────────────────
#
# Run by Mini Boss on startup to verify all dependencies, API keys,
# and MCP servers are configured. Outputs a structured report that
# the XO uses to guide the user through setup.
#
# Usage: bash preflight-check.sh [--project-dir DIR]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${1:-.}"
SHELL_RC="$HOME/.zshrc"
[ -f "$SHELL_RC" ] || SHELL_RC="$HOME/.bashrc"

PASS="✓"
FAIL="✗"
WARN="⚠"
issues=0
warnings=0

echo "═══ USS TENKARA — PREFLIGHT CHECK ═══"
echo ""

# ── 1. Python dependencies ───────────────────────────────────────────
echo "── PYTHON DEPS ──"

check_python_pkg() {
    local pkg="$1"
    local import_name="${2:-$1}"
    if python3 -c "import $import_name" 2>/dev/null; then
        echo "  $PASS $pkg"
    else
        echo "  $FAIL $pkg — not installed"
        echo "    Fix: pip3 install $pkg"
        issues=$((issues + 1))
    fi
}

check_python_pkg "textual" "textual"
check_python_pkg "rich" "rich"
check_python_pkg "watchdog" "watchdog"
check_python_pkg "anthropic" "anthropic"

echo ""

# ── 2. API Keys ──────────────────────────────────────────────────────
echo "── API KEYS ──"

check_api_key() {
    local name="$1"
    local env_var="$2"
    local value="${!env_var:-}"
    if [ -n "$value" ]; then
        local masked="${value:0:12}...${value: -4}"
        echo "  $PASS $name ($masked)"
    else
        echo "  $FAIL $name — not set"
        echo "    Fix: Add this line to $SHELL_RC:"
        echo "    export $env_var=\"\""
        echo "    Then run: source $SHELL_RC"
        issues=$((issues + 1))
    fi
}

check_api_key "Anthropic API Key" "ANTHROPIC_API_KEY"

# Check if key is also in shell rc for persistence
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    if grep -q "ANTHROPIC_API_KEY" "$SHELL_RC" 2>/dev/null; then
        echo "    (persisted in $SHELL_RC)"
    else
        echo "  $WARN Key is set in current session but NOT persisted in $SHELL_RC"
        echo "    Fix: Add to $SHELL_RC:"
        echo "    export ANTHROPIC_API_KEY=\"$ANTHROPIC_API_KEY\""
        warnings=$((warnings + 1))
    fi
fi

echo ""

# ── 3. CLI Tools ─────────────────────────────────────────────────────
echo "── CLI TOOLS ──"

check_cli() {
    local name="$1"
    local cmd="$2"
    local install_hint="$3"
    if command -v "$cmd" &>/dev/null; then
        local version
        version=$($cmd --version 2>/dev/null | head -1 || echo "installed")
        echo "  $PASS $name ($version)"
    else
        echo "  $FAIL $name — not found"
        echo "    Fix: $install_hint"
        issues=$((issues + 1))
    fi
}

check_cli "Claude CLI" "claude" "npm install -g @anthropic-ai/claude-code"
check_cli "Git" "git" "xcode-select --install"
check_cli "GitHub CLI" "gh" "brew install gh"

# RTK is optional
if command -v rtk &>/dev/null; then
    echo "  $PASS RTK ($(rtk --version 2>/dev/null || echo 'installed')) — token optimizer"
else
    echo "  $WARN RTK — not installed (optional, saves 60-90% tokens)"
    echo "    Install: brew install rtk && rtk init -g"
    warnings=$((warnings + 1))
fi

echo ""

# ── 4. MCP Servers ───────────────────────────────────────────────────
echo "── MCP SERVERS ──"

# Check Claude Code MCP config for Linear
check_mcp() {
    local name="$1"
    local search="$2"
    local install_hint="$3"

    # Check project-level .mcp.json
    local found=false
    for config_file in "$PROJECT_DIR/.mcp.json" "$HOME/.claude/mcp.json"; do
        if [ -f "$config_file" ] && grep -q "$search" "$config_file" 2>/dev/null; then
            found=true
            echo "  $PASS $name (in $(basename "$config_file"))"
            break
        fi
    done

    if [ "$found" = false ]; then
        echo "  $WARN $name — not configured"
        echo "    $install_hint"
        warnings=$((warnings + 1))
    fi
}

check_mcp "Linear" "linear" "Add Linear MCP to .mcp.json — see https://docs.linear.app/mcp"

echo ""

# ── 5. iTerm2 ────────────────────────────────────────────────────────
echo "── ITERM2 ──"

if [ -d "/Applications/iTerm.app" ]; then
    echo "  $PASS iTerm2 installed"
else
    echo "  $FAIL iTerm2 — not installed (required for agent panes)"
    echo "    Fix: brew install --cask iterm2"
    issues=$((issues + 1))
fi

# Check if iTerm2 scripting is enabled
if defaults read com.googlecode.iterm2 EnableAPIServer 2>/dev/null | grep -q 1; then
    echo "  $PASS iTerm2 API server enabled"
else
    echo "  $WARN iTerm2 API server — may not be enabled"
    echo "    Fix: iTerm2 → Settings → General → Magic → Enable Python API"
    warnings=$((warnings + 1))
fi

echo ""

# ── 6. Project Setup ─────────────────────────────────────────────────
echo "── PROJECT ──"

if [ -d "$PROJECT_DIR/.git" ] || git -C "$PROJECT_DIR" rev-parse --git-dir &>/dev/null; then
    echo "  $PASS Git repository"
else
    echo "  $FAIL Not a git repository"
    issues=$((issues + 1))
fi

sortie_dir="$PROJECT_DIR/.sortie"
if [ -d "$sortie_dir" ]; then
    echo "  $PASS .sortie/ directory exists"
else
    echo "  $WARN .sortie/ directory missing (will be created on first deploy)"
    warnings=$((warnings + 1))
fi

echo ""

# ── Summary ──────────────────────────────────────────────────────────
echo "═══ SUMMARY ═══"
if [ "$issues" -eq 0 ] && [ "$warnings" -eq 0 ]; then
    echo "  $PASS ALL STATIONS MANNED AND READY"
elif [ "$issues" -eq 0 ]; then
    echo "  $WARN $warnings warning(s) — operational but not optimal"
else
    echo "  $FAIL $issues issue(s), $warnings warning(s) — fix issues before deploying agents"
fi
echo ""

# Exit code: 0 = all good, 1 = has issues (warnings are OK)
exit "$( [ "$issues" -eq 0 ] && echo 0 || echo 1 )"
