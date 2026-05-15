---
name: token-savings
description: Setup wizard and dashboard for RTK (Bash output filtering), Pith (Read/Grep tool-result compression), and Headroom (API-side session compression). Run on first use to install and wire all three. Run again to see a live savings dashboard.
---

# /token-savings

Three-tool token savings stack. Each operates at a different lifecycle event, so they compose cleanly:

- **RTK** — `PreToolUse` hook on `Bash`. Filters verbose CLI output before it enters context (60-90% on shell-heavy turns).
- **Pith** — `PostToolUse` hook on all tools. Compresses Read/Grep/Glob results into skeletons + summaries before they hit context (~88% on file reads, ~91% on bash/build, caps grep at 25 matches). Also runs a token meter in the statusline and auto-compacts at 70%.
- **Headroom** — `ANTHROPIC_BASE_URL` proxy. Prefix-cache + ML compression on the wire to api.anthropic.com.

Stacked, expect roughly 60-70% reduction. The compounding win is that Pith shrinks the transcript Headroom has to compress, and produces byte-stable skeletons that warm Headroom's prefix cache turn over turn.

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

**Pith not installed:**
- All platforms: `bash <(curl -s https://raw.githubusercontent.com/abhisekjha/pith/main/install.sh)`
- Self-clones to `~/.local/share/pith`, copies hooks to `~/.claude/hooks/pith/`, patches `settings.json`, registers `/pith`, `/budget`, `/focus`, `/pith-graph` slash commands, and sets a `statusLine` token meter.
- ⚠️ **Back up `~/.claude/settings.json` first** — the installer overwrites `statusLine` unconditionally. If the user has a custom statusline, capture it before running and merge back after.
- After install, verify: `ls ~/.claude/hooks/pith/` shows `session-start.js`, `post-tool-use.js`, `prompt-submit.js`, `stop.js`.

Ask the user before running install commands. Show the command and wait for confirmation. The Pith installer is curl-bash — show the URL and offer to inspect with `curl -sL <url> | less` first if they want.

### 5. Wire hooks

Check `~/.claude/settings.json` for missing hooks. If a hook is missing, offer to add it.

**RTK hook** (PreToolUse, matcher: "Bash"):
- Hook script location: `~/.claude/hooks/rtk-rewrite.sh`
- If the script doesn't exist, create it from RTK's docs: https://github.com/rtk-ai/rtk
- IMPORTANT: The hook script MUST include `export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"` near the top. Without this, the hook silently fails because Claude Code runs hooks in a minimal PATH that doesn't include Homebrew.

**Headroom hook** (SessionStart):
- Hook script location: `~/.claude/hooks/headroom-autostart.sh`
- The script should: check if Headroom is already running on :8787, if not start it in background, wait for health endpoint.
- **CRITICAL**: The hook MUST set `ANTHROPIC_BASE_URL` via `$CLAUDE_ENV_FILE`. Without this, the proxy runs but receives zero traffic — Claude Code sends all API requests directly to Anthropic, completely bypassing compression. This is the #1 silent failure mode.
  ```bash
  # In the hook script, after confirming proxy is healthy:
  if [[ -n "${CLAUDE_ENV_FILE:-}" ]]; then
    echo "export ANTHROPIC_BASE_URL=http://localhost:8787" >> "$CLAUDE_ENV_FILE"
  fi
  ```
- The preflight checks both that the hook exists AND that it writes `ANTHROPIC_BASE_URL`.

**Pith hooks** (`SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop`):
- The Pith installer wires all four automatically. No manual `settings.json` editing needed.
- After install, verify with: `jq '.hooks | to_entries | map({k:.key, c:[.value[].hooks[]?.command]})' ~/.claude/settings.json` — you should see `pith/*.js` entries on all four events alongside the existing RTK (PreToolUse) and Headroom (SessionStart) entries.
- `SessionStart` will have two entries after install (Headroom autostart + Pith init). Both fire — additive, no conflict.
- The PostToolUse hook uses `matcher: ""` (all tools). Bash output replacement via PostToolUse has [an open bug on CC ≥v2.1.121](https://github.com/anthropics/claude-code/issues/54196) — Pith's Bash compression may silently no-op on macOS. That's fine: RTK already handles Bash. Read/Grep/Glob compression works as expected.

After wiring, re-run preflight to confirm: `bash "$SKILL_DIR/scripts/preflight.sh"`

**Note:** `preflight.sh` currently only checks RTK and Headroom. It will not flag a missing Pith install. If you want preflight to enforce all three, extend the script to check for `~/.claude/hooks/pith/post-tool-use.js`.

### 6. Dashboard

Run: `bash "$SKILL_DIR/scripts/dashboard.sh"`

Shows a formatted one-shot report with RTK savings and Headroom savings.

After showing the dashboard, tell the user about the live TUI monitor. First ensure the `rich` package is installed, then launch:

```
The TUI dashboard requires the `rich` Python package. Install it before launching:

  # Try pip first (most common)
  pip install rich

  # If pip is not available or restricted, use pipx
  pipx install rich          # installs into isolated env
  # OR use pip with --user flag
  python3 -m pip install --user rich

  # Verify it's installed
  python3 -c "import rich; print(rich.__version__)"

Then run in a separate terminal:

  python3 ~/.claude/skills/token-savings/scripts/tui.py

Stacked layout with RTK, Headroom, and Recent Requests panels.
Auto-refreshes every 2 seconds. Ctrl+C to exit.
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

`rtk init -g` regenerates `~/.claude/hooks/rtk-rewrite.sh` from scratch, blowing away any manual PATH edits. RTK also integrity-checks this file with SHA-256 and refuses to run if it's been modified. The recommended fix is a **wrapper script** that survives overwrites and strips the `permissionDecision` field (see gotcha #6):

Create `~/.claude/hooks/rtk-wrapper.sh`:
```bash
#!/usr/bin/env bash
# NOT managed by rtk — survives rtk init -g regenerations.
# Fixes: PATH for Homebrew/cargo, permissionDecision strip, provenance xattr bypass.

export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.cargo/bin:$PATH"

OUTPUT=$(bash "$(dirname "$0")/rtk-rewrite.sh" "$@")
EXIT_CODE=$?

if [ -z "$OUTPUT" ] || [ $EXIT_CODE -ne 0 ]; then
  exit $EXIT_CODE
fi

# Strip permissionDecision — see gotcha #6
echo "$OUTPUT" | jq '
  if .hookSpecificOutput then
    .hookSpecificOutput |= del(.permissionDecision, .permissionDecisionReason)
  else
    .
  end
' 2>/dev/null || echo "$OUTPUT"

exit $EXIT_CODE
```

Point the hook in `~/.claude/settings.json` at `bash ~/.claude/hooks/rtk-wrapper.sh` (note the `bash` prefix — see gotcha #6). RTK owns its script, you own the wrapper. Neither steps on the other.

### 2. RTK hook silently fails without PATH fix (GitHub #685)

Claude Code runs hooks in a minimal PATH (`/usr/bin:/bin:/usr/sbin:/sbin`). Homebrew binaries (`/opt/homebrew/bin/`) and cargo installs (`~/.cargo/bin/`) are invisible. Without the PATH export at the top of `rtk-rewrite.sh`, `command -v rtk` fails silently, the hook exits 0, and every command passes through unrewritten. Zero errors. Zero warnings. You just silently lose all savings.

**Symptoms:** `rtk gain` shows commands accumulating when you run it manually, but `rtk discover` shows thousands of missed commands from Claude Code sessions. The hook looks correct but isn't firing.

**Fix:** The hook script MUST have this near the top, before any `command -v` checks:
```bash
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.cargo/bin:$PATH"
```

The preflight script checks for this. If the PATH line is missing, it flags it.

### 3. Hook changes require a new session

Claude Code loads hooks at session start. If you fix the PATH issue or add a new hook, existing sessions (including running pilot agents) won't pick it up. Only new sessions get the fix. Don't restart running agents just for this — they'll get it on their next deploy.

### 4. Headroom falls back silently

If Headroom fails to start (port conflict, crash, missing dependency), the SessionStart hook exits 0 and Claude Code connects directly to Anthropic. No error. Sessions work fine — you just don't get compression. Check `curl -sf http://localhost:8787/health` to verify it's actually running.

**ANTHROPIC_BASE_URL not set (silent zero-traffic failure)**: The proxy can be healthy on :8787 but receiving zero traffic if `ANTHROPIC_BASE_URL` was never set. The hook must write `export ANTHROPIC_BASE_URL=http://localhost:8787` to `$CLAUDE_ENV_FILE`. Verify with `echo $ANTHROPIC_BASE_URL` in a Claude Code Bash call — if it's empty or points to `api.anthropic.com`, the proxy is being bypassed. The preflight script checks for this.

### 5. RTK name collision

Two different packages are named "rtk": **Rust Token Killer** (rtk-ai/rtk, this tool) and **Rust Type Kit** (reachingforthejack/rtk). If `rtk gain` returns "command not found" but `rtk --version` works, you have the wrong package. Uninstall and reinstall from `rtk-ai/tap/rtk`.

### 6. `permissionDecision: "allow"` silently kills savings

RTK's hook emits `"permissionDecision": "allow"` alongside `updatedInput` in its JSON output. This has two consequences:

1. **Security bypass** ([rtk-ai/rtk#260](https://github.com/rtk-ai/rtk/issues/260), [#1155](https://github.com/rtk-ai/rtk/issues/1155)): every rewritten command auto-approves without hitting Claude Code's permission system.
2. **Silent savings loss** ([rtk-ai/rtk#893](https://github.com/rtk-ai/rtk/issues/893)): when `skipDangerousModePermissionPrompt` or `skipAutoPermissionPrompt` is true in settings.json, Claude Code can skip the permission handling path entirely. When it does, `updatedInput` bundled with `permissionDecision` gets discarded. The original uncompressed command runs instead. No errors. Full token output hits the context window.

**Symptoms:** RTK gain shows high savings, but individual sessions intermittently show uncompressed output for commands RTK should be filtering. More likely if you use `--dangerously-skip-permissions` or have `skipAutoPermissionPrompt: true`.

**Fix:** The wrapper script in gotcha #1 strips `permissionDecision` and `permissionDecisionReason` from the hook output using jq, leaving only `updatedInput` and `hookEventName`. Claude Code processes the rewrite through its normal flow without the auto-allow bypass.

RTK's own integrity check prevents editing `rtk-rewrite.sh` directly — the fix must live in the wrapper.

### 7. macOS Sequoia `com.apple.provenance` causes intermittent hook failures

macOS Sequoia (and some Ventura builds) tags files with a `com.apple.provenance` extended attribute. When Claude Code invokes the hook via `/bin/sh`, this attribute can intermittently cause "Permission denied" errors — even when Unix permissions are 755.

**Symptoms:** Sporadic errors in Claude Code output that look like this:

```
⎿  PreToolUse:Bash hook error
⎿  Failed with non-blocking status code: /bin/sh: /Users/you/.claude/hooks/rtk-wrapper.sh: Permission denied
```

Non-blocking (exit code != 2), so the command runs uncompressed. The attribute cannot be removed with `xattr -d` — Sequoia reapplies it at the directory level.

**Note:** If you're seeing these errors, you're likely also being hit by gotcha #6 (`permissionDecision` auto-allow) at the same time. The provenance errors are visible — they show up in Claude's output. But the `permissionDecision` leak is silent — commands run uncompressed with no error. Both cause lost savings independently. The provenance errors are the canary: if you're seeing them, check whether the `permissionDecision` strip is also missing from your wrapper.

**Fix:** Prefix the hook command in `settings.json` with `bash` so the shell reads the file as data instead of exec'ing it:

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Bash",
      "hooks": [{
        "type": "command",
        "command": "bash /Users/you/.claude/hooks/rtk-wrapper.sh"
      }]
    }]
  }
}
```

This bypasses the exec permission check entirely. Apply the same `bash` prefix to any other hooks (e.g., `headroom-autostart.sh`).
