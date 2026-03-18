---
name: tower-queue
description: "Queue a sortie to Tower — ticket, file, or text → creates worktree, shows on TUI board. Deploy from TUI with D/R. Requires Tower to be running."
command: /tq
---

# Tower Queue — Fast Sortie Prep

Prep a worktree and add the pilot to the TUI board. The agent sits on deck as IDLE until you deploy from the TUI with **D** (open pane) or **R** (resume/launch).

**Requires Tower to be running.** Check with:

## Preflight Check

```bash
STATE_DIR="/tmp/uss-tenkara/_prifly"
HEARTBEAT_FILE="${STATE_DIR}/tower_heartbeat"

# Check heartbeat file exists AND is less than 30 seconds old
if [ -f "$HEARTBEAT_FILE" ]; then
  HEARTBEAT_AGE=$(( $(date +%s) - $(stat -f %m "$HEARTBEAT_FILE" 2>/dev/null || echo 0) ))
  if [ "$HEARTBEAT_AGE" -gt 30 ]; then
    # Heartbeat stale — Tower is dead
    TOWER_RUNNING=false
  else
    TOWER_RUNNING=true
  fi
else
  # Fallback: check legacy sentinel files (backwards compat)
  if [ -f "${STATE_DIR}/agents_window_id" ] || [ -f "${STATE_DIR}/tower_running" ]; then
    TOWER_RUNNING=true
  else
    TOWER_RUNNING=false
  fi
fi
```

If Tower is not running, tell the user:
"Tower's not running. Launch with `/tower <project-dir>` first, or use `/sortie` for interactive mode."

**IMPORTANT:** If Tower was just launched in this session, the heartbeat file exists and is fresh. Do not second-guess this check. If it passes, Tower is running.

## Input Detection

`/tq <input>` accepts anything. Detect the type automatically:

| Input | Detection | Resolution |
|-------|-----------|------------|
| Ticket ID | Matches `[A-Z]{2,}-\d+` (e.g., `ENG-123`) | Fetch from Linear via `mcp__linear__get_issue` |
| Ticket URL | Contains `linear.app/` | Extract ID from URL path, then fetch from Linear |
| File path | Path exists on disk (expand `~`, resolve relative paths via `Path.expanduser().resolve()`) | Read file, extract title (first `#` heading, YAML frontmatter `title:`, or filename) |
| Free-form text | Everything else | Use as directive text directly |

**Batch mode:** Comma-separated inputs are split and each processed independently. Deduplicate ticket IDs before processing (e.g., `ENG-123, ENG-123` → process once).

```
/tq ENG-123
/tq ENG-123, ENG-456, ENG-789
/tq ~/specs/auth-redesign.md
/tq ~/specs/auth.md, ENG-200, fix the broken webhook handler
/tq Implement rate limiting on the /api/v2/export endpoint with a 100 req/min cap
```

## Model Selection

Default model is `sonnet`. The user can specify a model with `--model` or `-m` anywhere in the input:

```
/tq ENG-123 --model opus
/tq -m haiku fix the typo in the README
/tq ENG-123, ENG-456 --model opus
```

Parse model flag FIRST, strip it from the input, then detect input type on the remainder. Valid models: `sonnet`, `opus`, `haiku`. In batch mode, the model applies to all items unless overridden per-item.

## Ensure Linear Ticket

After resolving the input, check if there's a Linear ticket ID:

- **Yes** (ticket or URL) → Use it. Get `gitBranchName` from the Linear response.
- **No** (file or text) → Skip Linear ticket creation. Use `SPEC-<slugified-title>` as ID, `spec/<slugified-title>` as branch. Do NOT prompt the user — just prep the worktree.

If the user explicitly wants a Linear ticket for a text/file directive, they can create one in Linear first and use `/tq <ticket-id>`.

## Create Worktree (no launch)

Resolve the project directory and call the deploy script in **no-launch mode** — this creates the worktree and `.sortie/` protocol files but does NOT open an iTerm pane or start claude:

```bash
DEPLOY=~/.claude/skills/tower/scripts/deploy-agent.sh
PROJECT_DIR=$(git rev-parse --show-toplevel 2>/dev/null)

bash "$DEPLOY" <ticket-id> \
  --no-launch \
  --model <model> \
  --branch "<git-branch-name>" \
  --directive "<description or file contents>" \
  --project-dir "$PROJECT_DIR"
```

**Check the exit code.** If deploy-agent.sh exits non-zero, report the failure clearly and do NOT tell the user the pilot is on deck.

The TUI's pilot roster scan picks up the new worktree automatically (next 3s cycle). The pilot appears on the board as **IDLE** — on deck, ready for deployment.

## Confirm

After prepping, tell the user:

```
ENG-123 on deck — "Add bulk pricing tier"
  Worktree: .claude/worktrees/ENG-123
  Branch: eng/eng-123-add-bulk-pricing-tier
  Model: sonnet
Hit D in Tower to deploy, or R to resume.
```

For batch — always report successes AND failures:

```
3 pilots on deck:
  ENG-123 — Add bulk pricing tier (sonnet)
  ENG-456 — Fix webhook race condition (opus)
  SPEC-auth-redesign — Auth redesign (sonnet)

1 failed:
  ENG-789 — branch already exists (worktree at .claude/worktrees/ENG-789)

Hit D in Tower to deploy any of them.
```

## No-args Mode

`/tq` with no arguments: pull all unstarted Linear tickets (Backlog + Todo, assigned to current user), present a quick summary list, and prep worktrees for all of them. Each appears on the TUI board as IDLE.
