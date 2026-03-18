#!/usr/bin/env python3
"""USS Tenkara PRI-FLY — Full agent orchestration TUI.

Commander dashboard for spawning, monitoring, and communicating with
Claude agents via stream-json protocol. You are the Air Boss.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import subprocess
import sys
import time as time_mod
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add lib to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from agent_manager import AgentManager, AgentProcess, StreamEvent
from sdk_bridge import SdkAgentManager, SdkAgent, AgentEvent, sdk_available
from inline_sentinel import InlineSentinel
from squadron_analyst import SquadronAnalyst
from status_observer import derive_status, narrate_transition
from pilot_roster import Pilot, PilotRoster, generate_personality_briefing, derive_mood, get_mini_boss_quote, get_pilot_launch_quote
from mission_queue import Mission, MissionQueue
from linear_bridge import (
    LinearTicket, is_ticket_id, fetch_ticket, list_issues as linear_list_issues,
    priority_label, priority_style,
)
from flight_ops import FlightOpsStrip
from read_sortie_state import read_sortie_state, AgentState
from parse_jsonl_metrics import JsonlMetrics, encode_project_path, CLAUDE_PROJECTS_DIR

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

import re as _re

from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button, DataTable, Footer, Input, RichLog, Static, TabbedContent, TabPane, TextArea,
)
from textual.worker import Worker, WorkerState

log = logging.getLogger(__name__)

# ── Constants (imported from lib/constants.py) ────────────────────────
from constants import (
    STATUS_ICONS, STATUS_COLORS, STATUS_SORT_ORDER, SOUNDS,
    _LEGACY_STATUS_MAP, _FLIGHT_STATUS_MAP,
    _FLIGHT_STATUS_MAX_AGE, _FUEL_JUMP_THRESHOLD, _SAR_RECOVERY_DELAY, _AAR_RECOVERY_DELAY,
)


# ── Status engine (imported from lib/status_engine.py) ────────────────
from status_engine import (
    _play_sound, _notify, _ctx_remaining, _map_flight_status,
    _flight_status_is_stale, _clear_flight_status, _derive_legacy_status,
    StatusReconciler, StatusTransition, validate_transition,
)


# ── _WorktreeFileHandler imported from scripts/monitoring.py ──────────


# ── Rendering helpers (imported from lib/rendering.py) ────────────────
from rendering import (
    fuel_gauge, _format_tokens, _format_elapsed,
    _tool_icon, _guess_lang_from_path,
    _render_assistant_content, _render_prose, _append_inline_code, _render_tool_detail,
)


# ── Screens (imported from scripts/screens.py) ───────────────────────
from screens import SplashScreen, ConfirmScreen, BriefingScreen, DeployInputScreen, LinearBrowseScreen

# ── Commands (imported from scripts/commands.py) ─────────────────────
from commands import CommandDispatcher

# ── Air Boss (imported from scripts/airboss.py) ──────────────────────
from airboss import AirBoss

# ── iTerm bridge (imported from lib/iterm_bridge.py) ─────────────────
from iterm_bridge import ItermBridge

# ── Monitoring (imported from scripts/monitoring.py) ─────────────────
from monitoring import Monitoring, _WorktreeFileHandler


# ── Widgets (imported from scripts/widgets.py) ───────────────────────
from widgets import PriFlyHeader, ChatInput, ChatPane, MissionQueuePanel, RadioChatter, DeckStatus, refresh_board_table

# ── Legacy sync (imported from scripts/legacy_sync.py) ──────────────
from legacy_sync import LegacySync

# ── Agent events (imported from scripts/agent_events.py) ────────────
from agent_events import AgentEventHandler

# ── Actions (imported from scripts/actions.py) ──────────────────────
from actions import Actions


# ── Main PRI-FLY App ────────────────────────────────────────────────

class PriFlyCommander(App):
    """USS Tenkara PRI-FLY — Agent Orchestration Commander."""

    CSS = """
    Screen { background: $surface; }

    #header-bar {
        dock: top; height: 2;
        background: $surface-darken-2;
        padding: 0 1;
    }

    #alert-bar {
        dock: top; height: auto; max-height: 2;
        background: $error; color: $text;
        text-align: center; display: none;
    }
    #alert-bar.visible { display: block; }

    #select-mode-banner {
        height: 1; background: #e5c07b; color: #282c34;
        text-align: center; text-style: bold;
    }
    #select-mode-banner.hidden { display: none; }

    #flight-strip {
        height: auto;
        max-height: 10;
        background: $surface-darken-2;
    }
    #flight-strip.collapsed { height: 0; display: none; }
    .panels-collapsed { height: 0; display: none; }

    #hotkey-bar {
        height: auto;
        max-height: 4;
        background: $surface-darken-2;
        color: $text-muted;
        padding: 0 1;
    }

    #main-content { height: 1fr; }

    /* Default mode: board + queue stacked */
    #board-section { height: 1fr; min-height: 6; }
    #sortie-header {
        height: 1;
        padding: 0 1;
        background: $surface-darken-2;
        color: $text;
    }
    #agent-table { height: 1fr; }

    #queue-section {
        height: auto; max-height: 14;
        border-top: solid $accent;
    }
    #queue-header {
        height: 1; padding: 0 1;
        background: $surface-darken-1;
    }
    #queue-table {
        height: auto; max-height: 12;
    }

    /* Comms: pre-built 2x2 grid with 4 slots */
    #comms-grid {
        display: none;
        height: 1fr;
        width: 1fr;
    }
    #comms-grid.active { display: block; }
    #board-section.compressed { width: 40; }
    .comms-row {
        height: 1fr;
        layout: horizontal;
    }
    .comms-row.empty { display: none; }
    .comms-slot {
        width: 1fr;
        height: 1fr;
    }
    .comms-slot.empty { display: none; }

    #airboss-section {
        height: auto; max-height: 12;
        border-top: solid $accent;
        background: $surface-darken-2;
    }
    #airboss-header {
        height: 1; padding: 0 1;
        background: #2a1a3a;
    }
    #airboss-log {
        height: auto; max-height: 10;
        padding: 0 1;
        overflow-x: hidden;
    }

    #radio-section {
        height: auto; max-height: 10;
        border-top: solid $accent;
        padding: 0 1;
    }

    #prifly-bar {
        height: 1;
        background: $surface-darken-1;
        border-top: solid $accent;
        padding: 0 0;
    }
    #prifly-hint {
        height: 1; padding: 0 1;
        color: $text-muted;
    }

    #deck-status {
        dock: bottom; height: 1;
        background: $surface-darken-1;
        padding: 0 1; color: $text-muted;
    }

    #modal-container {
        align: center middle; width: 80; height: auto;
        border: thick $accent; background: $surface; padding: 1 2;
    }

    ChatPane {
        height: 1fr;
        border-left: solid $accent;
    }
    ChatPane RichLog {
        height: 1fr;
    }
    .chat-input-row {
        dock: bottom;
        height: auto;
        max-height: 10;
    }
    .chat-input-row ChatInput {
        width: 1fr;
    }
    .chat-input-row Button {
        width: 8;
        min-width: 8;
        height: 3;
        margin: 0;
    }
    ChatPane > Static {
        height: auto; max-height: 4;
        padding: 0 1;
        background: $surface-darken-1;
    }
    ChatPane > .chat-close-hint {
        dock: bottom;
        height: 1;
        max-height: 1;
        background: #1a3a5c;
        color: $text;
        text-align: center;
        text-style: bold;
        padding: 0;
    }
    """

    BINDINGS = [
        # All show=False — HUD bar in PriFlyHeader handles display
        Binding("d", "open_comms", "Open Pane", priority=True, show=False),
        Binding("f", "toggle_flight_strip", "Flight", priority=True, show=False),
        Binding("l", "linear_browse", "Linear", priority=True, show=False),
        Binding("b", "open_bullboard", "BullBoard", priority=True, show=False),
        Binding("o", "open_browser", "Browser", priority=True, show=False),
        Binding("p", "open_pr", "PR", priority=True, show=False),
        Binding("r", "resume_selected", "Resume", priority=True, show=False),
        Binding("w", "waveoff_selected", "Wave-off", priority=True, show=False),
        Binding("x", "recall_selected", "Recall", priority=True, show=False),
        Binding("k", "compact_selected", "Compact", priority=True, show=False),
        Binding("v", "start_server", "DevServer", priority=True, show=False),
        Binding("m", "relaunch_miniboss", "Mini Boss", priority=True, show=False),
        Binding("t", "open_terminal", "Terminal", priority=True, show=False),
        Binding("z", "dismiss_selected", "Dismiss", priority=True, show=False),
        Binding("s", "sync_worktrees", "Sync", priority=True, show=False),
        Binding("escape", "focus_board", "Board", show=False),
        Binding("tab", "toggle_focus", "Focus", show=False),
        Binding("q", "quit", "Quit", show=False),
    ]

    # Reactive animation states
    _bingo_blink = reactive(True)
    _condition_pulse = reactive(True)

    def __init__(self, project_dir: Optional[str] = None) -> None:
        super().__init__()
        self._project_dir = project_dir or os.getcwd()
        self._roster = PilotRoster()
        self._mission_queue = MissionQueue()
        self._agent_mgr = AgentManager(
            project_dir=self._project_dir,
            on_event=self._on_agent_event,
            on_exit=self._on_agent_exit,
        )

        # SDK agent manager — in-process agents via Claude Agent SDK
        self._sdk_mgr = SdkAgentManager(
            on_event=self._on_sdk_agent_event,
            on_exit=self._on_sdk_agent_exit,
        ) if sdk_available() else None
        self._sdk_enabled = sdk_available()

        # SDK event batching — accumulate events, apply on UI tick (3s)
        # Prevents sprite flicker from high-frequency SDK event streams
        self._sdk_event_buffer: dict[str, list["AgentEvent"]] = {}  # callsign → pending events
        self._sdk_last_status_update: dict[str, float] = {}  # callsign → last status change time
        self._sdk_status_debounce_secs: float = 1.0  # min seconds between status transitions

        self._radio_log: list[dict] = []
        self._chat_panes: dict[str, ChatPane] = {}
        self._slot_map: dict[int, str] = {}  # slot_index -> callsign
        self._comms_active = False
        self._sorted_pilots: list[Pilot] = []
        self._previous_statuses: dict[str, str] = {}
        self._pending_permissions: dict[str, dict] = {}  # callsign -> pending permission request
        self._select_mode: bool = False
        self._iterm_panes: set[str] = set()  # callsigns with open iTerm2 comms panes

        # Auto-compact settings
        self._auto_compact = False
        self._auto_compact_threshold = 45
        self._auto_compact_idle = 30

        # Legacy agent tracking (worktree-based agents not spawned by us)
        self._legacy_agents: dict[str, AgentState] = {}  # ticket_id -> AgentState
        self._observer: Optional[Observer] = None
        self._watched_jsonl_dirs: set[str] = set()  # JSONL dirs already watched
        self._rtk_active: bool = False
        self._sentinel_pid: Optional[int] = None

        # Inline sentinel — runs classification in-process instead of subprocess
        self._inline_sentinel = InlineSentinel(
            project_dir=self._project_dir,
            on_status_change=self._on_sentinel_status_change,
        )
        self._use_inline_sentinel = True  # toggle to use subprocess sentinel instead

        # Squadron analyst — periodic Haiku assessment of flight deck
        self._analyst = SquadronAnalyst(
            on_assessment=self._on_squadron_assessment,
            interval=120,  # every 2 minutes
        )
        self._analyst.set_snapshot_provider(self._get_pilot_snapshot)

        # Status reconciler — token deltas, fuel jumps, multi-source priority
        self._reconciler = StatusReconciler(stale_threshold=4)

        # Command dispatcher — all /command handlers
        self._dispatcher = CommandDispatcher(self)

        # Air Boss — Mini Boss lifecycle
        self._airboss = AirBoss(self)

        # iTerm bridge — pane management
        self._iterm_bridge = ItermBridge(self)

        # Monitoring — sentinel, watchers, idle checks
        self._monitoring = Monitoring(self)

        # Legacy sync — worktree agent discovery and reconciliation
        self._legacy_sync = LegacySync(self)

        # Agent event handler — SDK + legacy stream event processing
        self._event_handler = AgentEventHandler(self)

        # Actions — hotkey action logic
        self._actions = Actions(self)

        # Air Boss — interactive Claude session in Pit Boss pane (no longer stream-json)
        self._airboss_spawned: bool = False

        # Background sync guard — prevent overlapping read_sortie_state calls
        self._sync_in_progress: bool = False

        # Board dirty tracking — skip table rebuild when state hasn't changed
        self._board_state_sig: str = ""

        # Dismissed tickets — prevents _sync_legacy_agents from re-adding them
        self._dismissed_tickets: set[str] = set()

    def compose(self) -> ComposeResult:
        yield PriFlyHeader(id="header-bar")
        yield Static(
            " ✂ SELECT MODE — Drag to select, Cmd+C to copy, F2 to exit",
            id="select-mode-banner",
            classes="hidden",
        )
        yield Static("", id="alert-bar")
        yield FlightOpsStrip(id="flight-strip")
        yield Static("", id="hotkey-bar")
        yield Horizontal(
            Vertical(
                Static("", id="sortie-header"),
                DataTable(id="agent-table"),
                id="board-section",
            ),
            Vertical(
                Horizontal(
                    Vertical(id="comms-slot-0", classes="comms-slot empty"),
                    Vertical(id="comms-slot-1", classes="comms-slot empty"),
                    id="comms-row-top",
                    classes="comms-row empty",
                ),
                Horizontal(
                    Vertical(id="comms-slot-2", classes="comms-slot empty"),
                    Vertical(id="comms-slot-3", classes="comms-slot empty"),
                    id="comms-row-bot",
                    classes="comms-row empty",
                ),
                id="comms-grid",
            ),
            id="main-content",
        )
        yield Vertical(
            Static("", id="prifly-hint"),
            id="prifly-bar",
        )
        yield Vertical(
            Static("", id="airboss-header"),
            RichLog(id="airboss-log", highlight=True, markup=True, auto_scroll=True, wrap=True),
            id="airboss-section",
        )
        yield MissionQueuePanel(id="queue-section")
        yield RadioChatter(id="radio-section")
        yield DeckStatus(id="deck-status")

    def on_mount(self) -> None:
        # Set up board table
        table = self.query_one("#agent-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns(
            "CALLSIGN", "MISSION", "MODEL", "STATUS", "FUEL", "TIME", "TOOLS",
        )

        # Terminal title
        self.title = "USS TENKARA PRI-FLY"

        # Animation timers
        self.set_interval(1.0, self._toggle_bingo)
        self.set_interval(2.0, self._toggle_condition)
        self.set_interval(2.0, self._refresh_ui)
        self.set_interval(10.0, self._check_idle_agents)

        # Init Air Boss header + spawn immediately (claims first Pit Boss pane)
        self._init_airboss()
        self._spawn_airboss()

        # Preflight: check RTK token optimizer
        self._check_rtk()

        # Sync existing worktree agents on startup
        self._sync_legacy_agents()
        self._start_watchers()

        # Periodic legacy sync (catches agents started outside commander)
        # Runs I/O in background to keep the main thread free
        self.set_interval(5.0, self._sync_legacy_agents)

        # Launch sentinel — either inline (in-process) or subprocess
        if self._use_inline_sentinel:
            self._inline_sentinel.start()
            self._add_radio("PRI-FLY", "SENTINEL — inline classifier online", "system")
        else:
            self._start_sentinel()
            self.set_interval(15.0, self._check_sentinel_health)

        # Start squadron analyst (Haiku-powered flight deck assessment)
        self._analyst.start()
        self._add_radio("PRI-FLY", "ANALYST — Haiku tactical assessment online (2min cycle)", "system")

        # Focus the board table
        self.query_one("#agent-table", DataTable).focus()

        # Initial table render (so agents show immediately)
        self._refresh_ui()

        # Show splash — auto-dismisses when initial sync completes or after 3s
        self._splash: Optional[SplashScreen] = SplashScreen()
        self.push_screen(self._splash)
        self.set_timer(2.5, self._dismiss_splash)

    def on_unmount(self) -> None:
        """Clean up all child processes and watchers.

        Each cleanup step is independently protected so a failure in one
        doesn't prevent the others from running. The TUI must exit cleanly
        even if children are in bad states.
        """
        try:
            self._agent_mgr.shutdown()
        except Exception as e:
            log.warning("Agent manager shutdown error: %s", e)

        try:
            if self._sdk_mgr:
                self._sdk_mgr.shutdown()
        except Exception as e:
            log.warning("SDK agent manager shutdown error: %s", e)

        try:
            if self._observer:
                self._observer.stop()
                self._observer.join(timeout=2)
        except Exception as e:
            log.warning("Observer cleanup error: %s", e)

        # Stop inline sentinel
        try:
            if self._inline_sentinel and self._inline_sentinel.is_alive:
                self._inline_sentinel.stop()
        except Exception:
            pass

        # Stop squadron analyst
        try:
            if self._analyst and self._analyst.is_alive:
                self._analyst.stop()
        except Exception:
            pass

        # Kill sentinel subprocess if we spawned one
        try:
            if self._sentinel_pid:
                os.kill(self._sentinel_pid, 15)  # SIGTERM
        except (ProcessLookupError, PermissionError, OSError):
            pass  # Already dead or can't kill

        # Kill any other orphaned sentinels we didn't spawn
        try:
            import signal
            result = subprocess.run(
                ["pgrep", "-f", "sentinel.py"],
                capture_output=True, text=True, timeout=5,
            )
            for pid_str in result.stdout.strip().splitlines():
                try:
                    os.kill(int(pid_str), signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError, ValueError):
                    pass
        except Exception:
            pass

        # Kill managed dev servers
        try:
            servers_file = Path(self._project_dir) / ".sortie" / "managed-servers.json"
            if servers_file.exists():
                import json as _json
                entries = _json.loads(servers_file.read_text(encoding="utf-8"))
                for entry in entries:
                    pid = entry.get("pid")
                    if pid:
                        try:
                            os.kill(int(pid), 15)
                        except (ProcessLookupError, PermissionError, OSError, ValueError):
                            pass
                servers_file.write_text("[]")
        except Exception:
            pass

        # Clean state files
        try:
            import shutil
            state_dir = Path("/tmp/uss-tenkara/_prifly")
            if state_dir.exists():
                shutil.rmtree(state_dir, ignore_errors=True)
        except Exception:
            pass

        try:
            hb = Path(self._project_dir) / ".sortie" / "sentinel-heartbeat.json"
            if hb.exists():
                hb.unlink()
        except Exception:
            pass

    # ── Legacy agent sync (worktree-based agents) ─────────────────────

    def _sync_legacy_agents(self) -> None:
        self._legacy_sync.sync()

    def _dismiss_splash(self) -> None:
        self._legacy_sync.dismiss_splash()

    def _apply_legacy_state(self, state) -> None:
        self._legacy_sync.apply(state)

    # ── Monitoring (delegated to scripts/monitoring.py) ────────────────

    def _sync_managed_servers(self) -> None:
        self._monitoring.sync_managed_servers()

    def _start_watchers(self) -> None:
        self._monitoring.start_watchers()

    def _start_sentinel(self) -> None:
        self._monitoring.start_sentinel()

    def _check_sentinel_health(self) -> None:
        self._monitoring.check_sentinel_health()

    def _watch_agent_jsonl(self, worktree_path: str) -> None:
        self._monitoring.watch_agent_jsonl(worktree_path)

    # ── Reactive watchers ─────────────────────────────────────────────

    def _toggle_bingo(self) -> None:
        self._bingo_blink = not self._bingo_blink

    def _toggle_condition(self) -> None:
        self._condition_pulse = not self._condition_pulse

    # ── Agent event callbacks (from background threads) ───────────────

    def _on_agent_event(self, callsign: str, event: StreamEvent) -> None:
        """Called from agent reader thread — must use call_from_thread."""
        try:
            self.call_from_thread(self._handle_agent_event, callsign, event)
        except Exception:
            pass

    def _on_agent_exit(self, callsign: str, return_code: int) -> None:
        try:
            self.call_from_thread(self._handle_agent_exit, callsign, return_code)
        except Exception:
            pass

    # ── SDK agent event callbacks ─────────────────────────────────────

    def _get_pilot_snapshot(self) -> list[dict]:
        """Build a snapshot of all pilots for the squadron analyst."""
        pilots = self._roster.all_pilots()
        now = time_mod.time()
        return [
            {
                "callsign": p.callsign,
                "status": p.status,
                "fuel_pct": p.fuel_pct,
                "tool_calls": p.tool_calls,
                "error_count": p.error_count,
                "ticket_id": p.ticket_id,
                "elapsed_mins": (now - p.launched_at) / 60 if p.launched_at > 0 else 0,
            }
            for p in pilots
        ]

    def _on_squadron_assessment(self, assessment: str) -> None:
        """Called from analyst thread with Haiku's tactical assessment."""
        try:
            self.call_from_thread(self._deliver_assessment, assessment)
        except Exception:
            pass

    def _deliver_assessment(self, assessment: str) -> None:
        """Deliver assessment to radio chatter AND XO's airboss log."""
        # Radio chatter — visible on TUI
        self._add_radio("ANALYST", assessment, "system")

        # Airboss log — XO sees it in the Mini Boss section
        try:
            from rich.text import Text as RichText
            airboss_log = self.query_one("#airboss-log", RichLog)
            t = RichText()
            t.append("  ★ ANALYST: ", style="bold magenta")
            t.append(assessment, style="white")
            airboss_log.write(t)
        except Exception:
            pass

    def _on_sentinel_status_change(self, ticket_id: str, old_status: str, new_status: str, phase: str) -> None:
        """Called from inline sentinel thread when an agent's status changes."""
        try:
            self.call_from_thread(
                self._add_radio, "SENTINEL",
                f"{ticket_id}: {old_status} → {new_status} ({phase})", "system",
            )
        except Exception:
            pass

    def _on_sdk_agent_event(self, callsign: str, event: "AgentEvent") -> None:
        """Called from SDK agent thread — must use call_from_thread."""
        try:
            self.call_from_thread(self._handle_sdk_event, callsign, event)
        except Exception:
            pass

    def _on_sdk_agent_exit(self, callsign: str, return_code: int) -> None:
        try:
            self.call_from_thread(self._handle_agent_exit, callsign, return_code)
        except Exception:
            pass

    def _handle_sdk_event(self, callsign: str, event: "AgentEvent") -> None:
        self._event_handler.handle_sdk_event(callsign, event)

    def _handle_agent_event(self, callsign: str, event: StreamEvent) -> None:
        self._event_handler.handle_agent_event(callsign, event)

    def _handle_agent_exit(self, callsign: str, return_code: int) -> None:
        self._event_handler.handle_agent_exit(callsign, return_code)

    # ── Radio log ────────────────────────────────────────────────────

    def _add_radio(self, callsign: str, message: str, entry_type: str = "normal") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._radio_log.append({
            "timestamp": ts,
            "callsign": callsign,
            "message": message,
            "type": entry_type,
        })
        # Keep bounded
        if len(self._radio_log) > 100:
            self._radio_log = self._radio_log[-100:]

    # ── Pri-Fly command parsing ──────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle deploy modal input (Pri-Fly input bar removed)."""
        # Only handle deploy modal inputs — other inputs handled by their screens
        pass

    def _handle_command(self, text: str) -> None:
        self._dispatcher.handle(text)

    def _trigger_compact(self, callsign: str) -> None:
        self._dispatcher.trigger_compact(callsign)

    # ── Air Boss (delegated to scripts/airboss.py) ─────────────────────

    def _check_rtk(self) -> None:
        self._airboss.check_rtk()

    def _init_airboss(self) -> None:
        self._airboss.init_header()

    def _spawn_airboss(self) -> None:
        self._airboss.spawn()

    def _send_to_airboss(self, text: str) -> None:
        self._airboss.send_message(text)

    def _update_airboss_status(self, status: str, style: str) -> None:
        self._airboss.update_status(status, style)

    def _build_sitrep_for_airboss(self) -> str:
        return self._airboss.build_sitrep()

    def _get_worktree_summary(self) -> str:
        return self._airboss.get_worktree_summary()

    # ── Linear integration (delegated to CommandDispatcher) ─────────

    # ── Table cursor events ──────────────────────────────────────────

    def on_data_table_row_highlighted(self, event) -> None:
        """Update keybind hints when cursor moves to a new row."""
        try:
            self._update_keybind_hints()
        except Exception:
            pass

    # ── Actions (keybindings) ────────────────────────────────────────

    def action_toggle_select_mode(self) -> None:
        self._actions.toggle_select_mode()

    def action_open_comms(self) -> None:
        pilot = self._get_selected_pilot()
        if not pilot:
            return
        self._iterm_panes.discard(pilot.callsign)
        self._iterm_bridge.open_agent_pane(pilot)

    # ── iTerm bridge (delegated to lib/iterm_bridge.py) ────────────────

    def _open_iterm_comms(self, callsign: str) -> None:
        self._iterm_bridge.open_comms(callsign)

    def _open_agent_pane(self, pilot) -> None:
        self._iterm_bridge.open_agent_pane(pilot)

    def _iterm_pane_cmd(self, callsign: str, cmd: str) -> None:
        self._iterm_bridge.pane_cmd(callsign, cmd)

    # ── Chat pane management (delegated — 60+ lines each) ────────────

    def _open_chat_pane(self, callsign: str) -> None:
        self._actions.open_chat_pane(callsign)

    def _close_chat_pane(self, callsign: str) -> None:
        self._actions.close_chat_pane(callsign)

    # ── View toggles ─────────────────────────────────────────────────

    def action_toggle_flight_strip(self) -> None:
        strip = self.query_one("#flight-strip", FlightOpsStrip)
        strip.toggle_class("collapsed")
        for widget_id in ("#airboss-section", "#queue-section", "#radio-section"):
            try:
                self.query_one(widget_id).toggle_class("panels-collapsed")
            except Exception:
                pass

    async def action_briefing(self) -> None:
        table = self.query_one("#agent-table", DataTable)
        if table.row_count == 0:
            return
        row_idx = table.cursor_row
        if row_idx >= len(self._sorted_pilots):
            return
        await self.push_screen(BriefingScreen(self._sorted_pilots[row_idx]))

    def action_toggle_focus(self) -> None:
        try:
            self.query_one("#agent-table", DataTable).focus()
        except Exception:
            pass

    def action_focus_board(self) -> None:
        try:
            self.query_one("#agent-table", DataTable).focus()
            self._update_keybind_hints()
        except Exception:
            pass

    # ── Selection helpers ────────────────────────────────────────────

    def _is_sdk_agent(self, callsign: str) -> bool:
        return bool(self._sdk_mgr and self._sdk_mgr.get(callsign))

    def _get_selected_pilot(self) -> Optional[Pilot]:
        try:
            table = self.query_one("#agent-table", DataTable)
            if table.row_count == 0:
                return None
            row_idx = table.cursor_row
            if row_idx >= len(self._sorted_pilots):
                return None
            return self._sorted_pilots[row_idx]
        except Exception:
            return None

    # ── Simple hotkey actions (inline — too small to extract) ────────

    def action_deploy(self) -> None:
        def _on_dismiss(result: Optional[tuple[str, str]]) -> None:
            if result:
                ticket, model = result
                self._dispatcher.cmd_deploy([ticket, "--model", model])
        self.push_screen(DeployInputScreen(), callback=_on_dismiss)

    def action_resume_selected(self) -> None:
        pilot = self._get_selected_pilot()
        if not pilot:
            self._add_radio("PRI-FLY", "No pilot selected", "error")
            return
        if pilot.status not in ("RECOVERED", "MAYDAY", "IDLE", "QUEUED"):
            self._add_radio("PRI-FLY", f"{pilot.callsign} is {pilot.status} — can't resume", "error")
            return
        self._dispatcher.cmd_resume([pilot.callsign])

    def action_waveoff_selected(self) -> None:
        pilot = self._get_selected_pilot()
        if not pilot:
            self._add_radio("PRI-FLY", "No pilot selected", "error")
            return
        self._dispatcher.cmd_wave_off([pilot.callsign])

    def action_recall_selected(self) -> None:
        pilot = self._get_selected_pilot()
        if not pilot:
            self._add_radio("PRI-FLY", "No pilot selected", "error")
            return
        if pilot.status != "AIRBORNE":
            self._add_radio("PRI-FLY", f"{pilot.callsign} is {pilot.status} — not airborne", "error")
            return
        self._dispatcher.cmd_recall([pilot.callsign])

    def action_compact_selected(self) -> None:
        pilot = self._get_selected_pilot()
        if not pilot:
            self._add_radio("PRI-FLY", "No pilot selected", "error")
            return
        self._dispatcher.cmd_compact([pilot.callsign])

    def action_sync_worktrees(self) -> None:
        self._sync_legacy_agents()
        self._add_radio("PRI-FLY", "SYNC — scanning worktrees", "system")

    def action_sitrep(self) -> None:
        self._dispatcher.cmd_sitrep()

    def action_open_terminal(self) -> None:
        pilot = self._get_selected_pilot()
        if pilot and pilot.worktree_path:
            target = pilot.worktree_path
            label = f"Terminal opened at {pilot.callsign} worktree"
        else:
            target = self._project_dir
            label = f"Terminal opened at {self._project_dir}"
        self._iterm_pane_cmd("TERMINAL", f"cd '{target}'")
        self._add_radio("PRI-FLY", label, "system")

    def action_relaunch_miniboss(self) -> None:
        if self._airboss_spawned and getattr(self, "_airboss_active", False):
            self._add_radio("MINI BOSS", "Already active — close its pane first to relaunch", "error")
            return
        self._airboss_spawned = False
        self._airboss_active = False
        self._iterm_panes.discard("MINI-BOSS")
        try:
            Path("/tmp/uss-tenkara/_prifly/miniboss-status").unlink(missing_ok=True)
        except OSError:
            pass
        self._spawn_airboss()

    # ── Heavy hotkey actions (delegated to scripts/actions.py) ───────

    def action_dismiss_selected(self) -> None:
        self._actions.dismiss_selected()

    def action_start_server(self) -> None:
        self._actions.start_server()

    def action_linear_browse(self) -> None:
        self._actions.linear_browse()

    def action_open_bullboard(self) -> None:
        self._actions.open_bullboard()

    def action_open_browser(self) -> None:
        self._actions.open_browser()

    def action_open_pr(self) -> None:
        self._actions.open_pr()

    def _extract_server_url(self, pilot) -> str:
        return self._actions.extract_server_url(pilot)

    def _get_github_repo_url(self, cwd: str) -> str:
        return self._actions.get_github_repo_url(cwd)

    def _get_linear_org(self) -> str:
        return self._actions.get_linear_org()

    # ── Idle detection + auto-compact (delegated to monitoring) ────────

    def _check_idle_agents(self) -> None:
        self._monitoring.check_idle_agents()

    # ── Fuel alerts (status is set by derive_status in _refresh_ui) ──

    def _check_fuel_alerts(self) -> None:
        """Check fuel levels and emit bingo warnings. Does NOT set status."""
        for pilot in self._roster.all_pilots():
            if pilot.status == "AIRBORNE" and pilot.fuel_pct <= 30:
                cs = pilot.callsign
                if cs not in self._reconciler.bingo_notified:
                    self._reconciler.bingo_notified.add(cs)
                    _play_sound("bingo")
                    self._add_radio(cs, f"BINGO FUEL — {pilot.fuel_pct}% remaining", "error")
                    _notify("USS TENKARA — BINGO", f"{cs} at {pilot.fuel_pct}%")

    # ── UI refresh ───────────────────────────────────────────────────

    def _refresh_ui(self) -> None:
        # Sync mission queue from .sortie/mission-queue/ directory
        try:
            queue_dir = Path(self._project_dir) / ".sortie" / "mission-queue"
            self._mission_queue.sync_from_dir(queue_dir)
        except Exception:
            pass

        # Sync managed servers from .sortie/managed-servers.json
        try:
            self._sync_managed_servers()
        except Exception:
            pass

        # ── Status observation — single source of truth ──
        # derive_status() reads evidence (JSONL age, session-ended, command.json)
        # and returns the status. Nothing else writes pilot.status for legacy agents.
        try:
            for pilot in self._roster.all_pilots():
                # Skip SDK-managed agents — their event stream handles status
                if self._is_sdk_agent(pilot.callsign):
                    continue
                # Skip if no worktree (can't observe)
                if not pilot.worktree_path:
                    continue
                wt = pilot.worktree_path
                if not Path(wt).is_absolute():
                    wt = str(Path(self._project_dir) / wt)

                old_status = pilot.status
                new_status = derive_status(wt, current_status=old_status)

                if new_status != old_status:
                    pilot.status = new_status
                    self._add_radio(pilot.callsign, f"{old_status} → {new_status}", "system")

                    # Haiku narrator — explain the transition (async, non-blocking)
                    import threading
                    def _narrate(cs=pilot.callsign, old=old_status, new=new_status, w=wt):
                        narrative = narrate_transition(cs, old, new, w)
                        if narrative:
                            try:
                                self.call_from_thread(
                                    self._add_radio, "ANALYST", f"{cs}: {narrative}", "system",
                                )
                            except Exception:
                                pass
                    threading.Thread(target=_narrate, daemon=True).start()
        except Exception as e:
            log.warning("Status observation error: %s", e)

        # Fuel alerts only — status is set by derive_status() above
        try:
            self._check_fuel_alerts()
        except Exception:
            pass

        try:
            self._roster.update_moods()
            self._refresh_table()
            self.query_one("#header-bar", PriFlyHeader).refresh()

            # Update sortie list header with count
            pilots = self._roster.all_pilots()
            airborne = sum(1 for p in pilots if p.status == "AIRBORNE")
            total = len(pilots)
            sortie_hdr = self.query_one("#sortie-header", Static)
            t = Text()
            t.append(" ✈ SORTIE LIST", style="bold bright_white")
            if total > 0:
                t.append(f"  {total} sortie{'s' if total != 1 else ''}", style="grey70")
                if airborne > 0:
                    t.append(f"  •  {airborne} airborne", style="bold green")
            sortie_hdr.update(t)
            self.query_one("#deck-status", DeckStatus).refresh()
            self.query_one("#queue-section", MissionQueuePanel).refresh_queue()
            self.query_one("#radio-section", RadioChatter).refresh()
        except Exception:
            pass  # Don't crash on periodic refresh

        # Update terminal title
        airborne = sum(1 for p in self._roster.all_pilots() if p.status == "AIRBORNE")
        recovered = sum(1 for p in self._roster.all_pilots() if p.status == "RECOVERED")
        self.title = f"USS TENKARA PRI-FLY — {airborne} AIRBORNE | {recovered} RECOVERED"

        # Update flight strip
        try:
            strip = self.query_one("#flight-strip", FlightOpsStrip)
            strip.update_pilots(self._roster.all_pilots())
        except Exception:
            pass

        # Refresh open chat headers
        for callsign, pane in self._chat_panes.items():
            try:
                pane.refresh_header()
            except Exception:
                pass

        # Update context-sensitive keybind hints
        try:
            self._update_keybind_hints()
        except Exception:
            pass

        # Check Mini Boss status via state file
        if self._airboss_spawned:
            try:
                mb_status = Path("/tmp/uss-tenkara/_prifly/miniboss-status").read_text().strip()
                if mb_status == "ACTIVE" and not getattr(self, "_airboss_active", False):
                    self._airboss_active = True
                    self._update_airboss_status("ACTIVE", "bold green")
                    self._add_radio("MINI BOSS", "ACTIVE — standing by for tasking", "success")
                elif mb_status == "OFFLINE" and getattr(self, "_airboss_active", False):
                    self._airboss_active = False
                    self._airboss_spawned = False
                    self._update_airboss_status("OFFLINE", "dim red")
                    self._add_radio("MINI BOSS", "OFFLINE — session ended", "error")
            except OSError:
                # No status file yet — check for pane as fallback
                if not getattr(self, "_airboss_active", False) and "MINI-BOSS" in self._iterm_panes:
                    self._airboss_active = True
                    self._update_airboss_status("ACTIVE", "bold green")

    def _update_keybind_hints(self) -> None:
        """Update the hotkey bar between flight strip and board."""
        hotkey = self.query_one("#hotkey-bar", Static)

        t = Text()

        if self._comms_active:
            # Comms mode — show chat-relevant actions
            t.append(" Ctrl+C", style="bold bright_white")
            t.append(" Close  ", style="grey50")
            t.append("Esc", style="bold bright_white")
            t.append(" Pri-Fly  ", style="grey50")
            t.append("Tab", style="bold bright_white")
            t.append(" Next Chat  ", style="grey50")
            t.append("│ ", style="grey30")
            t.append("Ctrl+Enter", style="bold bright_white")
            t.append(" Send  ", style="grey50")
            t.append("Enter", style="bold bright_white")
            t.append(" Newline  ", style="grey50")
            t.append("│ ", style="grey30")
            t.append("Opt+Drag", style="dim")
            t.append(" Select+Copy", style="grey42")
        else:
            # Board mode — HUD action bar with game-style grouping
            def _key(k: str, label: str, style_key: str = "bold cyan", active: bool = True) -> None:
                if active:
                    t.append(f" [{k}]", style=style_key)
                    t.append(label, style="grey70")
                else:
                    t.append(f" [{k}]", style="grey42")
                    t.append(label, style="grey42")

            def _sep() -> None:
                t.append("  \u2502  ", style="grey30")

            # Get selected pilot context
            pilot = self._get_selected_pilot()
            has_pilot = pilot is not None
            status = pilot.status if has_pilot else None
            has_pane = has_pilot and pilot.callsign in self._iterm_panes
            has_worktree = has_pilot and bool(pilot.worktree_path)
            has_server = has_pilot and bool(self._extract_server_url(pilot))

            # Row 1: Global — workspace | view | system
            t_label = "Worktree" if (has_pilot and has_worktree) else "Terminal"
            _key("T", t_label)
            _key("L", "Linear")
            _key("M", "Boss", "bold magenta")
            _key("S", "Sync")
            _sep()
            _key("F", "Flight")
            _sep()
            _key("Q", "Quit")

            # Row 2: Pilot context — identity | connection + dev | flight ops
            if has_pilot:
                t.append("\n")
                status_style = STATUS_COLORS.get(status, "white")
                t.append(f" {pilot.callsign}", style="bold yellow")
                t.append(f" {status}", style=status_style)
                _sep()
                # Connection group
                _key("D", "Pane", active=not has_pane)
                if has_pane and has_worktree:
                    _key("V", "Server")
                    if has_server:
                        _key("O", "Browser")
                    _key("B", "BullBoard")
                if has_worktree:
                    _key("P", "PR")
                _sep()
                # Flight ops group
                if status in ("RECOVERED", "MAYDAY", "IDLE"):
                    _key("R", "Resume", "bold green")
                if status == "AIRBORNE":
                    _key("X", "Recall", "bold yellow")
                    _key("K", "Compact", "bold cyan")
                if status not in ("RECOVERED",):
                    _key("W", "Wave-off", "bold red")
                _key("Z", "Dismiss")

        hotkey.update(t)

    def _refresh_table(self) -> None:
        refresh_board_table(self)


# ── Entry point ──────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="USS Tenkara PRI-FLY Commander")
    parser.add_argument(
        "--project-dir",
        default=os.environ.get("SORTIE_PROJECT_DIR"),
        help="Project root directory",
    )
    parser.add_argument(
        "--no-splash",
        action="store_true",
        help="Skip splash screen",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    app = PriFlyCommander(project_dir=args.project_dir)
    app.run()


if __name__ == "__main__":
    main()
