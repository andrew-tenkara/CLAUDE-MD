# Sortie — Parallel Agent Orchestrator

This directory contains the **sortie** skill — a system for orchestrating parallel Claude Code agents across Linear tickets. The skill enables the team to spawn isolated git worktrees with Claude agents working on separate tickets simultaneously, with centralized progress monitoring and lifecycle management.

## Quick Start

### Prerequisites

First, run the setup check to ensure your environment is ready:

```bash
bash .claude/skills/sortie/scripts/setup.sh
```

This verifies:

- Claude CLI is installed
- Git with worktree support (v2.5+)
- osascript (macOS)
- iTerm2 is running
- Linear MCP is configured (optional; manual fallback available)

### Basic Usage

Once prerequisites are met, you can invoke sortie from within Claude Code:

```
/sortie
```

This pulls all Linear tickets assigned to you in **Backlog** or **Todo** state and walks you through each one interactively. For each ticket:

1. **Review** — Assess scope, dependencies, acceptance criteria
2. **Recommend** — Suggest model (haiku/sonnet/opus) and parallelization strategy
3. **Approve or Skip** — Launch the agent or move to the next ticket

### Advanced Entry Points

- **`/sortie <ticket-url>`** — Process a single ticket
- **`/sortie <url>, <url>, <url>`** — Batch process multiple tickets (comma-separated)
- **`/sortie <file-path>`** — Launch from a local spec file instead of a Linear ticket
- **`/sortie resume <ticket-url>`** — Resume a ticket that's already in progress

## How It Works

### Architecture

The skill orchestrates agents through four main components:

1. **Directive** — Each agent receives a `.sortie/directive.md` with ticket details, acceptance criteria, scope, and constraints. The agent reads it, implements the work, and self-reviews.

2. **Settings** — Each worktree gets a `.claude/settings.json` that scopes the agent to its Linear branch only. Force pushes and destructive operations are hard-blocked at the CLI level.

3. **Lifecycle** — Agents follow a defined lifecycle:
   - Read directive → Implement → Self-review (pre-review) → Fix issues → Self-review again (post-review) → Commit & push

4. **Progress Tracking** — Agents log status to `.sortie/progress.md`. The orchestrator monitors this and the lifecycle marker files (`pre-review.done`, `post-review.done`) to track state.

### Script Reference

All supporting scripts live in `scripts/`:

- **`setup.sh`** — Verify prerequisites
- **`create-worktree.sh`** — Create a git worktree + `.sortie/` protocol directory
- **`write-settings.sh`** — Template `.claude/settings.json` with scoped permissions
- **`write-directive.sh`** — Template `directive.md` with ticket details
- **`spawn-pane.sh`** — Spawn a Claude Code agent in an iTerm2 pane
- **`check-status.sh`** — Monitor progress of all active agents
- **`cleanup.sh`** — Remove completed worktrees and branches

### Parallel Agents

For tickets that can be decomposed into independent workstreams (e.g., API + UI + Export), the orchestrator can spawn multiple sub-agents:

```
TEN-38: Implement supplier search with filters + results table + export
├─ Sub-1: API endpoint with filter logic (sonnet)
├─ Sub-2: React table component (sonnet)
└─ Sub-3: CSV export utility (haiku)
```

Each sub-agent works in its own worktree on a sub-branch. When all sub-agents complete their self-review, the orchestrator merges sub-branches into the parent and spawns an integration agent to verify coherence, run tests, and perform a final review.

## Team Onboarding

### For New Team Members

1. **Clone the repo** — You'll automatically have access to the sortie skill.

2. **Run setup.sh** — Verify your environment:

   ```bash
   bash .claude/skills/sortie/scripts/setup.sh
   ```

3. **Start a sortie** — Open Claude Code and invoke:

   ```
   /sortie
   ```

4. **Watch the agents work** — Agents spawn in iTerm2 panes. You can watch progress in real time or check status:
   ```
   /sortie
   ```
   (Then select "Check status" option)

### Model Selection Guidelines

The orchestrator recommends a model based on ticket scope:

| Model      | When to use                                                                                                 |
| ---------- | ----------------------------------------------------------------------------------------------------------- |
| **haiku**  | Lint fixes, copy changes, config updates, single-file tweaks, boilerplate, test writing, CSV export         |
| **sonnet** | Standard features, bug fixes, API endpoints, React components, service layer, most CRUD, moderate refactors |
| **opus**   | Multi-system refactors, complex business logic, architectural changes, deep debugging                       |

### Safety Guarantees

Each agent operates under strict constraints:

- **Branch-scoped** — Can only push to their assigned Linear branch
- **Read-only remote** — Can fetch/pull but cannot delete branches or force push
- **No file deletion** — Cannot use `rm`, `rmdir`, or destructive git operations
- **Sandboxed permissions** — Blocked at both CLI and settings levels

## Monitoring & Management

### Check Status

At any time, you can check the status of all active agents:

```
/sortie
```

Then choose "Check status". Output shows:

```
PRJ-52  [W1:P1]  ██████████ Done — pushed to feature/prj-52-...
PRJ-49  [W1:P2]  ██████░░░░ Reviewing — fixing issues
PRJ-45  [W1:P3]  ████░░░░░░ Working — building UI
```

### Clean Up

When agents complete and push, you can clean up their worktrees:

```
bash .claude/skills/sortie/scripts/cleanup.sh TEN-52
```

Or clean all completed sorties at once:

```
bash .claude/skills/sortie/scripts/cleanup.sh --all
```

## Troubleshooting

### "Claude CLI not found"

Install Claude Code: https://docs.anthropic.com/en/docs/claude-code

### "iTerm2 not running"

Launch iTerm2 from Applications, then try again.

### "Linear MCP not configured"

The skill falls back to manual ticket entry. To use Linear MCP for automatic ticket fetching, add it to `~/.claude/.mcp.json`:

```json
{
  "mcp_servers": {
    "linear": {
      "command": "..."
    }
  }
}
```

### Agent dies unexpectedly

Check the worktree's `.sortie/progress.md` to see if work was preserved. You can resume the ticket:

```
/sortie resume <ticket-url>
```

## Design Principles

- **Orchestrator, not implementer** — The skill coordinates agents; it doesn't write code.
- **Fully autonomous agents** — Once an agent receives its directive, it works independently without asking for input.
- **Reproducibility** — All agent work is scoped to linear branches with full commit history.
- **Safety first** — Destructive operations are impossible; all permissions are scoped tightly.
- **Real-time visibility** — Watch agents work across panes in iTerm2 or check status anytime.

## Learn More

For detailed orchestrator behavior, see `SKILL.md` in this directory. It covers:

- Ticket review process (scope, dependencies, acceptance criteria)
- Agent spawning (worktrees, permissions, directives)
- Parallel agent lifecycle and merge conflicts
- Error handling and recovery
- Remote operations and safety rules

---

Questions? Check `SKILL.md` for the full orchestrator spec.
