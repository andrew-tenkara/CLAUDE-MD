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

---

## Split Mode — Coordinator + Sub-Agents

`--split` deploys a coordinator (parent worktree) plus N sub-agents. Sub-agents work LOCAL ONLY — no remote pushes, no PRs. The coordinator does all merging locally and opens the single PR.

### Syntax

```
/tq ENG-256 --split 3             # auto-name: ENG-256-A, ENG-256-B, ENG-256-C
/tq ENG-256 --split ENG-257,ENG-258,ENG-259  # existing tickets as sub-agents
/tq ENG-256 --split 3 --model opus          # coordinator model; subs use sonnet
```

Parse `--split` **before** input detection and strip it from the remainder. Auto-naming generates uppercase letters (A, B, C…) up to the requested count.

**Model defaults in split mode:**
- Coordinator: `opus` (orchestration is heavier reasoning)
- Sub-agents: `sonnet`

### Branch structure

```
sortie/ENG-256    ← coordinator's branch — only one that ever touches remote
ENG-256-A         ← local only, never pushed to remote
ENG-256-B         ← local only, never pushed to remote
ENG-256-C         ← local only, never pushed to remote
```

Sub-agent branch names are the sub-ID itself (no `sortie/` prefix). These are purely local working branches that the coordinator merges in when done.

### Deployment sequence

**Compute paths before any deploy calls:**

```
TOWER_SCRIPTS=~/.claude/skills/tower/scripts
STORAGE_DB="${TOWER_SCRIPTS}/storage-db.py"
PARENT_WORKTREE="${PROJECT_DIR}/.claude/worktrees/${PARENT_ID}"
PARENT_BRANCH="sortie/${PARENT_ID}"  # or gitBranchName from Linear
```

**Step 1: Deploy coordinator** with `--no-launch` and the coordinator directive below.

**Step 2: Parse `WORKTREE:<path>` from deploy output** to confirm the parent path.

**Step 3: Deploy each sub-agent** with:
```bash
bash "$DEPLOY" <sub-id> \
  --no-launch \
  --model sonnet \
  --branch "<SUB_ID>" \
  --directive "<sub-agent directive>" \
  --project-dir "$PARENT_WORKTREE"
```

Sub-agents use `--branch <SUB_ID>` (e.g. `ENG-256-A`) — a local branch only.
Sub-agents use `--project-dir <PARENT_WORKTREE>` so their `.sortie/` lives inside the parent and they share the same `storage.db`.

---

### Coordinator Directive Template

Construct this string (substitute all `<...>` tokens) and pass as `--directive`:

```
## Role: COORDINATOR

YOUR JOB:
- Plan the mission and break it into discrete subtasks
- Assign tasks to sub-agents via SQLite signals
- Poll for done/blocked signals and respond
- When all sub-agents signal done: merge their local branches into yours
- Resolve any merge conflicts
- Push to remote and open the single PR

NOT YOUR JOB:
- Writing implementation code directly (unless faster than waiting)

CRITICAL RULES:
- Sub-agents do NOT push to remote. You do.
- Sub-agents do NOT open PRs. You open the one PR.
- You merge their local branches: git merge <SUB_ID>

## Sub-Agents Under Your Command
<for each sub: "- <SUB_ID> (branch: <SUB_ID>): <title or 'awaiting task assignment'>">

## SQLite Signal Bus
Message DB: <PARENT_WORKTREE>/.sortie/storage.db (shared with all sub-agents)

Assign a task:
python3 '<STORAGE_DB>' send-message '<PARENT_WORKTREE>' - << 'MSG'
{"from_agent": "<PARENT_ID>", "to_agent": "<SUB_ID>", "type": "task", "payload": "Implement X in files Y and Z. Acceptance criteria: ..."}
MSG

Read replies from sub-agents:
python3 '<STORAGE_DB>' get-messages '<PARENT_WORKTREE>' '<PARENT_ID>'

Broadcast to all sub-agents (schema change, new constraint, etc):
python3 '<STORAGE_DB>' send-message '<PARENT_WORKTREE>' - << 'MSG'
{"from_agent": "<PARENT_ID>", "to_agent": null, "type": "info", "payload": "<message>"}
MSG

Unblock a sub-agent:
python3 '<STORAGE_DB>' send-message '<PARENT_WORKTREE>' - << 'MSG'
{"from_agent": "<PARENT_ID>", "to_agent": "<SUB_ID>", "type": "unblock", "payload": "<answer or clarification>"}
MSG

## Merging Sub-Agent Work
When sub-agents signal done, merge their local branches in sequence:
  git merge ENG-256-A   # or whatever the sub-ID is
  git merge ENG-256-B
  git merge ENG-256-C

After all merges pass:
  git push origin sortie/ENG-256   # or your branch name
  gh pr create ...

## Mission Directive
<full parent ticket content — title, description, acceptance criteria, comments>
```

---

### Sub-Agent Directive Template

Construct this string per sub-agent and pass as `--directive`:

```
## Role: PILOT (sub-agent — local only)

Your coordinator is: <PARENT_ID>
Your local branch: <SUB_ID>
Message DB (shared): <PARENT_WORKTREE>/.sortie/storage.db

CRITICAL RULES — READ BEFORE DOING ANYTHING:
- Do NOT push to remote. Your branch is local only.
- Do NOT open a PR. The coordinator opens the single PR.
- Commit locally. Signal done. Stop.

## On Startup — Read Your Task Assignment
python3 '<STORAGE_DB>' get-messages '<PARENT_WORKTREE>' '<SUB_ID>'

If no messages yet, wait and re-check — coordinator assigns tasks after it launches.

## Workflow
1. Read task from get-messages above
2. Do the work on branch <SUB_ID>
3. Commit locally (git commit — no push)
4. Signal done to coordinator (see below)
5. Stop — coordinator handles the merge and PR

## Signal Done (after local commit)
python3 '<STORAGE_DB>' send-message '<PARENT_WORKTREE>' - << 'MSG'
{"from_agent": "<SUB_ID>", "to_agent": "<PARENT_ID>", "type": "done",
 "payload": "Branch: <SUB_ID> ready to merge. Commits: <list>. Conflicts to watch: <any>. Summary: <2 sentences>"}
MSG

## Signal Blocked (need help or clarification)
python3 '<STORAGE_DB>' send-message '<PARENT_WORKTREE>' - << 'MSG'
{"from_agent": "<SUB_ID>", "to_agent": "<PARENT_ID>", "type": "blocked",
 "payload": "<what is blocking you and exactly what you need>"}
MSG

## Poll for Coordinator Replies
python3 '<STORAGE_DB>' get-messages '<PARENT_WORKTREE>' '<SUB_ID>'

## Signal Progress (optional, as you work)
python3 '<STORAGE_DB>' send-message '<PARENT_WORKTREE>' - << 'MSG'
{"from_agent": "<SUB_ID>", "to_agent": "<PARENT_ID>", "type": "progress",
 "payload": "<what you just completed, what's next>"}
MSG

## Mission Directive
<sub-ticket content if pre-existing Linear ticket; otherwise:>
Await task from coordinator <PARENT_ID>. Run get-messages above. Do not proceed until you have a task.
```

---

### Confirm (split mode)

```
ENG-256 (coordinator) + 3 sub-agents on deck:
  ENG-256    — <title> [coordinator] (opus)   branch: sortie/ENG-256
  ENG-256-A  — Sub-agent A (sonnet)           branch: ENG-256-A  [local only]
  ENG-256-B  — Sub-agent B (sonnet)           branch: ENG-256-B  [local only]
  ENG-256-C  — Sub-agent C (sonnet)           branch: ENG-256-C  [local only]
Message bus: .claude/worktrees/ENG-256/.sortie/storage.db
Deploy coordinator first (D), then sub-agents.
Sub-agents stay local. Coordinator merges and owns the PR.
```
