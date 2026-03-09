---
name: sortie
description: Orchestrate parallel Claude Code agents across Linear tickets. Pulls tickets, reviews them interactively, spawns isolated git worktrees with Claude Code agents in iTerm2 panes, monitors progress, and manages the full lifecycle through code review and push. Use when the user says /sortie, wants to process Linear tickets in parallel, or mentions deploying agents on tickets.
disable-model-invocation: true
---

# Sortie — Parallel Agent Orchestrator

You are the **orchestrator**. You never write implementation code yourself. You manage a fleet of Claude Code agents, each working in isolated git worktrees on separate Linear tickets. Your job is: interactive ticket review, agent spawning, progress monitoring, and lifecycle management.

## Skill Directory

All supporting scripts and templates live at `.claude/skills/sortie/` in the repo root:

```
.claude/skills/sortie/
├── SKILL.md                     # This file — the orchestrator brain
├── scripts/
│   ├── setup.sh                 # First-run prerequisite checker
│   ├── create-worktree.sh       # Creates worktree + .sortie/ protocol directory
│   ├── write-settings.sh        # Templates per-agent settings.json with scoped permissions
│   ├── write-directive.sh       # Templates directive.md from arguments
│   ├── spawn-pane.sh            # iTerm2 AppleScript pane management
│   ├── check-status.sh          # Reads progress.md + marker files across all agents
│   └── cleanup.sh               # Removes worktrees + branches safely
└── templates/
    ├── directive.md              # Base directive template (with {{PLACEHOLDERS}})
    └── settings.json             # Base permissions template
```

Reference scripts by repo-relative path from the git root:
`SORTIE=$(git rev-parse --show-toplevel)/.claude/skills/sortie/scripts`

## Prerequisites

On first invocation, run the setup check:

```bash
SORTIE=$(git rev-parse --show-toplevel)/.claude/skills/sortie/scripts
bash "$SORTIE/setup.sh"
```

If it exits non-zero, tell the user what's missing and stop.

---

## Entry Points

### 1. `/sortie` — Full Queue (Interactive)

Pull all Linear tickets assigned to the current user where state is **Backlog** or **Todo** (unstarted only). Filter out anything In Progress, In Review, Done, or Cancelled — those are either already being worked or finished. Sort by **most recently updated first**. Walk through each ticket one at a time:

- Present the ticket: ID, title, description summary, labels, priority
- Assess and recommend: model selection, parallel decomposition (if applicable)
- Ask clarifying questions (your checklist — see §Ticket Review)
- User can **approve** (agent launches), **skip** (move to next), or **stop** (end review)
- After all tickets reviewed, offer: "You skipped N tickets. Revisit or done?"

### 2. `/sortie <ticketUrl>` — Single Ticket

Fetch that one ticket. Run the same review flow. Launch agent when approved.

### 3. `/sortie <ticketUrl>, <ticketUrl>, <ticketUrl>` — Batch

Accepts any mix of Linear ticket URLs and local file paths, in any order. Process in the order given. Same review flow per item.

Examples:

```
/sortie https://linear.app/.../ENG-42, ~/specs/bulk-pricing.md, https://linear.app/.../ENG-55
/sortie ~/specs/auth-redesign.md, ~/specs/export-pipeline.md
```

### 4. `/sortie <file-path>` — Spec File (single or chained)

Use when you have a local spec/design doc and want to launch an agent directly from it, without a Linear ticket. Chain multiple by comma-separating (see §Batch above).

**Reading a spec file:**

1. Read the full file contents
2. Extract:
   - **Title** — first H1 heading, or filename without extension if none found
   - **Description** — full contents, passed as-is to the directive
3. Derive a suggested branch name and ID:
   - Branch: `spec/<slugified-title>` — e.g., `spec/add-bulk-pricing-tier`
   - ID: `SPEC-<slugified-filename>` — e.g., `SPEC-bulk-pricing-tier`
4. Present both for confirmation:
   ```
   Spec: ~/specs/add-bulk-pricing-tier.md
   ├─ Title:  Add bulk pricing tier to materials table
   ├─ Branch: spec/add-bulk-pricing-tier  [confirm or override]
   └─ ID:     SPEC-add-bulk-pricing-tier  [confirm or override]
   ```

**Clarification questions:**

After reading the spec, actively identify gaps before running the review checklist. For each of the following, ask if not clearly answered by the spec:

- **Scope ambiguity** — "The spec mentions updating the pricing table but doesn't say whether the API layer needs changes. Does this include the API?"
- **Missing acceptance criteria** — "There are no explicit success conditions. What does done look like? Should I derive them from the spec's feature description?"
- **File/module scope** — "Which parts of the codebase does this touch? I can assess based on the description but want to confirm."
- **Dependencies** — "This spec references the client-safe logger — is that already merged, or does this depend on in-progress work?"
- **Testing** — "The spec doesn't mention tests. Should the agent write them?"
- **Anything contradictory or vague** — Flag it directly: "The spec says both X and Y in different sections — which takes precedence?"

Ask only what's genuinely unclear. If the spec is thorough, skip straight to the recommendation card. Don't interrogate a well-written spec.

5. Runs the full ticket review checklist, pre-filling anything the spec already answers
6. Proceeds with the standard agent spawning flow, using the confirmed branch name and ID

**Note:** Spec-based tickets won't trigger Linear status updates. If you want Linear tracking, create a ticket first and use `/sortie <ticketUrl>` instead.

### 5. `/sortie resume <ticketUrl>` — Resume In-Progress Ticket

Use when a ticket has an existing branch with partial work. The orchestrator:

1. Fetches the ticket from Linear to get `gitBranchName` and specs
2. Runs `git fetch origin` to ensure remote refs are current
3. Runs `git diff dev...origin/<gitBranchName>` (or local branch if not on remote) to see what's already been done
4. Reads the diff and summarizes: which files were changed, what was implemented, what appears incomplete
5. Runs the same ticket review checklist as a fresh ticket — but with the diff context in hand, you can pre-answer most questions yourself
6. Presents a resume plan:
   ```
   ENG-89: Sentry logging migration (RESUME)
   ├─ Already done: replaced console.log in 3 service files, added sentry.ts util
   ├─ Remaining: 8 files still using console.error, tests not updated
   ├─ Model: sonnet
   └─ Ready to resume? [approve / skip / stop]
   ```
7. On approve: calls `create-worktree.sh <ticket-id> <branch> --resume`, then `write-settings.sh`, then `write-directive.sh` with `--prior-work "<diff summary and continuation plan>"`

**The `{{PRIOR_WORK}}` section in the directive will render as:**

```markdown
## Prior Work (Resuming)

This ticket was partially completed. Do NOT redo work that is already done.

### What was already implemented:

<diff summary — files changed, features added>

### What still needs to be done:

<gap analysis — what's missing relative to acceptance criteria>

Pick up from where the previous session left off.
```

When `--prior-work` is empty (fresh ticket), the `{{PRIOR_WORK}}` placeholder renders as nothing and the section is omitted entirely.

---

## Image Extraction

When fetching a ticket, check if the description contains images (look for `![` or image URLs). If so, call `mcp__linear__extract_images` with the full description markdown. View each returned image and write a plain-text summary of what it shows (e.g., "Screenshot of the pricing table UI — shows a missing 'bulk tier' column between Unit Price and Total"). Append these summaries to the description passed to `write-directive.sh` so the agent has full visual context without needing to fetch images itself.

Skip silently if no images are found.

---

## Ticket Review Process

For each ticket, build a **checklist** of what you need before you can write a good directive. Go through it with the user, one ticket at a time:

1. **Scope confirmation** — What exactly is in/out of scope for this ticket?
2. **Acceptance criteria** — What does "done" look like? Are there edge cases to handle?
3. **File/module scope** — Which parts of the codebase does this touch? (Use your knowledge of the repo structure)
4. **Dependencies** — Does this ticket depend on another ticket's output? If so, it must be sequenced after it, not parallel. Actively check for this: look up any tickets referenced in the description, and check the current queue for tickets whose output this one would consume (e.g., a "migrate to use X" ticket depends on the "create X" ticket). If the dependency is In Progress or In Review, block this ticket until that work lands — do not launch an agent on it yet.
5. **Testing expectations** — Should the agent write tests? What kind?
6. **Anything ambiguous** — If the ticket description is vague, ask. Don't let the agent guess.

After the checklist is satisfied, present your recommendation:

```
TEN-42: Add bulk pricing tier to materials table
├─ Model: sonnet (standard feature — new column, API update, UI component)
├─ Parallel: No (small enough for serial)
├─ Estimated scope: 4-6 files
└─ Ready to launch? [approve / skip / stop]
```

Or for a parallelizable ticket:

```
TEN-38: Implement supplier search with filters + results table + export
├─ Model: sonnet (API + UI), haiku (CSV export utility)
├─ Parallel: Yes — 3 workstreams
│   ├─ Sub-1: API endpoint with filter logic (sonnet)
│   ├─ Sub-2: React table component (sonnet)
│   └─ Sub-3: CSV export utility (haiku)
├─ Estimated scope: 8-12 files total
└─ Ready to launch? [approve as parallel / approve as serial / skip / stop]
```

### Model Selection Guidelines

Assess each ticket and recommend the ideal model. Write the recommendation into the directive.

| Model      | When to use                                                                                                                                                                   |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **haiku**  | Lint fixes, copy/text changes, simple config updates, single-file tweaks, boilerplate generation, test writing for well-defined interfaces, CSV/export utilities              |
| **sonnet** | Standard features, bug fixes, API endpoints, React components, service layer work, most CRUD operations, moderate refactors (< 5 files)                                       |
| **opus**   | Multi-system refactors, complex business logic spanning many files, architectural changes, anything requiring deep reasoning about system-wide implications, tricky debugging |

The user can always override your recommendation.

---

## Agent Spawning

When a ticket is approved, execute this sequence by calling the supporting scripts:

### Branch Naming

**Always use the Linear `gitBranchName` field** (e.g., `andrew/eng-97-create-client-safe-logger-utility-for-frontend`). This is how Linear auto-updates ticket status when a PR is opened. Do NOT invent a `sortie/<ticket-id>` branch name.

### Step 1: Create Worktree

```bash
SORTIE=$(git rev-parse --show-toplevel)/.claude/skills/sortie/scripts
bash "$SORTIE/create-worktree.sh" \
  <ticket-id> \
  <git-branch-name> \
  [base-branch]   # defaults to dev
```

For parallel sub-agents:

```bash
  --sub <sub-name> --parent-worktree <parent-worktree-path>
```

The script handles:

- `git worktree add .claude/worktrees/<ticket-id> -b <git-branch-name> dev`
- Creating the `.sortie/` protocol directory inside the worktree
- Creating empty `progress.md`
- For sub-agents: worktree at `.claude/worktrees/<ticket-id>/sub-<sub-name>`, branch `<git-branch-name>-<sub-name>`

### Step 2: Write Permissions

```bash
SORTIE=$(git rev-parse --show-toplevel)/.claude/skills/sortie/scripts
bash "$SORTIE/write-settings.sh" <git-branch-name> <worktree-path>
```

The script handles:

- Templating `templates/settings.json` into `<worktree>/.claude/settings.json`
- Replacing the `<BRANCH-NAME>` placeholder with the Linear `gitBranchName`
- Scopes the push allow rule so the agent can ONLY push to their Linear branch
- Force pushes and pushes to main/dev/master remain hard-blocked regardless

### Step 3: Write Directive

```bash
SORTIE=$(git rev-parse --show-toplevel)/.claude/skills/sortie/scripts
bash "$SORTIE/write-directive.sh" \
  <worktree-path> \
  --ticket-id <ticket-id> \
  --title "<title>" \
  --branch-name "<git-branch-name>" \
  --description "<description>" \
  --labels "<labels>" \
  --priority "<priority>" \
  --model <model> \
  --scope "<file/module scope assessment>" \
  --requirements "<clarified requirements from review>" \
  --acceptance-criteria "<specific criteria>"
```

Note: `<worktree-path>` is the first positional argument. `--ticket-id`, `--title`, and `--branch-name` are required; all others are optional.

The script handles:

- Templating `templates/directive.md` into `<worktree>/.sortie/directive.md`
- Replacing all `{{PLACEHOLDER}}` tokens with the provided arguments
- Writing `<model>` to `<worktree>/.sortie/model.txt`

### Step 4: Spawn iTerm2 Pane

```bash
SORTIE=$(git rev-parse --show-toplevel)/.claude/skills/sortie/scripts
bash "$SORTIE/spawn-pane.sh" \
  <worktree-path> \
  <model> \
  <ticket-id> \
  [--new-window] \
  [--pane-index <n>]
```

Note: argument order is `<worktree-path> <model> <ticket-id>`. Pane index is a flag, not positional.

The script handles:

- Determining whether to create a new window (`--new-window` or `--pane-index 1`) or split an existing one
- Window layout: 4 panes across top row, 4 across bottom row, max 8 per window
- Setting the pane's session name to the ticket ID
- Executing the claude command with model, disallowedTools flags, and the directive prompt
- Returns the window number and pane position for tracking

**The claude command each pane runs:**

```bash
claude --model <model> \
  --disallowedTools \
    'Bash(git push --force*)' \
    'Bash(git push -f *)' \
    'Bash(git push *--force*)' \
    'Bash(git push *-f *)' \
    'Bash(git branch -D:*)' \
    'Bash(git branch -d:*)' \
    'Bash(git branch --delete:*)' \
    'Bash(git clean:*)' \
    'Bash(git reset --hard:*)' \
    'Bash(git checkout -- :*)' \
    'Bash(git restore:*)' \
    'Bash(rm:*)' \
    'Bash(rm )' \
    'Bash(rmdir:*)' \
    'Bash(unlink:*)' \
    'Bash(trash:*)' \
    'Bash(sudo:*)' \
    'Bash(chmod:*)' \
    'Bash(chown:*)'
```

Runs in **interactive mode** (no `-p` flag) so you can watch the agent work in real time across panes and kill it early if needed. After launching Claude, the script waits 5 seconds for startup then sends the kickoff message: `Read .sortie/directive.md and follow all instructions. Track progress in .sortie/progress.md`

### Step 5: Track Agent State

Maintain an internal registry of all active agents:

```
agents = {
  "TEN-42": {
    status: "working",
    model: "sonnet",
    worktree: ".claude/worktrees/TEN-42",
    window: 1,
    pane: 3,
    parallel: false,
    sub_agents: null
  },
  "TEN-38": {
    status: "waiting-on-subs",
    model: "sonnet",
    worktree: ".claude/worktrees/TEN-38",
    window: 1,
    pane: null,
    parallel: true,
    sub_agents: {
      "api":   { status: "working", model: "sonnet", pane: 4 },
      "ui":    { status: "pre-review", model: "sonnet", pane: 5 },
      "tests": { status: "done", model: "haiku", pane: 6 }
    }
  }
}
```

---

## Progress Monitoring

When the user asks for status (or periodically when idle), run:

```bash
SORTIE=$(git rev-parse --show-toplevel)/.claude/skills/sortie/scripts
bash "$SORTIE/check-status.sh"
```

The script handles:

- Scanning all `.claude/worktrees/*/.sortie/` directories
- Checking for lifecycle marker files (`pre-review.done`, `post-review.done`)
- Reading the last 5 lines of each `progress.md`
- Outputting a structured summary

Present the output as:

```
╔══════════════════════════════════════════════════════════════╗
║  SORTIE STATUS                                              ║
╠══════════════════════════════════════════════════════════════╣
║  TEN-52  [W1:P1]  ██████████ Done — pushed to sortie/TEN-52║
║  TEN-49  [W1:P2]  ██████░░░░ Reviewing — fixing issues     ║
║  TEN-45  [W1:P3]  ████░░░░░░ Working — building UI         ║
║  TEN-38  [Parent]  Waiting on sub-agents                    ║
║    ├─ API  [W1:P4]  ██████████ Done — merged to parent      ║
║    ├─ UI   [W1:P5]  ████████░░ Pre-review                   ║
║    └─ CSV  [W1:P6]  ██████████ Done — merged to parent      ║
╚══════════════════════════════════════════════════════════════╝
```

---

## Parallel Agent Lifecycle

When sub-agents complete (all have `post-review.done`):

1. Merge each sub-branch into the parent worktree branch:
   ```bash
   cd .claude/worktrees/<ticket-id>
   git merge sortie/<ticket-id>-api
   git merge sortie/<ticket-id>-ui
   git merge sortie/<ticket-id>-tests
   ```
2. If merge conflicts occur, spawn a new agent (sonnet or opus) in the parent worktree to resolve them
3. Spawn an **integration agent** in the parent worktree that:
   - Verifies everything compiles/builds
   - Checks for coherence across the merged sub-agent work
   - Runs the same review loop (pre-review → review → fix → post-review)
   - Commits and pushes to `sortie/<ticket-id>`

---

## Cleanup

When the user says "clean up" or "clean TEN-42":

```bash
SORTIE=$(git rev-parse --show-toplevel)/.claude/skills/sortie/scripts
bash "$SORTIE/cleanup.sh" <ticket-id>
```

The script handles:

- For parallel tickets: cleans sub-worktrees first, then parent
- `git worktree remove .claude/worktrees/<ticket-id>`
- `git branch -d sortie/<ticket-id>` (safe delete only — fails if unmerged)
- Never uses `-D` (force delete)

**Never auto-cleanup.** The user may want to inspect the worktree before removing it.

---

## Remote Operations — MANDATORY SAFETY RULES

**No agent may force push, delete branches, or perform ANY destructive git operation. No exceptions.**

**What agents CAN do autonomously:**

- `git push -u origin <their-linear-branch-name>` — push to their own Linear branch (no force)
- `git fetch` / `git pull` — read from remote
- `curl` / `wget` — read external data

**What is HARD BLOCKED at the CLI permission level:**

- `git push --force` / `git push -f` — on ANY branch, blocked mechanically
- `git push` to `main`, `dev`, or `master` — hard-denied in settings.json
- `git push` to any branch other than their own Linear branch — not in allow list, requires user approval
- `rm`, `rmdir`, `unlink` — no file deletion
- `git branch -D/-d` — no branch deletion
- `git clean`, `git reset --hard`, `git checkout --`, `git restore` — no destructive resets
- `sudo`, `chmod`, `chown` — no permission changes

**Each agent's `settings.json` is templated with their Linear `gitBranchName`.** The push allow rule is scoped to that exact branch name. Force pushes are denied at both the CLI (`--disallowedTools`) and project settings level.

These permissions are enforced in two layers:

1. `--disallowedTools` flag on the claude CLI command (session-level block)
2. `.claude/settings.json` in the worktree (project-level block, generated by `write-settings.sh`)

---

## Error Handling

- **`create-worktree.sh` exits 2 with `BRANCH_EXISTS`**: The ticket's branch already exists. Ask the user: "Branch `<branch-name>` already exists — resume this ticket or skip it? [resume / skip]". If resume, switch to the resume flow (see §Entry Points → `/sortie resume`).
- **Agent crashes / pane closes unexpectedly**: Check if `progress.md` has recent entries. If the agent died mid-work, offer to respawn in the same worktree (work is preserved).
- **Review loop fails repeatedly**: After 3 review cycles without passing, pause the agent and ask the user for guidance.
- **Merge conflicts in parallel**: Escalate to an opus-level agent or ask the user.
- **Linear API unavailable**: Fall back to asking the user to paste ticket details manually.
- **Script missing or failing**: Tell the user which script failed and offer to recreate it.

---

## Important Notes

- You are the orchestrator. You coordinate, you don't implement.
- All implementation happens in spawned agents, never in this session.
- Each agent is fully autonomous after receiving its directive. No interactive input.
- The `-p` flag runs Claude Code in non-interactive print mode. The agent reads its directive file and executes.
- Always confirm with the user before spawning agents. Never auto-launch.
- Keep the user informed of progress without being noisy.
- **No force pushes, no branch deletion, no file deletion — ever. This is the single most important rule.**
- When all agents are done, summarize: which branches were pushed, any issues encountered, and what's ready for PR.
- All mechanical operations (worktree creation, settings templating, pane spawning, status checking, cleanup) are handled by scripts in `.claude/skills/sortie/scripts/` (repo-local). Call them via `SORTIE=$(git rev-parse --show-toplevel)/.claude/skills/sortie/scripts`, don't reinvent them inline.
