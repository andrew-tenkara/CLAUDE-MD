#!/usr/bin/env python3
"""USS Tenkara CIC — Carrier-themed animated TUI dashboard for sortie agents.

v3: Reactive state, native Sparkline, Rich ProgressBar fuel gauges,
    row flash animations, macOS native notifications, ASCII carrier art,
    agent timeline panel, watchdog event-driven refresh.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time as time_mod
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

# Add lib to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from read_sortie_state import (
    read_sortie_state, get_all_progress_entries, AgentState, get_worktrees_root,
)
from parse_jsonl_metrics import JsonlMetrics, encode_project_path, CLAUDE_PROJECTS_DIR

from rich.text import Text
from rich.progress_bar import ProgressBar
from rich.table import Table as RichTable
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, RichLog, Sparkline, Static
from textual.worker import Worker, WorkerState

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent


# ── Carrier vocabulary ───────────────────────────────────────────────

STATUS_MAP = {
    "WORKING": "AIRBORNE",
    "PRE-REVIEW": "ON APPROACH",
    "DONE": "RECOVERED",
}

STATUS_ICONS = {
    "AIRBORNE": "✈",
    "ON APPROACH": "🔄",
    "RECOVERED": "✓",
    "MAYDAY": "⚠",
}

STATUS_COLORS = {
    "AIRBORNE": "dodger_blue1",
    "ON APPROACH": "dark_orange",
    "RECOVERED": "green",
    "MAYDAY": "bold red",
}

SQUADRON_MAP = {
    "opus": "Viper",
    "sonnet": "Iceman",
    "haiku": "Maverick",
}


# ── Callsign registry ───────────────────────────────────────────────

class CallsignRegistry:
    def __init__(self) -> None:
        self._assignments: dict[str, str] = {}
        self._counters: dict[str, int] = {}

    def get(self, ticket_id: str, model: str) -> str:
        if ticket_id not in self._assignments:
            squadron = SQUADRON_MAP.get(model.lower().split("-")[0], "Ghost")
            self._counters.setdefault(squadron, 0)
            self._counters[squadron] += 1
            self._assignments[ticket_id] = f"{squadron}-{self._counters[squadron]}"
        return self._assignments[ticket_id]


# ── Flight Ops sprites & state machine ────────────────────────────────

# Rightward (launch/cruise/ordnance) — NTDS phosphor green style
F14_CAT    = ["o==>", "O==>"]
F14_LAUNCH = [" >==>", "  >==>>"]
F14_CRUISE = [">==▷", ">==>"]
F14_BOMB   = [">==*", ">==>·", ">==▷"]
F14_MAYDAY = [">==x", "/==/", "x==x"]
# Leftward (return/trap)
F14_RTN    = ["◁==<", "<==<"]
F14_TRAP   = ["◁==|", "[-=]"]
# Parked
F14_PARKED = "[-=]"

PHASE_SPRITES = {
    "CAT": F14_CAT, "LAUNCH": F14_LAUNCH, "CRUISE": F14_CRUISE,
    "ORDNANCE": F14_BOMB, "MAYDAY": F14_MAYDAY, "RETURN": F14_RTN,
    "TRAP": F14_TRAP,
}

# Zone boundaries as % of strip width
ZONE_PCT = {
    "DECK": (0, 12), "CAT": (12, 22), "SKY": (22, 65),
    "TGT": (65, 72), "RTN": (72, 90), "TRAP": (90, 100),
}

# Phase timing (ticks at ~0.15s interval)
PHASE_TICKS = {
    "CAT": 8, "LAUNCH": 5, "CRUISE": 40, "TRAP": 6,
}

STATUS_SORT_ORDER = {
    "AIRBORNE": 0,
    "ON APPROACH": 1,
    "MAYDAY": 2,
    "RECOVERED": 3,
}


@dataclass
class FlightSprite:
    ticket_id: str
    callsign: str
    col: int = 0
    phase: str = "CAT"
    lane: int = 0          # 0=main, 1=upper (deconfliction)
    anim_frame: int = 0
    phase_ticks: int = 0   # ticks spent in current phase
    prev_status: str = ""


# ── macOS native notifications ───────────────────────────────────────

def _macos_notify(title: str, message: str, sound: str = "") -> None:
    """Fire a macOS Notification Center alert (non-blocking)."""
    script = f'display notification "{message}" with title "{title}"'
    if sound:
        script += f' sound name "{sound}"'
    try:
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


# ── Context → Fuel conversion ────────────────────────────────────────

def _ctx_remaining(ctx: dict) -> int | None:
    """Convert used_percentage to fuel remaining (0% = empty, 100% = full)."""
    used = ctx.get("used_percentage")
    if used is None:
        return None
    return max(0, 100 - int(used))


# ── Fuel gauge with Rich ProgressBar ────────────────────────────────

def fuel_gauge(pct: int | None, width: int = 10, bingo_blink: bool = False) -> Text:
    """Build a fuel gauge using remaining %. 0% = empty, 100% = full."""
    if pct is None:
        return Text(" N/A ", style="dim")

    if pct <= 20:
        complete_style = "bold red"
    elif pct <= 50:
        complete_style = "yellow"
    else:
        complete_style = "green"

    bar = Text()
    filled = round(pct / 100 * width)
    empty = width - filled
    bar.append("━" * filled, style=complete_style)
    bar.append("╌" * empty, style="grey37")
    bar.append(f" {pct}%", style=complete_style)

    if pct <= 10:
        if bingo_blink:
            bar.append(" BINGO!", style="bold bright_red")
        else:
            bar.append(" BINGO!", style="dim red")
    elif pct <= 20:
        bar.append(" ⚠", style="bold red")

    return bar


# ── Token formatting ─────────────────────────────────────────────────

def format_comms(input_tokens: int | None, output_tokens: int | None) -> Text:
    if input_tokens is None and output_tokens is None:
        return Text("–", style="grey37")

    def _fmt(n: int | None) -> str:
        if n is None:
            return "0"
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}k"
        return str(n)

    t = Text()
    t.append(_fmt(input_tokens), style="cyan")
    t.append("↑", style="grey70")
    t.append(" ", style="dim")
    t.append(_fmt(output_tokens), style="magenta")
    t.append("↓", style="grey70")
    return t


# ── Sparkline text (for table cells) ────────────────────────────────

def make_burn_sparkline(history: list[float]) -> Text:
    if not history:
        return Text("─" * 10, style="grey37")
    max_val = max(history) or 1
    chars = " ▁▂▃▄▅▆▇█"
    spark = ""
    for val in history[-10:]:
        idx = int(val / max_val * (len(chars) - 1))
        spark += chars[idx]
    return Text(spark, style="cyan")


# ── Ordnance ─────────────────────────────────────────────────────────

def make_ordnance_text(metrics: Optional[JsonlMetrics]) -> Text:
    if metrics is None:
        return Text("–", style="grey37")
    t = Text()
    t.append(str(metrics.total_tool_calls), style="bold white")
    t.append(" tx", style="grey70")
    if metrics.error_count > 0:
        t.append(f"  {metrics.error_count}✗", style="bold red")
    return t


# ── PID lookup ───────────────────────────────────────────────────────

def _find_claude_pid(worktree_path: str) -> Optional[int]:
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,command"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        candidates = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if "claude" not in line:
                continue
            if worktree_path in line:
                parts = line.split(None, 1)
                if parts:
                    try:
                        candidates.append(int(parts[0]))
                    except ValueError:
                        continue
        return candidates[0] if candidates else None
    except (subprocess.TimeoutExpired, OSError):
        return None


# ── CIC status derivation ───────────────────────────────────────────

def _derive_cic_status(agent: AgentState) -> str:
    """Map internal status to CIC vocab, handling relaunched agents."""
    internal = agent.status
    ctx = agent.context or {}
    stale = ctx.get("stale", True)
    has_context = ctx.get("used_percentage") is not None

    # Fresh context means agent is actively running — override stale markers
    if internal in ("DONE", "PRE-REVIEW") and has_context and not stale:
        return "AIRBORNE"

    # Recent JSONL activity also means running
    if internal in ("DONE", "PRE-REVIEW") and agent.jsonl_metrics:
        last_activity = agent.jsonl_metrics.last_activity_at
        if last_activity:
            try:
                activity_ts = datetime.fromisoformat(
                    last_activity.replace("Z", "+00:00")
                ).timestamp()
                if time_mod.time() - activity_ts < 90:
                    return "AIRBORNE"
            except (ValueError, AttributeError):
                pass

    return STATUS_MAP.get(internal, internal)


# ── Watchdog event handler ───────────────────────────────────────────

class _SortieFileHandler(FileSystemEventHandler):
    DEBOUNCE_SECONDS = 0.5

    def __init__(self, app: CarrierCIC) -> None:
        super().__init__()
        self._app = app
        self._last_event_time: float = 0.0
        self._pending: bool = False
        self._lock = threading.Lock()

    def _should_trigger(self, path: str) -> bool:
        p = Path(path)
        return (
            p.suffix == ".jsonl"
            or p.name in (
                "context.json", "progress.md", "model.txt",
                "pre-review.done", "post-review.done", "directive.md",
            )
        )

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._should_trigger(event.src_path):
            self._debounced_refresh()

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._should_trigger(event.src_path):
            self._debounced_refresh()

    def _debounced_refresh(self) -> None:
        now = time_mod.monotonic()
        with self._lock:
            self._last_event_time = now
            if self._pending:
                return
            self._pending = True

        def _fire():
            while True:
                time_mod.sleep(self.DEBOUNCE_SECONDS)
                with self._lock:
                    elapsed = time_mod.monotonic() - self._last_event_time
                    if elapsed >= self.DEBOUNCE_SECONDS:
                        self._pending = False
                        break
            try:
                self._app.call_from_thread(self._app._on_file_change)
            except Exception:
                pass

        threading.Thread(target=_fire, daemon=True).start()


# ── Modal screens ────────────────────────────────────────────────────

class TicketInputScreen(ModalScreen[str]):
    BINDINGS = [Binding("escape", "dismiss('')", "Cancel")]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self._prompt, id="modal-prompt"),
            Input(placeholder="e.g. ENG-103 or Viper-1", id="modal-input"),
            id="modal-container",
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())


class BriefingScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def __init__(self, callsign: str, agent: AgentState) -> None:
        super().__init__()
        self._callsign = callsign
        self._agent = agent

    def compose(self) -> ComposeResult:
        a = self._agent
        ctx = a.context or {}
        pct = _ctx_remaining(ctx)
        pct = pct if pct is not None else "?"

        sortie_dir = Path(a.worktree_path) / ".sortie"
        try:
            directive = (sortie_dir / "directive.md").read_text(encoding="utf-8").strip()
        except OSError:
            directive = "(no directive found)"

        lines = directive.split("\n")
        criteria_lines, file_lines = [], []
        in_criteria, in_files = False, False
        for line in lines:
            if "acceptance" in line.lower() or "criteria" in line.lower():
                in_criteria, in_files = True, False
                continue
            if "file" in line.lower() and ("target" in line.lower() or "scope" in line.lower()):
                in_files, in_criteria = True, False
                continue
            if line.startswith("##") or line.startswith("**"):
                in_criteria = in_files = False
            if in_criteria and line.strip():
                criteria_lines.append(line.strip())
            if in_files and line.strip():
                file_lines.append(line.strip())

        content = Text()
        content.append(f"PRE-FLIGHT BRIEFING: {self._callsign} ({a.ticket_id})\n", style="bold bright_white")
        content.append("─" * 60 + "\n", style="grey50")
        content.append(f"Mission:   {a.title}\n", style="white")
        content.append(f"Pilot:     {a.model.capitalize()} │ TOS: {a.elapsed_time} │ Fuel: {pct}%\n", style="white")
        content.append("\n")

        if file_lines:
            content.append("ORDNANCE MANIFEST:\n", style="bold yellow")
            for f in file_lines[:10]:
                content.append(f"  ├─ {f}\n", style="white")
        if criteria_lines:
            content.append("\nACCEPTANCE CRITERIA:\n", style="bold yellow")
            for c in criteria_lines[:10]:
                content.append(f"  ☐ {c}\n", style="white")
        if not file_lines and not criteria_lines:
            content.append("\nDIRECTIVE:\n", style="bold yellow")
            for line in lines[:20]:
                content.append(f"  {line}\n", style="white")
            if len(lines) > 20:
                content.append(f"  ... ({len(lines) - 20} more lines)\n", style="dim")
        content.append("\n                                    ESC to close", style="dim")

        yield Vertical(Static(content, id="briefing-content"), id="modal-container")


class DebriefScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def __init__(self, callsign: str, agent: AgentState) -> None:
        super().__init__()
        self._callsign = callsign
        self._agent = agent

    def compose(self) -> ComposeResult:
        a = self._agent
        m = a.jsonl_metrics
        diff_lines, summary_line = [], ""
        try:
            result = subprocess.run(
                ["git", "diff", "--stat", "dev...HEAD"],
                cwd=a.worktree_path, capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                raw = result.stdout.strip().split("\n")
                if raw:
                    summary_line = raw[-1] if "changed" in (raw[-1] if raw else "") else ""
                    diff_lines = raw[:-1] if summary_line else raw
        except (subprocess.TimeoutExpired, OSError):
            diff_lines = ["(failed to read git diff)"]

        content = Text()
        content.append(f"DEBRIEF: {self._callsign} ({a.ticket_id})\n", style="bold bright_white")
        content.append("─" * 60 + "\n", style="grey50")
        content.append(
            f"TRAP: {datetime.now().strftime('%H:%M:%S')} │ TOS: {a.elapsed_time}"
            f" │ Ordnance: {m.total_tool_calls if m else 0} tx\n", style="white",
        )
        content.append("\nFILES MODIFIED:\n", style="bold yellow")
        for line in diff_lines[:20]:
            content.append(f"  {line}\n", style="white")
        if len(diff_lines) > 20:
            content.append(f"  ... ({len(diff_lines) - 20} more)\n", style="dim")
        if summary_line:
            content.append(f"\n{summary_line}\n", style="bold green")
        content.append("\n                                    ESC to close", style="dim")

        yield Vertical(Static(content, id="debrief-content"), id="modal-container")


# ── CIC Header with ASCII carrier art ────────────────────────────────

CARRIER_ART = """\
      ╱▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔╲
⚓════╡  USS TENKARA  ━━  CIC  ━━  ╞══
      ╲▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁╱"""


class CICHeader(Static):
    def render(self) -> Text:
        app = self.app
        state = app._state
        condition_red = app._condition_red
        condition_pulse = app._condition_pulse

        airborne = on_deck = recovered = 0
        for a in state.agents:
            cic = _derive_cic_status(a)
            if cic == "AIRBORNE":
                airborne += 1
            elif cic == "RECOVERED":
                recovered += 1
            else:
                on_deck += 1

        header = Text()
        # Carrier art
        for line in CARRIER_ART.split("\n"):
            header.append(line, style="bold bright_white")
            header.append("\n")

        # Status line
        header.append("CONDITION: ", style="bold white")
        if condition_red:
            header.append("RED", style="bold red" if condition_pulse else "bold dark_red")
        else:
            header.append("GREEN", style="bold green")

        header.append("  │  ", style="grey50")
        header.append(f"AIRBORNE: {airborne}", style="dodger_blue1")
        header.append("  │  ", style="grey50")
        header.append(f"ON DECK: {on_deck}", style="dark_orange")
        header.append("  │  ", style="grey50")
        header.append(f"RECOVERED: {recovered}", style="green")
        header.append("  │  ", style="grey50")
        header.append(datetime.now().strftime("%H:%M:%S LOCAL"), style="white")

        return header


# ── Detail panel: Sparkline + agent info ─────────────────────────────

class DetailPanel(Static):
    """Shows burn rate Sparkline and agent detail for the selected row."""

    def render(self) -> Text:
        app = self.app
        agent = app._selected_agent
        if not agent:
            return Text("  Select a sortie for detail view", style="grey50")

        callsign = app._callsigns.get(agent.ticket_id, agent.model)
        cic = _derive_cic_status(agent)
        ctx = agent.context or {}
        pct = _ctx_remaining(ctx) or 0
        m = agent.jsonl_metrics

        t = Text()
        t.append(f" {callsign}", style="bold bright_white")
        t.append(f" ({agent.ticket_id})", style="grey70")
        t.append(f"  │  {cic}", style=STATUS_COLORS.get(cic, "white"))
        t.append(f"  │  Fuel: {pct}%", style="bold red" if pct <= 20 else "white")
        t.append(f"  │  TOS: {agent.elapsed_time}", style="grey70")

        if m:
            t.append(f"  │  {m.total_tool_calls} tx", style="white")
            if m.error_count:
                t.append(f"  {m.error_count}✗", style="bold red")
            t.append(f"  │  {m.agent_spawns} subagents", style="yellow")

        return t


class BurnSparkline(Sparkline):
    """Sparkline widget showing burn rate for the selected agent."""
    DEFAULT_CSS = """
    BurnSparkline {
        height: 2;
        margin: 0 1;
        min-width: 20;
    }
    """


# ── Timeline widget ──────────────────────────────────────────────────

class TimelineBar(Static):
    """Horizontal timeline showing recent tool activity per agent."""

    def render(self) -> Text:
        app = self.app
        state = app._state
        if not state.agents:
            return Text("  No sorties to display", style="grey50")

        t = Text()
        t.append(" TIMELINE ", style="bold grey70")
        now = time_mod.time()

        for agent in state.agents:
            callsign = app._callsigns.get(agent.ticket_id, agent.model)
            cic = _derive_cic_status(agent)
            color = STATUS_COLORS.get(cic, "white").replace("bold ", "")

            # Build a 30-char activity bar from recent_timeline
            bar_width = 30
            bar_chars = ["╌"] * bar_width

            if agent.jsonl_metrics and agent.jsonl_metrics.recent_timeline:
                for event in agent.jsonl_metrics.recent_timeline:
                    ts_str = event.get("timestamp", "")
                    if ts_str:
                        try:
                            ts = datetime.fromisoformat(
                                ts_str.replace("Z", "+00:00")
                            ).timestamp()
                            # Map to bar position: last 10 minutes → 30 chars
                            age = now - ts
                            if 0 <= age <= 600:
                                pos = bar_width - 1 - int(age / 600 * (bar_width - 1))
                                pos = max(0, min(bar_width - 1, pos))
                                bar_chars[pos] = "█"
                        except (ValueError, AttributeError):
                            pass

            t.append(f"\n {callsign:<12} ", style="bold")
            # Render bar with color
            for ch in bar_chars:
                if ch == "█":
                    t.append(ch, style=color)
                else:
                    t.append(ch, style="grey23")
            t.append("│", style="grey37")
            t.append(" now", style="dim")

        return t


# ── Deck Status Footer ───────────────────────────────────────────────

class DeckStatus(Static):
    def render(self) -> Text:
        app = self.app
        state = app._state
        fuel_pcts, total_ordnance = [], 0
        for a in state.agents:
            ctx = a.context or {}
            pct = _ctx_remaining(ctx)
            if pct is not None:
                fuel_pcts.append(pct)
            if a.jsonl_metrics:
                total_ordnance += a.jsonl_metrics.total_tool_calls

        avg_fuel = round(sum(fuel_pcts) / len(fuel_pcts)) if fuel_pcts else 0
        catapults = sum(1 for a in state.agents if a.status == "WORKING")

        t = Text()
        t.append("DECK: ", style="bold white")
        t.append(f"CAT 1-{max(catapults, 1)} READY", style="green")
        t.append(" │ ", style="grey50")
        t.append(f"FUEL AVG: {avg_fuel}%", style="yellow" if avg_fuel <= 50 else "green")
        t.append(" │ ", style="grey50")
        t.append(f"TOTAL ORDNANCE: {total_ordnance} tx", style="white")
        t.append(" │ ", style="grey50")
        t.append(f"SORTIES TODAY: {state.total}", style="white")
        return t


# ── Flight Ops Strip Widget ──────────────────────────────────────────

class FlightOpsStrip(Static):
    """NTDS-style horizontal flight ops display — sprites track agent phases."""

    frame: reactive[int] = reactive(0)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._sprites: dict[str, FlightSprite] = {}
        self._strip_width: int = 80

    def on_mount(self) -> None:
        self.set_interval(0.15, self._tick)

    def _tick(self) -> None:
        self.frame += 1

    def watch_frame(self, value: int) -> None:
        self._advance_sprites()
        self.refresh()

    def _zone_col(self, zone: str, offset_pct: float = 0.0) -> int:
        """Convert zone name + offset to column position."""
        lo, hi = ZONE_PCT[zone]
        pct = lo + (hi - lo) * offset_pct
        return int(pct / 100 * self._strip_width)

    def _phase_from_cic(self, cic_status: str, sprite: FlightSprite) -> str:
        """Map CIC status to flight phase."""
        if cic_status == "MAYDAY":
            return "MAYDAY"
        if cic_status == "RECOVERED":
            if sprite.phase in ("RETURN", "TRAP"):
                return sprite.phase  # let animation finish
            if sprite.phase == "PARKED":
                return "PARKED"
            return "RETURN"  # start return sequence
        if cic_status == "ON APPROACH":
            if sprite.phase in ("RETURN", "TRAP"):
                return sprite.phase
            return "RETURN"
        if cic_status == "AIRBORNE":
            if sprite.phase in ("CAT", "LAUNCH", "CRUISE", "ORDNANCE"):
                return sprite.phase  # let current phase play out
            return "CAT"  # new/relaunched → start at catapult
        return sprite.phase

    def update_agents(self, agents: list) -> None:
        """Sync sprites with current agent states — called on data refresh."""
        app = self.app
        seen: set[str] = set()

        for agent in agents:
            tid = agent.ticket_id
            seen.add(tid)
            cic = _derive_cic_status(agent)
            callsign = app._callsigns.get(tid, agent.model)
            short_label = tid.replace("ENG-", "") if tid.startswith("ENG-") else callsign.split("-")[-1]

            if tid not in self._sprites:
                # New agent
                sprite = FlightSprite(
                    ticket_id=tid, callsign=short_label,
                    prev_status=cic,
                )
                if cic == "RECOVERED":
                    sprite.phase = "PARKED"
                    sprite.col = self._zone_col("DECK", 0.5)
                elif cic == "ON APPROACH":
                    sprite.phase = "RETURN"
                    sprite.col = self._zone_col("RTN", 0.3)
                elif cic == "AIRBORNE":
                    sprite.phase = "CAT"
                    sprite.col = self._zone_col("CAT", 0.5)
                else:
                    sprite.phase = "CAT"
                    sprite.col = self._zone_col("CAT", 0.5)
                self._sprites[tid] = sprite
            else:
                sprite = self._sprites[tid]
                sprite.callsign = short_label
                new_phase = self._phase_from_cic(cic, sprite)

                # Detect status transitions
                if cic != sprite.prev_status:
                    if cic == "AIRBORNE" and sprite.prev_status == "RECOVERED":
                        # Relaunch
                        new_phase = "CAT"
                        sprite.col = self._zone_col("CAT", 0.5)
                        sprite.phase_ticks = 0
                        sprite.anim_frame = 0
                    elif cic == "ON APPROACH" and sprite.phase not in ("RETURN", "TRAP"):
                        new_phase = "RETURN"
                        sprite.col = self._zone_col("RTN", 0.2)
                        sprite.phase_ticks = 0
                    elif cic == "RECOVERED" and sprite.phase not in ("RETURN", "TRAP", "PARKED"):
                        new_phase = "RETURN"
                        sprite.col = self._zone_col("RTN", 0.3)
                        sprite.phase_ticks = 0
                    elif cic == "MAYDAY":
                        new_phase = "MAYDAY"

                sprite.phase = new_phase
                sprite.prev_status = cic

        # Remove sprites for agents that no longer exist
        gone = set(self._sprites.keys()) - seen
        for tid in gone:
            del self._sprites[tid]

    def _advance_sprites(self) -> None:
        """Advance each sprite within its current phase (called every tick)."""
        for sprite in self._sprites.values():
            sprite.anim_frame += 1
            sprite.phase_ticks += 1
            phase = sprite.phase

            if phase == "CAT":
                # Stationary, engine spool animation
                if sprite.phase_ticks >= PHASE_TICKS["CAT"]:
                    sprite.phase = "LAUNCH"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "LAUNCH":
                # Fast acceleration: +2 cols per tick
                sprite.col += 2
                sky_start = self._zone_col("SKY", 0.0)
                if sprite.col >= sky_start or sprite.phase_ticks >= PHASE_TICKS["LAUNCH"]:
                    sprite.phase = "CRUISE"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0
                    sprite.col = max(sprite.col, sky_start)

            elif phase == "CRUISE":
                # Drift right: +1 col every 3rd tick
                if sprite.phase_ticks % 3 == 0:
                    sprite.col += 1
                tgt_start = self._zone_col("TGT", 0.0)
                if sprite.col >= tgt_start:
                    sprite.phase = "ORDNANCE"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "ORDNANCE":
                # Oscillate in TGT zone
                tgt_lo = self._zone_col("TGT", 0.0)
                tgt_hi = self._zone_col("TGT", 1.0) - 5
                mid = (tgt_lo + tgt_hi) // 2
                offset = int(3 * ((sprite.phase_ticks % 20) - 10) / 10)
                sprite.col = mid + offset

            elif phase == "RETURN":
                # Drift left: -1 col every 3rd tick
                if sprite.phase_ticks % 3 == 0:
                    sprite.col -= 1
                trap_end = self._zone_col("TRAP", 0.5)
                if sprite.col <= trap_end:
                    sprite.phase = "TRAP"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "TRAP":
                # Decelerate into deck
                if sprite.phase_ticks % 2 == 0 and sprite.col > self._zone_col("DECK", 0.5):
                    sprite.col -= 1
                if sprite.phase_ticks >= PHASE_TICKS["TRAP"]:
                    sprite.phase = "PARKED"
                    sprite.col = self._zone_col("DECK", 0.5)
                    sprite.phase_ticks = 0

            elif phase == "PARKED":
                sprite.col = self._zone_col("DECK", 0.5)

            elif phase == "MAYDAY":
                # Tumble at current position (no movement)
                pass

            # Clamp
            sprite.col = max(0, min(self._strip_width - 6, sprite.col))

        # Lane deconfliction
        sprite_list = sorted(self._sprites.values(), key=lambda s: s.col)
        for s in sprite_list:
            s.lane = 0
        for i in range(len(sprite_list)):
            for j in range(i + 1, len(sprite_list)):
                if abs(sprite_list[i].col - sprite_list[j].col) < 7:
                    sprite_list[j].lane = 1

    def _get_sprite_text(self, sprite: FlightSprite) -> str:
        """Get the current animation frame text for a sprite."""
        phase = sprite.phase
        if phase == "PARKED":
            return F14_PARKED
        frames = PHASE_SPRITES.get(phase)
        if frames:
            return frames[sprite.anim_frame % len(frames)]
        return F14_PARKED

    def _get_sprite_style(self, sprite: FlightSprite) -> str:
        """Get Rich style for sprite based on phase."""
        phase = sprite.phase
        if phase == "MAYDAY":
            return "bold red"
        if phase in ("RETURN", "TRAP"):
            return "green"
        if phase == "PARKED":
            return "dark_green"
        return "bold green"

    def render(self) -> Text:
        """Build the flight ops strip as Rich Text."""
        try:
            w = self.size.width - 2  # account for border chars
        except Exception:
            w = 80
        self._strip_width = max(40, w)
        sw = self._strip_width

        # Build 3 content rows as character buffers
        # Row 0: zone labels  Row 1: sprites (lane 0)  Row 2: sprites (lane 1) / zones
        # Row 3: callsign labels
        row_upper = [" "] * sw   # lane 1 sprites
        row_main = [" "] * sw    # lane 0 sprites
        row_labels = [" "] * sw  # callsign labels

        # Fill zone backgrounds
        zone_fills = {
            "DECK": "▓", "CAT": "░", "SKY": " ", "TGT": "·", "TRAP": "▓",
        }
        for zone, (lo_pct, hi_pct) in ZONE_PCT.items():
            lo = int(lo_pct / 100 * sw)
            hi = int(hi_pct / 100 * sw)
            ch = zone_fills.get(zone, " ")
            for c in range(lo, min(hi, sw)):
                row_upper[c] = ch
                row_main[c] = ch

        # Place zone labels in upper row
        zone_label_text = {
            "DECK": "DECK", "CAT": "CAT", "TGT": "TGT", "TRAP": "TRAP",
        }
        for zone, label in zone_label_text.items():
            lo_pct, hi_pct = ZONE_PCT[zone]
            mid = int((lo_pct + hi_pct) / 2 / 100 * sw) - len(label) // 2
            for i, ch in enumerate(label):
                pos = mid + i
                if 0 <= pos < sw:
                    row_upper[pos] = ch

        # Place sprites and their callsign labels
        sprite_positions: list[tuple[int, int, str, str, str]] = []  # col, lane, text, style, callsign
        for sprite in self._sprites.values():
            txt = self._get_sprite_text(sprite)
            style = self._get_sprite_style(sprite)
            sprite_positions.append((sprite.col, sprite.lane, txt, style, sprite.callsign))

        # Blit sprites into row buffers (we'll overlay with Rich styling in compose)
        # For now, mark sprite positions and build Rich Text with styling

        # Build Rich Text output
        result = Text()

        # Top border
        title = " FLIGHT OPS "
        border_len = sw - len(title)
        left_border = border_len // 2
        right_border = border_len - left_border
        result.append(" ╔", style="dim green")
        result.append("═" * left_border, style="dim green")
        result.append(title, style="bold green")
        result.append("═" * right_border, style="dim green")
        result.append("╗\n", style="dim green")

        # Upper row (zone labels + lane 1 sprites)
        result.append(" ║", style="dim green")
        upper_text = "".join(row_upper)
        self._render_row_with_sprites(result, upper_text, sw, sprite_positions, target_lane=1)
        result.append("║\n", style="dim green")

        # Main row (lane 0 sprites)
        result.append(" ║", style="dim green")
        main_text = "".join(row_main)
        self._render_row_with_sprites(result, main_text, sw, sprite_positions, target_lane=0)
        result.append("║\n", style="dim green")

        # Label row (callsigns under sprites)
        result.append(" ║", style="dim green")
        label_buf = [" "] * sw
        for col, lane, txt, style, callsign in sprite_positions:
            if lane == 0:  # only label main-lane sprites on this row
                label_start = col + len(txt) // 2 - len(callsign) // 2
                for i, ch in enumerate(callsign):
                    pos = label_start + i
                    if 0 <= pos < sw:
                        label_buf[pos] = ch
        label_str = "".join(label_buf)
        result.append(label_str, style="green")
        result.append("║\n", style="dim green")

        # Bottom border
        result.append(" ╚", style="dim green")
        result.append("═" * sw, style="dim green")
        result.append("╝", style="dim green")

        return result

    def _render_row_with_sprites(
        self, result: Text, bg: str, sw: int,
        sprites: list[tuple[int, int, str, str, str]],
        target_lane: int,
    ) -> None:
        """Render a row, overlaying sprites at their positions."""
        # Build a list of (start, end, text, style) for sprites on this lane
        overlays: list[tuple[int, int, str, str]] = []
        for col, lane, txt, style, _callsign in sprites:
            if lane == target_lane:
                overlays.append((col, col + len(txt), txt, style))
        overlays.sort(key=lambda o: o[0])

        # Zone style map for background chars
        def _zone_style(pos: int) -> str:
            pct = pos / sw * 100 if sw > 0 else 0
            if pct < 12 or pct >= 90:
                return "grey30"
            if pct < 22:
                return "grey23"
            if 65 <= pct < 72:
                return "dim red"
            return "grey15"

        pos = 0
        for start, end, txt, style in overlays:
            # Render background before sprite
            while pos < start and pos < sw:
                result.append(bg[pos], style=_zone_style(pos))
                pos += 1
            # Render sprite
            for ch in txt:
                if pos < sw:
                    result.append(ch, style=style)
                    pos += 1
        # Render remaining background
        while pos < sw:
            result.append(bg[pos], style=_zone_style(pos))
            pos += 1


# ── Main CIC App ─────────────────────────────────────────────────────

class CarrierCIC(App):
    """USS Tenkara CIC — v3 with reactive state, sparklines, animations."""

    CSS = """
    Screen { background: $surface; }

    #header-bar {
        dock: top; height: 4;
        background: $surface-darken-2;
        padding: 0 1;
    }

    #alert-bar {
        dock: top; height: auto; max-height: 3;
        background: $error; color: $text;
        text-align: center; display: none;
    }
    #alert-bar.visible { display: block; }

    #flight-strip {
        height: 7;
        background: $surface-darken-2;
    }

    #agent-table { height: 1fr; min-height: 5; }

    #detail-section {
        height: auto; max-height: 5;
        border-top: solid $accent;
    }
    #detail-panel {
        height: 1; padding: 0 1;
        background: $surface-darken-1;
    }
    #burn-sparkline {
        height: 2; margin: 0 1;
    }
    #timeline-bar {
        height: auto; max-height: 8;
        padding: 0 1;
        background: $surface-darken-1;
    }

    #radio-chatter-section {
        height: auto; max-height: 10;
        border-top: solid $accent;
    }
    #radio-chatter-title {
        height: 1; padding: 0 1;
        background: $surface-darken-1; color: $text-muted;
    }
    #radio-chatter-log { height: auto; max-height: 8; padding: 0 1; }

    #deck-status {
        dock: bottom; height: 1;
        background: $surface-darken-1;
        padding: 0 1; color: $text-muted;
    }

    #modal-container {
        align: center middle; width: 70; height: auto;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    #modal-prompt { margin-bottom: 1; text-style: bold; }
    """

    BINDINGS = [
        Binding("e", "eject", "Eject"),
        Binding("l", "relaunch", "Relaunch"),
        Binding("p", "ping_stations", "Ping All Stations"),
        Binding("b", "briefing", "Briefing"),
        Binding("d", "debrief", "Debrief"),
        Binding("q", "quit", "Quit"),
    ]

    # Reactive animation states — watchers auto-trigger repaints
    _heartbeat_bright = reactive(True)
    _bingo_blink = reactive(True)
    _condition_red = reactive(False)
    _condition_pulse = reactive(True)

    def __init__(self, project_dir: Optional[str] = None) -> None:
        super().__init__()
        self._project_dir = project_dir
        self._state = read_sortie_state(project_dir=project_dir)
        self._callsigns = CallsignRegistry()
        self._burn_history: dict[str, list[float]] = defaultdict(list)
        self._previous_tokens: dict[str, int] = {}
        self._previous_callsigns: set[str] = set()
        self._previous_statuses: dict[str, str] = {}
        self._new_radio_entries: set[str] = set()
        self._observer: Optional[Observer] = None
        self._refresh_in_flight: bool = False
        self._selected_agent: Optional[AgentState] = None
        self._sorted_agents: list[AgentState] = []
        # Row flash: callsign -> (color, expiry_monotonic)
        self._row_flashes: dict[str, tuple[str, float]] = {}

    def compose(self) -> ComposeResult:
        yield CICHeader(id="header-bar")
        yield Static("", id="alert-bar")
        yield FlightOpsStrip(id="flight-strip")
        yield DataTable(id="agent-table")
        yield Vertical(
            DetailPanel(id="detail-panel"),
            BurnSparkline([], id="burn-sparkline"),
            TimelineBar(id="timeline-bar"),
            id="detail-section",
        )
        yield Vertical(
            Static(" 📻 RADIO CHATTER", id="radio-chatter-title"),
            RichLog(id="radio-chatter-log", highlight=True, markup=True, auto_scroll=False),
            id="radio-chatter-section",
        )
        yield DeckStatus(id="deck-status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#agent-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns(
            "CALLSIGN / SITREP", "PILOT", "FUEL", "COMMS / ORD",
            "TOS", "STATUS",
        )
        self._do_refresh_sync()
        self._start_watchers()
        self.set_interval(10.0, self._do_refresh_async)
        self.set_interval(1.5, self._toggle_heartbeat)
        self.set_interval(2.0, self._toggle_condition)
        self.set_interval(1.0, self._toggle_bingo)

    def on_unmount(self) -> None:
        self._stop_watchers()

    # ── Reactive watchers — auto-repaint on state change ─────────────

    def watch__heartbeat_bright(self, value: bool) -> None:
        self._refresh_table()

    def watch__condition_red(self, value: bool) -> None:
        self.query_one("#header-bar", CICHeader).refresh()

    def watch__condition_pulse(self, value: bool) -> None:
        if self._condition_red:
            self.query_one("#header-bar", CICHeader).refresh()

    # ── Animation toggles ────────────────────────────────────────────

    def _toggle_heartbeat(self) -> None:
        self._heartbeat_bright = not self._heartbeat_bright

    def _toggle_condition(self) -> None:
        if self._condition_red:
            self._condition_pulse = not self._condition_pulse

    def _toggle_bingo(self) -> None:
        self._bingo_blink = not self._bingo_blink

    # ── Table row selection → detail panel ───────────────────────────

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Update detail panel and sparkline when cursor moves."""
        try:
            row_idx = event.cursor_row
            if row_idx < len(self._sorted_agents):
                self._selected_agent = self._sorted_agents[row_idx]
                # Update sparkline with burn history
                tid = self._selected_agent.ticket_id
                history = self._burn_history.get(tid, [])
                sparkline = self.query_one("#burn-sparkline", BurnSparkline)
                sparkline.data = history if history else [0]
                self.query_one("#detail-panel", DetailPanel).refresh()
        except Exception:
            pass

    # ── Watchdog lifecycle ───────────────────────────────────────────

    def _start_watchers(self) -> None:
        handler = _SortieFileHandler(self)
        self._observer = Observer()
        worktrees_root = get_worktrees_root(self._project_dir)
        if worktrees_root.is_dir():
            self._observer.schedule(handler, str(worktrees_root), recursive=True)
        watched_jsonl: set[str] = set()
        for agent in self._state.agents:
            encoded = encode_project_path(agent.worktree_path)
            jsonl_dir = CLAUDE_PROJECTS_DIR / encoded
            dir_str = str(jsonl_dir)
            if jsonl_dir.is_dir() and dir_str not in watched_jsonl:
                self._observer.schedule(handler, dir_str, recursive=True)
                watched_jsonl.add(dir_str)
        try:
            self._observer.start()
        except Exception:
            self._observer = None

    def _stop_watchers(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None

    # ── Refresh lifecycle ────────────────────────────────────────────

    def _on_file_change(self) -> None:
        self._do_refresh_async()

    def _do_refresh_sync(self) -> None:
        self._state = read_sortie_state(project_dir=self._project_dir)
        self._update_burn_history()
        self._detect_transitions()
        self._refresh_ui()

    def _do_refresh_async(self) -> None:
        if self._refresh_in_flight:
            return
        self._refresh_in_flight = True
        self.run_worker(self._bg_read_state, name="refresh", exclusive=True, thread=True)

    def _bg_read_state(self) -> None:
        self._state = read_sortie_state(project_dir=self._project_dir)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name == "refresh" and event.state == WorkerState.SUCCESS:
            self._refresh_in_flight = False
            self._update_burn_history()
            self._detect_transitions()
            self._refresh_ui()
            self._ensure_jsonl_watchers()
        elif event.worker.name == "refresh" and event.state in (
            WorkerState.ERROR, WorkerState.CANCELLED
        ):
            self._refresh_in_flight = False

    def _ensure_jsonl_watchers(self) -> None:
        if not self._observer:
            return
        already_watched = {
            w.path for w in self._observer._watches
        } if hasattr(self._observer, '_watches') else set()
        handler = _SortieFileHandler(self)
        for agent in self._state.agents:
            encoded = encode_project_path(agent.worktree_path)
            jsonl_dir = CLAUDE_PROJECTS_DIR / encoded
            dir_str = str(jsonl_dir)
            if jsonl_dir.is_dir() and dir_str not in already_watched:
                try:
                    self._observer.schedule(handler, dir_str, recursive=True)
                except Exception:
                    pass

    # ── State processing ─────────────────────────────────────────────

    def _update_burn_history(self) -> None:
        for agent in self._state.agents:
            tid = agent.ticket_id
            m = agent.jsonl_metrics
            current = m.total_tokens if m else 0
            prev = self._previous_tokens.get(tid, 0)
            if prev > 0:
                delta = max(0, current - prev)
                self._burn_history[tid].append(delta)
                if len(self._burn_history[tid]) > 10:
                    self._burn_history[tid] = self._burn_history[tid][-10:]
            self._previous_tokens[tid] = current

    def _detect_transitions(self) -> None:
        current_callsigns: set[str] = set()

        for agent in self._state.agents:
            callsign = self._callsigns.get(agent.ticket_id, agent.model)
            current_callsigns.add(callsign)
            cic_status = _derive_cic_status(agent)
            prev_status = self._previous_statuses.get(callsign)

            if callsign not in self._previous_callsigns:
                cat_num = len(current_callsigns)
                self.notify(
                    f"CATAPULT {cat_num} — {callsign} AIRBORNE, DECK CLEAR",
                    title="🟢 LAUNCH", timeout=5,
                )
                self._flash_row(callsign, "cyan", 2.0)
                _macos_notify("USS Tenkara — LAUNCH", f"{callsign} AIRBORNE")

            elif prev_status and prev_status != cic_status:
                if cic_status == "RECOVERED":
                    self.notify(
                        f"TRAP — {callsign} RECOVERED │ Wire 3 │ TOS: {agent.elapsed_time}",
                        title="✓ TRAP", timeout=5,
                    )
                    self._flash_row(callsign, "green", 2.0)

                elif cic_status == "MAYDAY":
                    self.notify(
                        f"MAYDAY — {callsign} │ Pilot ejected │ Press 'l' to relaunch",
                        title="🔴 MAYDAY", severity="error", timeout=8,
                    )
                    self._flash_row(callsign, "red", 3.0)
                    _macos_notify("USS Tenkara — MAYDAY", f"{callsign} PILOT EJECTED")

                elif cic_status == "AIRBORNE" and prev_status == "RECOVERED":
                    self.notify(
                        f"{callsign} RELAUNCHED — back on station",
                        title="🔁 RELAUNCH", timeout=5,
                    )
                    self._flash_row(callsign, "cyan", 2.0)
                    _macos_notify("USS Tenkara — RELAUNCH", f"{callsign} back on station")

                elif cic_status == "ON APPROACH" and prev_status == "AIRBORNE":
                    self.notify(
                        f"{callsign} ON APPROACH — pre-review complete",
                        title="🔄 APPROACH", timeout=4,
                    )
                    self._flash_row(callsign, "dark_orange", 1.5)

            # Fuel critical notifications (macOS)
            ctx = agent.context or {}
            pct = _ctx_remaining(ctx)
            if pct is not None and pct <= 10:
                _macos_notify("USS Tenkara — BINGO FUEL", f"{callsign} at {pct}% remaining")

            self._previous_statuses[callsign] = cic_status

        self._previous_callsigns = current_callsigns

    def _flash_row(self, callsign: str, color: str, duration: float) -> None:
        """Register a temporary row flash for a callsign."""
        expiry = time_mod.monotonic() + duration
        self._row_flashes[callsign] = (color, expiry)
        # Schedule cleanup
        self.set_timer(duration + 0.1, self._clear_expired_flashes)

    def _clear_expired_flashes(self) -> None:
        now = time_mod.monotonic()
        expired = [k for k, (_, exp) in self._row_flashes.items() if now >= exp]
        for k in expired:
            del self._row_flashes[k]
        if expired:
            self._refresh_table()

    # ── UI rendering ─────────────────────────────────────────────────

    def _refresh_ui(self) -> None:
        self._refresh_table()
        self._refresh_radio_chatter()
        self.query_one("#header-bar", CICHeader).refresh()
        self.query_one("#deck-status", DeckStatus).refresh()
        self.query_one("#detail-panel", DetailPanel).refresh()
        self.query_one("#timeline-bar", TimelineBar).refresh()
        # Sync flight ops strip with current agent states
        try:
            self.query_one("#flight-strip", FlightOpsStrip).update_agents(self._state.agents)
        except Exception:
            pass

    def _refresh_table(self) -> None:
        table = self.query_one("#agent-table", DataTable)
        table.clear()

        critical_agents: list[str] = []
        any_critical = False
        now = time_mod.monotonic()

        # Sort: AIRBORNE first, then ON APPROACH, MAYDAY, RECOVERED last; within same status by ticket ID
        self._sorted_agents = sorted(
            self._state.agents,
            key=lambda a: (STATUS_SORT_ORDER.get(_derive_cic_status(a), 9), a.ticket_id),
        )

        # Build latest radio chatter per agent for sitrep sync
        all_entries = get_all_progress_entries(self._state.agents, max_entries=50)
        latest_chatter: dict[str, str] = {}
        for entry in all_entries:
            tid = entry.get("ticket_id", "")
            if tid not in latest_chatter:
                latest_chatter[tid] = entry.get("message", "")

        for agent in self._sorted_agents:
            callsign = self._callsigns.get(agent.ticket_id, agent.model)
            cic_status = _derive_cic_status(agent)

            # Check for active row flash
            flash_color = None
            if callsign in self._row_flashes:
                color, expiry = self._row_flashes[callsign]
                if now < expiry:
                    flash_color = color

            # ── Callsign + sitrep (two-line cell) ──
            callsign_display = f"{callsign} ({agent.ticket_id})"
            if agent.is_sub_agent:
                label = agent.sub_name or agent.ticket_id
                callsign_display = f"  └ {callsign} ({agent.ticket_id}/{label})"

            # Prefer latest JSONL assistant message for sitrep (what Claude actually said)
            m = agent.jsonl_metrics
            sitrep = ""
            if m and m.recent_messages:
                last_msg = m.recent_messages[-1].get("text", "").strip()
                if last_msg:
                    sitrep = last_msg.split("\n")[0].strip()
            if not sitrep:
                sitrep = latest_chatter.get(agent.ticket_id, "")
            if not sitrep:
                sitrep = agent.last_progress[-1] if agent.last_progress else "(no comms)"
                if sitrep.startswith("[") and "]" in sitrep:
                    sitrep = sitrep[sitrep.index("]") + 1:].strip()
            if len(sitrep) > 60:
                sitrep = sitrep[:57] + "..."

            # Apply row flash styling
            flash_style = f"on {flash_color}" if flash_color else ""

            cs_cell = Text()
            cs_cell.append(callsign_display, style=f"bold {flash_style}".strip())
            cs_cell.append("\n")
            cs_cell.append(f"  {sitrep}", style=f"grey70 italic {flash_style}".strip())

            # ── Pilot ──
            pilot = agent.model.capitalize()

            # ── Fuel (remaining context) ──
            ctx = agent.context or {}
            pct = _ctx_remaining(ctx)
            stale = ctx.get("stale", True)
            bar = fuel_gauge(pct, bingo_blink=self._bingo_blink)
            if stale and pct is not None:
                bar.append(" ⏸", style="dim")

            if pct is not None and pct <= 20:
                any_critical = True
                critical_agents.append(f"{callsign} ({pct}%)")

            # ── Comms + Ordnance (two-line cell) ──
            m = agent.jsonl_metrics
            input_tokens = m.input_tokens if m else None
            output_tokens = m.output_tokens if m else None

            comms_ord = Text()
            comms_line = format_comms(input_tokens, output_tokens)
            comms_ord.append_text(comms_line)
            comms_ord.append("\n")
            ord_line = make_ordnance_text(m)
            comms_ord.append_text(ord_line)

            # ── Status (two-line: status + burn sparkline) ──
            icon = STATUS_ICONS.get(cic_status, "?")
            color = STATUS_COLORS.get(cic_status, "white")
            status_text = Text()
            if cic_status == "AIRBORNE":
                icon_style = "bold bright_white" if self._heartbeat_bright else "dim"
                status_text.append(f"{icon} ", style=icon_style)
            else:
                status_text.append(f"{icon} ", style=color)
            status_text.append(cic_status, style=f"bold {color}")

            if stale and pct is not None and cic_status == "AIRBORNE":
                status_text.append("\n  COMMS DARK", style="dim grey50")
            else:
                sparkline = make_burn_sparkline(self._burn_history.get(agent.ticket_id, []))
                status_text.append("\n  ")
                status_text.append_text(sparkline)

            table.add_row(
                cs_cell,
                Text(pilot, style=f"italic {flash_style}".strip()),
                bar,
                comms_ord,
                Text(agent.elapsed_time, style=f"grey70 {flash_style}".strip()),
                status_text,
                height=2,
            )

        self._condition_red = any_critical
        alert_bar = self.query_one("#alert-bar")
        if critical_agents:
            names = ", ".join(critical_agents)
            alert_bar.update(f"⚠ FUEL CRITICAL: {names} — BINGO RTB ⚠")
            alert_bar.add_class("visible")
        else:
            alert_bar.remove_class("visible")

    def _refresh_radio_chatter(self) -> None:
        log = self.query_one("#radio-chatter-log", RichLog)
        was_at_top = log.scroll_y <= 0
        log.clear()

        # Merge progress entries with JSONL assistant messages
        entries = get_all_progress_entries(self._state.agents, max_entries=10)

        # Add JSONL assistant text messages
        for agent in self._state.agents:
            m = agent.jsonl_metrics
            if m and m.recent_messages:
                for msg_entry in m.recent_messages[-5:]:
                    ts_raw = msg_entry.get("timestamp", "")
                    text = msg_entry.get("text", "").strip()
                    if not text:
                        continue
                    # Take first line only, truncate for readability
                    first_line = text.split("\n")[0].strip()
                    if len(first_line) > 120:
                        first_line = first_line[:117] + "..."
                    # Format timestamp to HH:MM
                    ts_display = "--:--"
                    sort_key = ""
                    if ts_raw:
                        try:
                            dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                            ts_display = dt.strftime("%H:%M")
                            sort_key = ts_raw
                        except (ValueError, AttributeError):
                            pass
                    entries.append({
                        "ticket_id": agent.ticket_id,
                        "timestamp": ts_display,
                        "message": first_line,
                        "type": "normal",
                        "sort_key": sort_key or f"{ts_display}:{agent.ticket_id}",
                        "source": "jsonl",
                    })

        # De-dupe by message content per agent, prefer jsonl source
        seen: dict[str, str] = {}  # (ticket_id, msg[:40]) -> source
        deduped = []
        for entry in entries:
            dedup_key = f"{entry.get('ticket_id')}:{entry.get('message', '')[:40]}"
            prev_source = seen.get(dedup_key)
            if prev_source is None:
                seen[dedup_key] = entry.get("source", "progress")
                deduped.append(entry)
            elif prev_source == "progress" and entry.get("source") == "jsonl":
                # Replace progress entry with jsonl version
                deduped = [e for e in deduped if f"{e.get('ticket_id')}:{e.get('message', '')[:40]}" != dedup_key]
                seen[dedup_key] = "jsonl"
                deduped.append(entry)

        # Sort by sort_key descending, take top 10
        deduped.sort(key=lambda e: e.get("sort_key", ""), reverse=True)
        entries = deduped[:10]

        if not entries:
            log.write(Text("  All stations quiet.", style="grey50"))
            return

        current_keys = set()
        for entry in entries:
            key = f"{entry['ticket_id']}:{entry['timestamp']}:{entry.get('message', '')[:30]}"
            current_keys.add(key)

        for entry in entries:
            ts = entry.get("timestamp", "--:--")
            ticket = entry.get("ticket_id", "???")
            msg = entry.get("message", "")
            entry_type = entry.get("type", "normal")

            callsign = ticket
            for agent in self._state.agents:
                if agent.ticket_id == ticket:
                    callsign = self._callsigns.get(ticket, agent.model)
                    break

            key = f"{ticket}:{ts}:{msg[:30]}"
            is_new = key not in self._new_radio_entries

            line = Text()
            line.append(f" [{ts}] ", style="grey70")
            line.append(f"{callsign}: ", style="bold bright_white" if is_new else "bold")

            if entry_type == "error":
                line.append(msg, style="bold red")
            elif entry_type == "success":
                line.append(msg, style="bold green")
            else:
                line.append(msg, style="bold bright_white" if is_new else "white")

            log.write(line)
        self._new_radio_entries = current_keys
        if was_at_top:
            log.scroll_home(animate=False)

    # ── Agent lookup ─────────────────────────────────────────────────

    def _find_agent(self, query: str) -> Optional[AgentState]:
        q = query.upper().strip()
        for agent in self._state.agents:
            cs = self._callsigns.get(agent.ticket_id, agent.model).upper()
            if cs == q:
                return agent
        if "/" in q:
            ticket_part, sub_part = q.split("/", 1)
            for agent in self._state.agents:
                if agent.ticket_id.upper() == ticket_part and (agent.sub_name or "").upper() == sub_part:
                    return agent
        for agent in self._state.agents:
            if agent.ticket_id.upper() == q:
                return agent
        return None

    def _get_selected_agent(self) -> Optional[AgentState]:
        table = self.query_one("#agent-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_idx = table.cursor_row
            if row_idx < len(self._sorted_agents):
                return self._sorted_agents[row_idx]
        except Exception:
            pass
        return None

    # ── Actions ──────────────────────────────────────────────────────

    def action_ping_stations(self) -> None:
        self._do_refresh_sync()
        self.notify("All stations, CIC — SITREP updated", timeout=2)

    async def action_eject(self) -> None:
        ticket_id = await self.push_screen_wait(
            TicketInputScreen("EJECT — enter callsign or ticket ID:")
        )
        if not ticket_id:
            return
        agent = self._find_agent(ticket_id)
        if not agent:
            self.notify(f"Callsign {ticket_id} not found", severity="error", timeout=3)
            return
        callsign = self._callsigns.get(agent.ticket_id, agent.model)
        pid = _find_claude_pid(agent.worktree_path)
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                self.notify(f"Sent SIGTERM to {callsign} (PID {pid})", title="EJECT", timeout=3)
            except OSError as e:
                self.notify(f"Eject failed for {callsign}: {e}", severity="error", timeout=3)
        else:
            self.notify(f"No running process found for {callsign}", severity="warning", timeout=3)

    async def action_relaunch(self) -> None:
        ticket_id = await self.push_screen_wait(
            TicketInputScreen("RELAUNCH — enter callsign or ticket ID:")
        )
        if not ticket_id:
            return
        agent = self._find_agent(ticket_id)
        if not agent:
            self.notify(f"Callsign {ticket_id} not found", severity="error", timeout=3)
            return
        callsign = self._callsigns.get(agent.ticket_id, agent.model)
        spawn_candidates = []
        if self._project_dir:
            spawn_candidates.append(
                Path(self._project_dir) / ".claude" / "skills" / "sortie" / "scripts" / "spawn-pane.sh"
            )
        spawn_candidates.append(Path.cwd() / ".claude" / "skills" / "sortie" / "scripts" / "spawn-pane.sh")
        spawn_script = next((c for c in spawn_candidates if c.exists()), None)
        if not spawn_script:
            self.notify("spawn-pane.sh not found — cannot relaunch", severity="error", timeout=3)
            return
        try:
            subprocess.Popen(
                [str(spawn_script), agent.worktree_path, agent.model, agent.ticket_id],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            cat_num = sum(1 for a in self._state.agents if a.status == "WORKING") + 1
            self.notify(f"Respawning {callsign} — catapult {cat_num}", title="RELAUNCH", timeout=3)
        except (FileNotFoundError, PermissionError, OSError) as e:
            self.notify(f"Relaunch failed: {e}", severity="error", timeout=3)

    async def action_briefing(self) -> None:
        agent = self._get_selected_agent()
        if not agent:
            ticket_id = await self.push_screen_wait(
                TicketInputScreen("BRIEFING — enter callsign or ticket ID:")
            )
            if not ticket_id:
                return
            agent = self._find_agent(ticket_id)
            if not agent:
                self.notify(f"Callsign {ticket_id} not found", severity="error", timeout=3)
                return
        callsign = self._callsigns.get(agent.ticket_id, agent.model)
        await self.push_screen(BriefingScreen(callsign, agent))

    async def action_debrief(self) -> None:
        agent = self._get_selected_agent()
        if not agent:
            ticket_id = await self.push_screen_wait(
                TicketInputScreen("DEBRIEF — enter callsign or ticket ID:")
            )
            if not ticket_id:
                return
            agent = self._find_agent(ticket_id)
            if not agent:
                self.notify(f"Callsign {ticket_id} not found", severity="error", timeout=3)
                return
        callsign = self._callsigns.get(agent.ticket_id, agent.model)
        await self.push_screen(DebriefScreen(callsign, agent))


# ── Entry point ──────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="USS Tenkara CIC Dashboard")
    parser.add_argument(
        "--project-dir",
        default=os.environ.get("SORTIE_PROJECT_DIR"),
        help="Project root directory (default: SORTIE_PROJECT_DIR env or git detection)",
    )
    args = parser.parse_args()
    app = CarrierCIC(project_dir=args.project_dir)
    app.run()


if __name__ == "__main__":
    main()
