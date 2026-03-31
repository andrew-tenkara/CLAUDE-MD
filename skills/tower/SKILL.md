---
name: tower
description: "USS Tenkara Pri-Fly — Full agent orchestration TUI with hotkey-driven agent management and Mini Boss AI"
command: /tower
---

# USS Tenkara Pri-Fly — Tower

Full agent orchestration dashboard. You are the Air Boss — spawn agents, manage missions, and command from a single pane of glass. All actions via hotkeys. Talk to Mini Boss in its iTerm2 pane for complex orchestration.

## Prerequisites

### Required

- **Python 3.12+** with `textual` and `watchdog` (auto-installed on first launch)
- **iTerm2** — TUI + agent panes use AppleScript iTerm2 integration
- **Claude CLI** (`claude`) in PATH

### Recommended

| Tool | Purpose | Install |
|------|---------|---------|
| **Headroom** | Context compression proxy for all pilots | `pip install 'headroom-ai[all]'` |
| **RTK** | CLI output token compression (60-90% savings) | `brew install rtk-ai/tap/rtk && rtk init -g` |
| **CGC** | Code graph + fuzzy symbol search (dedup check) | `pipx install codegraphcontext` — see setup below |
| **Serena** | Per-worktree code intelligence MCP | `uvx --from git+https://github.com/oraios/serena serena` |

### CGC (CodeGraphContext) Setup — Optional

CGC adds fuzzy symbol search (edit-distance matching) to the dedup check hook. Without it, the hook falls back to Serena `find_symbol` (exact match) and Grep (regex) — which work fine for most cases. CGC catches near-duplicates that exact search misses.

If installed, setup is:

1. `pipx install codegraphcontext` — binary at `~/.local/bin/cgc`, FalkorDB Lite backend (zero-config, no Docker)
2. `cgc mcp start` — register as MCP server (user scope, available across all projects)
3. `cgc index /path/to/your-project/src` — one-time index, file watcher keeps it current after that
4. Add `.cgcignore` at project root to exclude node_modules, build artifacts, test fixtures

**Notes:**
- Indexes the main repo's `src/` only — do NOT index individual worktree paths (creates duplicate nodes)
- Re-index on base branch switch: `cgc index --force /path/to/your-project/src` (~15s)
- Troubleshooting: `cgc list-repos` to verify, `cgc clean` for orphaned nodes

## How to Launch

Open the commander in a **new iTerm2 window** (never run the TUI directly via Bash tool):

```bash
bash ~/.claude/skills/tower/scripts/launch-commander.sh --project-dir <PROJECT_DIR>
```

Options:
- `--project-dir <path>` — project root (defaults to git root or cwd)
- `--linear-org <org>` — Linear organization slug (saved to config.json)

**IMPORTANT**: Do NOT run commander-dashboard.py directly — it's a Textual TUI that needs a real terminal. Always use launch-commander.sh.

## Window Layout

- **Window 1**: TUI dashboard (flight strip, board, radio, queue)
- **Window 2**: Pit Boss — Mini Boss pane (first) + agent Claude sessions as split panes

## Flight Lifecycle

1. **Deploy** → agent appears on deck as **ON_DECK** (orange sprite, engines warm)
2. **Tokens start flowing** → auto-transitions to **IN_FLIGHT** (takeoff animation)
3. **Tokens stop** → **ON_APPROACH** (landing animation begins)
4. **Pane closed / session ends** → **RECOVERED** (parked on deck)
5. **Dismiss** → auto-debrief written to sqlite, worktree removed

## Keybindings

### Board Mode (agent table focused)

| Key | Action |
|-----|--------|
| `D` | Deploy — open pane + run directive (full mission kickoff) |
| `V` | Spin up dev server for selected pilot |
| `O` | Open pilot's localhost server in browser |
| `P` | Open pilot's GitHub PR in browser |
| `B` | Open BullBoard for selected pilot |
| `L` | Browse Linear issues (or open pilot's ticket) |
| `T` | Open terminal at worktree (or project root) |
| `W` | Wave-off — hard kill + server cleanup |
| `Z` | Dismiss — debrief + remove worktree |
| `K` | Compact — trigger context compaction |
| `S` | Sync — re-scan worktrees |
| `M` | Relaunch Mini Boss |
| `H` | Headroom monitor |
| `F` | Toggle flight strip visibility |
| `Q` | Quit |

### Mission Queue (queue table focused)

| Key | Action |
|-----|--------|
| `Enter` | Deploy selected mission |
| `L` | Open selected mission's Linear ticket |
| `Del/Backspace/X` | Remove from queue |

### Navigation

| Key | Action |
|-----|--------|
| `Esc` | Return focus to board |
| `Tab` | Cycle focus |
| `↑/↓` | Select pilot on board |

### Chat Pane (comms mode)

| Key | Action |
|-----|--------|
| `Ctrl+Enter` | Send message to agent |
| `Enter` | Newline |
| `Ctrl+C` | Close chat pane |
| `Esc` | Return to Pri-Fly |
| `Tab` | Next chat pane |

## Slash Commands

Available in the radio/command input:

| Command | Action |
|---------|--------|
| `/deploy <ticket> [--model X]` | Launch new agent |
| `/queue <desc> [--priority N]` | Add to mission queue |
| `/linear [--team X]` | Browse Linear issues |
| `/recall <callsign>` | Graceful wind-down |
| `/wave-off <callsign>` | Hard kill |
| `/resume <callsign>` | Resume in existing worktree |
| `/compact <callsign\|idle\|all>` | Trigger compaction |
| `/auto-compact on\|off` | Auto-compact idle agents |
| `/sitrep` | Request status from all |
| `/briefing <callsign>` | Show directive |
| `/auto on\|off [max]` | Auto-deploy from queue |
| `/rearm <callsign> <ticket>` | Reassign recovered agent |

## XO Tools

CLI tools for the Mini Boss (XO) to manage the dashboard:

```bash
bash ~/.claude/skills/tower/scripts/xo-tools.sh <command> [args...]
```

| Command | Action |
|---------|--------|
| `board` | Show flight deck state |
| `board-json` | Board state as JSON |
| `health` | Full system health report |
| `set-status <ticket> <status>` | Override agent status |
| `dismiss <ticket>` | Force RECOVERED + session-ended |
| `inject <ticket> <message>` | Queue directive for agent |
| `reassign-model <ticket> <model>` | Change model |
| `clear-stale` | Mark dead agents as ended |
| `tail-agent <ticket>` | Tail agent's JSONL stream |
| `token-savings` | RTK + Headroom savings report |

## Mini Boss

Mini Boss (Opus) spawns automatically on launch in the first Pit Boss pane. It assesses open worktrees and Linear status on startup. Talk to it directly in its iTerm2 pane for:
- Triaging tickets (model + priority assessment)
- Complex orchestration and mission planning
- Queue management
- Anything that needs conversation

## Vocabulary

| Status | Meaning |
|--------|---------|
| ON_DECK | Pane open, not using tokens |
| IN_FLIGHT | Pane open, actively consuming tokens |
| ON_APPROACH | Tokens stopped, landing sequence |
| RECOVERED | Pane closed, on deck |

## Callsign System

Squadron names assigned per-mission (per ticket), not per model:
- Same ticket = same squadron (Phoenix-1, Phoenix-2)
- Different ticket = different squadron
- Pool: Phoenix, Reaper, Ghost, Viper, Iceman, Maverick, Shadow, Thunder, Raptor, Falcon

## Project Intelligence DB

All pilots share a SQLite database at `<PROJECT_DIR>/.sortie/storage.db`. Auto-debriefs are written on session end (pane close or graceful exit), capturing:
- Files touched (from transcript parsing)
- Key decisions (last substantive assistant messages)
- Error count
- Git branch and model

The next pilot on the same ticket receives this as `## Prior Intelligence` in their directive, plus FTS-ranked cross-ticket insights from agents that touched similar files.
