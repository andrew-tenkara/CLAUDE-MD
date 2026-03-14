---
name: tower-queue
description: "Queue a sortie to Tower — ticket, file, or text → creates worktree, shows on TUI board. Deploy from TUI with D/R. Requires Tower to be running."
command: /tq
---

# Tower Queue — Fast Sortie Prep

Prep a worktree and add the pilot to the TUI board. The agent sits on deck as IDLE until you deploy from the TUI with **D** (open pane) or **R** (resume/launch).

**Requires Tower to be running.** If Tower isn't up, tell the user:
"Tower's not running. Launch with `/tower <project-dir>` first, or use `/sortie` for interactive mode."

## Preflight Check

```bash
[ -f /tmp/uss-tenkara/_prifly/window_id ]
```

If the file doesn't exist, Tower hasn't been launched. Tell the user:
"Tower's not running. Launch with `/tower <project-dir>` first, or use `/sortie` for interactive mode."

## Input Detection

`/tq <input>` accepts anything. Detect the type automatically:

| Input | Detection | Resolution |
|-------|-----------|------------|
| Ticket ID | Matches `[A-Z]{2,}-\d+` (e.g., `ENG-123`) | Fetch from Linear via `mcp__linear__get_issue` |
| Ticket URL | Contains `linear.app/` | Extract ID from URL path, then fetch from Linear |
| File path | Path exists on disk after `~` expansion | Read file, extract title (first `#` heading or filename) |
| Free-form text | Everything else | Use as directive text directly |

**Batch mode:** Comma-separated inputs are split and each processed independently.

```
/tq ENG-123
/tq ENG-123, ENG-456, ENG-789
/tq ~/specs/auth-redesign.md
/tq ~/specs/auth.md, ENG-200, fix the broken webhook handler
/tq Implement rate limiting on the /api/v2/export endpoint with a 100 req/min cap
```

## Ensure Linear Ticket

After resolving the input, check if there's a Linear ticket ID:

- **Yes** (ticket or URL) → Use it. Get `gitBranchName` from the Linear response.
- **No** (file or text) → Ask: **"No Linear ticket — create one? [y/n]"**
  - **Yes** → `mcp__linear__create_issue` with title + description. Use returned ID and `gitBranchName`.
  - **No** → Use `SPEC-<slugified-title>` as ID, `spec/<slugified-title>` as branch.

## Create Worktree (no launch)

Resolve the project directory and call the deploy script in **no-launch mode** — this creates the worktree and `.sortie/` protocol files but does NOT open an iTerm pane or start claude:

```bash
DEPLOY=~/.claude/skills/tower/scripts/deploy-agent.sh
PROJECT_DIR=$(git rev-parse --show-toplevel 2>/dev/null)

bash "$DEPLOY" <ticket-id> \
  --no-launch \
  --model sonnet \
  --branch "<git-branch-name>" \
  --directive "<description or file contents>" \
  --project-dir "$PROJECT_DIR"
```

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

For batch:

```
3 pilots on deck:
  ENG-123 — Add bulk pricing tier (sonnet)
  ENG-456 — Fix webhook race condition (sonnet)
  SPEC-auth-redesign — Auth redesign (sonnet)
Hit D in Tower to deploy any of them.
```

## No-args Mode

`/tq` with no arguments: pull all unstarted Linear tickets (Backlog + Todo, assigned to current user), present a quick summary list, and prep worktrees for all of them. Each appears on the TUI board as IDLE.
