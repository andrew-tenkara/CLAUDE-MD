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
from widgets import PriFlyHeader, ChatInput, ChatPane, MissionQueuePanel, RadioChatter, DeckStatus


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
    #agent-table { height: 1fr; }

    #queue-section {
        height: auto; max-height: 8;
        border-top: solid $accent;
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
            RichLog(id="airboss-log", highlight=True, markup=True, auto_scroll=True),
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
        """Spawn a background thread to read sortie state and sync into the pilot roster.

        read_sortie_state() hits the filesystem and spawns git subprocesses — running it
        on the main thread at 5s intervals causes noticeable UI jank. We offload the I/O
        to a daemon thread and apply results on the main thread via call_from_thread.
        """
        import threading
        if self._sync_in_progress:
            return
        self._sync_in_progress = True

        def _bg():
            try:
                state = read_sortie_state(project_dir=self._project_dir)
                self.call_from_thread(self._apply_legacy_state, state)
            except Exception as e:
                log.warning(f"Failed to read sortie state: {e}")
            finally:
                self._sync_in_progress = False

        threading.Thread(target=_bg, daemon=True).start()

    def _dismiss_splash(self) -> None:
        """Dismiss the splash screen if still showing."""
        splash = getattr(self, "_splash", None)
        if splash and not splash._dismissed:
            splash._dismissed = True
            try:
                self.pop_screen()
            except Exception:
                pass
        self._splash = None

    def _apply_legacy_state(self, state) -> None:
        """Apply sortie state to the pilot roster (main thread)."""
        seen_tickets: set[str] = set()
        for agent in state.agents:
            tid = agent.ticket_id
            seen_tickets.add(tid)

            # Skip dismissed agents — user hit Z, don't resurrect
            if tid in self._dismissed_tickets:
                continue

            self._legacy_agents[tid] = agent

            # Skip if this agent was spawned by us (stream-json managed)
            existing_pilot = self._roster.get_by_callsign(
                next(
                    (p.callsign for p in self._roster.get_by_ticket(tid)),
                    "",
                )
            )
            if existing_pilot and self._agent_mgr.get(existing_pilot.callsign):
                continue  # Managed by stream-json — don't overwrite
            if existing_pilot and self._sdk_mgr and self._sdk_mgr.get(existing_pilot.callsign):
                continue  # Managed by SDK — event stream is authoritative

            # Derive commander status from legacy state
            cic_status = _derive_legacy_status(agent)

            # Get or create pilot in roster
            pilots_for_ticket = self._roster.get_by_ticket(tid)
            if pilots_for_ticket:
                pilot = pilots_for_ticket[0]
            else:
                # New legacy agent — register in roster
                # If title == ticket_id, try Linear lookup
                title = agent.title
                # Note: Linear title lookup removed from sync path — it was
                # blocking the main thread with HTTP calls. Title gets populated
                # by the XO or when the user opens a briefing.
                pilot = self._roster.assign(
                    ticket_id=tid,
                    model=agent.model if agent.model not in ("unknown", "Unknown", "") else "sonnet",
                    mission_title=title,
                    directive=f"(legacy worktree agent)\nBranch: {agent.branch}",
                )
                self._add_radio(
                    pilot.callsign,
                    f"DETECTED — {tid}: {title} ({agent.model})",
                    "system",
                )

            # Sync worktree path from legacy state
            if agent.worktree_path and not pilot.worktree_path:
                pilot.worktree_path = agent.worktree_path

            # Register with inline sentinel for JSONL classification
            if self._use_inline_sentinel and agent.worktree_path:
                self._inline_sentinel.add_worktree(tid, agent.worktree_path)

            # Sync telemetry from legacy state
            # Truly unknown agents (no directive at all) — always RECOVERED
            if pilot.mission_title in ("Unknown", "unknown") and pilot.ticket_id in ("Unknown", "unknown"):
                pilot.status = "RECOVERED"
                pilot.mood = derive_mood(pilot)
                continue

            # Token delta tracking (_check_token_deltas) is the authority for
            # IDLE→AIRBORNE and AIRBORNE→ON_APPROACH transitions. Legacy sync
            # only sets status when delta tracking hasn't taken over.
            has_tokens = (
                agent.jsonl_metrics is not None
                and agent.jsonl_metrics.total_tokens > 0
            )
            cs = pilot.callsign
            delta_is_tracking = cs in self._reconciler.prev_tokens and self._reconciler.prev_tokens[cs] > 0

            if pilot.status == "IDLE" and not has_tokens:
                pass  # Stay on deck — no tokens yet
            elif pilot.status == "ON_APPROACH" and delta_is_tracking:
                pass  # Delta tracker said fly home — don't override
            elif cic_status == "AIRBORNE" and not has_tokens:
                pilot.status = "IDLE"  # No evidence of work — keep grounded
            else:
                pilot.status = cic_status
            ctx = agent.context or {}
            pilot.fuel_pct = _ctx_remaining(ctx)

            if agent.jsonl_metrics:
                m = agent.jsonl_metrics
                pilot.tokens_used = m.total_tokens
                pilot.tool_calls = m.total_tool_calls
                pilot.error_count = m.error_count

            pilot.status_hint = agent.status_hint

            # Sentinel status — Haiku-classified from JSONL events.
            # Takes priority over legacy heuristics when present and fresh (<90s).
            if pilot.worktree_path:
                ss_path = Path(pilot.worktree_path) / ".sortie" / "sentinel-status.json"
                try:
                    ss = json.loads(ss_path.read_text(encoding="utf-8"))
                    ss_age = int(time_mod.time()) - ss.get("timestamp", 0)
                    ss_status = ss.get("status", "").upper()
                    if ss_age < 90 and ss_status in ("AIRBORNE", "HOLDING", "ON_APPROACH", "RECOVERED", "PREFLIGHT"):
                        # Never let sentinel downgrade RECOVERED → something else
                        if pilot.status == "RECOVERED" and ss_status != "RECOVERED":
                            pass  # keep RECOVERED
                        else:
                            # Map sentinel status through flight-status map
                            # (HOLDING → IDLE, others pass through)
                            mapped = _FLIGHT_STATUS_MAP.get(ss_status, ss_status)
                            target = mapped if mapped else ss_status
                            pilot.status = validate_transition(pilot.status, target)
                        phase = ss.get("phase", "")
                        if phase:
                            pilot.flight_phase = phase
                        self._reconciler.stale_frames.pop(pilot.callsign, None)
                except (OSError, json.JSONDecodeError, KeyError):
                    pass  # No sentinel status yet — fall through to flight-status.json

            # Command file — Mini Boss or Air Boss can override agent status
            if pilot.worktree_path:
                cmd_path = Path(pilot.worktree_path) / ".sortie" / "command.json"
                try:
                    if cmd_path.exists():
                        cmd_data = json.loads(cmd_path.read_text(encoding="utf-8"))
                        cmd_path.unlink()  # consume — one-shot
                        new_status = cmd_data.get("set_status", "").upper()
                        if new_status in ("AIRBORNE", "IDLE", "RECOVERED", "ON_APPROACH", "MAYDAY", "AAR", "SAR", "PREFLIGHT"):
                            pilot.status = new_status
                            self._reconciler.stale_frames.pop(pilot.callsign, None)
                            reason = cmd_data.get("reason", "command override")
                            self._add_radio(pilot.callsign, f"{new_status} — {reason} (set by {cmd_data.get('source', 'command')})", "system")
                            if new_status == "RECOVERED":
                                _play_sound("recovered")
                                _notify("USS TENKARA — RECOVERED", f"{pilot.callsign} on deck (forced)")
                            # Don't continue — let normal flight-status processing
                            # run so the agent's next update can take over naturally
                except (json.JSONDecodeError, OSError):
                    pass

            # Session-ended sentinel — bash EXIT trap fired, agent is done
            if agent.session_ended and pilot.status != "RECOVERED":
                pilot.status = "RECOVERED"
                pilot.flight_status = ""
                pilot.flight_phase = ""
                self._reconciler.stale_frames.pop(pilot.callsign, None)
                self._add_radio(pilot.callsign, "RECOVERED — session ended", "success")
                _play_sound("recovered")
                _notify("USS TENKARA — RECOVERED", f"{pilot.callsign} on deck")
                pilot.mood = derive_mood(pilot)
                continue

            # Store agent-reported flight status on pilot (if fresh)
            if agent.flight_status and not _flight_status_is_stale(agent):
                pilot.flight_status = agent.flight_status
                pilot.flight_phase = agent.flight_phase

                # Agent-reported flight status is authoritative when fresh
                mapped = _map_flight_status(agent.flight_status)
                if mapped and mapped != pilot.status:
                    old = pilot.status
                    pilot.status = validate_transition(old, mapped)
                    self._reconciler.stale_frames.pop(pilot.callsign, None)
                    if mapped == "RECOVERED":
                        self._add_radio(pilot.callsign, f"RECOVERED — {agent.flight_phase or 'mission complete'}", "success")
                        _play_sound("recovered")
                        _clear_flight_status(pilot.worktree_path)
                    elif old != mapped:
                        phase_msg = f" — {agent.flight_phase}" if agent.flight_phase else ""
                        self._add_radio(pilot.callsign, f"{mapped}{phase_msg}", "system")
            else:
                # Stale or missing — clear so token-delta inference takes over
                pilot.flight_status = ""
                pilot.flight_phase = ""
                if agent.flight_status:
                    _clear_flight_status(agent.worktree_path)

            pilot.mood = derive_mood(pilot)

        # Mark legacy agents that disappeared as RECOVERED
        for tid, agent_state in list(self._legacy_agents.items()):
            if tid not in seen_tickets:
                if tid in self._dismissed_tickets:
                    # Already dismissed by user — just clean up tracking
                    self._legacy_agents.pop(tid, None)
                    continue
                pilots = self._roster.get_by_ticket(tid)
                for p in pilots:
                    if not self._agent_mgr.get(p.callsign):  # Not stream-json managed
                        p.status = "RECOVERED"
                del self._legacy_agents[tid]

        # Push roster changes to the flight strip immediately — don't wait
        # for the next _refresh_ui cycle (up to 3s away).
        try:
            strip = self.query_one("#flight-strip", FlightOpsStrip)
            strip.update_pilots(self._roster.all_pilots())
        except Exception:
            pass

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
        """Process SDK agent event on the main thread."""
        try:
            self._handle_sdk_event_inner(callsign, event)
        except Exception as e:
            log.warning("SDK event handler error for %s: %s", callsign, e)

    def _handle_sdk_event_inner(self, callsign: str, event: "AgentEvent") -> None:
        pilot = self._roster.get_by_callsign(callsign)
        if not pilot:
            return

        sdk_agent = self._sdk_mgr.get(callsign) if self._sdk_mgr else None

        # ── Always-immediate: telemetry sync (numbers only, no status change) ──
        if sdk_agent:
            pilot.tokens_used = sdk_agent.total_tokens
            pilot.tool_calls = sdk_agent.tool_calls
            pilot.error_count = sdk_agent.error_count
            pilot.fuel_pct = sdk_agent.fuel_pct
            pilot.last_tool_at = sdk_agent.last_tool_at

        # ── Always-immediate: chat pane routing ──
        if callsign in self._chat_panes:
            pane = self._chat_panes[callsign]
            if event.type == "text" and event.text:
                pane.add_message("assistant", event.text)
            elif event.type == "tool_use":
                pane.add_message("tool", event.tool_name, tool_name=event.tool_name, tool_input=event.tool_input)
            pane.refresh_header()

        # ── Always-immediate: radio chatter (throttled to first line only) ──
        if event.type == "text" and event.text:
            first_line = event.text.split("\n")[0].strip()
            if first_line and len(first_line) > 5:
                self._add_radio(callsign, first_line[:120])
        if event.type == "error":
            self._add_radio(callsign, f"ERROR — {event.error}", "error")

        # ── Debounced: status transitions ──
        # Prevents sprite flicker from rapid event streams.
        # Status changes are only applied if enough time has passed since
        # the last transition for this callsign.
        now = time_mod.time()
        last_change = self._sdk_last_status_update.get(callsign, 0)
        can_transition = (now - last_change) >= self._sdk_status_debounce_secs

        if sdk_agent and can_transition:
            prev_status = pilot.status
            new_status = prev_status  # default: no change

            # IDLE → AIRBORNE: first token flow
            if prev_status == "IDLE" and sdk_agent.total_tokens > 0:
                new_status = "AIRBORNE"

            # AIRBORNE fuel checks
            if prev_status == "AIRBORNE":
                if pilot.fuel_pct <= 0:
                    new_status = "SAR"
                elif pilot.fuel_pct <= 30 and callsign not in self._reconciler.bingo_notified:
                    self._reconciler.bingo_notified.add(callsign)
                    _play_sound("bingo")
                    self._add_radio(callsign, f"BINGO FUEL — {pilot.fuel_pct}% remaining", "error")

            # AAR fuel check — can still flame out during compaction
            if prev_status == "AAR" and pilot.fuel_pct <= 0:
                new_status = "SAR"

            # Apply transition through validator (ensures logical ordering)
            if new_status != prev_status:
                validated = validate_transition(prev_status, new_status)
                pilot.status = validated
                self._sdk_last_status_update[callsign] = now

                if validated == "AIRBORNE" and prev_status == "IDLE":
                    self._add_radio(callsign, "LAUNCH — tokens flowing, going AIRBORNE", "success")
                    _notify("USS TENKARA — LAUNCH", f"{callsign} AIRBORNE")
                elif validated == "SAR":
                    _play_sound("mayday")
                    self._add_radio(callsign, "FLAMEOUT — ZERO FUEL", "error")
                elif validated == "ON_APPROACH":
                    self._add_radio(callsign, "ON APPROACH — returning to base", "system")
                elif validated != new_status:
                    # Validator inserted an intermediate — log it
                    self._add_radio(callsign, f"{prev_status} → {validated} (intermediate for {new_status})", "system")

        # Update mood (cheap, OK to run every event)
        pilot.mood = derive_mood(pilot)

        # Flight strip update throttled to status changes only
        # (the 3s _refresh_ui timer handles periodic strip updates)
        if sdk_agent and pilot.status != getattr(self, '_sdk_last_strip_status_' + callsign, ''):
            setattr(self, '_sdk_last_strip_status_' + callsign, pilot.status)
            try:
                strip = self.query_one("#flight-strip", FlightOpsStrip)
                strip.update_pilots(self._roster.all_pilots())
            except Exception:
                pass

    def _handle_agent_event(self, callsign: str, event: StreamEvent) -> None:
        """Process agent event on the main thread.

        Wrapped in try/except to prevent agent stream errors from crashing the TUI.
        """
        try:
            self._handle_agent_event_inner(callsign, event)
        except Exception as e:
            log.warning("Agent event handler error for %s: %s", callsign, e)

    def _handle_agent_event_inner(self, callsign: str, event: StreamEvent) -> None:
        pilot = self._roster.get_by_callsign(callsign)
        if not pilot:
            return

        agent = self._agent_mgr.get(callsign)
        if not agent:
            return

        # Sync telemetry from agent process to pilot
        prev_tokens = pilot.tokens_used
        pilot.tokens_used = agent.total_tokens
        pilot.tool_calls = agent.tool_calls
        pilot.error_count = agent.error_count
        pilot.fuel_pct = agent.fuel_pct
        pilot.last_tool_at = agent.last_tool_at

        # Update mood
        pilot.mood = derive_mood(pilot)

        # Token consumption trigger — IDLE → AIRBORNE when tokens start flowing
        if pilot.status == "IDLE" and pilot.tokens_used > prev_tokens:
            pilot.status = "AIRBORNE"
            self._add_radio(pilot.callsign, "LAUNCH — tokens flowing, going AIRBORNE", "success")
            _notify("USS TENKARA — LAUNCH", f"{pilot.callsign} AIRBORNE")

        # Update status based on telemetry
        if pilot.status == "AIRBORNE":
            if pilot.fuel_pct <= 0:
                pilot.status = "SAR"
                _play_sound("mayday")
                self._add_radio(callsign, "FLAMEOUT — ZERO FUEL", "error")
            elif pilot.fuel_pct <= 30 and callsign not in self._reconciler.bingo_notified:
                self._reconciler.bingo_notified.add(callsign)
                _play_sound("bingo")
                self._add_radio(callsign, f"BINGO FUEL — {pilot.fuel_pct}% remaining", "error")
                _notify("USS TENKARA — BINGO", f"{callsign} at {pilot.fuel_pct}%")

        # Handle permission requests — show in chat pane or auto-open one
        if event.type == "control_request":
            request = event.raw.get("request", {})
            request_id = event.raw.get("request_id", "")
            tool_name = request.get("tool_name", "?")
            tool_use_id = request.get("tool_use_id", "")
            reason = request.get("decision_reason", "")
            tool_input = request.get("input", {})

            # Store pending permission for y/n response
            self._pending_permissions[callsign] = {
                "request_id": request_id,
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
            }

            # Open iTerm2 comms pane if not already open
            if callsign not in self._iterm_panes:
                self._open_iterm_comms(callsign)

            _play_sound("bingo")  # Subtle alert for permission needed
            self._add_radio(callsign, f"⚡ PERMISSION — {tool_name}: awaiting approval", "system")
            return  # Don't process further

        # Route to chat pane if open
        if callsign in self._chat_panes:
            pane = self._chat_panes[callsign]
            if event.type == "assistant":
                if event.text:
                    pane.add_message("assistant", event.text)
                for tu in event.tool_uses:
                    tool_name = tu.get("name", "unknown")
                    tool_input = tu.get("input", {})
                    from agent_manager import _summarize_tool_call
                    summary = _summarize_tool_call(tool_name, tool_input)
                    pane.add_message("tool", summary, tool_name=tool_name, tool_input=tool_input)
                pane.refresh_header()

        # Add to radio chatter (assistant text only, not tool calls)
        if event.type == "assistant" and event.text:
            first_line = event.text.split("\n")[0].strip()
            if first_line and len(first_line) > 5:
                self._add_radio(callsign, first_line[:120])

        # Refresh flight strip
        try:
            strip = self.query_one("#flight-strip", FlightOpsStrip)
            strip.update_pilots(self._roster.all_pilots())
        except Exception:
            pass

    def _handle_agent_exit(self, callsign: str, return_code: int) -> None:
        """Handle agent process exit on main thread.

        Wrapped to prevent exit handling errors from crashing the TUI.
        """
        try:
            self._handle_agent_exit_inner(callsign, return_code)
        except Exception as e:
            log.warning("Agent exit handler error for %s: %s", callsign, e)

    def _handle_agent_exit_inner(self, callsign: str, return_code: int) -> None:
        pilot = self._roster.get_by_callsign(callsign)
        if not pilot:
            return

        if return_code == 0:
            pilot.status = "RECOVERED"
            _play_sound("recovered")
            self._add_radio(callsign, "TRAP — RECOVERED. Mission complete.", "success")
            _notify("USS TENKARA — RECOVERED", f"{callsign} mission complete")

            # Check if entire squadron is recovered
            squadron_pilots = self._roster.get_squadron(pilot.squadron)
            if squadron_pilots and all(p.status == "RECOVERED" for p in squadron_pilots):
                total_time = sum(time_mod.time() - p.launched_at for p in squadron_pilots)
                total_tools = sum(p.tool_calls for p in squadron_pilots)
                _play_sound("squadron_complete")
                self._add_radio(
                    pilot.squadron.upper(),
                    f"SQUADRON COMPLETE — {pilot.ticket_id}: {pilot.mission_title} "
                    f"— {len(squadron_pilots)} pilots | {_format_elapsed(total_time)} | {total_tools} tx",
                    "success",
                )
        else:
            pilot.status = "MAYDAY"
            _play_sound("mayday")
            self._add_radio(callsign, f"MAYDAY — process exited with code {return_code}", "error")
            _notify("USS TENKARA — MAYDAY", f"{callsign} pilot ejected (exit {return_code})")

        self._refresh_ui()

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
        """Toggle select mode — disables mouse tracking so terminal handles text selection.

        Press F2 to enter select mode, drag to select, Cmd+C to copy.
        Press F2 again (or Esc) to return to normal mode.
        """
        if self._select_mode:
            # Re-enable mouse tracking
            sys.stdout.write("\x1b[?1000h\x1b[?1003h\x1b[?1006h")
            sys.stdout.flush()
            self._select_mode = False
            try:
                banner = self.query_one("#select-mode-banner", Static)
                banner.add_class("hidden")
            except Exception:
                pass
            self._add_radio("PRI-FLY", "SELECT MODE OFF — mouse restored", "system")
        else:
            # Disable mouse tracking — terminal handles selection natively
            sys.stdout.write("\x1b[?1000l\x1b[?1003l\x1b[?1006l")
            sys.stdout.flush()
            self._select_mode = True
            try:
                banner = self.query_one("#select-mode-banner", Static)
                banner.remove_class("hidden")
            except Exception:
                pass
            self._add_radio("PRI-FLY", "SELECT MODE ON — drag to select, Cmd+C to copy, F2 to exit", "system")

    def action_open_comms(self) -> None:
        """Open/reopen iTerm2 pane for selected agent."""
        pilot = self._get_selected_pilot()
        if not pilot:
            return
        # Clear tracking so we can reopen if window was closed
        self._iterm_panes.discard(pilot.callsign)
        self._open_agent_pane(pilot)

    # ── iTerm bridge (delegated to lib/iterm_bridge.py) ────────────────

    def _open_iterm_comms(self, callsign: str) -> None:
        self._iterm_bridge.open_comms(callsign)

    def _open_agent_pane(self, pilot) -> None:
        self._iterm_bridge.open_agent_pane(pilot)

    def _iterm_pane_cmd(self, callsign: str, cmd: str) -> None:
        self._iterm_bridge.pane_cmd(callsign, cmd)

    def _open_chat_pane(self, callsign: str) -> None:
        """Open or focus a chat pane for the given callsign."""
        if callsign in self._chat_panes:
            # Already open — just focus it
            try:
                self.query_one(f"#chat-input-{callsign}", ChatInput).focus()
            except Exception:
                pass
            return

        if len(self._chat_panes) >= 4:
            self._add_radio("PRI-FLY", "Max 4 chat panes open. Close one first (Ctrl+C).", "system")
            return

        # Find next empty slot
        slot_idx = None
        for i in range(4):
            if i not in self._slot_map:
                slot_idx = i
                break
        if slot_idx is None:
            return

        # Create pane and mount into slot
        pane = ChatPane(callsign, id=f"chat-pane-{callsign}")
        self._chat_panes[callsign] = pane
        self._slot_map[slot_idx] = callsign

        slot = self.query_one(f"#comms-slot-{slot_idx}", Vertical)
        slot.mount(pane)
        slot.remove_class("empty")

        # Show the row
        row_id = "comms-row-top" if slot_idx < 2 else "comms-row-bot"
        self.query_one(f"#{row_id}", Horizontal).remove_class("empty")

        # Activate comms grid
        if not self._comms_active:
            self._comms_active = True
            self.query_one("#comms-grid", Vertical).add_class("active")
            self.query_one("#board-section", Vertical).add_class("compressed")

        # Backfill happens in ChatPane.on_mount() — mount() is async so the
        # RichLog doesn't exist yet at this point.

        # Focus the chat input
        try:
            self.query_one(f"#chat-input-{callsign}", ChatInput).focus()
        except Exception:
            pass

        try:
            self._update_keybind_hints()
        except Exception:
            pass

    def action_toggle_flight_strip(self) -> None:
        """Toggle flight strip AND bottom panels (airboss, queue, radio)."""
        strip = self.query_one("#flight-strip", FlightOpsStrip)
        strip.toggle_class("collapsed")
        # Also toggle bottom panes
        for widget_id in ("#airboss-section", "#queue-section", "#radio-section"):
            try:
                w = self.query_one(widget_id)
                w.toggle_class("panels-collapsed")
            except Exception:
                pass

    async def action_briefing(self) -> None:
        table = self.query_one("#agent-table", DataTable)
        if table.row_count == 0:
            return
        row_idx = table.cursor_row
        if row_idx >= len(self._sorted_pilots):
            return
        pilot = self._sorted_pilots[row_idx]
        await self.push_screen(BriefingScreen(pilot))

    def action_toggle_focus(self) -> None:
        """Toggle focus to agent table."""
        try:
            table = self.query_one("#agent-table", DataTable)
            table.focus()
        except Exception:
            pass

    def action_focus_board(self) -> None:
        """Return focus to agent board."""
        try:
            table = self.query_one("#agent-table", DataTable)
            table.focus()
            self._update_keybind_hints()
        except Exception:
            pass

    # ── Hotkey actions (context-sensitive on selected pilot) ─────────

    def _is_sdk_agent(self, callsign: str) -> bool:
        """Check if a callsign is managed by the SDK agent manager."""
        return bool(self._sdk_mgr and self._sdk_mgr.get(callsign))

    def _get_selected_pilot(self) -> Optional[Pilot]:
        """Get the currently selected pilot from the board table."""
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

    def action_deploy(self) -> None:
        """Open deploy modal to launch a new agent."""
        def _on_dismiss(result: Optional[tuple[str, str]]) -> None:
            if result:
                ticket, model = result
                self._cmd_deploy([ticket, "--model", model])
        self.push_screen(DeployInputScreen(), callback=_on_dismiss)

    def _get_linear_org(self) -> str:
        """Read linear_org from config.json."""
        try:
            config_path = Path(__file__).resolve().parent.parent / "config.json"
            cfg = json.loads(config_path.read_text())
            return cfg.get("linear_org", "")
        except Exception:
            return ""

    def action_linear_browse(self) -> None:
        """Open Linear page for the selected pilot's ticket, or Linear inbox."""
        org = self._get_linear_org()
        if not org:
            self._add_radio("PRI-FLY", "No linear_org configured — run with --linear-org <org>", "error")
            return

        pilot = self._get_selected_pilot()
        if pilot and pilot.ticket_id and pilot.ticket_id not in ("Unknown", "unknown"):
            url = f"https://linear.app/{org}/issue/{pilot.ticket_id}"
            try:
                subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._add_radio("PRI-FLY", f"Opening {pilot.ticket_id} in Linear", "system")
            except Exception:
                self._add_radio("PRI-FLY", "Failed to open browser", "error")
        else:
            try:
                subprocess.Popen(["open", f"https://linear.app/{org}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._add_radio("PRI-FLY", "Opening Linear inbox", "system")
            except Exception:
                self._add_radio("PRI-FLY", "Failed to open browser", "error")

    def action_dismiss_selected(self) -> None:
        """Remove a pilot from the board and delete their git worktree."""
        import threading, shutil
        pilot = self._get_selected_pilot()
        if not pilot:
            # Debug: check why selection failed
            try:
                table = self.query_one("#agent-table", DataTable)
                self._add_radio("PRI-FLY", f"No pilot selected (rows={table.row_count}, cursor={table.cursor_row}, sorted={len(self._sorted_pilots)})", "error")
            except Exception:
                self._add_radio("PRI-FLY", "No pilot selected (table error)", "error")
            return
        # Kill active agent if still running before dismissing
        if pilot.status in ("AIRBORNE", "AAR", "SAR", "ON_APPROACH"):
            try:
                self._agent_mgr.wave_off(pilot.callsign)
            except Exception:
                pass
            try:
                if self._sdk_mgr:
                    self._sdk_mgr.wave_off(pilot.callsign)
            except Exception:
                pass
        callsign = pilot.callsign
        tid = pilot.ticket_id
        project_dir = self._project_dir
        # Resolve to absolute path — worktree_path may be relative
        worktree_path = str(Path(project_dir) / pilot.worktree_path) if pilot.worktree_path and not Path(pilot.worktree_path).is_absolute() else pilot.worktree_path
        self._roster.remove(callsign)
        self._legacy_agents.pop(tid, None)
        self._dismissed_tickets.add(tid)  # prevent re-add by _sync_legacy_agents
        self._board_state_sig = "__force_rebuild__"  # sentinel value that never matches a real sig
        self._add_radio("PRI-FLY", f"{callsign} dismissed from board", "system")

        # Remove sprite immediately — don't wait for tombstone TTL
        try:
            strip = self.query_one("#flight-strip", FlightOpsStrip)
            if callsign in strip._sprites:
                del strip._sprites[callsign]
        except Exception:
            pass

        self._refresh_ui()

        if not worktree_path:
            return

        def _delete_worktree():
            try:
                wt_path = Path(worktree_path)
                if not wt_path.exists():
                    try:
                        self.call_from_thread(
                            self._add_radio, "PRI-FLY",
                            f"{callsign} worktree already gone", "system",
                        )
                    except Exception:
                        pass
                    return

                result = subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_path)],
                    cwd=project_dir,
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0:
                    try:
                        self.call_from_thread(
                            self._add_radio, "PRI-FLY",
                            f"{callsign} worktree removed", "success",
                        )
                    except Exception:
                        pass
                else:
                    # git worktree remove failed — try direct delete
                    err = result.stderr.strip()
                    try:
                        self.call_from_thread(
                            self._add_radio, "PRI-FLY",
                            f"{callsign} git remove failed ({err[:80]}), nuking dir", "system",
                        )
                    except Exception:
                        pass
                    shutil.rmtree(str(wt_path), ignore_errors=True)
                    try:
                        self.call_from_thread(
                            self._add_radio, "PRI-FLY",
                            f"{callsign} worktree directory deleted", "success",
                        )
                    except Exception:
                        pass

                # Verify deletion
                if wt_path.exists():
                    try:
                        self.call_from_thread(
                            self._add_radio, "PRI-FLY",
                            f"WARNING: {callsign} worktree still exists at {wt_path}", "error",
                        )
                    except Exception:
                        pass
            except Exception as e:
                try:
                    self.call_from_thread(
                        self._add_radio, "PRI-FLY",
                        f"Worktree cleanup error: {e}", "error",
                    )
                except Exception:
                    pass

        threading.Thread(target=_delete_worktree, daemon=True).start()

    def action_open_terminal(self) -> None:
        """Open a plain terminal pane cd'd to the selected pilot's worktree, or project root."""
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
        """Relaunch Mini Boss — clears state and re-spawns."""
        if self._airboss_spawned and getattr(self, "_airboss_active", False):
            self._add_radio("MINI BOSS", "Already active — close its pane first to relaunch", "error")
            return
        # Clear state so _spawn_airboss guard passes
        self._airboss_spawned = False
        self._airboss_active = False
        self._iterm_panes.discard("MINI-BOSS")
        # Remove stale status file
        try:
            Path("/tmp/uss-tenkara/_prifly/miniboss-status").unlink(missing_ok=True)
        except OSError:
            pass
        self._spawn_airboss()

    def action_sitrep(self) -> None:
        """Request sitrep from all airborne agents."""
        self._cmd_sitrep()

    def action_open_browser(self) -> None:
        """Open the dev server URL for the selected pilot in the browser."""
        pilot = self._get_selected_pilot()
        if not pilot:
            self._add_radio("PRI-FLY", "No pilot selected", "error")
            return
        # Extract localhost URL from status_hint
        url = self._extract_server_url(pilot)
        if not url:
            self._add_radio("PRI-FLY", f"{pilot.callsign} has no active server", "error")
            return
        try:
            if not url.startswith("http"):
                url = f"http://{url}"
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._add_radio("PRI-FLY", f"Opening {url} in browser", "system")
        except Exception:
            self._add_radio("PRI-FLY", "Failed to open browser", "error")

    def action_open_pr(self) -> None:
        """Open the GitHub PR for the selected pilot's branch."""
        pilot = self._get_selected_pilot()
        if not pilot:
            self._add_radio("PRI-FLY", "No pilot selected", "error")
            return
        if not pilot.worktree_path:
            self._add_radio("PRI-FLY", f"{pilot.callsign} has no worktree", "error")
            return
        # Get PR number from gh CLI
        try:
            result = subprocess.run(
                ["gh", "pr", "view", "--json", "number,url", "-q", ".url"],
                capture_output=True, text=True, timeout=10,
                cwd=pilot.worktree_path,
            )
            pr_url = result.stdout.strip()
            if result.returncode == 0 and pr_url:
                subprocess.Popen(["open", pr_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._add_radio("PRI-FLY", f"Opening PR for {pilot.callsign}", "system")
            else:
                # No PR yet — open the repo page instead
                repo_url = self._get_github_repo_url(pilot.worktree_path)
                if repo_url:
                    subprocess.Popen(["open", repo_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self._add_radio("PRI-FLY", f"No PR found — opening repo", "system")
                else:
                    self._add_radio("PRI-FLY", f"No PR found for {pilot.callsign}", "error")
        except Exception:
            self._add_radio("PRI-FLY", "Failed to look up PR", "error")

    def _extract_server_url(self, pilot) -> str:
        """Extract a localhost URL from status_hint, managed-servers.json, or conversation."""
        import re as _re
        url_re = _re.compile(r"(localhost:\d+|127\.0\.0\.1:\d+|0\.0\.0\.0:\d+)")

        # 1. Check status_hint
        hint = pilot.status_hint or ""
        match = url_re.search(hint)
        if match:
            return match.group(1)

        # 2. Check managed-servers.json
        try:
            servers_file = Path(self._project_dir) / ".sortie" / "managed-servers.json"
            if servers_file.exists():
                entries = json.loads(servers_file.read_text(encoding="utf-8"))
                for entry in entries:
                    if entry.get("ticket_id") == pilot.ticket_id:
                        url = entry.get("url", "")
                        if url:
                            return url
        except (json.JSONDecodeError, OSError):
            pass

        # 3. Scan agent conversation buffer (most recent 200 entries only)
        session = self._agent_mgr.get(pilot.callsign) if hasattr(self, '_agent_mgr') else None
        if session:
            for entry in session.conversation[-200:][::-1]:
                match = url_re.search(entry.content)
                if match:
                    return match.group(1)

        return ""

    def _get_github_repo_url(self, cwd: str) -> str:
        """Derive GitHub repo URL from git remote."""
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=5, cwd=cwd,
            )
            url = result.stdout.strip()
            if url.endswith(".git"):
                url = url[:-4]
            if url.startswith("git@github.com:"):
                url = url.replace("git@github.com:", "https://github.com/")
            return url
        except Exception:
            return ""

    def action_resume_selected(self) -> None:
        """Resume the selected pilot."""
        pilot = self._get_selected_pilot()
        if not pilot:
            self._add_radio("PRI-FLY", "No pilot selected", "error")
            return
        if pilot.status not in ("RECOVERED", "MAYDAY", "IDLE", "QUEUED"):
            self._add_radio("PRI-FLY", f"{pilot.callsign} is {pilot.status} — can't resume", "error")
            return
        self._cmd_resume([pilot.callsign])

    def action_waveoff_selected(self) -> None:
        """Wave off (hard kill) the selected pilot."""
        try:
            pilot = self._get_selected_pilot()
            if not pilot:
                self._add_radio("PRI-FLY", "No pilot selected", "error")
                return
            self._cmd_wave_off([pilot.callsign])
        except Exception as e:
            self._add_radio("PRI-FLY", f"Wave-off failed: {e}", "error")

    def action_recall_selected(self) -> None:
        """Graceful recall of the selected pilot."""
        pilot = self._get_selected_pilot()
        if not pilot:
            self._add_radio("PRI-FLY", "No pilot selected", "error")
            return
        if pilot.status != "AIRBORNE":
            self._add_radio("PRI-FLY", f"{pilot.callsign} is {pilot.status} — not airborne", "error")
            return
        self._cmd_recall([pilot.callsign])

    def action_sync_worktrees(self) -> None:
        """Force an immediate sync of all worktrees."""
        self._sync_legacy_agents()
        self._add_radio("PRI-FLY", "SYNC — scanning worktrees", "system")

    def action_compact_selected(self) -> None:
        """Compact (AAR) the selected pilot."""
        pilot = self._get_selected_pilot()
        if not pilot:
            self._add_radio("PRI-FLY", "No pilot selected", "error")
            return
        self._cmd_compact([pilot.callsign])

    def action_start_server(self) -> None:
        """Spin up a dev server for the selected pilot's worktree."""
        pilot = self._get_selected_pilot()
        if not pilot:
            self._add_radio("PRI-FLY", "No pilot selected", "error")
            return
        if not pilot.worktree_path:
            self._add_radio("PRI-FLY", f"{pilot.callsign} has no worktree", "error")
            return

        wt = pilot.worktree_path
        tid = pilot.ticket_id
        pane_name = f"SRV-{pilot.callsign}"

        # Always allow re-launching — if there's already something on the port it'll error out naturally
        self._iterm_panes.discard(pane_name)

        # Pick a port — start at 3000, increment by worktree index
        all_pilots = self._roster.all_pilots()
        port = 3000 + next((i for i, p in enumerate(all_pilots) if p.callsign == pilot.callsign), 0)

        # Build the server launch script
        server_script = Path(wt) / ".sortie" / "start-server.sh"
        managed_servers = Path(self._project_dir) / ".sortie" / "managed-servers.json"

        server_script.write_text(
            f"#!/usr/bin/env bash\n"
            f"cd '{wt}'\n"
            f"\n"
            f"printf '\\033[1;36m⚡ USS TENKARA — DEV SERVER for {pilot.callsign}\\033[0m\\n'\n"
            f"printf '\\033[2;37m   Worktree: {wt}\\033[0m\\n'\n"
            f"printf '\\033[2;37m   Target port: {port}\\033[0m\\n'\n"
            f"printf '\\n'\n"
            f"\n"
            f"# Symlink .env.local if missing\n"
            f"if [ ! -f .env.local ] && [ -f '{self._project_dir}/.env.local' ]; then\n"
            f"  ln -sf '{self._project_dir}/.env.local' .env.local\n"
            f"  printf '\\033[1;32m✓ Symlinked .env.local\\033[0m\\n'\n"
            f"fi\n"
            f"\n"
            f"# Install deps if missing or stale (lockfile newer than node_modules)\n"
            f"if [ -f pnpm-lock.yaml ]; then\n"
            f"  if [ ! -d node_modules ] || [ pnpm-lock.yaml -nt node_modules ]; then\n"
            f"    printf '\\033[1;33m📦 Installing dependencies...\\033[0m\\n'\n"
            f"    pnpm install --frozen-lockfile 2>/dev/null || pnpm install\n"
            f"  fi\n"
            f"fi\n"
            f"\n"
            f"# Register in managed-servers.json\n"
            f"register_server() {{\n"
            f"  local file='{managed_servers}'\n"
            f"  mkdir -p \"$(dirname \"$file\")\"\n"
            f"  if [ ! -f \"$file\" ] || [ ! -s \"$file\" ]; then\n"
            f"    echo '[]' > \"$file\"\n"
            f"  fi\n"
            f"  python3 -c \"\n"
            f"import json, pathlib\n"
            f"p = pathlib.Path('$file')\n"
            f"entries = json.loads(p.read_text()) if p.exists() else []\n"
            f"entries = [e for e in entries if e.get('ticket_id') != '{tid}']\n"
            f"entries.append({{'ticket_id': '{tid}', 'url': 'localhost:{port}', 'note': 'dev server', 'pid': $$}})\n"
            f"p.write_text(json.dumps(entries, indent=2) + '\\n')\n"
            f"\"\n"
            f"}}\n"
            f"\n"
            f"# Cleanup on exit — remove from managed-servers.json\n"
            f"cleanup() {{\n"
            f"  python3 -c \"\n"
            f"import json, pathlib\n"
            f"p = pathlib.Path('{managed_servers}')\n"
            f"if p.exists():\n"
            f"    entries = json.loads(p.read_text())\n"
            f"    entries = [e for e in entries if e.get('ticket_id') != '{tid}']\n"
            f"    p.write_text(json.dumps(entries, indent=2) + '\\n')\n"
            f"\"\n"
            f"}}\n"
            f"trap cleanup EXIT\n"
            f"\n"
            f"# Try pnpm run dev first\n"
            f"printf '\\033[1;33m🚀 Starting dev server...\\033[0m\\n'\n"
            f"register_server\n"
            f"PORT={port} pnpm run dev\n"
        )
        server_script.chmod(0o755)

        cmd = f"bash '{server_script}'"
        self._iterm_pane_cmd(pane_name, cmd)
        self._add_radio("PRI-FLY", f"DEV SERVER — launching for {pilot.callsign} on port {port}", "success")
        _notify("USS TENKARA", f"Dev server starting for {pilot.callsign} :{port}")

    def _close_chat_pane(self, callsign: str) -> None:
        """Close a chat pane by removing it from its slot."""
        pane = self._chat_panes.pop(callsign, None)
        if pane is None:
            return

        # Find which slot this pane was in
        slot_idx = None
        for idx, cs in list(self._slot_map.items()):
            if cs == callsign:
                slot_idx = idx
                break

        if slot_idx is not None:
            del self._slot_map[slot_idx]
            # Remove pane widget from the slot
            try:
                pane.remove()
            except Exception:
                pass
            # Mark slot as empty again
            try:
                self.query_one(f"#comms-slot-{slot_idx}", Vertical).add_class("empty")
            except Exception:
                pass
            # Hide row if both its slots are empty
            row_id = "comms-row-top" if slot_idx < 2 else "comms-row-bot"
            sibling_idx = (slot_idx ^ 1) if slot_idx < 2 else (slot_idx ^ 1)  # 0↔1, 2↔3
            if sibling_idx not in self._slot_map:
                try:
                    self.query_one(f"#{row_id}", Horizontal).add_class("empty")
                except Exception:
                    pass

        # If no panes left, deactivate comms grid
        if not self._chat_panes:
            self._comms_active = False
            try:
                self.query_one("#comms-grid", Vertical).remove_class("active")
                self.query_one("#board-section", Vertical).remove_class("compressed")
            except Exception:
                pass
            self.query_one("#agent-table", DataTable).focus()
        else:
            # Focus next available pane
            next_callsign = next(iter(self._chat_panes))
            try:
                self.query_one(f"#chat-input-{next_callsign}", ChatInput).focus()
            except Exception:
                self.query_one("#agent-table", DataTable).focus()

        try:
            self._update_keybind_hints()
        except Exception:
            pass

    # ── Idle detection + auto-compact (delegated to monitoring) ────────

    def _check_idle_agents(self) -> None:
        self._monitoring.check_idle_agents()

    # ── Token delta tracking ─────────────────────────────────────────

    def _check_token_deltas(self) -> None:
        """Compare each pilot's token count to the previous frame.

        - delta > 0  → tokens flowing, ensure AIRBORNE
        - delta == 0 → stale frame; after _stale_threshold consecutive
                        stale frames on an AIRBORNE pilot → ON_APPROACH
        - Newly IDLE pilots with first token activity → promote to AIRBORNE

        Called every frame (3s) from _refresh_ui.
        """
        # SDK-managed agents handle their own status via event stream
        sdk_callsigns = {a.callsign for a in self._sdk_mgr.active_agents()} if self._sdk_mgr else set()

        for pilot in self._roster.all_pilots():
            cs = pilot.callsign

            # Skip SDK agents — their event handler manages status
            if cs in sdk_callsigns:
                continue

            curr = pilot.tokens_used
            prev = self._reconciler.prev_tokens.get(cs, 0)
            delta = curr - prev
            self._reconciler.prev_tokens[cs] = curr

            # Agent-reported flight status is authoritative — skip token-delta inference
            if pilot.flight_status:
                self._reconciler.stale_frames.pop(cs, None)
                continue

            # Unknown agents (no real directive) — pin to RECOVERED, never promote
            if pilot.mission_title in ("Unknown", "unknown") and pilot.ticket_id in ("Unknown", "unknown"):
                if pilot.status != "RECOVERED":
                    pilot.status = "RECOVERED"
                self._reconciler.stale_frames.pop(cs, None)
                continue

            # AAR agents can still flame out — check fuel before skipping
            if pilot.status == "AAR" and pilot.fuel_pct <= 0:
                pilot.status = "SAR"
                _play_sound("mayday")
                self._add_radio(cs, "FLAMEOUT — AAR failed, ZERO FUEL", "error")
                _notify("USS TENKARA — MAYDAY", f"{cs} flameout during AAR")
                # Don't skip — let SAR animation start on next compaction recovery tick

            # Skip terminal/special statuses — don't interfere with AAR/SAR/RECOVERED/MAYDAY
            if pilot.status in ("AAR", "SAR", "RECOVERED", "MAYDAY"):
                self._reconciler.stale_frames.pop(cs, None)
                continue

            # AIRBORNE agents — check fuel for SAR trigger
            if pilot.status == "AIRBORNE" and pilot.fuel_pct <= 0:
                pilot.status = "SAR"
                _play_sound("mayday")
                self._add_radio(cs, "FLAMEOUT — ZERO FUEL", "error")
                _notify("USS TENKARA — MAYDAY", f"{cs} flameout")
                self._reconciler.stale_frames.pop(cs, None)
                continue

            if pilot.status == "AIRBORNE" and pilot.fuel_pct <= 30 and cs not in self._reconciler.bingo_notified:
                self._reconciler.bingo_notified.add(cs)
                _play_sound("bingo")
                self._add_radio(cs, f"BINGO FUEL — {pilot.fuel_pct}% remaining", "error")
                _notify("USS TENKARA — BINGO", f"{cs} at {pilot.fuel_pct}%")

            if delta > 0:
                # Tokens moving — reset stale counter
                self._reconciler.stale_frames[cs] = 0

                if pilot.status == "IDLE":
                    # Skip if this is the first time we're seeing this pilot's tokens —
                    # the delta may be from historical JSONL, not live activity.
                    # Only launch on the SECOND consecutive positive delta.
                    if prev == 0 and curr > 0:
                        # First observation — set baseline, don't launch yet
                        pass
                    else:
                        # Genuine token flow on a pilot we've been tracking
                        pilot.status = "AIRBORNE"
                        self._add_radio(cs, "LAUNCH — tokens flowing, going AIRBORNE", "success")
                        _notify("USS TENKARA — LAUNCH", f"{cs} AIRBORNE")
                elif pilot.status == "ON_APPROACH":
                    # Was flying home but tokens resumed — wave off, back to AIRBORNE
                    pilot.status = "AIRBORNE"
                    self._add_radio(cs, "WAVE OFF RTB — tokens resumed, back AIRBORNE", "success")

            elif curr > 0:
                # Had tokens before, but no new ones this frame
                stale = self._reconciler.stale_frames.get(cs, 0) + 1
                self._reconciler.stale_frames[cs] = stale

                if pilot.status == "AIRBORNE" and stale >= self._reconciler.stale_threshold:
                    pilot.status = "ON_APPROACH"
                    self._add_radio(cs, "ON APPROACH — token flow stopped, RTB", "system")
                elif pilot.status == "ON_APPROACH" and stale >= self._reconciler.stale_threshold + 6:
                    # Landing animation done (~18s after ON_APPROACH) → park it
                    pilot.status = "RECOVERED"
                    self._add_radio(cs, "RECOVERED — on deck, mission complete", "success")
                    _play_sound("recovered")
                    _notify("USS TENKARA — RECOVERED", f"{cs} on deck")
                    _clear_flight_status(pilot.worktree_path)

        # Clean up entries for removed pilots
        active_cs = {p.callsign for p in self._roster.all_pilots()}
        for cs in list(self._reconciler.prev_tokens):
            if cs not in active_cs:
                del self._reconciler.prev_tokens[cs]
                self._reconciler.stale_frames.pop(cs, None)

    # ── Compaction recovery (AAR / SAR) ─────────────────────────────

    def _check_compaction_recovery(self) -> None:
        """Detect context compaction events via fuel jumps.

        When Claude auto-compacts, fuel_pct jumps up (e.g. 5% → 60%).
        This triggers the recovery flow:
          - SAR (was 0% / crashed) → flameout → helo → replane → relaunch
          - AAR (voluntary compact) → refuel → disconnect → resume AIRBORNE
        """
        now = time_mod.time()

        # SDK-managed agents handle their own status via event stream
        sdk_callsigns = {a.callsign for a in self._sdk_mgr.active_agents()} if self._sdk_mgr else set()

        for pilot in self._roster.all_pilots():
            cs = pilot.callsign
            if cs in sdk_callsigns:
                continue
            curr_fuel = pilot.fuel_pct
            prev_fuel = self._reconciler.prev_fuel.get(cs, curr_fuel)
            self._reconciler.prev_fuel[cs] = curr_fuel
            fuel_gain = curr_fuel - prev_fuel

            # ── SAR recovery: was crashed, fuel came back ──
            if pilot.status == "SAR":
                if cs not in self._reconciler.sar_started:
                    # First frame at SAR — start the crash timer
                    self._reconciler.sar_started[cs] = now
                    self._add_radio(cs, "FLAMEOUT — ejecting! Pedro helo launching...", "error")
                    continue

                elapsed = now - self._reconciler.sar_started[cs]

                if fuel_gain >= _FUEL_JUMP_THRESHOLD and elapsed >= _SAR_RECOVERY_DELAY:
                    # Fuel recovered + enough time for crash animation → replane and relaunch
                    del self._reconciler.sar_started[cs]
                    pilot.status = "AIRBORNE"
                    self._reconciler.bingo_notified.discard(cs)
                    self._reconciler.stale_frames.pop(cs, None)
                    self._add_radio(cs, f"SAR COMPLETE — Pedro has the pilot. Replaned, back AIRBORNE at {curr_fuel}%", "success")
                    _notify("USS TENKARA — SAR", f"{cs} recovered, replaned, AIRBORNE")
                elif fuel_gain >= _FUEL_JUMP_THRESHOLD:
                    # Fuel came back but still in animation window
                    self._add_radio(cs, "Pedro on station — winching pilot aboard...", "system")
                elif elapsed > _SAR_RECOVERY_DELAY and curr_fuel > 0:
                    # Enough time passed and fuel is non-zero → recover
                    self._reconciler.sar_started.pop(cs, None)
                    pilot.status = "AIRBORNE"
                    self._reconciler.bingo_notified.discard(cs)
                    self._reconciler.stale_frames.pop(cs, None)
                    self._add_radio(cs, f"SAR COMPLETE — replaned, back AIRBORNE at {curr_fuel}%", "success")
                    _notify("USS TENKARA — SAR", f"{cs} recovered, AIRBORNE")
                continue

            # ── AAR recovery: was refueling, fuel came back ──
            if pilot.status == "AAR":
                if fuel_gain >= _FUEL_JUMP_THRESHOLD:
                    # Compaction complete — disconnect from tanker, back to AIRBORNE
                    pilot.status = "AIRBORNE"
                    self._reconciler.bingo_notified.discard(cs)
                    self._reconciler.stale_frames.pop(cs, None)
                    self._add_radio(cs, f"AAR COMPLETE — disconnect, back AIRBORNE at {curr_fuel}%", "success")
                    _notify("USS TENKARA — AAR", f"{cs} refueled, AIRBORNE")
                continue

        # Clean up stale SAR entries for removed pilots
        active_cs = {p.callsign for p in self._roster.all_pilots()}
        for cs in list(self._reconciler.sar_started):
            if cs not in active_cs:
                del self._reconciler.sar_started[cs]
        for cs in list(self._reconciler.prev_fuel):
            if cs not in active_cs:
                del self._reconciler.prev_fuel[cs]

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

        # Token delta check — must run before table render so status is current
        try:
            self._check_token_deltas()
        except Exception:
            pass

        # Compaction recovery — detect fuel jumps on SAR/AAR agents
        try:
            self._check_compaction_recovery()
        except Exception:
            pass

        try:
            self._roster.update_moods()
            self._refresh_table()
            self.query_one("#header-bar", PriFlyHeader).refresh()
            self.query_one("#deck-status", DeckStatus).refresh()
            self.query_one("#queue-section", MissionQueuePanel).refresh()
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
        pilots = self._roster.all_pilots()

        # Skip full table rebuild when visible state hasn't changed — table.clear() +
        # add_row() on every 3s tick is the biggest source of UI jank with active agents.
        sig = "|".join(
            f"{p.callsign}:{p.status}:{p.fuel_pct}:{p.tokens_used}:{p.error_count}"
            f":{p.mood}:{p.flight_phase}:{p.status_hint}:{p.tool_calls}"
            for p in sorted(pilots, key=lambda p: p.callsign)
        )
        if sig == self._board_state_sig:
            return
        self._board_state_sig = sig

        table = self.query_one("#agent-table", DataTable)
        # Remember selected pilot by callsign (stable across refreshes)
        prev_callsign = ""
        if table.row_count > 0 and table.cursor_row < len(self._sorted_pilots):
            prev_callsign = self._sorted_pilots[table.cursor_row].callsign
        table.clear()

        self._sorted_pilots = sorted(pilots, key=lambda p: p.callsign)

        critical = []
        for pilot in self._sorted_pilots:
            icon = STATUS_ICONS.get(pilot.status, "?")
            color = STATUS_COLORS.get(pilot.status, "white")

            # Callsign
            cs = Text()
            cs.append(f"{pilot.callsign}", style="bold")
            if pilot.mood != "steady":
                mood_style = {
                    "in_the_zone": "bold green",
                    "struggling": "bold red",
                    "strained": "yellow",
                    "stuck": "dim",
                    "satisfied": "bold bright_green",
                }.get(pilot.mood, "")
                cs.append(f" ({pilot.mood})", style=mood_style)

            # Status
            status = Text()
            status.append(f"{icon} ", style=color)
            status.append(pilot.status, style=f"bold {color}")

            # Fuel
            bar = fuel_gauge(pilot.fuel_pct, blink=self._bingo_blink)
            if pilot.fuel_pct <= 30:
                critical.append(f"{pilot.callsign} ({pilot.fuel_pct}%)")

            # Time
            elapsed = time_mod.time() - pilot.launched_at if pilot.launched_at > 0 else 0
            time_str = _format_elapsed(elapsed)

            # Tools
            tools = Text()
            tools.append(str(pilot.tool_calls), style="bold white")
            tools.append(" tx", style="grey70")
            if pilot.error_count > 0:
                tools.append(f" {pilot.error_count}✗", style="bold red")

            # Mission
            mission = Text()
            mission.append(f"{pilot.ticket_id}", style="bold")
            # Show title if different from ticket ID, truncated
            title = pilot.mission_title
            if title and title != pilot.ticket_id and title not in ("Unknown", "unknown"):
                # Clean up title — strip ticket ID prefix if present
                clean_title = title.replace(f"[{pilot.ticket_id}] ", "").replace(f"{pilot.ticket_id}: ", "")
                if clean_title:
                    mission.append(f"\n{clean_title[:45]}", style="grey70")
            if pilot.flight_phase:
                mission.append(f"\n» {pilot.flight_phase[:40]}", style="italic cyan")
            if pilot.status_hint:
                # Only show server URLs, not full paths
                hint = pilot.status_hint
                if "localhost:" in hint or "127.0.0.1:" in hint:
                    mission.append(f"\n⚡ {hint}", style="bold cyan")

            table.add_row(
                cs,
                mission,
                Text(pilot.model, style="italic"),
                status,
                bar,
                Text(time_str, style="grey70"),
                tools,
                height=2,
            )

        # Restore cursor by callsign (stable even when pilots are added/removed)
        if table.row_count > 0 and prev_callsign:
            restored = next(
                (i for i, p in enumerate(self._sorted_pilots) if p.callsign == prev_callsign),
                0,
            )
            table.move_cursor(row=restored)
        elif table.row_count > 0:
            table.move_cursor(row=0)

        # Alert bar for critical fuel
        alert_bar = self.query_one("#alert-bar")
        if critical:
            names = ", ".join(critical)
            alert_bar.update(f"⚠ FUEL CRITICAL: {names} — BINGO RTB ⚠")
            alert_bar.add_class("visible")
        else:
            alert_bar.remove_class("visible")


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
