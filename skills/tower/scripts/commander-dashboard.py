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
from status_observer import derive_status
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
    _FLIGHT_STATUS_MAX_AGE,
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

        # Tower heartbeat — /tq checks this to verify Tower is alive
        self._write_heartbeat()
        self.set_interval(10.0, self._write_heartbeat)

        # Animation timers
        self.set_interval(1.0, self._toggle_bingo)
        self.set_interval(2.0, self._toggle_condition)
        self.set_interval(2.0, self._refresh_ui)
        self.set_interval(10.0, self._check_idle_agents)
        self.set_interval(60.0, self._check_pipeline_heartbeats)  # pipeline sub-agent timeout

        # Init Air Boss header + spawn immediately (claims first Pit Boss pane)
        self._init_airboss()
        self._spawn_airboss()

        # Preflight: check RTK token optimizer
        self._check_rtk()

        # Sync existing worktree agents on startup
        self._sync_legacy_agents()
        self._start_watchers()

        # Restore active pipeline states from checkpoint files
        try:
            self._restore_pipeline_states()
        except Exception:
            pass

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

        # Remove heartbeat so /tq knows Tower is gone
        try:
            self._cleanup_heartbeat()
        except Exception:
            pass

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
        """Open a bare Claude session in the worktree — reads progress, asks what's next."""
        pilot = self._get_selected_pilot()
        if not pilot:
            self._add_radio("PRI-FLY", "No pilot selected", "error")
            return
        if not pilot.worktree_path:
            self._add_radio("PRI-FLY", f"{pilot.callsign} has no worktree", "error")
            return
        if pilot.callsign in self._iterm_panes:
            self._add_radio("PRI-FLY", f"{pilot.callsign} already has an active pane", "error")
            return
        self._iterm_bridge.resume_agent_pane(pilot)

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
        if pilot.status != "IN_FLIGHT":
            self._add_radio("PRI-FLY", f"{pilot.callsign} is {pilot.status} — not in flight", "error")
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
            if pilot.status == "IN_FLIGHT" and pilot.fuel_pct <= 30:
                cs = pilot.callsign
                if cs not in self._reconciler.bingo_notified:
                    self._reconciler.bingo_notified.add(cs)
                    self._add_radio(cs, f"BINGO FUEL — {pilot.fuel_pct}% remaining", "error")

    # ── Pipeline handoff ──────────────────────────────────────────

    def _check_pipeline_handoff(self, recovered_pilot) -> None:
        """When a pilot lands RECOVERED, handle merge-back + pipeline progression.

        Three things happen:
        1. If sub-agent: merge their branch back to parent, notify siblings
        2. If all agents at this pipeline_seq are done (fan-in): deploy next seq
        3. Next seq may be a single mission (sequential) or multiple (fan-out)
        """
        import threading

        tid = recovered_pilot.ticket_id
        mission = self._mission_queue.get(tid)
        if not mission or not mission.pipeline_id:
            return

        # Mark this mission complete + checkpoint state
        self._mission_queue.mark_complete(tid)
        self._save_pipeline_state(mission.pipeline_id)

        # ── Step 1: Merge-back + sibling notification (fan-out) ──────
        if mission.parent_ticket and recovered_pilot.worktree_path:
            self._merge_back_to_parent(recovered_pilot, mission)

        # ── Step 2: Fan-in gate — are all siblings at this seq done? ──
        if not self._mission_queue.seq_complete(mission.pipeline_id, mission.pipeline_seq):
            remaining = [
                m for m in self._mission_queue.siblings_at_seq(mission.pipeline_id, mission.pipeline_seq)
                if m.status not in ("COMPLETE", "DEPLOYED", "FAILED")
            ]
            names = ", ".join(m.id for m in remaining)
            self._add_radio(
                "PRI-FLY",
                f"HOLDING — waiting for siblings at seq {mission.pipeline_seq}: {names}",
                "system",
            )
            return

        # ── Step 2b: Check for failures + apply on_failure policy ────
        failures = self._mission_queue.seq_has_failures(mission.pipeline_id, mission.pipeline_seq)
        if failures:
            # Determine policy from the pipeline (use first mission's on_failure as pipeline default)
            all_at_seq = self._mission_queue.siblings_at_seq(mission.pipeline_id, mission.pipeline_seq)
            policy = all_at_seq[0].on_failure if all_at_seq else "abort"

            failed_names = ", ".join(f.id for f in failures)

            if policy == "abort":
                self._add_radio(
                    "PRI-FLY",
                    f"PIPELINE ABORT — {failed_names} failed at seq {mission.pipeline_seq}",
                    "error",
                )
                # Compensating transaction: revert merge commits from this seq
                self._compensate_failed_seq(mission.pipeline_id, mission.pipeline_seq)
                self._save_pipeline_state(mission.pipeline_id)
                return

            elif policy == "retry":
                # Re-queue the failed missions for another attempt
                for f in failures:
                    f.status = "ON_DECK"
                    self._add_radio("PRI-FLY", f"RETRY — re-queuing {f.id}", "system")
                self._save_pipeline_state(mission.pipeline_id)
                return  # Will re-deploy on next check cycle

            elif policy == "continue":
                self._add_radio(
                    "PRI-FLY",
                    f"CONTINUING — {failed_names} failed but on_failure=continue, proceeding to next seq",
                    "system",
                )
                # Fall through to deploy next seq with partial results

        self._add_radio(
            "PRI-FLY",
            f"FAN-IN — all agents at seq {mission.pipeline_seq} complete",
            "success",
        )

        # ── Step 3: Deploy next seq (may be 1 mission or many) ───────
        next_seq_missions = self._mission_queue.ready_to_fan_out(
            mission.pipeline_id,
            mission.pipeline_seq + 1,
        )
        if not next_seq_missions:
            self._add_radio("PRI-FLY", f"PIPELINE COMPLETE — {mission.pipeline_id}", "success")
            _play_sound("squadron_complete")
            _notify("USS TENKARA", f"Pipeline {mission.pipeline_id} complete")
            self._save_pipeline_state(mission.pipeline_id)
            return

        # Build handoff context from all completed agents at current seq
        progress_context = self._gather_seq_progress(mission.pipeline_id, mission.pipeline_seq)

        for next_mission in next_seq_missions:
            handoff_header = (
                f"## Pipeline Handoff — Stage {next_mission.pipeline_seq}\n\n"
                f"All agents at stage {mission.pipeline_seq} are complete.\n"
            )
            if failures and mission.on_failure == "continue":
                failed_names = ", ".join(f.id for f in failures)
                handoff_header += (
                    f"\n**WARNING:** The following agents at the previous stage FAILED: {failed_names}\n"
                    f"Their work may be incomplete. Check carefully before building on it.\n"
                )
            if progress_context:
                handoff_header += (
                    f"\n### Previous Stage Progress\n"
                    f"```\n{progress_context[:3000]}\n```\n\n"
                )
            handoff_header += "---\n\n"

            next_mission.spec_content = handoff_header + next_mission.spec_content
            next_mission.status = "DEPLOYING"

            if len(next_seq_missions) > 1:
                self._add_radio(
                    "PRI-FLY",
                    f"FAN-OUT — deploying {next_mission.id} ({next_mission.sub_name or next_mission.title})",
                    "success",
                )
            else:
                self._add_radio(
                    "PRI-FLY",
                    f"HANDOFF — deploying stage {next_mission.pipeline_seq}: {next_mission.title}",
                    "success",
                )

            deploy_args = [next_mission.id, "--model", next_mission.model]
            self._dispatcher.cmd_deploy(deploy_args)

        if len(next_seq_missions) > 1:
            _play_sound("recovered")
            _notify("USS TENKARA — FAN-OUT", f"{len(next_seq_missions)} agents deploying at seq {next_seq_missions[0].pipeline_seq}")

        # Checkpoint after deploying next stage
        self._save_pipeline_state(mission.pipeline_id)

    def _merge_back_to_parent(self, recovered_pilot, mission) -> None:
        """Merge a sub-agent's branch back to the parent ticket's branch.

        Uses a fencing lock (.sortie/merge.lock) to serialize concurrent merges.
        After merging, writes pull-parent.json to each active sibling's worktree.
        """
        import subprocess as _sp

        parent_ticket = mission.parent_ticket
        parent_pilots = self._roster.get_by_ticket(parent_ticket)
        parent_pilot = parent_pilots[0] if parent_pilots else None
        parent_branch = f"sortie/{parent_ticket}"

        if not recovered_pilot.worktree_path:
            return

        # Determine the sub-agent's branch name
        try:
            result = _sp.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=10,
                cwd=recovered_pilot.worktree_path,
            )
            sub_branch_name = result.stdout.strip()
        except Exception:
            self._add_radio("PRI-FLY", f"Could not determine branch for {recovered_pilot.callsign}", "error")
            return

        merge_cwd = parent_pilot.worktree_path if parent_pilot and parent_pilot.worktree_path else self._project_dir

        # ── Fencing lock — serialize concurrent merge-backs ──────────
        # Prevents race when two siblings finish simultaneously and both
        # try to merge. The lock file contains the callsign of the holder.
        lock_path = Path(merge_cwd) / ".sortie" / "merge.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        # Spin-wait with timeout (max 30s)
        lock_start = time_mod.time()
        while lock_path.exists():
            if time_mod.time() - lock_start > 30:
                self._add_radio("PRI-FLY", f"MERGE TIMEOUT — lock held too long, forcing", "error")
                lock_path.unlink(missing_ok=True)
                break
            time_mod.sleep(0.5)

        try:
            lock_path.write_text(recovered_pilot.callsign)

            result = _sp.run(
                ["git", "merge", sub_branch_name, "--no-edit",
                 "-m", f"Merge {recovered_pilot.callsign} ({sub_branch_name}) into {parent_branch}"],
                capture_output=True, text=True, timeout=30,
                cwd=merge_cwd,
            )
            if result.returncode == 0:
                self._add_radio(
                    "PRI-FLY",
                    f"MERGED — {recovered_pilot.callsign} → {parent_branch}",
                    "success",
                )
            else:
                self._add_radio(
                    "PRI-FLY",
                    f"MERGE CONFLICT — {recovered_pilot.callsign} → {parent_branch}: {result.stderr[:100]}",
                    "error",
                )
                return  # Don't notify siblings if merge failed
        except Exception as e:
            self._add_radio("PRI-FLY", f"Merge failed: {e}", "error")
            return
        finally:
            lock_path.unlink(missing_ok=True)

        # Notify active siblings via filesystem signal
        active_sibs = self._mission_queue.active_siblings(
            mission.pipeline_id, mission.pipeline_seq, exclude_id=mission.id,
        )
        for sib_mission in active_sibs:
            sib_pilots = self._roster.get_by_ticket(sib_mission.id)
            for sib_pilot in sib_pilots:
                if sib_pilot.worktree_path and sib_pilot.status == "IN_FLIGHT":
                    self._write_pull_signal(sib_pilot, recovered_pilot, parent_branch)

    def _write_pull_signal(self, target_pilot, merged_pilot, parent_branch: str) -> None:
        """Write pull-parent.json into a sibling's worktree.

        The agent's directive tells it to watch for this file and pull when it appears.
        """
        signal_path = Path(target_pilot.worktree_path) / ".sortie" / "pull-parent.json"
        try:
            signal_path.write_text(json.dumps({
                "merged_by": merged_pilot.callsign,
                "branch": parent_branch,
                "message": f"{merged_pilot.callsign} merged their work. Run: git pull origin {parent_branch}",
                "timestamp": int(time_mod.time()),
            }, indent=2))
            self._add_radio(
                target_pilot.callsign,
                f"PULL SIGNAL — {merged_pilot.callsign} merged, pull from {parent_branch}",
                "system",
            )
        except OSError as e:
            log.warning("Failed to write pull signal for %s: %s", target_pilot.callsign, e)

    def _compensate_failed_seq(self, pipeline_id: str, seq: int) -> None:
        """Compensating transaction: revert merge commits from failed pipeline seq.

        When on_failure=abort and a seq has failures, we revert any merge commits
        that siblings at this seq made to the parent branch. This prevents partial
        work from contaminating the parent.

        Inspired by the saga pattern — each step has a compensating action.
        """
        import subprocess as _sp

        # Find the parent ticket for this pipeline
        missions_at_seq = self._mission_queue.siblings_at_seq(pipeline_id, seq)
        parent_ticket = ""
        for m in missions_at_seq:
            if m.parent_ticket:
                parent_ticket = m.parent_ticket
                break
        if not parent_ticket:
            return  # No parent — nothing to revert

        parent_pilots = self._roster.get_by_ticket(parent_ticket)
        parent_pilot = parent_pilots[0] if parent_pilots else None
        merge_cwd = parent_pilot.worktree_path if parent_pilot and parent_pilot.worktree_path else self._project_dir

        # Find completed (merged) missions at this seq — those are the ones to revert
        completed = [m for m in missions_at_seq if m.status == "COMPLETE"]
        if not completed:
            return  # Nothing was merged, nothing to revert

        # Revert the merge commits (most recent first)
        # Each merge commit message contains the callsign, so we can find them
        for m in reversed(completed):
            pilots = self._roster.get_by_ticket(m.id)
            for pilot in pilots:
                try:
                    # Find the merge commit by message pattern
                    result = _sp.run(
                        ["git", "log", "--oneline", "--grep",
                         f"Merge {pilot.callsign}", "-1", "--format=%H"],
                        capture_output=True, text=True, timeout=10,
                        cwd=merge_cwd,
                    )
                    commit_hash = result.stdout.strip()
                    if not commit_hash:
                        continue

                    # Revert it
                    revert_result = _sp.run(
                        ["git", "revert", "--no-edit", "-m", "1", commit_hash],
                        capture_output=True, text=True, timeout=30,
                        cwd=merge_cwd,
                    )
                    if revert_result.returncode == 0:
                        self._add_radio(
                            "PRI-FLY",
                            f"REVERTED — {pilot.callsign} merge undone on parent branch",
                            "system",
                        )
                    else:
                        self._add_radio(
                            "PRI-FLY",
                            f"REVERT FAILED — {pilot.callsign}: {revert_result.stderr[:80]}",
                            "error",
                        )
                except Exception as e:
                    self._add_radio("PRI-FLY", f"Revert error for {pilot.callsign}: {e}", "error")

    def _gather_seq_progress(self, pipeline_id: str, seq: int) -> str:
        """Gather progress.md content from all agents at a given pipeline seq."""
        parts = []
        for mission in self._mission_queue.siblings_at_seq(pipeline_id, seq):
            pilots = self._roster.get_by_ticket(mission.id)
            for pilot in pilots:
                if pilot.worktree_path:
                    progress_file = Path(pilot.worktree_path) / ".sortie" / "progress.md"
                    try:
                        if progress_file.exists():
                            content = progress_file.read_text(encoding="utf-8").strip()
                            if content:
                                label = mission.sub_name or mission.id
                                parts.append(f"=== {pilot.callsign} ({label}) ===\n{content}")
                    except OSError:
                        pass
        return "\n\n".join(parts)

    # ── Pipeline state checkpoint (survives Tower restart) ─────────

    def _save_pipeline_state(self, pipeline_id: str) -> None:
        """Write pipeline progress to disk so Tower can resume after crash.

        Writes to .sortie/pipeline-state.json in the project dir.
        On restart, _restore_pipeline_state() reads it back.
        """
        missions = [
            m for m in self._mission_queue.all_missions()
            if m.pipeline_id == pipeline_id
        ]
        if not missions:
            return

        state = {
            "pipeline_id": pipeline_id,
            "updated_at": int(time_mod.time()),
            "missions": [
                {
                    "id": m.id,
                    "title": m.title,
                    "status": m.status,
                    "pipeline_seq": m.pipeline_seq,
                    "sub_name": m.sub_name,
                    "parent_ticket": m.parent_ticket,
                    "model": m.model,
                }
                for m in sorted(missions, key=lambda x: (x.pipeline_seq, x.id))
            ],
        }

        state_dir = Path(self._project_dir) / ".sortie"
        state_dir.mkdir(parents=True, exist_ok=True)
        state_file = state_dir / f"pipeline-{pipeline_id}.json"
        try:
            # Atomic write
            tmp = state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2))
            tmp.rename(state_file)
        except OSError as e:
            log.warning("Failed to save pipeline state for %s: %s", pipeline_id, e)

    def _restore_pipeline_states(self) -> None:
        """On startup, restore active pipeline states from checkpoint files.

        Called from on_mount after initial sync.
        """
        state_dir = Path(self._project_dir) / ".sortie"
        if not state_dir.is_dir():
            return
        for f in state_dir.glob("pipeline-*.json"):
            try:
                state = json.loads(f.read_text(encoding="utf-8"))
                pid = state.get("pipeline_id", "")
                if not pid:
                    continue
                # Only restore if pipeline has incomplete missions
                missions = state.get("missions", [])
                has_incomplete = any(m["status"] not in ("COMPLETE",) for m in missions)
                if has_incomplete:
                    self._add_radio("PRI-FLY", f"PIPELINE RESTORED — {pid} (from checkpoint)", "system")
            except (OSError, json.JSONDecodeError):
                pass

    # ── Sub-agent heartbeat timeout (prevents infinite fan-in wait) ──

    _PIPELINE_HEARTBEAT_TIMEOUT = 600  # 10 minutes without progress.md update = stale

    def _check_pipeline_heartbeats(self) -> None:
        """Check if any pipeline sub-agents have gone silent.

        If a sub-agent's progress.md hasn't been updated in 10 minutes and
        they're IN_FLIGHT, mark them RECOVERED so the fan-in gate doesn't wait forever.
        Called from the idle check timer.
        """
        now = time_mod.time()
        for mission in self._mission_queue.all_missions():
            if not mission.pipeline_id or mission.status != "ACTIVE":
                continue
            pilots = self._roster.get_by_ticket(mission.id)
            for pilot in pilots:
                if pilot.status != "IN_FLIGHT" or not pilot.worktree_path:
                    continue
                # Check progress.md mtime as heartbeat
                progress_file = Path(pilot.worktree_path) / ".sortie" / "progress.md"
                try:
                    if progress_file.exists():
                        age = now - progress_file.stat().st_mtime
                        if age > self._PIPELINE_HEARTBEAT_TIMEOUT:
                            pilot.status = "RECOVERED"
                            self._add_radio(
                                pilot.callsign,
                                f"PIPELINE TIMEOUT — no progress update in {int(age)}s",
                                "error",
                            )
                except OSError:
                    pass

    # ── Tower heartbeat ─────────────────────────────────────────────

    def _write_heartbeat(self) -> None:
        """Touch the heartbeat file so /tq can verify Tower is alive.

        Written every 10s. /tq considers Tower dead if heartbeat > 30s old.
        """
        try:
            hb_path = Path("/tmp/uss-tenkara/_prifly/tower_heartbeat")
            hb_path.parent.mkdir(parents=True, exist_ok=True)
            hb_path.touch()
        except OSError:
            pass

    def _cleanup_heartbeat(self) -> None:
        """Remove heartbeat file on clean exit so /tq doesn't see a stale Tower."""
        try:
            Path("/tmp/uss-tenkara/_prifly/tower_heartbeat").unlink(missing_ok=True)
        except OSError:
            pass

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

                    # Pipeline handoff — auto-deploy next mission when agent lands
                    if new_status == "RECOVERED" and pilot.ticket_id:
                        self._check_pipeline_handoff(pilot)

                    # Pipeline failure — mark mission FAILED, trigger on_failure policy
                    if new_status == "RECOVERED" and pilot.ticket_id:
                        pipeline_mission = self._mission_queue.get(pilot.ticket_id)
                        if pipeline_mission and pipeline_mission.pipeline_id:
                            # Check if this was an abnormal recovery (no session-ended)
                            pass
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
            airborne = sum(1 for p in pilots if p.status == "IN_FLIGHT")
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
        airborne = sum(1 for p in self._roster.all_pilots() if p.status == "IN_FLIGHT")
        recovered = sum(1 for p in self._roster.all_pilots() if p.status == "RECOVERED")
        self.title = f"USS TENKARA PRI-FLY — {airborne} IN FLIGHT | {recovered} RECOVERED"

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
                if status in ("RECOVERED", "ON_DECK"):
                    _key("R", "Resume", "bold green")
                if status == "IN_FLIGHT":
                    _key("X", "Recall", "bold yellow")
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
