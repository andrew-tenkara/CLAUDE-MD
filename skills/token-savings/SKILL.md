---
name: token-savings
description: Setup wizard and dashboard for RTK (command output filtering) and Headroom (session compression). Run on first use to install and wire both tools. Run again to see a live savings dashboard.
---

# /token-savings

Two-tool token savings system: RTK filters command output (60-90% savings), Headroom compresses API context (prefix caching + ML compression). Together they reduce token consumption by roughly 50%.

## Flow

1. Run `bash "$SKILL_DIR/scripts/preflight.sh"` and capture the output.
2. If output contains `PREFLIGHT:PASS` → run the dashboard (step 6).
3. If output contains `PREFLIGHT:NEEDS_SETUP` → walk through setup (steps 4-5).

## Setup (only when preflight fails)

### 4. Install missing tools

**RTK not installed:**
- Detect platform:
  - macOS: `brew install rtk-ai/tap/rtk`
  - Linux/WSL: `cargo install rtk-cli`
- After install, verify: `rtk --version`

**Headroom not installed:**
- All platforms: `pip install "headroom-ai[proxy]"`
- After install, verify: `headroom --version`

Ask the user before running install commands. Show the command and wait for confirmation.

### 5. Wire hooks

Check `~/.claude/settings.json` for missing hooks. If a hook is missing, offer to add it.

**RTK hook** (PreToolUse, matcher: "Bash"):
- Hook script location: `~/.claude/hooks/rtk-rewrite.sh`
- If the script doesn't exist, create it from RTK's docs: https://github.com/rtk-ai/rtk
- IMPORTANT: The hook script MUST include `export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"` near the top. Without this, the hook silently fails because Claude Code runs hooks in a minimal PATH that doesn't include Homebrew.

**Headroom hook** (SessionStart):
- Hook script location: `~/.claude/hooks/headroom-autostart.sh`
- The script should: check if Headroom is already running on :8787, if not start it in background, wait for health endpoint.

After wiring, re-run preflight to confirm: `bash "$SKILL_DIR/scripts/preflight.sh"`

### 6. Dashboard

Run: `bash "$SKILL_DIR/scripts/dashboard.sh"`

Shows a formatted one-shot report with RTK savings and Headroom savings.

After showing the dashboard, tell the user about the live TUI monitor:

```
For a live dashboard that auto-refreshes, run this in a separate terminal:

  python3 ~/.claude/skills/token-savings/scripts/tui.py

Split-pane view with RTK on the left, Headroom on the right.
Auto-refreshes every 5 seconds. Ctrl+C to exit.
Requires: pip install rich
```

## When invoked with no setup needed

Skip straight to step 6 (dashboard). After showing stats, always provide the TUI command and also mention:
- "Run `rtk discover` to find missed optimization opportunities"
- "Open http://localhost:8787/dashboard for the Headroom-only web view"

## Bonus: Headroom Learn

Headroom includes a `headroom learn` command that analyzes past Claude Code sessions for failure patterns (wrong paths, missing modules, stubborn retries) and generates preventive context. Run in dry-run mode first to see what it finds:

```bash
headroom learn --project /path/to/your/project
```

Add `--apply` to write recommendations to CLAUDE.md and MEMORY.md. Uses LiteLLM so it works with any model provider.

## Gotchas

### 1. `rtk init -g` overwrites your PATH fix

`rtk init -g` regenerates `~/.claude/hooks/rtk-rewrite.sh` from scratch, blowing away any manual PATH edits. The recommended fix is a **wrapper script** that survives overwrites:

Create `~/.claude/hooks/rtk-wrapper.sh`:
```bash
#!/usr/bin/env bash
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.cargo/bin:$PATH"
exec bash "$(dirname "$0")/rtk-rewrite.sh" "$@"
```

Then point the hook in `~/.claude/settings.json` at `rtk-wrapper.sh` instead of `rtk-rewrite.sh`. RTK owns its script, you own the wrapper. Neither steps on the other.

### 2. RTK hook silently fails without PATH fix (GitHub #685)

Claude Code runs hooks in a minimal PATH (`/usr/bin:/bin:/usr/sbin:/sbin`). Homebrew binaries (`/opt/homebrew/bin/`) and cargo installs (`~/.cargo/bin/`) are invisible. Without the PATH export at the top of `rtk-rewrite.sh`, `command -v rtk` fails silently, the hook exits 0, and every command passes through unrewritten. Zero errors. Zero warnings. You just silently lose all savings.

**Symptoms:** `rtk gain` shows commands accumulating when you run it manually, but `rtk discover` shows thousands of missed commands from Claude Code sessions. The hook looks correct but isn't firing.

**Fix:** The hook script MUST have this near the top, before any `command -v` checks:
```bash
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.cargo/bin:$PATH"
```

The preflight script checks for this. If the PATH line is missing, it flags it.

### 2. Hook changes require a new session

Claude Code loads hooks at session start. If you fix the PATH issue or add a new hook, existing sessions (including running pilot agents) won't pick it up. Only new sessions get the fix. Don't restart running agents just for this — they'll get it on their next deploy.

### 3. Headroom falls back silently

If Headroom fails to start (port conflict, crash, missing dependency), the SessionStart hook exits 0 and Claude Code connects directly to Anthropic. No error. Sessions work fine — you just don't get compression. Check `curl -sf http://localhost:8787/health` to verify it's actually running.

### 4. RTK name collision

Two different packages are named "rtk": **Rust Token Killer** (rtk-ai/rtk, this tool) and **Rust Type Kit** (reachingforthejack/rtk). If `rtk gain` returns "command not found" but `rtk --version` works, you have the wrong package. Uninstall and reinstall from `rtk-ai/tap/rtk`.
