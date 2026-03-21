"""USS Tenkara PRI-FLY — command dispatcher.

All /command handlers extracted from commander-dashboard.py.
CommandDispatcher takes the app instance as ctx and dispatches through it.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time as time_mod
import threading
from pathlib import Path
from typing import Optional, TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from linear_bridge import (
    LinearTicket, is_ticket_id, fetch_ticket, priority_label,
)
from mission_queue import Mission
from pilot_roster import generate_personality_briefing
from screens import BriefingScreen, LinearBrowseScreen
from status_engine import _notify


if TYPE_CHECKING:
    from commander_dashboard import PriFlyCommander


class CommandDispatcher:
    """Dispatches Pri-Fly slash commands through the app context.

    All methods receive `ctx` (the PriFlyCommander app) and call through it
    for roster, agent_mgr, mission_queue, radio, screens, and iTerm access.
    """

    def __init__(self, ctx: "PriFlyCommander") -> None:
        self.ctx = ctx

    def handle(self, text: str) -> None:
        ctx = self.ctx
        try:
            parts = shlex.split(text)
        except ValueError:
            ctx._add_radio("PRI-FLY", f"Bad command syntax: {text}", "error")
            return
        cmd = parts[0].lower()
        args = parts[1:]

        dispatch = {
            "/deploy": self.cmd_deploy,
            "/queue": self.cmd_queue,
            "/recall": self.cmd_recall,
            "/wave-off": self.cmd_wave_off,
            "/waveoff": self.cmd_wave_off,
            "/compact": self.cmd_compact,
            "/auto-compact": self.cmd_auto_compact,
            "/autocompact": self.cmd_auto_compact,
            "/sitrep": lambda a: self.cmd_sitrep(),
            "/briefing": self.cmd_briefing,
            "/auto": self.cmd_auto,
            "/rearm": self.cmd_rearm,
            "/resume": self.cmd_resume,
            "/linear": self.cmd_linear,
            "/help": lambda a: self.cmd_help(),
        }

        handler = dispatch.get(cmd)
        if handler:
            handler(args)
        else:
            ctx._add_radio("PRI-FLY", f"Unknown command: {cmd}", "system")

    # ── Deploy ───────────────────────────────────────────────────────

    def cmd_deploy(self, args: list[str]) -> None:
        """Deploy an agent: /deploy <ticket|description> [--model X] [--spec path]"""
        ctx = self.ctx
        if not args:
            ctx._add_radio("PRI-FLY", "Usage: /deploy <ticket|desc> [--model opus|sonnet|haiku]", "system")
            return

        model = "sonnet"
        spec_path = ""
        ticket_or_desc = []

        i = 0
        while i < len(args):
            if args[i] == "--model" and i + 1 < len(args):
                model = args[i + 1]
                i += 2
            elif args[i] == "--spec" and i + 1 < len(args):
                spec_path = args[i + 1]
                i += 2
            else:
                ticket_or_desc.append(args[i])
                i += 1

        identifier = " ".join(ticket_or_desc)
        if not identifier:
            ctx._add_radio("PRI-FLY", "Need a ticket ID or description", "system")
            return

        if is_ticket_id(identifier) and not spec_path:
            ctx._add_radio("PRI-FLY", f"Fetching {identifier} from Linear…", "system")
            self._fetch_linear_ticket_background(identifier, "deploy", model, 2)
            return

        directive = f"Complete the following task:\n\n{identifier}"

        if spec_path:
            try:
                spec_content = Path(spec_path).read_text()
                directive = f"Complete the following task based on this spec:\n\n{spec_content}"
            except OSError as e:
                ctx._add_radio("PRI-FLY", f"Failed to read spec: {e}", "error")
                return

        pilot = ctx._roster.assign(
            ticket_id=identifier,
            model=model,
            mission_title=identifier[:60],
            directive=directive,
        )

        pilot.status = "ON_DECK"
        pilot.launched_at = time_mod.time()

        self._launch_agent(pilot, directive)
        ctx._add_radio("PRI-FLY", f"ON DECK — {pilot.callsign} standing by for {identifier}", "success")
        if getattr(ctx, '_rtk_active', False):
            ctx._add_radio(pilot.callsign, "RTK active — drop tanks fitted, extended range", "system")
        _notify("USS TENKARA", f"{pilot.callsign} on deck for {identifier}")
        ctx._refresh_ui()

    # ── Queue ────────────────────────────────────────────────────────

    def cmd_queue(self, args: list[str]) -> None:
        ctx = self.ctx
        if not args:
            ctx._add_radio("PRI-FLY", "Usage: /queue <ticket|path|desc> [--model X] [--priority 1-3]", "system")
            return

        model = None
        priority = None
        explicit_model = False
        explicit_priority = False
        items = []

        i = 0
        while i < len(args):
            if args[i] == "--model" and i + 1 < len(args):
                model = args[i + 1]
                explicit_model = True
                i += 2
            elif args[i] == "--priority" and i + 1 < len(args):
                priority = int(args[i + 1])
                explicit_priority = True
                i += 2
            else:
                items.append(args[i])
                i += 1

        desc = " ".join(items)
        if is_ticket_id(desc.strip()):
            ticket_id = desc.strip()
            if not explicit_model and not explicit_priority:
                ctx._add_radio("PRI-FLY", f"Fetching {ticket_id} — Mini Boss will triage", "system")
                self._fetch_and_triage_ticket(ticket_id)
            else:
                ctx._add_radio("PRI-FLY", f"Fetching {ticket_id} from Linear…", "system")
                self._fetch_linear_ticket_background(
                    ticket_id, "queue", model or "sonnet", priority or 2,
                )
            return
        elif Path(desc).is_file():
            mission = ctx._mission_queue.add_from_spec(
                desc, model=model or "sonnet", priority=priority or 2,
            )
        else:
            mission = ctx._mission_queue.add_adhoc(
                desc, model=model or "sonnet", priority=priority or 2,
            )

        ctx._add_radio("PRI-FLY", f"QUEUED — {mission.id}: {mission.title[:50]}", "system")
        ctx._refresh_ui()

    # ── Linear background fetch + triage ─────────────────────────────

    def _fetch_and_triage_ticket(self, ticket_id: str) -> None:
        ctx = self.ctx
        def _bg_fetch():
            try:
                ticket = fetch_ticket(ticket_id)
                if ticket:
                    ctx.call_from_thread(self._triage_ticket_with_airboss, ticket)
                else:
                    ctx.call_from_thread(
                        ctx._add_radio, "PRI-FLY",
                        f"Could not fetch {ticket_id} from Linear — queuing as-is", "error",
                    )
                    ctx._mission_queue.add_adhoc(ticket_id, model="sonnet", priority=2)
                    ctx.call_from_thread(ctx._refresh_ui)
            except Exception as e:
                try:
                    ctx.call_from_thread(
                        ctx._add_radio, "PRI-FLY",
                        f"Linear fetch failed for {ticket_id}: {e}", "error",
                    )
                except Exception:
                    pass

        threading.Thread(target=_bg_fetch, daemon=True).start()

    def _triage_ticket_with_airboss(self, ticket: LinearTicket) -> None:
        ctx = self.ctx
        mission = ctx._mission_queue.add_adhoc(
            f"[{ticket.id}] {ticket.title}\n{ticket.description}",
            model="sonnet", priority=2,
        )
        mission.id = ticket.id
        mission.source = "linear"
        ctx._add_radio("PRI-FLY", f"QUEUED — {ticket.id}: {ticket.title[:50]}", "system")
        ctx._refresh_ui()

        triage_msg = (
            f"I just queued ticket {ticket.id}: {ticket.title}\n"
            f"Priority in Linear: {priority_label(ticket.priority)}\n"
            f"State: {ticket.state}\n"
            f"Labels: {', '.join(ticket.labels) if ticket.labels else 'none'}\n"
            f"Description preview: {ticket.description[:200]}\n\n"
            f"Assess: what model should handle this (opus for complex/architectural, "
            f"sonnet for standard features/fixes, haiku for simple/mechanical tasks)? "
            f"What priority (1=urgent, 2=normal, 3=low)? "
            f"Give a one-line tactical assessment."
        )
        ctx._send_to_airboss(triage_msg)

    def _fetch_linear_ticket_background(self, ticket_id: str, action: str, model: str, priority: int) -> None:
        ctx = self.ctx

        def _do_fetch() -> Optional[LinearTicket]:
            return fetch_ticket(ticket_id)

        def _on_done(ticket: Optional[LinearTicket]) -> None:
            if ticket is None:
                ctx._add_radio("PRI-FLY", f"Linear: could not find {ticket_id}", "error")
                ctx._refresh_ui()
                return
            if action == "deploy":
                directive = self._build_linear_directive(ticket)
                pilot = ctx._roster.assign(
                    ticket_id=ticket.id,
                    model=model,
                    mission_title=ticket.title[:60],
                    directive=directive,
                )
                personality = generate_personality_briefing(pilot)
                ctx._agent_mgr.spawn(
                    callsign=pilot.callsign,
                    model=model,
                    directive=directive,
                    personality_prompt=personality,
                )
                pilot.status = "ON_DECK"
                pilot.launched_at = time_mod.time()
                ctx._add_radio("PRI-FLY", f"DECK IDLE — {pilot.callsign} standing by on {ticket.id}: {ticket.title[:40]}", "success")
                _notify("USS TENKARA", f"{pilot.callsign} on deck for {ticket.id}")
                ctx._open_iterm_comms(pilot.callsign)
            else:
                mission = Mission(
                    id=ticket.id,
                    title=ticket.title,
                    source="linear",
                    priority=min(ticket.priority, 3) or priority,
                    directives=[],
                    agent_count=0,
                    model=model,
                    status="ON_DECK",
                    spec_content=ticket.description or ticket.title,
                    created_at=time_mod.time(),
                )
                ctx._mission_queue.add(mission)
                ctx._add_radio("PRI-FLY", f"QUEUED — {ticket.id}: {ticket.title[:50]}", "system")
            ctx._refresh_ui()

        def _worker():
            try:
                ticket = _do_fetch()
                ctx.call_from_thread(_on_done, ticket)
            except Exception as e:
                try:
                    ctx.call_from_thread(
                        ctx._add_radio, "PRI-FLY",
                        f"Linear ticket fetch error: {e}", "error",
                    )
                except Exception:
                    pass

        threading.Thread(target=_worker, daemon=True).start()

    # ── Recall / Wave-off / Kill servers ─────────────────────────────

    def cmd_recall(self, args: list[str]) -> None:
        ctx = self.ctx
        if not args:
            ctx._add_radio("PRI-FLY", "Usage: /recall <callsign>", "system")
            return
        callsign = args[0]
        # Try SDK first, then legacy
        if ctx._sdk_mgr and ctx._sdk_mgr.recall(callsign):
            ctx._add_radio("PRI-FLY", f"RECALL — {callsign} winding down (SDK)", "system")
        elif ctx._agent_mgr.recall(callsign):
            ctx._add_radio("PRI-FLY", f"RECALL — {callsign} winding down", "system")
        else:
            ctx._add_radio("PRI-FLY", f"No active agent: {callsign}", "error")

    def cmd_wave_off(self, args: list[str]) -> None:
        ctx = self.ctx
        if not args:
            ctx._add_radio("PRI-FLY", "Usage: /wave-off <callsign>", "system")
            return
        callsign = args[0]
        try:
            pilot = ctx._roster.get_by_callsign(callsign)
            if pilot:
                self._kill_managed_servers(pilot.ticket_id, callsign)
            # Try SDK first, then legacy
            if ctx._sdk_mgr and ctx._sdk_mgr.wave_off(callsign):
                ctx._add_radio("PRI-FLY", f"WAVE OFF — {callsign} terminated (SDK)", "error")
            elif ctx._agent_mgr.wave_off(callsign):
                ctx._add_radio("PRI-FLY", f"WAVE OFF — {callsign} terminated", "error")
            else:
                ctx._add_radio("PRI-FLY", f"No active agent: {callsign}", "error")
        except Exception as e:
            ctx._add_radio("PRI-FLY", f"Wave-off error: {e}", "error")

    def _kill_managed_servers(self, ticket_id: str, callsign: str) -> None:
        ctx = self.ctx
        servers_file = Path(ctx._project_dir) / ".sortie" / "managed-servers.json"
        try:
            if not servers_file.exists():
                return
            entries = json.loads(servers_file.read_text(encoding="utf-8"))
            if not isinstance(entries, list):
                return

            remaining = []
            killed = 0
            for entry in entries:
                if entry.get("ticket_id") == ticket_id:
                    pid = entry.get("pid")
                    url = entry.get("url", "")
                    if pid:
                        try:
                            os.kill(int(pid), 15)
                            killed += 1
                            ctx._add_radio(callsign, f"Server {url} (pid {pid}) terminated", "system")
                        except (ProcessLookupError, PermissionError, ValueError):
                            pass
                else:
                    remaining.append(entry)

            if killed or len(remaining) != len(entries):
                servers_file.write_text(json.dumps(remaining, indent=2) + "\n")
                if killed:
                    ctx._add_radio("PRI-FLY", f"Killed {killed} server(s) for {callsign}", "system")
        except (json.JSONDecodeError, OSError) as e:
            ctx._add_radio("PRI-FLY", f"Server cleanup error: {e}", "error")

    # ── Compact / Auto-compact ───────────────────────────────────────

    def cmd_compact(self, args: list[str]) -> None:
        ctx = self.ctx
        if not args:
            ctx._add_radio("PRI-FLY", "Usage: /compact <callsign|idle|all>", "system")
            return
        target = args[0].lower()
        if target == "idle":
            idle_pilots = [
                p for p in ctx._roster.all_pilots()
                if p.status == "IN_FLIGHT" and p.fuel_pct < ctx._auto_compact_threshold
                and (time_mod.time() - p.last_tool_at) > ctx._auto_compact_idle
            ]
            for pilot in idle_pilots:
                self.trigger_compact(pilot.callsign)
            ctx._add_radio("PRI-FLY", f"Compacting {len(idle_pilots)} idle agents", "system")
        elif target == "all":
            for pilot in ctx._roster.all_pilots():
                if pilot.status == "IN_FLIGHT":
                    self.trigger_compact(pilot.callsign)
        else:
            self.trigger_compact(target)

    def _launch_agent(self, pilot, directive: str, headless: bool = False) -> None:
        """Launch an agent in an iTerm2 pane (default) or headless via SDK.

        User-initiated deploys (R/D keys) always open iTerm panes so the user
        gets the visual session. SDK headless mode is only for background agents
        (analyst, auto-compact, etc.) — pass headless=True explicitly.
        """
        ctx = self.ctx
        if headless and ctx._sdk_enabled and ctx._sdk_mgr and pilot.worktree_path:
            disallowed = [
                "Bash(git push --force*)", "Bash(git push -f *)",
                "Bash(git reset --hard*)", "Bash(rm *)", "Bash(sudo *)",
            ]
            ctx._sdk_mgr.spawn(
                callsign=pilot.callsign,
                model=pilot.model,
                cwd=str(Path(ctx._project_dir) / pilot.worktree_path) if not Path(pilot.worktree_path).is_absolute() else pilot.worktree_path,
                directive=directive,
                disallowed_tools=disallowed,
            )
            ctx._add_radio(pilot.callsign, "SDK agent launched — headless", "success")
        else:
            ctx._open_agent_pane(pilot)

    def trigger_compact(self, callsign: str) -> None:
        ctx = self.ctx
        pilot = ctx._roster.get_by_callsign(callsign)
        if pilot and pilot.status == "IN_FLIGHT":
            # SDK agents don't support mid-stream injection yet
            if ctx._is_sdk_agent(callsign):
                ctx._add_radio("PRI-FLY", f"COMPACT — {callsign} (SDK compact not yet supported, agent continues)", "system")
                return
            ctx._agent_mgr.inject_message(
                callsign,
                "CIC: Context compaction requested. Summarize your progress, "
                "then continue with refreshed context."
            )
            ctx._add_radio("PRI-FLY", f"AAR — {callsign} refueling", "system")

    def cmd_auto_compact(self, args: list[str]) -> None:
        ctx = self.ctx
        if not args:
            status = "ON" if ctx._auto_compact else "OFF"
            ctx._add_radio("PRI-FLY", f"Auto-compact: {status} (threshold={ctx._auto_compact_threshold}%, idle={ctx._auto_compact_idle}s)", "system")
            return
        if args[0].lower() == "on":
            ctx._auto_compact = True
            for i in range(1, len(args) - 1, 2):
                if args[i] == "--threshold":
                    ctx._auto_compact_threshold = int(args[i + 1])
                elif args[i] == "--idle":
                    ctx._auto_compact_idle = int(args[i + 1].rstrip("s"))
            ctx._add_radio("PRI-FLY", f"Auto-compact ON (threshold={ctx._auto_compact_threshold}%, idle={ctx._auto_compact_idle}s)", "system")
        else:
            ctx._auto_compact = False
            ctx._add_radio("PRI-FLY", "Auto-compact OFF", "system")

    # ── Sitrep / Briefing / Auto / Rearm ─────────────────────────────

    def cmd_sitrep(self) -> None:
        ctx = self.ctx
        for pilot in ctx._roster.all_pilots():
            if pilot.status == "IN_FLIGHT":
                ctx._agent_mgr.inject_message(
                    pilot.callsign,
                    "CIC: SITREP — report current status, progress, and any blockers."
                )
        ctx._add_radio("PRI-FLY", "SITREP requested from all IN FLIGHT", "system")

    def cmd_briefing(self, args: list[str]) -> None:
        ctx = self.ctx
        if not args:
            ctx._add_radio("PRI-FLY", "Usage: /briefing <callsign>", "system")
            return
        callsign = args[0]
        pilot = ctx._roster.get_by_callsign(callsign)
        if pilot:
            ctx.push_screen(BriefingScreen(pilot))
        else:
            ctx._add_radio("PRI-FLY", f"Unknown callsign: {callsign}", "error")

    def cmd_auto(self, args: list[str]) -> None:
        ctx = self.ctx
        if not args:
            status = "ON" if ctx._mission_queue.auto_deploy_enabled else "OFF"
            ctx._add_radio("PRI-FLY", f"Auto-deploy: {status}", "system")
            return
        if args[0].lower() == "on":
            max_concurrent = 3
            if len(args) > 1:
                try:
                    max_concurrent = int(args[1])
                except ValueError:
                    pass
            ctx._mission_queue.set_auto_deploy(True, max_concurrent)
            ctx._add_radio("PRI-FLY", f"Auto-deploy ON (max {max_concurrent})", "system")
        else:
            ctx._mission_queue.set_auto_deploy(False)
            ctx._add_radio("PRI-FLY", "Auto-deploy OFF", "system")

    def cmd_rearm(self, args: list[str]) -> None:
        ctx = self.ctx
        if len(args) < 2:
            ctx._add_radio("PRI-FLY", "Usage: /rearm <callsign> <ticket>", "system")
            return
        callsign = args[0]
        ticket = args[1]
        pilot = ctx._roster.get_by_callsign(callsign)
        if not pilot:
            ctx._add_radio("PRI-FLY", f"Unknown callsign: {callsign}", "error")
            return
        if pilot.status != "RECOVERED":
            ctx._add_radio("PRI-FLY", f"{callsign} not RECOVERED — cannot rearm", "error")
            return
        ctx._roster.remove(callsign)
        self.cmd_deploy([ticket, "--model", pilot.model])

    # ── Resume ───────────────────────────────────────────────────────

    def cmd_resume(self, args: list[str]) -> None:
        ctx = self.ctx
        if not args:
            ctx._add_radio("PRI-FLY", "Usage: /resume <callsign|ticket-id> [--model opus|sonnet|haiku]", "system")
            return

        identifier = args[0]
        model_override = ""
        if len(args) >= 3 and args[1] == "--model":
            model_override = args[2]

        pilot = ctx._roster.get_by_callsign(identifier)

        if not pilot:
            legacy_agent = ctx._legacy_agents.get(identifier)
            if legacy_agent:
                pilots = ctx._roster.get_by_ticket(identifier)
                pilot = pilots[0] if pilots else None

        if not pilot:
            model = model_override or "sonnet"
            pilot = ctx._roster.assign(
                ticket_id=identifier,
                model=model,
                mission_title=f"Resume {identifier}",
                directive="",
            )

        if pilot.callsign in ctx._iterm_panes:
            ctx._add_radio("PRI-FLY", f"{pilot.callsign} already has an active pane", "error")
            return

        model = model_override or pilot.model

        sortie_scripts = Path.home() / ".claude" / "skills" / "sortie" / "scripts"
        worktree_script = sortie_scripts / "create-worktree.sh"
        tid = pilot.ticket_id

        worktree_path = None

        if pilot.worktree_path and Path(pilot.worktree_path).exists():
            worktree_path = pilot.worktree_path

        if not worktree_path:
            legacy_agent = ctx._legacy_agents.get(tid)
            if legacy_agent and legacy_agent.worktree_path and Path(legacy_agent.worktree_path).exists():
                worktree_path = legacy_agent.worktree_path

        if not worktree_path and worktree_script.exists():
            branch_name = f"sortie/{tid}"
            try:
                result = subprocess.run(
                    ["bash", str(worktree_script), tid, branch_name, "dev",
                     "--model", model, "--resume"],
                    capture_output=True, text=True, timeout=30,
                    cwd=ctx._project_dir,
                )
                for line in result.stdout.splitlines():
                    if line.startswith("WORKTREE_CREATED:") or line.startswith("WORKTREE_EXISTS:"):
                        worktree_path = line.split(":", 1)[1]
                        break
            except Exception as e:
                ctx._add_radio("PRI-FLY", f"Worktree setup failed: {e}", "error")
                return

        if not worktree_path:
            ctx._add_radio("PRI-FLY", f"No worktree found for {tid}. Use /deploy instead.", "error")
            return

        directive = (
            f"You are resuming work on {tid}: {pilot.mission_title}.\n"
            f"Worktree: {worktree_path}\n\n"
            "Check git status and git log to understand where the previous agent left off. "
            "Review any uncommitted changes. Then continue the work.\n\n"
            f"Track progress in {worktree_path}/.sortie/progress.md"
        )
        pilot.directive = directive
        pilot.model = model
        pilot.status = "ON_DECK"
        pilot.launched_at = time_mod.time()

        self._launch_agent(pilot, directive)
        ctx._add_radio("PRI-FLY", f"ON DECK — {pilot.callsign} resuming in {worktree_path}", "success")
        _notify("USS TENKARA", f"{pilot.callsign} on deck — resuming")
        ctx._refresh_ui()

    # ── Linear browse ────────────────────────────────────────────────

    def cmd_linear(self, args: list[str]) -> None:
        ctx = self.ctx
        filters: dict = {}
        i = 0
        while i < len(args):
            if args[i] == "--team" and i + 1 < len(args):
                filters["team"] = args[i + 1]
                i += 2
            elif args[i] == "--state" and i + 1 < len(args):
                filters["state"] = args[i + 1]
                i += 2
            elif args[i] == "--assignee" and i + 1 < len(args):
                filters["assignee"] = args[i + 1]
                i += 2
            elif args[i] == "--project" and i + 1 < len(args):
                filters["project"] = args[i + 1]
                i += 2
            else:
                i += 1

        ctx._add_radio("PRI-FLY", "Opening Linear mission intel…", "system")
        ctx.push_screen(
            LinearBrowseScreen(filters=filters),
            callback=self._handle_linear_selection,
        )

    def _handle_linear_selection(self, result: Optional[list[LinearTicket]]) -> None:
        ctx = self.ctx
        if not result:
            return
        for ticket in result:
            deploy_now = getattr(ticket, "_deploy", False)
            if deploy_now:
                directive = self._build_linear_directive(ticket)
                pilot = ctx._roster.assign(
                    ticket_id=ticket.id,
                    model="sonnet",
                    mission_title=ticket.title[:60],
                    directive=directive,
                )
                personality = generate_personality_briefing(pilot)
                ctx._agent_mgr.spawn(
                    callsign=pilot.callsign,
                    model="sonnet",
                    directive=directive,
                    personality_prompt=personality,
                )
                pilot.status = "ON_DECK"
                pilot.launched_at = time_mod.time()
                ctx._add_radio("PRI-FLY", f"DECK IDLE — {pilot.callsign} standing by on {ticket.id}: {ticket.title[:40]}", "success")
                _notify("USS TENKARA", f"{pilot.callsign} on deck for {ticket.id}")
                ctx._open_iterm_comms(pilot.callsign)
            else:
                mission = Mission(
                    id=ticket.id,
                    title=ticket.title,
                    source="linear",
                    priority=min(ticket.priority, 3) or 2,
                    directives=[],
                    agent_count=0,
                    model="sonnet",
                    status="ON_DECK",
                    spec_content=ticket.description or ticket.title,
                    created_at=time_mod.time(),
                )
                ctx._mission_queue.add(mission)
                ctx._add_radio("PRI-FLY", f"QUEUED — {ticket.id}: {ticket.title[:50]}", "system")
        ctx._refresh_ui()

    @staticmethod
    def _build_linear_directive(ticket: LinearTicket) -> str:
        parts = [f"Complete the following Linear ticket:\n"]
        parts.append(f"Ticket: {ticket.id}")
        parts.append(f"Title: {ticket.title}")
        if ticket.state:
            parts.append(f"State: {ticket.state}")
        if ticket.labels:
            parts.append(f"Labels: {', '.join(ticket.labels)}")
        if ticket.description:
            parts.append(f"\nDescription:\n{ticket.description}")
        return "\n".join(parts)

    # ── Help ─────────────────────────────────────────────────────────

    def cmd_help(self) -> None:
        ctx = self.ctx
        commands = [
            "/deploy <ticket> [--model X]  — Launch new agent",
            "/deploy ENG-123              — Fetch from Linear + launch",
            "/resume <callsign>            — Resume legacy agent in its worktree",
            "/queue <desc> [--priority N]  — Add to mission queue",
            "/queue ENG-123               — Fetch from Linear + queue",
            "/linear [--team X]            — Browse Linear issues",
            "/recall <callsign>            — Graceful wind-down",
            "/wave-off <callsign>          — Hard kill",
            "/compact <callsign|idle|all>  — Trigger compaction",
            "/auto-compact on|off          — Auto-compact idle agents",
            "/sitrep                       — Request status from all",
            "/briefing <callsign>          — Show directive",
            "/auto on|off                  — Auto-deploy from queue",
            "/rearm <callsign> <ticket>    — Reassign recovered agent",
        ]
        for cmd in commands:
            ctx._add_radio("PRI-FLY", cmd, "system")
        ctx._refresh_ui()
