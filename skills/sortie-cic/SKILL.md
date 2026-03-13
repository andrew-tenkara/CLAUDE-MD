---
name: sortie-cic
description: "USS Tenkara CIC — Carrier-themed animated TUI dashboard for sortie agents"
command: /sortie-cic
---

# USS Tenkara CIC Dashboard

Aircraft carrier Combat Information Center (CIC) themed overlay for the sortie agent system. Displays real-time agent status using carrier aviation vocabulary.

## How to Launch

When invoked, open the CIC dashboard in a **new iTerm2 window** so the TUI renders properly:

```bash
bash ~/.claude/skills/sortie-cic/scripts/launch-cic.sh --project-dir <PROJECT_DIR>
```

Where `<PROJECT_DIR>` is the current working directory (use `$PWD` or the known project root).

If arguments are provided (e.g. a ticket ID), pass them through but still use `--project-dir`:

```bash
bash ~/.claude/skills/sortie-cic/scripts/launch-cic.sh --project-dir /path/to/project
```

**IMPORTANT**: Do NOT run carrier-dashboard.py directly via the Bash tool — it's a Textual TUI that needs a real terminal. Always use launch-cic.sh which opens a new iTerm2 window.

## Vocabulary

| Internal | CIC Display |
|----------|-------------|
| Agent working | AIRBORNE |
| Pre-review | ON APPROACH |
| Done | RECOVERED |
| Crashed / no PID | MAYDAY |
| Context stale | COMMS DARK |

## Callsign System

Agents receive squadron callsigns by model:
- **Opus** → Viper squadron (Viper-1, Viper-2...)
- **Sonnet** → Iceman squadron (Iceman-1, Iceman-2...)
- **Haiku** → Maverick squadron (Maverick-1, Maverick-2...)

## Keybindings

| Key | Action |
|-----|--------|
| `e` | Eject (kill agent) |
| `l` | Relaunch (respawn agent) |
| `p` | Ping All Stations (refresh) |
| `b` | Briefing (show directive) |
| `d` | Debrief (git diff summary) |
| `q` | Quit |
