---
name: sortie-commander
description: "USS Tenkara Pri-Fly — Full agent orchestration TUI with hotkey-driven agent management and Mini Boss AI"
command: /sortie-commander
---

# USS Tenkara Pri-Fly — Sortie Commander

Full agent orchestration dashboard. You are the Air Boss — spawn agents, manage missions, and command from a single pane of glass. All actions via hotkeys. Talk to Mini Boss in its iTerm2 pane for complex orchestration.

## How to Launch

Open the commander in a **new iTerm2 window** (never run the TUI directly via Bash tool):

```bash
bash ~/.claude/skills/sortie-commander/scripts/launch-commander.sh --project-dir <PROJECT_DIR>
```

**IMPORTANT**: Do NOT run commander-dashboard.py directly — it's a Textual TUI that needs a real terminal. Always use launch-commander.sh.

## Window Layout

- **Window 1**: TUI dashboard (flight strip, board, radio, queue)
- **Window 2**: Pit Boss — Mini Boss pane (first) + agent Claude sessions as split panes

## Flight Lifecycle

1. **Deploy/Resume** → agent appears on deck as **IDLE** (orange sprite, engines warm)
2. **Tokens start flowing** → auto-transitions to **AIRBORNE** (takeoff animation)
3. **Work complete** → **RECOVERED** (landing animation, parked)
4. **Crash** → **MAYDAY** / **SAR** (recovery animation)

## Keybindings

### Global (always available)

| Key | Action |
|-----|--------|
| `D` | Open agent pane (iTerm2 split) |
| `V` | Spin up dev server for selected pilot |
| `O` | Open pilot's localhost server in browser |
| `P` | Open pilot's GitHub PR in browser |
| `L` | Browse Linear issues (or pilot's ticket) |
| `M` | Relaunch Mini Boss |
| `R` | Resume — relaunch recovered/idle pilot |
| `W` | Wave-off — hard kill (with confirmation) |
| `K` | Compact — trigger AAR refueling |
| `X` | Recall — graceful wind-down |
| `F` | Toggle flight strip visibility |
| `Q` | Quit |

### Navigation

| Key | Action |
|-----|--------|
| `Esc` | Return focus to board |
| `Tab` | Cycle focus |
| `↑/↓` | Select pilot on board |

## Mini Boss

Mini Boss (Opus) spawns automatically on launch in the first Pit Boss pane. It assesses open worktrees and Linear status on startup. Talk to it directly in its iTerm2 pane for:
- Triaging tickets (model + priority assessment)
- Complex orchestration and mission planning
- Rearm, auto-compact, queue management
- Anything that needs conversation

## Vocabulary

| Status | Meaning |
|--------|---------|
| IDLE | Pane open, on deck, waiting for commands |
| AIRBORNE | Actively consuming tokens |
| ON APPROACH | Pre-review |
| RECOVERED | Done, parked |
| MAYDAY | Crashed / no PID |
| AAR | Compacting (refueling animation) |
| SAR | Crash recovery animation |

## Callsign System

Squadron names assigned per-mission (per ticket), not per model:
- Same ticket = same squadron (Phoenix-1, Phoenix-2)
- Different ticket = different squadron
- Pool: Phoenix, Reaper, Ghost, Viper, Iceman, Maverick, Shadow, Thunder, Raptor, Falcon
