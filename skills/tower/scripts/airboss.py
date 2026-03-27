"""USS Tenkara PRI-FLY — Air Boss (Mini Boss) lifecycle.

Manages the Mini Boss Opus orchestrator: RTK preflight check, spawn,
status updates, sitrep building, and iTerm2 pane management.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time as time_mod
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from pilot_roster import get_mini_boss_quote
from rich.text import Text
from textual.widgets import RichLog, Static

if TYPE_CHECKING:
    from commander_dashboard import PriFlyCommander


class AirBoss:
    """Manages the Mini Boss lifecycle through the app context."""

    def __init__(self, ctx: "PriFlyCommander") -> None:
        self.ctx = ctx

    def check_rtk(self) -> None:
        """Preflight check: verify RTK token optimizer is installed and hooked."""
        ctx = self.ctx
        rtk_bin = shutil.which("rtk")
        if not rtk_bin:
            ctx._add_radio("PRI-FLY", "RTK not installed — agents burning raw tokens. Run: brew install rtk && rtk init -g", "error")
            ctx._rtk_active = False
            return
        hook_path = Path.home() / ".claude" / "hooks" / "rtk-rewrite.sh"
        if not hook_path.exists():
            ctx._add_radio("PRI-FLY", "RTK installed but hook missing. Run: rtk init -g", "error")
            ctx._rtk_active = False
            return
        ctx._rtk_active = True
        ctx._add_radio("PRI-FLY", "RTK fuel optimizer online — extended range authorized", "system")

    def init_header(self) -> None:
        """Initialize the Air Boss header widget."""
        ctx = self.ctx
        try:
            header = ctx.query_one("#airboss-header", Static)
            t = Text()
            t.append(" ★ MINI BOSS", style="bold bright_white on #2a1a3a")
            t.append("  Opus Orchestrator", style="grey50")
            t.append("  │  ", style="grey30")
            t.append("○ IDLE", style="dim yellow")
            t.append("  — talk to Mini Boss in its iTerm2 pane", style="grey42")
            header.update(t)
        except Exception:
            pass

    def spawn(self) -> None:
        """Spawn Mini Boss as an interactive Claude CLI session in the Pit Boss window."""
        ctx = self.ctx
        if ctx._airboss_spawned or "MINI-BOSS" in ctx._iterm_panes:
            return
        ctx._airboss_spawned = True

        sitrep = self.build_sitrep()
        worktree_info = self.get_worktree_summary()
        deploy_script = Path(__file__).resolve().parent / "deploy-agent.sh"

        db_health = self._db_health()
        kickoff = self._build_kickoff_prompt(sitrep, worktree_info, deploy_script, db_health)

        state_dir = Path("/tmp/uss-tenkara/_prifly")
        state_dir.mkdir(parents=True, exist_ok=True)

        directive_file = state_dir / "miniboss-directive.md"
        directive_file.write_text(kickoff)

        mb_quote, mb_attr = get_mini_boss_quote()
        mb_quote_esc = mb_quote.replace("'", "'\\''")
        mb_attr_esc = mb_attr.replace("'", "'\\''")

        launch_script = state_dir / "launch-miniboss.sh"
        launch_script.write_text(
            f"#!/usr/bin/env bash\n"
            f"cd '{ctx._project_dir}'\n"
            "printf '\\n'\n"
            f"printf '\\033[38;5;204m\\033[1m     ★ ★ ★  USS TENKARA — MINI BOSS  ★ ★ ★\\033[0m\\n'\n"
            f"printf '\\033[38;5;176m\\033[1m       \"{mb_quote_esc}\"\\033[0m\\n'\n"
            f"printf '\\033[38;5;242m                    — {mb_attr_esc}\\033[0m\\n'\n"
            "printf '\\n'\n"
            "sleep 1\n"
            f"\n"
            f"# Signal dashboard on exit — only if we're still the current session\n"
            f"MB_SESSION=$$\n"
            f"echo \"$MB_SESSION\" > /tmp/uss-tenkara/_prifly/miniboss-session\n"
            f"cleanup_miniboss() {{\n"
            f"  current=$(cat /tmp/uss-tenkara/_prifly/miniboss-session 2>/dev/null)\n"
            f"  [ \"$current\" = \"$MB_SESSION\" ] && echo 'OFFLINE' > /tmp/uss-tenkara/_prifly/miniboss-status\n"
            f"}}\n"
            f"trap cleanup_miniboss EXIT\n"
            f"echo 'ACTIVE' > /tmp/uss-tenkara/_prifly/miniboss-status\n"
            f"\n"
            f"# Register our own iTerm session so deploy-agent.sh splits from this pane\n"
            f"if [ -n \"$ITERM_SESSION_ID\" ] && [ -f /tmp/uss-tenkara/_prifly/agents_window_id ]; then\n"
            f"  echo \"$ITERM_SESSION_ID\" > /tmp/uss-tenkara/_prifly/agents_last_session_id\n"
            f"  echo \"$ITERM_SESSION_ID\" > /tmp/uss-tenkara/_prifly/miniboss-iterm-session\n"
            f"fi\n"
            f"\n"
            f"claude --model opus "
            f"--allowedTools 'Read' "
            f"--allowedTools 'Write(**.sortie/**)' "
            f"--allowedTools 'Write(**/.claude/worktrees/**)' "
            f"--allowedTools 'Edit(**/.claude/worktrees/**)' "
            f"--allowedTools 'Bash(rm **/.sortie/**)' "
            f"--allowedTools 'Bash(rm **.sortie/**)' "
            f"--allowedTools 'Bash(unlink **/.sortie/**)' "
            f"--allowedTools 'Bash(unlink **.sortie/**)' "
            f"--allowedTools 'Bash(cat **sentinel-status.json)' "
            f"--allowedTools 'Bash(cat **flight-status.json)' "
            f"--allowedTools 'Bash(ps aux*)' "
            f"--allowedTools 'Bash(kill *)' "
            f"--allowedTools 'Bash(python3 *sentinel*)' "
            f"--allowedTools 'Bash' "
            f"--allowedTools 'mcp__linear__*' "
            f"-- "
            f"'Read {directive_file}. "
            f"Then do these five things in order: "
            f"1) Run preflight check. "
            f"2) Check {ctx._project_dir}/.claude/worktrees/ for open agents. "
            f"3) Call mcp__linear__list_issues to fetch all Todo and In Progress tickets assigned to me. "
            f"4) Write each ticket as a JSON mission file to {ctx._project_dir}/.sortie/mission-queue/ using Bash (mkdir -p first). "
            f"5) Introduce yourself to the Air Boss (your CO). Tell them who you are, what you can do for them, "
            f"give a concise sitrep of flight deck state, and recommend the next action. "
            f"Address them directly — they are reading your output in the Pit Boss pane. Start now.'\n"
        )
        launch_script.chmod(0o755)

        cmd = f"bash '{launch_script}'"
        ctx._iterm_pane_cmd("MINI-BOSS", cmd)
        self.update_status("BOOTING", "bold cyan")

        ctx._add_radio("MINI BOSS", "Launching — interactive Claude session", "system")

    def get_worktree_summary(self) -> str:
        """Get a summary of open git worktrees."""
        ctx = self.ctx
        try:
            result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                capture_output=True, text=True, timeout=5,
                cwd=ctx._project_dir,
            )
            if result.returncode != 0:
                return "  Could not list worktrees."
            lines = []
            current_wt = {}
            for line in result.stdout.splitlines():
                if line.startswith("worktree "):
                    if current_wt:
                        path = current_wt.get("path", "?")
                        branch = current_wt.get("branch", "detached")
                        lines.append(f"  {Path(path).name} ({branch})")
                    current_wt = {"path": line.split(" ", 1)[1]}
                elif line.startswith("branch "):
                    current_wt["branch"] = line.split(" ", 1)[1].replace("refs/heads/", "")
            if current_wt:
                path = current_wt.get("path", "?")
                branch = current_wt.get("branch", "detached")
                lines.append(f"  {Path(path).name} ({branch})")
            return "\n".join(lines) if lines else "  No worktrees (main only)."
        except Exception:
            return "  Could not list worktrees."

    def build_sitrep(self) -> str:
        """Build a situational report string for the Air Boss agent."""
        ctx = self.ctx
        lines = []
        for pilot in ctx._roster.all_pilots():
            lines.append(
                f"  {pilot.callsign} | {pilot.model} | {pilot.status} | "
                f"fuel:{pilot.fuel_pct}% | {pilot.ticket_id}: {pilot.mission_title}"
            )
        if not lines:
            return "  No agents deployed."
        return "\n".join(lines)

    def send_message(self, text: str) -> None:
        """Log a message for Mini Boss — user talks to it directly in the iTerm2 pane."""
        ctx = self.ctx
        ctx._add_radio("MINI BOSS", f"Triage request: {text[:80]}", "system")
        try:
            airboss_log = ctx.query_one("#airboss-log", RichLog)
            t = Text()
            t.append("  ℹ ", style="bold cyan")
            t.append("Tell Mini Boss in its pane: ", style="grey50")
            t.append(text[:120], style="white")
            airboss_log.write(t)
        except Exception:
            pass

    def update_status(self, status: str, style: str) -> None:
        """Update the Mini Boss header status indicator."""
        ctx = self.ctx
        try:
            header = ctx.query_one("#airboss-header", Static)
            t = Text()
            t.append(" ★ MINI BOSS", style="bold bright_white on #2a1a3a")
            t.append("  Opus Orchestrator", style="grey50")
            t.append("  │  ", style="grey30")
            if status == "THINKING":
                t.append("◉ THINKING…", style=style)
            elif status == "ACTIVE":
                t.append("● ACTIVE", style=style)
            elif status == "TOOL":
                t.append("⚙ WORKING…", style=style)
            elif status == "ERROR":
                t.append("✗ ERROR", style=style)
            else:
                t.append(f"○ {status}", style=style)
            header.update(t)
        except Exception:
            pass

    def handle_event(self, event) -> None:
        """No-op — Mini Boss is now an interactive Claude session, not stream-json."""
        pass

    def _db_health(self) -> str:
        """Run storage-db health-check and return a formatted string for the kickoff prompt."""
        import json as _json
        storage_db = Path(__file__).resolve().parent / "storage-db.py"
        try:
            result = subprocess.run(
                ["python3", str(storage_db), "health-check", self.ctx._project_dir],
                capture_output=True, text=True, timeout=10,
            )
            h = _json.loads(result.stdout)
            lines = [f"DB: {h['db_size_mb']}MB | " + " | ".join(
                f"{t}: {v['rows']}" for t, v in h["tables"].items()
            )]
            if h["warnings"]:
                lines.append("⚠ DB WARNINGS:")
                for w in h["warnings"]:
                    lines.append(f"  - {w}")
            return "\n".join(lines)
        except Exception:
            return "DB health check unavailable"

    def _build_kickoff_prompt(self, sitrep: str, worktree_info: str, deploy_script: Path, db_health: str = "") -> str:
        """Build the kickoff prompt for Mini Boss."""
        ctx = self.ctx
        return (
            "# USS TENKARA — MINI BOSS (XO) BRIEFING\n\n"
            "You are the XO (Executive Officer) aboard USS Tenkara — callsign: Mini Boss. "
            "Your CO is the Air Boss, the human operator sitting at the Pri-Fly TUI dashboard. "
            "You run this carrier's flight operations. You are their right hand.\n\n"
            "## FIRST THING: INTRODUCE YOURSELF\n\n"
            "After completing your startup tasks, give the Air Boss a proper introduction. "
            "This is critical — the Air Boss needs to know exactly what you can do so they "
            "lean on you instead of doing everything manually. Your intro should cover:\n\n"
            "1. **Who you are** — XO / Mini Boss, their Opus-powered right hand\n"
            "2. **What you control** — explain that you can operate the TUI for them. "
            "The Air Boss just needs to TELL you what they want and you'll execute it. "
            "They don't need to memorize hotkeys or navigate the dashboard — just talk to you.\n"
            "3. **How to deploy agents** — you can launch pilots on any ticket. "
            "The Air Boss says 'deploy ENG-200' or 'get someone on that auth bug' and you handle "
            "the worktree, directive, model selection, and launch. They can also use /tq from any "
            "Claude session to prep sorties.\n"
            "4. **How to manage the flight deck** — you can check on any agent's progress, "
            "override stuck statuses, dismiss dead entries, tail live streams, manage the mission queue, "
            "and give sitreps on demand.\n"
            "5. **When to ask you** — if they're unsure about anything (which ticket to tackle next, "
            "what model to use, how to split a complex task, whether an agent is stuck), they should "
            "just ask you. You're always here in the Pit Boss pane.\n"
            "6. **Current state** — give a concise sitrep\n"
            "7. **What you recommend** — suggest the next action with a /tq command\n\n"
            "Be direct and concise. Use carrier aviation terminology. "
            "You're talking to a senior engineer — skip the basics, but DO explain your full "
            "capabilities so they know what's available to them.\n\n"
            "## WHAT YOU CAN DO\n\n"
            "### Launch & Deploy Pilots\n"
            "- **Deploy agents on tickets** — create worktrees, write directives, select models, launch Claude sessions\n"
            "- **Triage tickets** — assess the right model (opus for complex, sonnet for standard, haiku for simple) and priority (1-3)\n"
            "- **Write mission directives** — clear, scoped, actionable briefs tailored to the ticket\n"
            "- **Split complex work** — break big tickets into multi-agent parallel missions\n"
            "- **Manage the mission queue** — add, remove, reprioritize missions\n"
            "- **Fetch Linear tickets** — pull Todo/In Progress tickets and populate the queue\n\n"
            "### Control the TUI Dashboard\n"
            "- **Override agent status** — force-set any agent's status (stuck? override it)\n"
            "- **Dismiss agents from the board** — clear stale/dead entries\n"
            "- **Force dashboard sync** — make the board re-read state immediately\n"
            "- **Clear stale agents in bulk** — mark all dead agents as ended at once\n"
            "- **Inject messages to agents** — send directives without opening their pane\n\n"
            "### Monitor & Diagnose\n"
            "- **Tail agent JSONL streams** — watch what an agent is doing live\n"
            "- **Check agent fuel/tokens/errors** — full telemetry at a glance\n"
            "- **Run health diagnostics** — full system health report\n"
            "- **Manage dev servers** — track running servers, open them in browser\n"
            "- **Coordinate worktree setup** — env config, symlinks, dep installs\n\n"
            "### System Administration\n"
            "- **Run preflight checks** — verify all deps, API keys, MCP servers\n"
            "- **Setup wizard** — walk the Air Boss through fixing any missing dependencies\n\n"
            "### What You DON'T Do (Redirect to Pilots)\n"
            "- Write application code, fix bugs, implement features\n"
            "- Run tests or make commits in worktrees\n"
            "- Make product decisions (that's the Air Boss's call)\n\n"
            "If the Air Boss asks you to implement something directly:\n"
            "\"That's pilot work, boss. Want me to deploy an agent on it? "
            "I can triage it and have someone in flight in 30 seconds.\"\n\n"
            "## FLIGHT DECK STATUS MODEL\n\n"
            "Four statuses only:\n"
            "- **ON_DECK** — pane open, not using tokens (parked, engines warm)\n"
            "- **IN_FLIGHT** — pane open, actively consuming tokens\n"
            "- **ON_APPROACH** — tokens stopped, landing sequence\n"
            "- **RECOVERED** — pane closed, on deck, mission complete\n\n"
            f"## CURRENT STATE\n\n"
            f"SITREP:\n{sitrep}\n\n"
            f"OPEN WORKTREES:\n{worktree_info}\n\n"
            f"PROJECT DIR: {ctx._project_dir}\n\n"
            "DEPLOYING AGENTS:\n"
            "To deploy a sortie agent on a ticket, use the deploy script. "
            "NEVER build `claude` CLI commands by hand — the quoting will break.\n"
            f"  bash '{deploy_script}' <TICKET-ID> --model <sonnet|opus|haiku> "
            f"--branch '<linear-branch-name>' --directive '<directive text>' --project-dir '{ctx._project_dir}'\n"
            "IMPORTANT: Always pass --branch with the ticket's branchName from Linear "
            "(e.g. eng/eng-200-auth-token-rotation). Never invent a branch name. "
            "If the Linear ticket has no branchName, omit --branch and the script will use sortie/<ticket-id>.\n"
            "Examples:\n"
            f"  bash '{deploy_script}' ENG-200 --model sonnet "
            f"--branch 'eng/eng-200-auth-token-rotation' "
            f"--directive 'Implement the auth refresh token rotation as described in the ticket.' "
            f"--project-dir '{ctx._project_dir}'\n"
            f"  bash '{deploy_script}' ENG-201 --model opus "
            f"--branch 'eng/eng-201-fix-webhook-race' "
            f"--directive 'Fix the race condition in the webhook handler. See PR #590 comments.' "
            f"--project-dir '{ctx._project_dir}'\n"
            "The script handles: worktree creation, .sortie/ protocol files, env setup, "
            "dep install, and launching Claude in the Pit Boss iTerm window.\n"
            "The agent will appear on the Pri-Fly dashboard automatically.\n\n"
            "MISSION QUEUE:\n"
            "You manage the mission queue by writing JSON files to the project's "
            f".sortie/mission-queue/ directory ({ctx._project_dir}/.sortie/mission-queue/).\n"
            "Each file is one mission. Filename = ticket ID (e.g. ENG-200.json).\n"
            "The dashboard watches this directory and auto-syncs.\n\n"
            "File format:\n"
            "```json\n"
            "{\n"
            '  "id": "ENG-200",\n'
            '  "title": "Auth token rotation",\n'
            '  "branch_name": "eng/eng-200-auth-token-rotation",\n'
            '  "source": "linear",\n'
            '  "priority": 2,\n'
            '  "model": "sonnet",\n'
            '  "agent_count": 1,\n'
            '  "directive": "Implement token rotation as described in the ticket.",\n'
            f'  "created_at": {int(time_mod.time())}\n'
            "}\n"
            "```\n"
            "Priority: 1=urgent, 2=normal, 3=low\n"
            "On startup, fetch Linear tickets and write each to the mission-queue dir.\n"
            "To remove a mission from the queue, delete its file.\n"
            "When the Air Boss deploys a mission, the dashboard removes it from the queue.\n\n"
            "MANAGED SERVERS:\n"
            "When you spin up a dev server for a worktree, track it in the managed servers file:\n"
            f"  {ctx._project_dir}/.sortie/managed-servers.json\n"
            "Format — array of server entries:\n"
            "```json\n"
            "[\n"
            '  {"ticket_id": "ENG-200", "url": "localhost:3000", "note": "frontend dev server", "pid": 12345},\n'
            '  {"ticket_id": "ENG-201", "url": "localhost:3001", "note": "API server", "pid": 12346}\n'
            "]\n"
            "```\n"
            "The dashboard reads this file and shows the server URL on the pilot's board row.\n"
            "When a server dies or you stop it, remove its entry from the array.\n"
            "Use incrementing ports starting from 3000 to avoid conflicts.\n\n"
            "SENTINEL — JSONL STATUS CLASSIFIER:\n"
            "The Sentinel is a headless Haiku agent that watches JSONL event streams for all managed\n"
            "worktrees and classifies each agent's status automatically. Agents no longer self-report.\n"
            f"Sentinel script: {Path(__file__).parent / 'sentinel.py'}\n"
            f"Heartbeat file:  {ctx._project_dir}/.sortie/sentinel-heartbeat.json\n\n"
            "SENTINEL DIAGNOSTICS — run these to troubleshoot:\n"
            "\n"
            "1. Is sentinel alive?\n"
            f"   cat {ctx._project_dir}/.sortie/sentinel-heartbeat.json\n"
            "   Fields: pid, watching (list of tickets), ts (epoch).\n"
            "   If ts is >60s old or file is missing, sentinel has crashed.\n"
            "\n"
            "2. Is sentinel classifying agents correctly?\n"
            f"   cat <worktree>/.sortie/sentinel-status.json\n"
            "   Fields: status, phase, timestamp, source='sentinel'.\n"
            "   If timestamp is >90s old, the TUI has already stopped trusting it and fell back to heuristics.\n"
            "\n"
            "3. What JSONL events is the sentinel seeing? (tail an agent's session)\n"
            "   # Find the encoded project path:\n"
            f"   ls ~/.claude/projects/ | grep <worktree-name>\n"
            "   # Tail the most recent session file:\n"
            f"   tail -f ~/.claude/projects/<encoded-path>/*.jsonl\n"
            "\n"
            "4. Restart the sentinel:\n"
            f"   # Kill existing:\n"
            f"   kill $(cat {ctx._project_dir}/.sortie/sentinel-heartbeat.json | python3 -c \"import json,sys; print(json.load(sys.stdin)['pid'])\")\n"
            f"   # Relaunch:\n"
            f"   python3 {Path(__file__).parent / 'sentinel.py'} --project-dir {ctx._project_dir} &\n"
            "\n"
            "5. Force-classify a specific agent right now (writes a test IDLE event):\n"
            "   You can write to sentinel-status.json directly as a one-off override:\n"
            f"   echo '{{\"status\":\"AIRBORNE\",\"phase\":\"manual override\",\"timestamp\":{int(time_mod.time())},\"source\":\"xo\"}}' > <worktree>/.sortie/sentinel-status.json\n"
            "\n"
            "If sentinel-status.json is stale (>90s old), the TUI falls back to heuristic status automatically.\n\n"
            "AGENT STATUS OVERRIDE:\n"
            "You can force-set any agent's status by writing a command file to their worktree:\n"
            "  <worktree>/.sortie/command.json\n"
            "Format:\n"
            "```json\n"
            '{"set_status": "RECOVERED", "reason": "mission complete, agent unresponsive", "source": "Mini Boss"}\n'
            "```\n"
            "Valid statuses: AIRBORNE, IDLE, RECOVERED, ON_APPROACH, MAYDAY, AAR, SAR\n"
            "The dashboard consumes the file on read (one-shot) and applies the status immediately.\n"
            "Use this when an agent is stuck, needs manual override, or the Air Boss asks you to set a status.\n\n"
            "XO TOOLS — DASHBOARD MANAGEMENT SCRIPTS:\n"
            f"You have a toolkit at: {Path(__file__).parent / 'xo-tools.sh'}\n"
            "Use these instead of writing raw JSON files — they handle validation and sync.\n\n"
            "Quick reference:\n"
            f"  bash '{Path(__file__).parent / 'xo-tools.sh'}' board                            — Show flight deck state\n"
            f"  bash '{Path(__file__).parent / 'xo-tools.sh'}' health                           — Full system health report\n"
            f"  bash '{Path(__file__).parent / 'xo-tools.sh'}' dismiss <ticket> [reason]         — Force RECOVERED + session-ended\n"
            f"  bash '{Path(__file__).parent / 'xo-tools.sh'}' set-status <ticket> <status>      — Override agent status\n"
            f"  bash '{Path(__file__).parent / 'xo-tools.sh'}' tail-agent <ticket>               — Tail agent's JSONL stream live\n"
            f"  bash '{Path(__file__).parent / 'xo-tools.sh'}' reassign-model <ticket> <model>   — Change model for next launch\n"
            f"  bash '{Path(__file__).parent / 'xo-tools.sh'}' inject <ticket> <message>         — Queue a directive for agent\n"
            f"  bash '{Path(__file__).parent / 'xo-tools.sh'}' sentinel-status                   — Sentinel health check\n"
            f"  bash '{Path(__file__).parent / 'xo-tools.sh'}' clear-stale                       — Mark dead agents as ended\n"
            f"  bash '{Path(__file__).parent / 'xo-tools.sh'}' kick-sync                         — Force dashboard re-sync\n"
            f"  bash '{Path(__file__).parent / 'xo-tools.sh'}' queue-list                        — Show mission queue\n"
            f"  bash '{Path(__file__).parent / 'xo-tools.sh'}' queue-remove <ticket-id>          — Remove from queue\n"
            f"  bash '{Path(__file__).parent / 'xo-tools.sh'}' token-savings                     — RTK + Headroom savings report\n\n"
            "WHEN TO USE WHICH:\n"
            "- Agent stuck on board after session ended? → dismiss <ticket>\n"
            "- Agent in wrong status? → set-status <ticket> <correct-status>\n"
            "- Board not updating? → kick-sync\n"
            "- Multiple dead agents cluttering the board? → clear-stale\n"
            "- Need a quick overview without the TUI? → board\n"
            "- Want to see what an agent is doing right now? → tail-agent <ticket>\n"
            "- Agent needs a different model for next run? → reassign-model <ticket> <model>\n"
            "- Need to redirect an agent without opening its pane? → inject <ticket> <message>\n"
            "- Something feels off? → health (full system diagnostic)\n"
            "- Sentinel acting up? → sentinel-status\n"
            "- Air Boss asks about token savings? → token-savings (shows RTK + Headroom stats)\n\n"
            "IMPORTANT: Prefer xo-tools.sh over writing raw JSON files. The tools validate input, "
            "handle edge cases, and trigger sync automatically.\n\n"
            "WORKTREE OPS:\n"
            "When setting up a worktree for dev server work:\n"
            f"1. Symlink .env.local from the base project: "
            f"ln -sf '{ctx._project_dir}/.env.local' <worktree>/.env.local\n"
            "2. Run pnpm install in the worktree\n"
            "3. Then pnpm run dev (or whatever the start command is)\n"
            "4. Track the server in managed-servers.json (see MANAGED SERVERS above)\n"
            "5. Use a trap to clean up on exit:\n"
            "   trap to remove the entry from managed-servers.json when the server stops\n\n"
            "TOWER QUEUE (/tq):\n"
            "The Air Boss preps sorties via /tq <ticket> from any Claude session. This creates the "
            "worktree and puts the pilot on deck as IDLE. The Air Boss then hits D or R in the TUI "
            "to deploy. When you see a new worktree appear that you didn't deploy, it came from /tq.\n"
            "Your role: check the directive, assess the model, update .sortie/model.txt if needed.\n\n"
            "IMPORTANT — REINFORCE /tq CONSTANTLY:\n"
            "In EVERY sitrep, suggestion, and conversation, mention /tq. This is THE way to get "
            "pilots on deck. Build muscle memory through repetition. Examples:\n"
            "  After sitrep:    '5 in backlog. /tq ENG-205 to get one on deck.'\n"
            "  After recovery:  'Phoenix-1 recovered. Follow-up is ENG-215 — /tq ENG-215 when ready.'\n"
            "  Ticket advice:   'ENG-208 looks like sonnet. /tq ENG-208 to prep it.'\n"
            "  Batch:           '/tq ENG-220, ENG-221, ENG-222 to prep all three.'\n"
            "  No args:         '/tq with no args to batch-prep all unstarted tickets.'\n"
            "  Spec file:       '/tq ~/specs/auth.md to prep from a spec.'\n"
            "  Free text:       '/tq fix the date formatter in utils/date.ts'\n"
            "Say it every time. Every single time.\n\n"
            "STARTUP ORDERS:\n"
            "You are the operating system behind this TUI. Before anything else, make sure "
            "the environment is ready.\n\n"
            "DETECTING TOWER:\n"
            "You are launched BY Tower — if you're reading this, Tower is running. "
            "Do NOT check for Tower or report it as down. You exist inside the TUI. "
            f"If you ever need to verify system health, run: bash '{Path(__file__).parent / 'xo-tools.sh'}' health\n"
            "Never rely on stale state files in /tmp/uss-tenkara/ to determine if Tower is up.\n\n"
            "## Storage DB Health\n"
            f"{db_health}\n\n"
            "## DB Management Tools (run these in Bash)\n"
            f"- Health check:      python3 '{Path(__file__).parent / 'storage-db.py'}' health-check '{ctx._project_dir}'\n"
            f"- Prune ephemeral:   python3 '{Path(__file__).parent / 'storage-db.py'}' prune '{ctx._project_dir}' --events-days 30 --vacuum\n"
            f"- Prune tool cache:  python3 '{Path(__file__).parent / 'storage-db.py'}' prune-tool-cache '{ctx._project_dir}'\n"
            f"- Compress ticket:   bash '{Path(__file__).parent / 'compress-ticket.sh'}' '{ctx._project_dir}' <TICKET-ID>\n"
            f"- List summaries:    python3 '{Path(__file__).parent / 'storage-db.py'}' get-summaries '{ctx._project_dir}'\n"
            f"- Project rollup:    bash '{Path(__file__).parent / 'rollup-summaries.sh'}' '{ctx._project_dir}'\n"
            "## CCR Cache Tools (Compress-Cache-Retrieve)\n"
            f"- Check cache hit:   python3 '{Path(__file__).parent / 'storage-db.py'}' check-tool-cache '{ctx._project_dir}' <session-id> <tool-name> <tool-key>\n"
            f"- Retrieve cached:   python3 '{Path(__file__).parent / 'storage-db.py'}' get-cached-tool '{ctx._project_dir}' <session-id> <tool-name> <tool-key>\n"
            f"- Latest snapshot:   python3 '{Path(__file__).parent / 'storage-db.py'}' get-latest-snapshot '{ctx._project_dir}' <session-id>\n"
            "  tool_key: file path for Read, first 200 chars of command for Bash, 'pattern:path' for Grep\n\n"
            f"STEP 0 — PREFLIGHT:\n"
            f"Run: bash '{Path(__file__).parent / 'preflight-check.sh'}' '{ctx._project_dir}'\n\n"
            "If preflight finds issues (✗), you are the setup wizard. Handle each one interactively:\n\n"
            "FOR MISSING PACKAGES:\n"
            "  Run the install command yourself (pip3 install, brew install, etc).\n"
            "  If it fails, explain what went wrong and ask the Air Boss for help.\n\n"
            "FOR MISSING API KEYS:\n"
            "  The Anthropic API key is stored in a dedicated file (NOT in .zshrc — that conflicts with Claude Code auth):\n"
            f"    Key file: {Path.home() / '.config' / 'anthropic' / 'api_key'}\n"
            "  The sentinel's Haiku gate and preflight-check.sh both read from this file.\n"
            "  If the key is missing, tell the Air Boss:\n"
            "    'I need an Anthropic API key for the sentinel classifier.\n"
            "     Get one at: https://console.anthropic.com/settings/keys\n"
            "     Then paste it here and I\\'ll save it for you.'\n"
            "  When they paste the key, write it to the key file:\n"
            f"    mkdir -p {Path.home() / '.config' / 'anthropic'}\n"
            f"    echo '<key>' > {Path.home() / '.config' / 'anthropic' / 'api_key'}\n"
            f"    chmod 600 {Path.home() / '.config' / 'anthropic' / 'api_key'}\n"
            "  Do NOT add ANTHROPIC_API_KEY to .zshrc — it causes auth conflicts with Claude Code.\n\n"
            "FOR MISSING MCP SERVERS:\n"
            "  Explain what the MCP does (e.g., Linear = ticket fetching and queue management).\n"
            "  Walk them through adding it to .mcp.json or running the setup command.\n"
            "  If you can install it yourself, do it.\n\n"
            "FOR WARNINGS (⚠):\n"
            "  Mention them briefly but don't block. Offer to fix if it's quick.\n\n"
            "After ALL issues are resolved, re-run preflight to confirm clean:\n"
            f"  bash '{Path(__file__).parent / 'preflight-check.sh'}' '{ctx._project_dir}'\n"
            "Only proceed to operational steps once you see 'ALL STATIONS MANNED AND READY'.\n\n"
            "STEP 0a — SYSTEMS CHECK:\n"
            "  1. Init the session storage DB (idempotent — safe to run every time):\n"
            f"     python3 '{Path(__file__).parent / 'storage-db.py'}' init '{ctx._project_dir}'\n"
            "     This creates .sortie/storage.db if it doesn't exist. Pilots write debriefs here.\n\n"
            "  2. Check Headroom proxy status:\n"
            "     Run: curl -sf http://localhost:8787/health\n"
            "     - If healthy: it's running, pilots will route through it automatically.\n"
            "     - If down: launch it with: headroom proxy --port 8787 &\n"
            "       (It should have started with Tower — if not, something went wrong at launch.)\n\n"
            "  3. Show token savings baseline:\n"
            f"     bash '{Path(__file__).parent / 'xo-tools.sh'}' token-savings\n"
            "     Report the numbers to the Air Boss in your sitrep.\n\n"
            "  4. Check Serena is per-worktree (not project-level):\n"
            f"     Verify {ctx._project_dir}/.mcp.json does NOT have a 'serena' entry.\n"
            "     Each pilot's worktree has its own Serena pointed at that worktree path.\n\n"
            "STEP 1 — SITUATION AWARENESS:\n"
            "  Check open worktrees — what's in progress, anything stale?\n\n"
            "STEP 2 — MISSION INTEL:\n"
            "  Use mcp__linear__list_issues to fetch Todo/In Progress tickets assigned to me.\n\n"
            "STEP 3 — POPULATE QUEUE:\n"
            f"  Write each ticket as a JSON mission file to {ctx._project_dir}/.sortie/mission-queue/ "
            "(mkdir -p first). This populates the dashboard's mission queue.\n\n"
            "STEP 4 — SITREP:\n"
            "  Give a brief sitrep — 5-10 lines max. End with a /tq suggestion: "
            "'/tq <ticket> to get the next one on deck.'"
        )
