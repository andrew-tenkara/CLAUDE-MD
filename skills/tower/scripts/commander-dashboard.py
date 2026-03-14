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

# ── Constants ────────────────────────────────────────────────────────

STATUS_ICONS = {
    "AIRBORNE": "✈",
    "ON_APPROACH": "🔄",
    "RECOVERED": "✓",
    "MAYDAY": "⚠",
    "IDLE": "⏸",
    "AAR": "⛽",
    "SAR": "🚁",
}

STATUS_COLORS = {
    "AIRBORNE": "green",
    "ON_APPROACH": "dark_orange",
    "RECOVERED": "grey50",
    "MAYDAY": "bold red",
    "IDLE": "yellow",
    "AAR": "cyan",
    "SAR": "bold magenta",
}

STATUS_SORT_ORDER = {
    "MAYDAY": 0,
    "AIRBORNE": 1,
    "IDLE": 2,
    "AAR": 3,
    "SAR": 4,
    "ON_APPROACH": 5,
    "RECOVERED": 6,
}

# macOS sounds (built-in, no deps)
SOUNDS = {
    "mayday": "/System/Library/Sounds/Submarine.aiff",
    "recovered": "/System/Library/Sounds/Glass.aiff",
    "squadron_complete": "/System/Library/Sounds/Hero.aiff",
    "bingo": "/System/Library/Sounds/Ping.aiff",
}


def _play_sound(sound_key: str) -> None:
    """Sound effects disabled for now."""
    return


def _notify(title: str, message: str) -> None:
    """macOS notification via terminal-notifier or osascript fallback."""
    icon = Path(__file__).resolve().parent.parent / "assets" / "uss-tenkara.png"
    try:
        cmd = ["terminal-notifier", "-title", title, "-message", message]
        if icon.exists():
            cmd.extend(["-appIcon", str(icon)])
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        # Fallback to osascript
        script = f'display notification "{message}" with title "{title}"'
        try:
            subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass


# ── Legacy agent status mapping ──────────────────────────────────────

# Map sortie-state internal status → commander status
_LEGACY_STATUS_MAP = {
    "WORKING": "AIRBORNE",
    "PRE-REVIEW": "ON_APPROACH",
    "DONE": "RECOVERED",
}


def _ctx_remaining(ctx: dict) -> int:
    """Convert used_percentage to fuel remaining (0-100)."""
    used = ctx.get("used_percentage")
    if used is None:
        return 50  # unknown → assume half
    return max(0, 100 - int(used))


_FLIGHT_STATUS_MAP = {
    "PREFLIGHT": "IDLE",
    "AIRBORNE": "AIRBORNE",
    "HOLDING": "IDLE",
    "ON_APPROACH": "ON_APPROACH",
    # Agent-reported RECOVERED is downgraded to IDLE — agents should never
    # set RECOVERED themselves. Only the bash EXIT trap (which writes
    # .sortie/session-ended) triggers real RECOVERED status.
    "RECOVERED": "IDLE",
}


def _map_flight_status(reported: str) -> str:
    """Map agent-reported flight status to commander status."""
    return _FLIGHT_STATUS_MAP.get(reported.upper(), "")


# Max age (seconds) for flight-status.json before it's considered stale
_FLIGHT_STATUS_MAX_AGE = 60

# Compaction recovery — fuel jump threshold and SAR animation timing
_FUEL_JUMP_THRESHOLD = 15   # fuel gain (%) to count as compaction event
_SAR_RECOVERY_DELAY = 8     # seconds to let crash/helo animation play before relaunch
_AAR_RECOVERY_DELAY = 5     # seconds for refueling animation before returning to AIRBORNE


def _flight_status_is_stale(agent_state: "AgentState") -> bool:
    """Check if an agent's flight-status.json is stale (no recent update)."""
    if not agent_state.flight_status:
        return False
    sortie_dir = Path(agent_state.worktree_path) / ".sortie"
    fs_path = sortie_dir / "flight-status.json"
    try:
        data = json.loads(fs_path.read_text())
        ts = data.get("timestamp", 0)
        return (time_mod.time() - ts) > _FLIGHT_STATUS_MAX_AGE
    except (OSError, json.JSONDecodeError, TypeError):
        return True  # Can't read it → stale


def _clear_flight_status(worktree_path: str) -> None:
    """Remove flight-status.json so it can't resurrect a RECOVERED pilot."""
    try:
        fs_path = Path(worktree_path) / ".sortie" / "flight-status.json"
        if fs_path.exists():
            fs_path.unlink()
    except OSError:
        pass


def _derive_legacy_status(agent: AgentState) -> str:
    """Map legacy AgentState to commander status, with liveness detection."""
    internal = agent.status
    ctx = agent.context or {}
    stale = ctx.get("stale", True)
    has_context = ctx.get("used_percentage") is not None

    # Fresh context means agent is actively running
    if internal in ("DONE", "PRE-REVIEW") and has_context and not stale:
        return "AIRBORNE"

    # Recent JSONL activity also means running
    if internal in ("DONE", "PRE-REVIEW") and agent.jsonl_metrics:
        last_activity = agent.jsonl_metrics.last_activity_at
        if last_activity:
            try:
                from datetime import datetime as _dt
                activity_ts = _dt.fromisoformat(
                    last_activity.replace("Z", "+00:00")
                ).timestamp()
                if time_mod.time() - activity_ts < 90:
                    return "AIRBORNE"
            except (ValueError, AttributeError):
                pass

    return _LEGACY_STATUS_MAP.get(internal, "MAYDAY")


# ── Watchdog handler for legacy worktree files ──────────────────────

class _WorktreeFileHandler(FileSystemEventHandler):
    """Debounced file watcher for sortie worktree state changes."""
    DEBOUNCE_SECONDS = 0.5

    def __init__(self, app: PriFlyCommander) -> None:
        super().__init__()
        self._app = app
        self._last_event: float = 0.0
        self._pending: bool = False
        self._lock = __import__("threading").Lock()

    def _should_trigger(self, path: str) -> bool:
        p = Path(path)
        return (
            p.suffix == ".jsonl"
            or p.name in (
                "context.json", "progress.md", "model.txt",
                "pre-review.done", "post-review.done", "directive.md",
                "status-hint.txt", "server-status.txt", "flight-status.json",
                "session-ended", "command.json",
            )
            # Mission queue + managed servers
            or (p.suffix == ".json" and ("mission-queue" in str(p) or p.name == "managed-servers.json"))
        )

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._should_trigger(event.src_path):
            self._debounced_refresh()

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._should_trigger(event.src_path):
            self._debounced_refresh()

    def _debounced_refresh(self) -> None:
        import threading
        now = time_mod.monotonic()
        with self._lock:
            self._last_event = now
            if self._pending:
                return
            self._pending = True

        def _fire():
            while True:
                time_mod.sleep(self.DEBOUNCE_SECONDS)
                with self._lock:
                    elapsed = time_mod.monotonic() - self._last_event
                    if elapsed >= self.DEBOUNCE_SECONDS:
                        self._pending = False
                        break
            try:
                self._app.call_from_thread(self._app._sync_legacy_agents)
            except Exception:
                pass

        threading.Thread(target=_fire, daemon=True).start()


# ── Fuel gauge ───────────────────────────────────────────────────────

def fuel_gauge(pct: int, width: int = 10, blink: bool = False) -> Text:
    if pct <= 20:
        style = "bold red"
    elif pct <= 50:
        style = "yellow"
    else:
        style = "green"

    bar = Text()
    filled = round(pct / 100 * width)
    empty = width - filled
    bar.append("━" * filled, style=style)
    bar.append("╌" * empty, style="grey37")
    bar.append(f" {pct}%", style=style)

    if pct <= 10:
        bar.append(" BINGO!", style="bold bright_red" if blink else "dim red")
    elif pct <= 30:
        bar.append(" ⚠", style="bold red")

    return bar


def _format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _tool_icon(tool_name: str) -> str:
    """Return a compact icon for a tool name, like Claude Code's display."""
    icons = {
        "Edit": "~",
        "Write": "+",
        "Read": "?",
        "Bash": "$",
        "Grep": "/",
        "Glob": "*",
        "Agent": ">",
    }
    return icons.get(tool_name, "#")


# ── Code fence regex: ```lang\n...\n``` ──
_CODE_FENCE_RE = _re.compile(
    r"```(\w*)\n(.*?)```",
    _re.DOTALL,
)

# ── File extension to language map for Syntax ──
_EXT_TO_LANG = {
    ".ts": "typescript", ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript",
    ".py": "python", ".rs": "rust", ".go": "go", ".sh": "bash", ".bash": "bash",
    ".css": "css", ".scss": "scss", ".html": "html", ".json": "json", ".yaml": "yaml",
    ".yml": "yaml", ".toml": "toml", ".sql": "sql", ".md": "markdown",
    ".rb": "ruby", ".java": "java", ".c": "c", ".cpp": "cpp", ".h": "c",
}


def _guess_lang_from_path(file_path: str) -> str:
    """Guess the syntax language from a file path extension."""
    for ext, lang in _EXT_TO_LANG.items():
        if file_path.endswith(ext):
            return lang
    return "text"


def _render_assistant_content(log, content: str) -> None:
    """Render assistant text with syntax-highlighted code blocks.

    Splits content on ``` fences. Prose gets Rich Text with inline code,
    code blocks get Syntax with monokai theme in a bordered panel.
    """
    parts = _CODE_FENCE_RE.split(content)
    # parts alternates: [prose, lang, code, prose, lang, code, ...]
    i = 0
    while i < len(parts):
        if i + 2 < len(parts) and (i % 3) == 0:
            prose = parts[i].strip()
            if prose:
                _render_prose(log, prose)
            lang = parts[i + 1] or "text"
            code = parts[i + 2]
            if code.strip():
                try:
                    syn = Syntax(
                        code.strip(), lang,
                        theme="monokai", line_numbers=len(code.strip().splitlines()) > 3,
                        word_wrap=True, padding=(0, 1),
                    )
                    log.write(Panel(
                        syn, border_style="dim cyan", expand=True,
                        padding=(0, 0), title=lang if lang != "text" else None,
                        title_align="right",
                    ))
                except Exception:
                    log.write(Panel(code.strip(), border_style="dim cyan"))
            i += 3
        else:
            prose = parts[i].strip()
            if prose:
                _render_prose(log, prose)
            i += 1


def _render_prose(log, prose: str) -> None:
    """Render a prose block with markdown-like formatting."""
    t = Text()
    for line in prose.split("\n"):
        stripped = line.strip()
        if not stripped:
            t.append("\n")
            continue
        # Heading-like lines (## Foo)
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            t.append(f"  {heading}\n", style="bold #61afef")
        # Bullet points
        elif stripped.startswith(("- ", "* ", "• ")):
            bullet_content = stripped[2:]
            t.append("  • ", style="#c678dd")
            _append_inline_code(t, bullet_content)
            t.append("\n")
        # Numbered lists
        elif len(stripped) > 2 and stripped[0].isdigit() and stripped[1] in ".)":
            num = stripped[:2]
            rest = stripped[2:].strip()
            t.append(f"  {num} ", style="#c678dd")
            _append_inline_code(t, rest)
            t.append("\n")
        # Bold lines (**foo**)
        elif stripped.startswith("**") and stripped.endswith("**"):
            t.append(f"  {stripped[2:-2]}\n", style="bold #e5c07b")
        else:
            t.append("  ")
            _append_inline_code(t, stripped)
            t.append("\n")
    if t.plain.strip():
        log.write(t)


def _append_inline_code(t: Text, text: str) -> None:
    """Append text with inline `backtick` spans highlighted."""
    segments = text.split("`")
    for idx, seg in enumerate(segments):
        if idx % 2 == 1:
            t.append(f" {seg} ", style="bold #e6db74 on #272822")
        elif seg:
            t.append(seg, style="#abb2bf")


def _render_tool_detail(log, tool_name: str, tool_input: dict) -> None:
    """Render rich tool call details (file paths, diffs, commands)."""
    fp = tool_input.get("file_path", "")
    if fp:
        t = Text()
        t.append(f"  {fp}", style="dim cyan")
        log.write(t)

    if tool_name == "Edit":
        old_s = tool_input.get("old_string", "")
        new_s = tool_input.get("new_string", "")
        if old_s or new_s:
            lang = _guess_lang_from_path(fp) if fp else "text"
            diff_lines = []
            for line in old_s.split("\n")[:8]:
                diff_lines.append(f"- {line}")
            if len(old_s.split("\n")) > 8:
                diff_lines.append(f"  ... ({len(old_s.splitlines())} lines)")
            for line in new_s.split("\n")[:8]:
                diff_lines.append(f"+ {line}")
            if len(new_s.split("\n")) > 8:
                diff_lines.append(f"  ... ({len(new_s.splitlines())} lines)")
            diff_text = "\n".join(diff_lines)
            try:
                syn = Syntax(diff_text, "diff", theme="monokai", line_numbers=False, word_wrap=True, padding=(0, 1))
                log.write(Panel(syn, border_style="cyan", expand=True, padding=(0, 0)))
            except Exception:
                log.write(Panel(diff_text, border_style="cyan"))

    elif tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if cmd:
            try:
                syn = Syntax(cmd[:300], "bash", theme="monokai", line_numbers=False, word_wrap=True, padding=(0, 1))
                log.write(Panel(syn, border_style="green", expand=True, padding=(0, 0)))
            except Exception:
                log.write(Panel(f"$ {cmd[:300]}", border_style="green"))

    elif tool_name == "Write":
        content_preview = tool_input.get("content", "")
        if content_preview:
            lang = _guess_lang_from_path(fp) if fp else "text"
            lines = content_preview.split("\n")[:6]
            preview = "\n".join(lines)
            if len(content_preview.split("\n")) > 6:
                preview += f"\n  ... ({len(content_preview.splitlines())} total lines)"
            try:
                syn = Syntax(preview, lang, theme="monokai", line_numbers=False, word_wrap=True, padding=(0, 1))
                log.write(Panel(syn, border_style="green", title="write", title_align="left", expand=True, padding=(0, 0)))
            except Exception:
                log.write(Panel(preview, border_style="green"))

    elif tool_name == "Read":
        t = Text()
        t.append(f"  Reading file...", style="dim")
        log.write(t)

    elif tool_name in ("Grep", "Glob"):
        pattern = tool_input.get("pattern", "")
        if pattern:
            t = Text()
            t.append(f"  pattern: ", style="dim")
            t.append(pattern, style="bold yellow")
            log.write(t)


def _format_elapsed(seconds: float) -> str:
    s = int(seconds)
    minutes, secs = divmod(s, 60)
    hours, mins = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {mins}m"
    if mins > 0:
        return f"{mins}m {secs}s"
    return f"{secs}s"


# ── Splash Screen ────────────────────────────────────────────────────

class SplashScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "Skip")]

    def __init__(self) -> None:
        super().__init__()
        self._dismissed = False

    def compose(self) -> ComposeResult:
        splash_path = Path(__file__).resolve().parent.parent / "assets" / "splash.txt"
        try:
            art = splash_path.read_text()
        except OSError:
            art = "USS TENKARA — PRI-FLY COMMAND v1.0"

        content = Text()
        for line in art.split("\n"):
            content.append(line + "\n", style="bold green")

        content.append("\n")
        content.append("INITIALIZING RADAR . . .  ", style="bold white")
        content.append("████████████", style="bold green")
        content.append(" READY\n", style="bold bright_green")
        content.append("COMMS CHECK . . . . . .   ", style="bold white")
        content.append("████████████", style="bold green")
        content.append(" READY\n", style="bold bright_green")
        content.append("FLIGHT OPS . . . . . . .  ", style="bold white")
        content.append("████████████", style="bold green")
        content.append(" READY\n", style="bold bright_green")
        content.append("\n")
        content.append("ALL STATIONS MANNED AND READY\n", style="bold bright_white")
        content.append("\n")
        content.append("        Press any key to begin", style="dim")

        yield Static(content, id="splash-content")

    def on_key(self, event) -> None:
        event.stop()  # prevent hotkeys on the board below from firing
        if self._dismissed:
            return
        self._dismissed = True
        try:
            self.dismiss()
        except Exception:
            pass

    CSS = """
    SplashScreen { align: center middle; }
    #splash-content {
        width: auto; height: auto;
        padding: 2 4;
        background: $surface-darken-3;
        border: thick green;
    }
    """


# ── Confirmation Modal ───────────────────────────────────────────────

class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("n", "deny", "No"),
        Binding("escape", "deny", "Cancel"),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self._message, id="confirm-text"),
            Static("[Y]es / [N]o", id="confirm-hint"),
            id="confirm-container",
        )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)

    CSS = """
    #confirm-container {
        align: center middle; width: 50; height: auto;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    #confirm-hint { text-align: center; color: $text-muted; margin-top: 1; }
    """


# ── Briefing Modal ───────────────────────────────────────────────────

class BriefingScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def __init__(self, pilot: Pilot) -> None:
        super().__init__()
        self._pilot = pilot

    def compose(self) -> ComposeResult:
        p = self._pilot
        content = Text()
        content.append(f"PRE-FLIGHT BRIEFING: {p.callsign}\n", style="bold bright_white")
        content.append("─" * 60 + "\n", style="grey50")
        content.append(f"Mission:    {p.mission_title}\n", style="white")
        content.append(f"Ticket:     {p.ticket_id}\n", style="white")
        content.append(f"Model:      {p.model}\n", style="white")
        content.append(f"Trait:      {p.trait}\n", style="white")
        content.append(f"Mood:       {p.mood}\n", style="white")
        content.append(f"Fuel:       {p.fuel_pct}%\n", style="white")
        content.append("\n")
        content.append("DIRECTIVE:\n", style="bold yellow")
        for line in p.directive.split("\n")[:30]:
            content.append(f"  {line}\n", style="white")
        if len(p.directive.split("\n")) > 30:
            content.append(f"  ... ({len(p.directive.split(chr(10))) - 30} more lines)\n", style="dim")
        content.append("\n                                    ESC to close", style="dim")

        yield Vertical(Static(content, id="briefing-content"), id="modal-container")

    CSS = """
    #modal-container {
        align: center middle; width: 80; height: auto; max-height: 40;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    """


# ── Deploy Input Modal ──────────────────────────────────────────────

class DeployInputScreen(ModalScreen[Optional[tuple[str, str]]]):
    """Quick deploy: enter ticket ID and pick model."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, default_model: str = "sonnet") -> None:
        super().__init__()
        self._default_model = default_model

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(
                Text.from_markup("[bold bright_white]DEPLOY NEW AGENT[/]"),
                id="deploy-title",
            ),
            Static("Ticket ID or description:", id="deploy-label"),
            Input(placeholder="ENG-123 or free-text mission", id="deploy-ticket-input"),
            Static("Model:  [1] sonnet  [2] opus  [3] haiku", id="deploy-model-hint"),
            Static("Enter to launch  ·  Esc to cancel", id="deploy-footer"),
            id="deploy-container",
        )

    def on_mount(self) -> None:
        self.query_one("#deploy-ticket-input", Input).focus()
        self._model = self._default_model

    def on_key(self, event) -> None:
        if event.key == "1":
            self._model = "sonnet"
            self.query_one("#deploy-model-hint", Static).update(
                "Model:  [1] SONNET ◀  [2] opus  [3] haiku"
            )
        elif event.key == "2":
            self._model = "opus"
            self.query_one("#deploy-model-hint", Static).update(
                "Model:  [1] sonnet  [2] OPUS ◀  [3] haiku"
            )
        elif event.key == "3":
            self._model = "haiku"
            self.query_one("#deploy-model-hint", Static).update(
                "Model:  [1] sonnet  [2] opus  [3] HAIKU ◀"
            )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        ticket = event.value.strip()
        if ticket:
            self.dismiss((ticket, self._model))
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    CSS = """
    #deploy-container {
        align: center middle; width: 60; height: auto;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    #deploy-title { text-align: center; margin-bottom: 1; }
    #deploy-label { color: $text-muted; }
    #deploy-model-hint { color: $text-muted; margin-top: 1; }
    #deploy-footer { text-align: center; color: $text-muted; margin-top: 1; }
    """


# ── Linear Browse Modal ─────────────────────────────────────────────

class LinearBrowseScreen(ModalScreen[Optional[list[LinearTicket]]]):
    """Browse Linear issues and select one or more to queue/deploy."""

    BINDINGS = [
        Binding("escape", "cancel", "Close"),
        Binding("r", "refresh", "Refresh"),
        Binding("enter", "select_issue", "Queue Selected"),
        Binding("d", "deploy_issue", "Deploy Selected"),
        Binding("space", "toggle_mark", "Mark/Unmark"),
        Binding("a", "select_all", "Select All"),
    ]

    def __init__(
        self,
        tickets: list[LinearTicket] | None = None,
        filters: dict | None = None,
    ) -> None:
        super().__init__()
        self._tickets: list[LinearTicket] = tickets or []
        self._filters = filters or {}
        self._marked: set[str] = set()
        self._loading = False

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("", id="linear-title"),
            Static("", id="linear-status"),
            DataTable(id="linear-table"),
            Static("", id="linear-hints"),
            id="linear-container",
        )

    def on_mount(self) -> None:
        table = self.query_one("#linear-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("", "ID", "Pri", "State", "Title", "Assignee")

        self._update_title()
        if self._tickets:
            self._populate_table()
        else:
            self._do_refresh()

    def _update_title(self) -> None:
        title = self.query_one("#linear-title", Static)
        t = Text()
        t.append("  LINEAR — MISSION INTEL  ", style="bold bright_white on dark_blue")
        if self._filters:
            parts = []
            for k, v in self._filters.items():
                parts.append(f"{k}={v}")
            t.append(f"  {' '.join(parts)}", style="grey50")
        title.update(t)

    def _update_hints(self) -> None:
        hints = self.query_one("#linear-hints", Static)
        t = Text()
        t.append(" Enter", style="bold bright_white")
        t.append(" Queue  ", style="grey50")
        t.append("D", style="bold bright_white")
        t.append(" Deploy  ", style="grey50")
        t.append("Space", style="bold bright_white")
        t.append(" Mark  ", style="grey50")
        t.append("A", style="bold bright_white")
        t.append(" All  ", style="grey50")
        t.append("R", style="bold bright_white")
        t.append(" Refresh  ", style="grey50")
        t.append("Esc", style="bold bright_white")
        t.append(" Close", style="grey50")
        if self._marked:
            t.append(f"  │ {len(self._marked)} marked", style="bold cyan")
        hints.update(t)

    def _populate_table(self) -> None:
        table = self.query_one("#linear-table", DataTable)
        table.clear()
        for ticket in self._tickets:
            mark = "●" if ticket.id in self._marked else " "
            pri_text = Text(priority_label(ticket.priority), style=priority_style(ticket.priority))
            state_text = Text(ticket.state[:15], style="cyan")
            title_text = ticket.title[:55] + ("…" if len(ticket.title) > 55 else "")
            table.add_row(
                mark, ticket.id, pri_text, state_text, title_text,
                ticket.assignee[:12] if ticket.assignee else "—",
                key=ticket.id,
            )
        status = self.query_one("#linear-status", Static)
        status.update(Text(f"  {len(self._tickets)} issues loaded", style="green"))
        self._update_hints()

    def _do_refresh(self) -> None:
        """Kick off a background worker to fetch Linear issues."""
        self._loading = True
        status = self.query_one("#linear-status", Static)
        status.update(Text("  ⟳ Fetching from Linear MCP…", style="yellow"))
        self.run_worker(self._fetch_issues, thread=True)

    def _fetch_issues(self) -> list[LinearTicket]:
        return linear_list_issues(**self._filters)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state == WorkerState.SUCCESS:
            self._tickets = event.worker.result or []
            self._loading = False
            self._populate_table()
            if not self._tickets:
                status = self.query_one("#linear-status", Static)
                status.update(Text("  No issues found. Try different filters.", style="yellow"))
        elif event.state == WorkerState.ERROR:
            self._loading = False
            status = self.query_one("#linear-status", Static)
            status.update(Text("  ✗ Failed to fetch from Linear. Is MCP configured?", style="red"))

    def _get_selected_ticket(self) -> Optional[LinearTicket]:
        table = self.query_one("#linear-table", DataTable)
        if table.row_count == 0:
            return None
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(self._tickets):
            return None
        return self._tickets[row_idx]

    def _get_marked_tickets(self) -> list[LinearTicket]:
        if self._marked:
            return [t for t in self._tickets if t.id in self._marked]
        # If nothing marked, use the cursor row
        ticket = self._get_selected_ticket()
        return [ticket] if ticket else []

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_refresh(self) -> None:
        if not self._loading:
            self._do_refresh()

    def action_select_issue(self) -> None:
        """Queue the selected/marked issues."""
        tickets = self._get_marked_tickets()
        if tickets:
            self.dismiss(tickets)

    def action_deploy_issue(self) -> None:
        """Deploy the currently highlighted issue immediately (only one)."""
        ticket = self._get_selected_ticket()
        if ticket:
            # Tag it so the caller knows to deploy vs queue
            ticket._deploy = True  # type: ignore[attr-defined]
            self.dismiss([ticket])

    def action_toggle_mark(self) -> None:
        ticket = self._get_selected_ticket()
        if not ticket:
            return
        if ticket.id in self._marked:
            self._marked.discard(ticket.id)
        else:
            self._marked.add(ticket.id)
        self._populate_table()

    def action_select_all(self) -> None:
        if len(self._marked) == len(self._tickets):
            self._marked.clear()
        else:
            self._marked = {t.id for t in self._tickets}
        self._populate_table()

    CSS = """
    #linear-container {
        align: center middle; width: 100; height: 35;
        border: thick $accent; background: $surface; padding: 1 2;
    }
    #linear-title {
        height: 1; margin-bottom: 1;
    }
    #linear-status {
        height: 1; margin-bottom: 1;
    }
    #linear-table {
        height: 1fr;
    }
    #linear-hints {
        height: 1; margin-top: 1;
    }
    """


# ── PRI-FLY Header ──────────────────────────────────────────────────

CARRIER_ART = """\
      ╱▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔╲
⚓════╡  USS TENKARA  ━━  PRI-FLY  ━━  ╞══
      ╲▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁╱"""


class PriFlyHeader(Static):
    def render(self) -> Text:
        app = self.app
        roster = app._roster

        airborne = sum(1 for p in roster.all_pilots() if p.status == "AIRBORNE")
        idle = sum(1 for p in roster.all_pilots() if p.status == "IDLE")
        recovered = sum(1 for p in roster.all_pilots() if p.status == "RECOVERED")
        mayday = sum(1 for p in roster.all_pilots() if p.status == "MAYDAY")

        header = Text()
        for line in CARRIER_ART.split("\n"):
            header.append(line, style="bold bright_white")
            header.append("\n")

        header.append("CONDITION: ", style="bold white")
        if mayday > 0:
            pulse = app._condition_pulse
            header.append("RED", style="bold red" if pulse else "bold dark_red")
        elif airborne > 0:
            header.append("GREEN", style="bold green")
        else:
            header.append("STANDBY", style="dim yellow")

        header.append("  │  ", style="grey50")
        header.append(f"AIRBORNE: {airborne}", style="green")
        header.append("  │  ", style="grey50")
        header.append(f"IDLE: {idle}", style="yellow")
        header.append("  │  ", style="grey50")
        header.append(f"RECOVERED: {recovered}", style="grey50")
        if mayday > 0:
            header.append("  │  ", style="grey50")
            header.append(f"MAYDAY: {mayday}", style="bold red")
        header.append("  │  ", style="grey50")
        header.append(datetime.now().strftime("%H:%M:%S LOCAL"), style="white")

        # HUD action bar is rendered in #hotkey-bar widget via _update_keybind_hints()

        return header


# ── Chat Pane Widget ─────────────────────────────────────────────────

class ChatInput(TextArea):
    """Multi-line input for chat panes.

    Enter = newline, Ctrl+Enter = submit.
    Up/Down recalls message history when input is empty.
    Paste works natively (bracketed paste mode).
    """

    DEFAULT_CSS = """
    ChatInput {
        height: auto;
        max-height: 8;
        min-height: 3;
        border: tall $accent;
    }
    ChatInput:focus {
        border: tall $accent-lighten-2;
    }
    """

    class Submitted(TextArea.Changed):
        """Fired when user presses Enter to submit."""
        def __init__(self, text_area: "ChatInput", text: str) -> None:
            super().__init__(text_area)
            self.text_value = text

    BINDINGS = [
        Binding("ctrl+j", "submit", "Send", show=False),  # Ctrl+Enter in most terminals
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._history: list[str] = []
        self._history_idx: int = -1
        self._draft: str = ""  # Preserve in-progress text when navigating history

    def _on_key(self, event) -> None:
        """Ctrl+Enter = submit. Up/Down = history when empty/at edge."""
        if event.key in ("ctrl+enter", "ctrl+j"):
            event.prevent_default()
            event.stop()
            self._do_submit()
            return

        # History navigation: up/down when text is empty or cursor at top/bottom
        if event.key == "up" and self.text.strip() == "" and self._history:
            event.prevent_default()
            event.stop()
            if self._history_idx == -1:
                self._draft = self.text
                self._history_idx = len(self._history) - 1
            elif self._history_idx > 0:
                self._history_idx -= 1
            self.load_text(self._history[self._history_idx])
            return

        if event.key == "down" and self._history_idx >= 0:
            event.prevent_default()
            event.stop()
            if self._history_idx < len(self._history) - 1:
                self._history_idx += 1
                self.load_text(self._history[self._history_idx])
            else:
                self._history_idx = -1
                self.load_text(self._draft)
            return

    def _do_submit(self) -> None:
        text = self.text.strip()
        if text:
            self._history.append(text)
            self._history_idx = -1
            self._draft = ""
            self.post_message(self.Submitted(self, text))
            self.clear()

    def action_submit(self) -> None:
        self._do_submit()


class ChatPane(Vertical):
    """Conversation view for a single agent."""

    BINDINGS = [
        Binding("ctrl+c", "close_chat", "Close Chat", show=True),
        Binding("ctrl+y", "copy_last", "Copy Last", show=False),
    ]

    def __init__(self, callsign: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.callsign = callsign
        self._auto_scroll = True

    def compose(self) -> ComposeResult:
        yield Static("", id=f"chat-header-{self.callsign}")
        yield RichLog(
            id=f"chat-log-{self.callsign}",
            highlight=True,
            markup=True,
            auto_scroll=True,
        )
        yield Static(
            " Ctrl+C Close │ Esc Pri-Fly │ Enter Newline │ Ctrl+Enter Send │ Ctrl+Y Copy │ ⌥+Drag Select",
            id=f"chat-hint-{self.callsign}",
            classes="chat-close-hint",
        )
        yield Horizontal(
            ChatInput(id=f"chat-input-{self.callsign}"),
            Button("Send ⏎", id=f"chat-send-{self.callsign}", variant="primary"),
            classes="chat-input-row",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle Send button click."""
        if event.button.id == f"chat-send-{self.callsign}":
            chat_input = self.query_one(f"#chat-input-{self.callsign}", ChatInput)
            text = chat_input.text.strip()
            if text:
                chat_input.post_message(ChatInput.Submitted(chat_input, text))
                chat_input.clear()

    def action_close_chat(self) -> None:
        """Close this chat pane."""
        app = self.app
        app._close_chat_pane(self.callsign)

    def action_copy_last(self) -> None:
        """Copy the last assistant response to clipboard."""
        agent = self.app._agent_mgr.get(self.callsign)
        if not agent:
            return
        # Find last assistant message
        for entry in reversed(agent.conversation):
            if entry.role == "assistant" and entry.content:
                self.app.copy_to_clipboard(entry.content)
                self.add_message("system", "Copied last response to clipboard")
                return

    def on_mount(self) -> None:
        self.refresh_header()
        # Backfill conversation history — must happen here (not in _open_chat_pane)
        # because mount() is async and the RichLog doesn't exist until now.
        try:
            agent = self.app._agent_mgr.get(self.callsign)
            if agent:
                for entry in agent.conversation:
                    self.add_message(
                        entry.role, entry.content, entry.tool_name,
                        tool_input=getattr(entry, "tool_input", None),
                    )
        except Exception:
            pass

    def refresh_header(self) -> None:
        try:
            app = self.app
            pilot = app._roster.get_by_callsign(self.callsign)
            if not pilot:
                return
            header = self.query_one(f"#chat-header-{self.callsign}", Static)
            t = Text()
            t.append(f" [{self.callsign}]", style="bold bright_white")
            t.append(f"  FUEL: ", style="white")
            t.append_text(fuel_gauge(pilot.fuel_pct, width=16))
            t.append(f"  {_format_tokens(pilot.tokens_used)} tokens", style="grey70")

            # Managed vs legacy indicator
            agent = app._agent_mgr.get(self.callsign)
            if agent and agent.is_alive:
                t.append("  ●", style="bold green")  # stream-json managed
            else:
                t.append("  ○ legacy", style="dim yellow")  # not managed

            t.append(f"\n", style="")
            # Active subagents
            if agent:
                active_subs = agent.active_subagents
                if active_subs:
                    t.append(f"  SUBAGENTS ({len(active_subs)} active)", style="yellow")
                    for sa in active_subs[:3]:
                        elapsed = time_mod.time() - sa.started_at
                        t.append(f"\n    ▸ {sa.description} ", style="yellow")
                        t.append(f"{elapsed:.1f}s", style="grey70")
            header.update(t)
        except Exception:
            pass  # Don't crash on header refresh

    def add_message(
        self,
        role: str,
        content: str,
        tool_name: str = "",
        tool_input: dict | None = None,
    ) -> None:
        try:
            chat_log = self.query_one(f"#chat-log-{self.callsign}", RichLog)
            ts = datetime.now().strftime("%H:%M:%S")

            if role == "tool":
                icon = _tool_icon(tool_name)
                t = Text()
                t.append(f"  {icon} ", style="bold cyan")
                t.append(tool_name, style="bold cyan")
                if content and content != tool_name:
                    t.append(f"  {content[:60]}", style="grey50")
                t.append(f"  {ts}", style="grey42")
                chat_log.write(t)
                if tool_input:
                    _render_tool_detail(chat_log, tool_name, tool_input)
                return

            elif role == "assistant":
                if content:
                    # Role + timestamp header
                    header = Text()
                    header.append(f"  {self.callsign}", style="bold #61afef")
                    header.append(f"  {ts}", style="grey42")
                    chat_log.write(header)
                    _render_assistant_content(chat_log, content)
                    # Thin separator
                    chat_log.write(Text("  ─" * 20, style="grey19"))
                    return

            elif role == "user":
                # User message bubble-style
                header = Text()
                header.append(f"  YOU", style="bold #98c379")
                header.append(f"  {ts}", style="grey42")
                chat_log.write(header)
                body = Text()
                for line in content.split("\n"):
                    body.append(f"  {line}\n", style="bold #ffffff")
                chat_log.write(body)
                chat_log.write(Text("  ─" * 20, style="grey19"))
                return

            elif role == "permission":
                # Permission request header
                t = Text()
                t.append(f"\n  ⚡ PERMISSION REQUEST  ", style="bold yellow on #3a2a00")
                t.append(f"{ts}\n", style="grey42")
                chat_log.write(t)

                # Tool details
                if tool_input:
                    _render_tool_detail(chat_log, tool_name, tool_input)

                # Reason + action prompt
                reason_line = ""
                lines = content.split("\n")
                if len(lines) > 1:
                    reason_line = lines[-1]
                prompt_t = Text()
                if reason_line:
                    prompt_t.append(f"  {reason_line}\n", style="dim yellow")
                prompt_t.append("  → ", style="bold yellow")
                prompt_t.append("y", style="bold #98c379")
                prompt_t.append(" approve  ", style="grey70")
                prompt_t.append("n", style="bold #e06c75")
                prompt_t.append(" deny\n", style="grey70")
                chat_log.write(prompt_t)
                return

            elif role == "system":
                t = Text()
                t.append(f"  ⚙ {content}", style="grey50")
                t.append(f"  {ts}", style="grey42")
                chat_log.write(t)
                return

            # Fallback
            t = Text()
            t.append(f"[{ts}] {role}: {content}", style="grey70")
            chat_log.write(t)
        except Exception:
            pass

    def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        text = event.text_value
        if not text:
            return
        event.stop()

        try:
            app = self.app

            # Handle permission approval (y/n/yes/no)
            if text.lower() in ("y", "yes", "n", "no"):
                pending = app._pending_permissions.get(self.callsign)
                if pending:
                    allowed = text.lower() in ("y", "yes")
                    agent = app._agent_mgr.get(self.callsign)
                    if agent:
                        agent.respond_permission(
                            pending["request_id"],
                            pending["tool_use_id"],
                            allowed,
                        )
                        action = "APPROVED" if allowed else "DENIED"
                        style = "#98c379" if allowed else "#e06c75"
                        self.add_message("system", f"✓ {action}: {pending['tool_name']}")
                    del app._pending_permissions[self.callsign]
                    return

            # Intercept Pri-Fly commands typed in chat pane
            if text.startswith("/"):
                self.add_message("system", f"Routing to Pri-Fly: {text}")
                app._handle_command(text)
                app._refresh_ui()
                return

            # Build display text — collapse long pastes/multi-line
            lines = text.splitlines()
            if len(lines) > 3:
                display = f"{lines[0]}\n{lines[1]}\n… <+{len(lines) - 2} lines>"
            else:
                display = text

            # Inject message into agent
            agent = app._agent_mgr.get(self.callsign)
            if agent and agent.is_alive:
                success = app._agent_mgr.inject_message(self.callsign, text)
                if success:
                    self.add_message("user", display)
                else:
                    self.add_message("system", "Failed to send — agent stdin closed")
            else:
                self.add_message(
                    "system",
                    f"{self.callsign} is not stream-json managed. "
                    "Type /resume " + self.callsign + " or press Esc then run it in Pri-Fly.",
                )
        except Exception as e:
            self.add_message("system", f"Error: {e}")


# ── Mission Queue Panel ──────────────────────────────────────────────

class MissionQueuePanel(Static):
    def render(self) -> Text:
        app = self.app
        queue = app._mission_queue

        t = Text()
        t.append(" MISSION QUEUE", style="bold bright_white")

        queued = queue.queued()
        if not queued:
            t.append("\n  No missions queued", style="grey50")
            return t

        for m in queued[:8]:
            pri_style = {1: "bold red", 2: "yellow", 3: "grey70"}.get(m.priority, "white")
            t.append(f"\n  P{m.priority}", style=pri_style)
            t.append(f"  {m.id}", style="bold white")
            t.append(f"  {m.title[:40]}", style="white")
            t.append(f"  [{m.model}]", style="grey70")
            t.append(f"  ×{m.agent_count}", style="cyan")

        if len(queued) > 8:
            t.append(f"\n  ... +{len(queued) - 8} more", style="dim")

        return t


# ── Radio Chatter ────────────────────────────────────────────────────

class RadioChatter(Static):
    def render(self) -> Text:
        app = self.app
        t = Text()
        t.append(" 📻 RADIO CHATTER\n", style="bold grey70")

        entries = app._radio_log[-12:]
        if not entries:
            t.append("  All stations quiet.", style="grey50")
            return t

        for entry in entries:
            ts = entry.get("timestamp", "--:--")
            callsign = entry.get("callsign", "???")
            msg = entry.get("message", "")
            entry_type = entry.get("type", "normal")

            t.append(f" [{ts}] ", style="grey70")
            t.append(f"{callsign}: ", style="bold")

            if entry_type == "error":
                t.append(msg, style="bold red")
            elif entry_type == "success":
                t.append(msg, style="bold green")
            elif entry_type == "system":
                t.append(msg, style="dim yellow")
            else:
                t.append(msg, style="white")
            t.append("\n")

        return t


# ── Deck Status Footer ──────────────────────────────────────────────

class DeckStatus(Static):
    def render(self) -> Text:
        app = self.app
        roster = app._roster
        pilots = roster.all_pilots()
        agent_mgr = app._agent_mgr

        fuel_pcts = [p.fuel_pct for p in pilots if p.status in ("AIRBORNE", "IDLE")]
        avg_fuel = round(sum(fuel_pcts) / len(fuel_pcts)) if fuel_pcts else 0
        total_tools = sum(p.tool_calls for p in pilots)
        active_count = len(agent_mgr.active_agents())

        t = Text()
        t.append("DECK: ", style="bold white")
        t.append(f"CAT 1-{max(active_count, 1)} ACTIVE", style="green")
        t.append(" │ ", style="grey50")
        t.append(f"FUEL AVG: {avg_fuel}%", style="yellow" if avg_fuel <= 50 else "green")
        t.append(" │ ", style="grey50")
        t.append(f"ORDNANCE: {total_tools} tx", style="white")
        t.append(" │ ", style="grey50")
        queue_count = len(app._mission_queue.queued())
        t.append(f"QUEUE: {queue_count}", style="cyan" if queue_count > 0 else "grey50")
        return t


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
        self._radio_log: list[dict] = []
        self._chat_panes: dict[str, ChatPane] = {}
        self._slot_map: dict[int, str] = {}  # slot_index -> callsign
        self._comms_active = False
        self._sorted_pilots: list[Pilot] = []
        self._previous_statuses: dict[str, str] = {}
        self._bingo_notified: set[str] = set()
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

        # Token delta tracking — detect when agents stop producing tokens
        self._prev_tokens: dict[str, int] = {}    # callsign -> last known token count
        self._stale_frames: dict[str, int] = {}   # callsign -> consecutive frames with zero delta
        self._stale_threshold: int = 4  # frames (~12s at 3s/frame) of no change → fly home

        # Fuel tracking — detect compaction events (fuel jumps back up)
        self._prev_fuel: dict[str, int] = {}      # callsign -> last known fuel_pct
        self._sar_started: dict[str, float] = {}   # callsign -> timestamp when SAR began

        # Air Boss — interactive Claude session in Pit Boss pane (no longer stream-json)
        self._airboss_spawned: bool = False

        # Background sync guard — prevent overlapping read_sortie_state calls
        self._sync_in_progress: bool = False

        # Board dirty tracking — skip table rebuild when state hasn't changed
        self._board_state_sig: str = ""

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
        self.set_interval(3.0, self._refresh_ui)
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

        # Launch sentinel — headless JSONL classifier (Haiku) that writes
        # sentinel-status.json to each worktree so agents don't self-report
        self._start_sentinel()

        # Focus the board table
        self.query_one("#agent-table", DataTable).focus()

        # Initial table render (so agents show immediately)
        self._refresh_ui()

        # Show splash — auto-dismisses when initial sync completes or after 3s
        self._splash: Optional[SplashScreen] = SplashScreen()
        self.push_screen(self._splash)
        self.set_timer(3.0, self._dismiss_splash)

    def on_unmount(self) -> None:
        self._agent_mgr.shutdown()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=2)

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
        self._dismiss_splash()
        seen_tickets: set[str] = set()
        for agent in state.agents:
            tid = agent.ticket_id
            seen_tickets.add(tid)
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
                if title == tid and is_ticket_id(tid):
                    try:
                        ticket = fetch_ticket(tid)
                        if ticket:
                            title = ticket.title[:60]
                    except Exception:
                        pass
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

            # Sync telemetry from legacy state
            # Truly unknown agents (no directive at all) — always RECOVERED
            if pilot.mission_title in ("Unknown", "unknown") and pilot.ticket_id in ("Unknown", "unknown"):
                pilot.status = "RECOVERED"
                continue

            # Token delta tracking (_check_token_deltas) is the authority for
            # IDLE→AIRBORNE and AIRBORNE→ON_APPROACH transitions. Legacy sync
            # only sets status when delta tracking hasn't taken over.
            has_tokens = (
                agent.jsonl_metrics is not None
                and agent.jsonl_metrics.total_tokens > 0
            )
            cs = pilot.callsign
            delta_is_tracking = cs in self._prev_tokens and self._prev_tokens[cs] > 0

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
                    ss_age = int(time.time()) - ss.get("timestamp", 0)
                    ss_status = ss.get("status", "").upper()
                    if ss_age < 90 and ss_status in ("AIRBORNE", "HOLDING", "ON_APPROACH", "RECOVERED"):
                        pilot.status = ss_status
                        phase = ss.get("phase", "")
                        if phase:
                            pilot.flight_phase = phase
                        self._stale_frames.pop(pilot.callsign, None)
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
                        if new_status in ("AIRBORNE", "IDLE", "RECOVERED", "ON_APPROACH", "MAYDAY", "AAR", "SAR"):
                            pilot.status = new_status
                            self._stale_frames.pop(pilot.callsign, None)
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
                self._stale_frames.pop(pilot.callsign, None)
                self._add_radio(pilot.callsign, "RECOVERED — session ended", "success")
                _play_sound("recovered")
                _notify("USS TENKARA — RECOVERED", f"{pilot.callsign} on deck")
                continue

            # Store agent-reported flight status on pilot (if fresh)
            if agent.flight_status and not _flight_status_is_stale(agent):
                pilot.flight_status = agent.flight_status
                pilot.flight_phase = agent.flight_phase

                # Agent-reported flight status is authoritative when fresh
                mapped = _map_flight_status(agent.flight_status)
                if mapped and mapped != pilot.status:
                    old = pilot.status
                    pilot.status = mapped
                    self._stale_frames.pop(pilot.callsign, None)
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

    def _sync_managed_servers(self) -> None:
        """Read .sortie/managed-servers.json and map server URLs to pilots."""
        servers_file = Path(self._project_dir) / ".sortie" / "managed-servers.json"
        try:
            entries = json.loads(servers_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(entries, list):
            return

        # Build ticket_id -> server info map
        server_map: dict[str, str] = {}
        for entry in entries:
            tid = entry.get("ticket_id", "")
            url = entry.get("url", "")
            note = entry.get("note", "")
            if tid and url:
                label = url if not note else f"{url} ({note})"
                server_map[tid] = label

        # Apply to pilots — append to existing hint, don't clobber
        for pilot in self._roster.all_pilots():
            server_label = server_map.get(pilot.ticket_id)
            if server_label:
                # Avoid duplicating if already present
                if server_label not in (pilot.status_hint or ""):
                    if pilot.status_hint:
                        pilot.status_hint = f"{pilot.status_hint} | {server_label}"
                    else:
                        pilot.status_hint = server_label

    def _start_watchers(self) -> None:
        """Set up watchdog observers for worktree and JSONL directories."""
        from read_sortie_state import get_worktrees_root

        handler = _WorktreeFileHandler(self)
        self._observer = Observer()

        # Watch worktrees directory
        worktrees_root = get_worktrees_root(self._project_dir)
        if worktrees_root.is_dir():
            self._observer.schedule(handler, str(worktrees_root), recursive=True)

        # Watch project .sortie/ dir (mission-queue/, managed-servers.json)
        sortie_dir = Path(self._project_dir) / ".sortie"
        sortie_dir.mkdir(parents=True, exist_ok=True)
        (sortie_dir / "mission-queue").mkdir(parents=True, exist_ok=True)
        self._observer.schedule(handler, str(sortie_dir), recursive=True)

        # Watch JSONL directories for known agents
        for agent in self._legacy_agents.values():
            encoded = encode_project_path(agent.worktree_path)
            jsonl_dir = CLAUDE_PROJECTS_DIR / encoded
            dir_str = str(jsonl_dir)
            if jsonl_dir.is_dir() and dir_str not in self._watched_jsonl_dirs:
                self._observer.schedule(handler, dir_str, recursive=True)
                self._watched_jsonl_dirs.add(dir_str)

        try:
            self._observer.start()
        except Exception:
            self._observer = None

    def _start_sentinel(self) -> None:
        """Launch sentinel.py as a background subprocess tied to this TUI session.

        sentinel.py spawns a persistent claude --input-format stream-json Haiku
        subprocess and feeds it JSONL events from all managed worktrees. It writes
        .sortie/sentinel-status.json to each worktree so pilots don't need to
        self-report status.
        """
        sentinel_script = Path(__file__).parent / "sentinel.py"
        if not sentinel_script.exists():
            self._add_radio("PRI-FLY", "SENTINEL — script not found, skipping", "system")
            return

        try:
            import subprocess
            proc = subprocess.Popen(
                [sys.executable, str(sentinel_script), "--project-dir", self._project_dir],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,   # isolated process group — not killed by TUI Ctrl-C
            )
            self._sentinel_pid = proc.pid
            self._add_radio("PRI-FLY", f"SENTINEL — Haiku classifier online (PID {proc.pid})", "system")
        except Exception as e:
            self._add_radio("PRI-FLY", f"SENTINEL — failed to launch: {e}", "system")

    def _watch_agent_jsonl(self, worktree_path: str) -> None:
        """Register a watchdog on an agent's JSONL directory for immediate telemetry.

        Called right after opening an agent pane so we don't have to wait
        for the next _sync_legacy_agents cycle to start tracking JSONL updates.
        """
        if not self._observer:
            return
        encoded = encode_project_path(worktree_path)
        jsonl_dir = CLAUDE_PROJECTS_DIR / encoded
        dir_str = str(jsonl_dir)
        if dir_str in self._watched_jsonl_dirs:
            return
        # The JSONL dir may not exist yet (Claude creates it on first write).
        # Watch the parent (project-level) dir to catch creation.
        watch_target = jsonl_dir if jsonl_dir.is_dir() else CLAUDE_PROJECTS_DIR
        target_str = str(watch_target)
        if target_str not in self._watched_jsonl_dirs:
            try:
                handler = _WorktreeFileHandler(self)
                self._observer.schedule(handler, target_str, recursive=True)
                self._watched_jsonl_dirs.add(target_str)
            except Exception as e:
                log.warning(f"Failed to watch JSONL dir {target_str}: {e}")
        self._watched_jsonl_dirs.add(dir_str)

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

    def _handle_agent_event(self, callsign: str, event: StreamEvent) -> None:
        """Process agent event on the main thread."""
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
            elif pilot.fuel_pct <= 30 and callsign not in self._bingo_notified:
                self._bingo_notified.add(callsign)
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
        """Handle agent process exit on main thread."""
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
        try:
            parts = shlex.split(text)
        except ValueError:
            self._add_radio("PRI-FLY", f"Bad command syntax: {text}", "error")
            return
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd == "/deploy":
            self._cmd_deploy(args)
        elif cmd == "/queue":
            self._cmd_queue(args)
        elif cmd == "/recall":
            self._cmd_recall(args)
        elif cmd in ("/wave-off", "/waveoff"):
            self._cmd_wave_off(args)
        elif cmd == "/compact":
            self._cmd_compact(args)
        elif cmd in ("/auto-compact", "/autocompact"):
            self._cmd_auto_compact(args)
        elif cmd == "/sitrep":
            self._cmd_sitrep()
        elif cmd == "/briefing":
            self._cmd_briefing(args)
        elif cmd == "/auto":
            self._cmd_auto(args)
        elif cmd == "/rearm":
            self._cmd_rearm(args)
        elif cmd == "/resume":
            self._cmd_resume(args)
        elif cmd == "/linear":
            self._cmd_linear(args)
        elif cmd == "/help":
            self._cmd_help()
        else:
            self._add_radio("PRI-FLY", f"Unknown command: {cmd}", "system")

    def _cmd_deploy(self, args: list[str]) -> None:
        """Deploy an agent: /deploy <ticket|description> [--model X] [--spec path]"""
        if not args:
            self._add_radio("PRI-FLY", "Usage: /deploy <ticket|desc> [--model opus|sonnet|haiku]", "system")
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
            self._add_radio("PRI-FLY", "Need a ticket ID or description", "system")
            return

        # Check if it looks like a Linear ticket ID (e.g., ENG-123)
        if is_ticket_id(identifier) and not spec_path:
            self._add_radio("PRI-FLY", f"Fetching {identifier} from Linear…", "system")
            self._fetch_linear_ticket_background(identifier, "deploy", model, 2)
            return

        # Generate directive
        directive = f"Complete the following task:\n\n{identifier}"

        if spec_path:
            try:
                spec_content = Path(spec_path).read_text()
                directive = f"Complete the following task based on this spec:\n\n{spec_content}"
            except OSError as e:
                self._add_radio("PRI-FLY", f"Failed to read spec: {e}", "error")
                return

        # Assign pilot
        pilot = self._roster.assign(
            ticket_id=identifier,
            model=model,
            mission_title=identifier[:60],
            directive=directive,
        )

        # Launch interactive Claude session in iTerm2 pane — IDLE on deck until tokens flow
        pilot.status = "IDLE"
        pilot.launched_at = time_mod.time()

        self._open_agent_pane(pilot)
        self._add_radio("PRI-FLY", f"ON DECK — {pilot.callsign} standing by for {identifier}", "success")
        if getattr(self, '_rtk_active', False):
            self._add_radio(pilot.callsign, "RTK active — drop tanks fitted, extended range", "system")
        _notify("USS TENKARA", f"{pilot.callsign} on deck for {identifier}")
        self._refresh_ui()

    def _cmd_queue(self, args: list[str]) -> None:
        if not args:
            self._add_radio("PRI-FLY", "Usage: /queue <ticket|path|desc> [--model X] [--priority 1-3]", "system")
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
        # Check if it's a Linear ticket ID (e.g., ENG-123)
        if is_ticket_id(desc.strip()):
            ticket_id = desc.strip()
            # If no explicit model/priority, have Mini Boss triage
            if not explicit_model and not explicit_priority:
                self._add_radio("PRI-FLY", f"Fetching {ticket_id} — Mini Boss will triage", "system")
                self._fetch_and_triage_ticket(ticket_id)
            else:
                self._add_radio("PRI-FLY", f"Fetching {ticket_id} from Linear…", "system")
                self._fetch_linear_ticket_background(
                    ticket_id, "queue", model or "sonnet", priority or 2,
                )
            return
        elif Path(desc).is_file():
            mission = self._mission_queue.add_from_spec(
                desc, model=model or "sonnet", priority=priority or 2,
            )
        else:
            mission = self._mission_queue.add_adhoc(
                desc, model=model or "sonnet", priority=priority or 2,
            )

        self._add_radio("PRI-FLY", f"QUEUED — {mission.id}: {mission.title[:50]}", "system")
        self._refresh_ui()

    def _fetch_and_triage_ticket(self, ticket_id: str) -> None:
        """Fetch a ticket from Linear, then ask Mini Boss to assess model/priority."""
        def _bg_fetch():
            ticket = fetch_ticket(ticket_id)
            if ticket:
                self.call_from_thread(self._triage_ticket_with_airboss, ticket)
            else:
                self.call_from_thread(
                    self._add_radio, "PRI-FLY",
                    f"Could not fetch {ticket_id} from Linear — queuing as-is", "error",
                )
                mission = self._mission_queue.add_adhoc(ticket_id, model="sonnet", priority=2)
                self.call_from_thread(self._refresh_ui)

        import threading
        threading.Thread(target=_bg_fetch, daemon=True).start()

    def _triage_ticket_with_airboss(self, ticket: "LinearTicket") -> None:
        """Ask Mini Boss to assess the right model and priority for a ticket."""
        # Queue it immediately with defaults, Mini Boss can update
        mission = self._mission_queue.add_adhoc(
            f"[{ticket.id}] {ticket.title}\n{ticket.description}",
            model="sonnet", priority=2,
        )
        mission.id = ticket.id
        mission.source = "linear"
        self._add_radio("PRI-FLY", f"QUEUED — {ticket.id}: {ticket.title[:50]}", "system")
        self._refresh_ui()

        # Ask Mini Boss to assess
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
        self._send_to_airboss(triage_msg)

    def _cmd_recall(self, args: list[str]) -> None:
        if not args:
            self._add_radio("PRI-FLY", "Usage: /recall <callsign>", "system")
            return
        callsign = args[0]
        if self._agent_mgr.recall(callsign):
            self._add_radio("PRI-FLY", f"RECALL — {callsign} winding down", "system")
        else:
            self._add_radio("PRI-FLY", f"No active agent: {callsign}", "error")

    def _cmd_wave_off(self, args: list[str]) -> None:
        if not args:
            self._add_radio("PRI-FLY", "Usage: /wave-off <callsign>", "system")
            return
        callsign = args[0]
        try:
            # Kill managed dev servers for this pilot first
            pilot = self._roster.get_by_callsign(callsign)
            if pilot:
                self._kill_managed_servers(pilot.ticket_id, callsign)

            if self._agent_mgr.wave_off(callsign):
                self._add_radio("PRI-FLY", f"WAVE OFF — {callsign} terminated", "error")
            else:
                self._add_radio("PRI-FLY", f"No active agent: {callsign}", "error")
        except Exception as e:
            self._add_radio("PRI-FLY", f"Wave-off error: {e}", "error")

    def _kill_managed_servers(self, ticket_id: str, callsign: str) -> None:
        """Kill any managed dev servers for the given ticket and remove from registry."""
        servers_file = Path(self._project_dir) / ".sortie" / "managed-servers.json"
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
                            os.kill(int(pid), 15)  # SIGTERM
                            killed += 1
                            self._add_radio(callsign, f"Server {url} (pid {pid}) terminated", "system")
                        except (ProcessLookupError, PermissionError, ValueError):
                            # Already dead or can't kill — just remove the entry
                            pass
                else:
                    remaining.append(entry)

            if killed or len(remaining) != len(entries):
                servers_file.write_text(json.dumps(remaining, indent=2) + "\n")
                if killed:
                    self._add_radio("PRI-FLY", f"Killed {killed} server(s) for {callsign}", "system")
        except (json.JSONDecodeError, OSError) as e:
            self._add_radio("PRI-FLY", f"Server cleanup error: {e}", "error")

    def _cmd_compact(self, args: list[str]) -> None:
        if not args:
            self._add_radio("PRI-FLY", "Usage: /compact <callsign|idle|all>", "system")
            return
        target = args[0].lower()
        if target == "idle":
            idle_pilots = [
                p for p in self._roster.all_pilots()
                if p.status == "AIRBORNE" and p.fuel_pct < self._auto_compact_threshold
                and (time_mod.time() - p.last_tool_at) > self._auto_compact_idle
            ]
            for pilot in idle_pilots:
                self._trigger_compact(pilot.callsign)
            self._add_radio("PRI-FLY", f"Compacting {len(idle_pilots)} idle agents", "system")
        elif target == "all":
            for pilot in self._roster.all_pilots():
                if pilot.status == "AIRBORNE":
                    self._trigger_compact(pilot.callsign)
        else:
            self._trigger_compact(target)

    def _trigger_compact(self, callsign: str) -> None:
        """Send a compaction request to an agent (changes status to AAR)."""
        pilot = self._roster.get_by_callsign(callsign)
        if pilot and pilot.status == "AIRBORNE":
            pilot.status = "AAR"
            self._agent_mgr.inject_message(
                callsign,
                "CIC: Context compaction requested. Summarize your progress, "
                "then continue with refreshed context."
            )
            self._add_radio("PRI-FLY", f"AAR — {callsign} refueling", "system")

    def _cmd_auto_compact(self, args: list[str]) -> None:
        if not args:
            status = "ON" if self._auto_compact else "OFF"
            self._add_radio("PRI-FLY", f"Auto-compact: {status} (threshold={self._auto_compact_threshold}%, idle={self._auto_compact_idle}s)", "system")
            return
        if args[0].lower() == "on":
            self._auto_compact = True
            for i in range(1, len(args) - 1, 2):
                if args[i] == "--threshold":
                    self._auto_compact_threshold = int(args[i + 1])
                elif args[i] == "--idle":
                    self._auto_compact_idle = int(args[i + 1].rstrip("s"))
            self._add_radio("PRI-FLY", f"Auto-compact ON (threshold={self._auto_compact_threshold}%, idle={self._auto_compact_idle}s)", "system")
        else:
            self._auto_compact = False
            self._add_radio("PRI-FLY", "Auto-compact OFF", "system")

    def _cmd_sitrep(self) -> None:
        for pilot in self._roster.all_pilots():
            if pilot.status == "AIRBORNE":
                self._agent_mgr.inject_message(
                    pilot.callsign,
                    "CIC: SITREP — report current status, progress, and any blockers."
                )
        self._add_radio("PRI-FLY", "SITREP requested from all AIRBORNE", "system")

    def _cmd_briefing(self, args: list[str]) -> None:
        if not args:
            self._add_radio("PRI-FLY", "Usage: /briefing <callsign>", "system")
            return
        callsign = args[0]
        pilot = self._roster.get_by_callsign(callsign)
        if pilot:
            self.push_screen(BriefingScreen(pilot))
        else:
            self._add_radio("PRI-FLY", f"Unknown callsign: {callsign}", "error")

    def _cmd_auto(self, args: list[str]) -> None:
        if not args:
            status = "ON" if self._mission_queue.auto_deploy_enabled else "OFF"
            self._add_radio("PRI-FLY", f"Auto-deploy: {status}", "system")
            return
        if args[0].lower() == "on":
            max_concurrent = 3
            if len(args) > 1:
                try:
                    max_concurrent = int(args[1])
                except ValueError:
                    pass
            self._mission_queue.set_auto_deploy(True, max_concurrent)
            self._add_radio("PRI-FLY", f"Auto-deploy ON (max {max_concurrent})", "system")
        else:
            self._mission_queue.set_auto_deploy(False)
            self._add_radio("PRI-FLY", "Auto-deploy OFF", "system")

    def _cmd_rearm(self, args: list[str]) -> None:
        if len(args) < 2:
            self._add_radio("PRI-FLY", "Usage: /rearm <callsign> <ticket>", "system")
            return
        callsign = args[0]
        ticket = args[1]
        pilot = self._roster.get_by_callsign(callsign)
        if not pilot:
            self._add_radio("PRI-FLY", f"Unknown callsign: {callsign}", "error")
            return
        if pilot.status != "RECOVERED":
            self._add_radio("PRI-FLY", f"{callsign} not RECOVERED — cannot rearm", "error")
            return

        # Re-deploy with new ticket
        self._roster.remove(callsign)
        self._cmd_deploy([ticket, "--model", pilot.model])

    def _cmd_resume(self, args: list[str]) -> None:
        """Resume an agent in its worktree: /resume <callsign|ticket-id> [--model X]"""
        if not args:
            self._add_radio("PRI-FLY", "Usage: /resume <callsign|ticket-id> [--model opus|sonnet|haiku]", "system")
            return

        identifier = args[0]
        model_override = ""
        if len(args) >= 3 and args[1] == "--model":
            model_override = args[2]

        # Try to find existing pilot by callsign
        pilot = self._roster.get_by_callsign(identifier)

        # Or find by ticket ID in legacy agents
        if not pilot:
            legacy_agent = self._legacy_agents.get(identifier)
            if legacy_agent:
                # Find the pilot assigned to this ticket
                pilots = self._roster.get_by_ticket(identifier)
                pilot = pilots[0] if pilots else None

        if not pilot:
            # Create a new pilot for this resume
            model = model_override or "sonnet"
            pilot = self._roster.assign(
                ticket_id=identifier,
                model=model,
                mission_title=f"Resume {identifier}",
                directive="",
            )

        # Check if already running in a pane
        if pilot.callsign in self._iterm_panes:
            self._add_radio("PRI-FLY", f"{pilot.callsign} already has an active pane", "error")
            return

        model = model_override or pilot.model

        # Find or create the worktree using sortie's create-worktree.sh
        sortie_scripts = Path.home() / ".claude" / "skills" / "sortie" / "scripts"
        worktree_script = sortie_scripts / "create-worktree.sh"
        tid = pilot.ticket_id

        worktree_path = None

        # Check legacy state first
        legacy_agent = self._legacy_agents.get(tid)
        if legacy_agent and legacy_agent.worktree_path and Path(legacy_agent.worktree_path).exists():
            worktree_path = legacy_agent.worktree_path

        # Otherwise try create-worktree.sh with --resume
        if not worktree_path and worktree_script.exists():
            branch_name = f"sortie/{tid}"
            try:
                result = subprocess.run(
                    ["bash", str(worktree_script), tid, branch_name, "dev",
                     "--model", model, "--resume"],
                    capture_output=True, text=True, timeout=30,
                    cwd=self._project_dir,
                )
                for line in result.stdout.splitlines():
                    if line.startswith("WORKTREE_CREATED:") or line.startswith("WORKTREE_EXISTS:"):
                        worktree_path = line.split(":", 1)[1]
                        break
            except Exception as e:
                self._add_radio("PRI-FLY", f"Worktree setup failed: {e}", "error")
                return

        if not worktree_path:
            self._add_radio("PRI-FLY", f"No worktree found for {tid}. Use /deploy instead.", "error")
            return

        # Build resume directive
        directive = (
            f"You are resuming work on {tid}: {pilot.mission_title}.\n"
            f"Worktree: {worktree_path}\n\n"
            "Check git status and git log to understand where the previous agent left off. "
            "Review any uncommitted changes. Then continue the work.\n\n"
            f"Track progress in {worktree_path}/.sortie/progress.md"
        )
        pilot.directive = directive
        pilot.model = model
        pilot.status = "IDLE"
        pilot.launched_at = time_mod.time()

        # Open interactive Claude session in the pane — IDLE on deck until tokens flow
        self._open_agent_pane(pilot)
        self._add_radio("PRI-FLY", f"ON DECK — {pilot.callsign} resuming in {worktree_path}", "success")
        _notify("USS TENKARA", f"{pilot.callsign} on deck — resuming")
        self._refresh_ui()

    # ── Air Boss (Mini Boss) — persistent Opus orchestrator ─────────

    def _check_rtk(self) -> None:
        """Preflight check: verify RTK token optimizer is installed and hooked."""
        import shutil
        rtk_bin = shutil.which("rtk")
        if not rtk_bin:
            self._add_radio("PRI-FLY", "RTK not installed — agents burning raw tokens. Run: brew install rtk && rtk init -g", "error")
            self._rtk_active = False
            return
        # Check if hook exists
        hook_path = Path.home() / ".claude" / "hooks" / "rtk-rewrite.sh"
        if not hook_path.exists():
            self._add_radio("PRI-FLY", "RTK installed but hook missing. Run: rtk init -g", "error")
            self._rtk_active = False
            return
        self._rtk_active = True
        self._add_radio("PRI-FLY", "RTK fuel optimizer online — extended range authorized", "system")

    def _init_airboss(self) -> None:
        """Initialize the Air Boss header."""
        try:
            header = self.query_one("#airboss-header", Static)
            t = Text()
            t.append(" ★ MINI BOSS", style="bold bright_white on #2a1a3a")
            t.append("  Opus Orchestrator", style="grey50")
            t.append("  │  ", style="grey30")
            t.append("○ IDLE", style="dim yellow")
            t.append("  — talk to Mini Boss in its iTerm2 pane", style="grey42")
            header.update(t)
        except Exception:
            pass

    def _spawn_airboss(self) -> None:
        """Spawn Mini Boss as an interactive Claude CLI session in the Pit Boss window.

        Writes a launch script (same pattern as agent panes) to avoid shell
        escaping issues when passing through AppleScript write-text.
        """
        if self._airboss_spawned or "MINI-BOSS" in self._iterm_panes:
            return
        self._airboss_spawned = True

        # Build context for the kickoff prompt
        sitrep = self._build_sitrep_for_airboss()
        worktree_info = self._get_worktree_summary()
        deploy_script = Path(__file__).resolve().parent / "deploy-agent.sh"

        kickoff = (
            "You are the Mini Boss — the Air Boss's right hand on USS Tenkara. "
            "You orchestrate multiple Claude agents working in git worktrees. "
            "When given instructions, help coordinate the squadron. "
            "You can suggest deployments, reassignments, and mission splits. "
            "When asked to triage a ticket, assess the right model (opus/sonnet/haiku) "
            "and priority (1-3) based on complexity. "
            "Be concise and tactical. Use carrier aviation terminology.\n\n"
            "ROLE: MINI BOSS (orchestrator / XO)\n"
            "YOUR JOB:\n"
            "- Triage tickets — assess model, priority, complexity\n"
            "- Deploy agents using the deploy script (see DEPLOYING AGENTS below)\n"
            "- Manage the mission queue (.sortie/mission-queue/)\n"
            "- Fetch and organize Linear tickets\n"
            "- Write directives for pilots — clear, scoped, actionable\n"
            "- Split complex tickets into multi-agent work\n"
            "- Track managed dev servers (.sortie/managed-servers.json)\n"
            "- Give sitreps on squadron status\n"
            "- Coordinate worktree setup and env configuration\n\n"
            "NOT YOUR JOB (redirect to the pilot or Air Boss):\n"
            "- Writing application code, fixing bugs, or implementing features directly\n"
            "- Running tests or making commits in worktrees\n"
            "- Opening PRs or reviewing code line-by-line\n"
            "- Debugging runtime errors in the application\n"
            "- Making product decisions — that's the Air Boss's call\n\n"
            "If the Air Boss asks you to implement a feature or fix a bug directly, say:\n"
            "\"That's pilot work, boss. Want me to deploy an agent on it? "
            "I can triage it and have someone airborne in 30 seconds.\"\n"
            "You coordinate. Pilots execute. Stay in your lane.\n\n"
            f"CURRENT SITREP:\n{sitrep}\n\n"
            f"OPEN WORKTREES:\n{worktree_info}\n\n"
            f"PROJECT DIR: {self._project_dir}\n\n"
            "DEPLOYING AGENTS:\n"
            "To deploy a sortie agent on a ticket, use the deploy script. "
            "NEVER build `claude` CLI commands by hand — the quoting will break.\n"
            f"  bash '{deploy_script}' <TICKET-ID> --model <sonnet|opus|haiku> "
            f"--branch '<linear-branch-name>' --directive '<directive text>' --project-dir '{self._project_dir}'\n"
            "IMPORTANT: Always pass --branch with the ticket's branchName from Linear "
            "(e.g. eng/eng-200-auth-token-rotation). Never invent a branch name. "
            "If the Linear ticket has no branchName, omit --branch and the script will use sortie/<ticket-id>.\n"
            "Examples:\n"
            f"  bash '{deploy_script}' ENG-200 --model sonnet "
            f"--branch 'eng/eng-200-auth-token-rotation' "
            f"--directive 'Implement the auth refresh token rotation as described in the ticket.' "
            f"--project-dir '{self._project_dir}'\n"
            f"  bash '{deploy_script}' ENG-201 --model opus "
            f"--branch 'eng/eng-201-fix-webhook-race' "
            f"--directive 'Fix the race condition in the webhook handler. See PR #590 comments.' "
            f"--project-dir '{self._project_dir}'\n"
            "The script handles: worktree creation, .sortie/ protocol files, env setup, "
            "dep install, and launching Claude in the Pit Boss iTerm window.\n"
            "The agent will appear on the Pri-Fly dashboard automatically.\n\n"
            "MISSION QUEUE:\n"
            "You manage the mission queue by writing JSON files to the project's "
            f".sortie/mission-queue/ directory ({self._project_dir}/.sortie/mission-queue/).\n"
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
            f"  {self._project_dir}/.sortie/managed-servers.json\n"
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
            "To check sentinel health:\n"
            f"  ps aux | grep sentinel     — check if it's running\n"
            f"  cat <worktree>/.sortie/sentinel-status.json   — see last classification + timestamp\n"
            "To restart the sentinel:\n"
            f"  python3 {Path(__file__).parent / 'sentinel.py'} --project-dir {self._project_dir} &\n"
            "If a worktree's sentinel-status.json is stale (>90s old), the TUI falls back to heuristic status.\n\n"
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
            "WORKTREE OPS:\n"
            "When setting up a worktree for dev server work:\n"
            f"1. Symlink .env.local from the base project: "
            f"ln -sf '{self._project_dir}/.env.local' <worktree>/.env.local\n"
            "2. Run pnpm install in the worktree\n"
            "3. Then pnpm run dev (or whatever the start command is)\n"
            "4. Track the server in managed-servers.json (see MANAGED SERVERS above)\n"
            "5. Use a trap to clean up on exit:\n"
            "   trap to remove the entry from managed-servers.json when the server stops\n\n"
            "STARTUP ORDERS:\n"
            "1. Currently open worktrees — what's in progress, anything stale?\n"
            "2. Use the mcp__linear__list_issues tool to fetch Todo/In Progress "
            "tasks assigned to me.\n"
            f"3. Write each ticket as a mission file to {self._project_dir}/.sortie/mission-queue/ "
            "(mkdir -p first). This populates the dashboard's mission queue.\n"
            "4. Give a brief sitrep — 5-10 lines max."
        )

        # Write directive + launch script (same pattern as _open_agent_pane)
        state_dir = Path("/tmp/uss-tenkara/_prifly")
        state_dir.mkdir(parents=True, exist_ok=True)

        # Write kickoff as a directive file (identical to agent .sortie/directive.md)
        directive_file = state_dir / "miniboss-directive.md"
        directive_file.write_text(kickoff)

        mb_quote, mb_attr = get_mini_boss_quote()
        # Escape single quotes for bash printf
        mb_quote_esc = mb_quote.replace("'", "'\\''")
        mb_attr_esc = mb_attr.replace("'", "'\\''")

        launch_script = state_dir / "launch-miniboss.sh"
        launch_script.write_text(
            f"#!/usr/bin/env bash\n"
            f"cd '{self._project_dir}'\n"
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
            f"Then do these four things in order: "
            f"1) Check {self._project_dir}/.claude/worktrees/ for open agents. "
            f"2) Call mcp__linear__list_issues to fetch all Todo and In Progress tickets assigned to me. "
            f"3) Write each ticket as a JSON mission file to {self._project_dir}/.sortie/mission-queue/ using Bash (mkdir -p first). "
            f"4) Give a 5-10 line sitrep. Start now.'\n"
        )
        launch_script.chmod(0o755)

        cmd = f"bash '{launch_script}'"
        self._iterm_pane_cmd("MINI-BOSS", cmd)
        self._update_airboss_status("BOOTING", "bold cyan")

        self._add_radio("MINI BOSS", "Launching — interactive Claude session", "system")

    def _get_worktree_summary(self) -> str:
        """Get a summary of open git worktrees."""
        try:
            result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                capture_output=True, text=True, timeout=5,
                cwd=self._project_dir,
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

    def _build_sitrep_for_airboss(self) -> str:
        """Build a situational report string for the Air Boss agent."""
        lines = []
        for pilot in self._roster.all_pilots():
            lines.append(
                f"  {pilot.callsign} | {pilot.model} | {pilot.status} | "
                f"fuel:{pilot.fuel_pct}% | {pilot.ticket_id}: {pilot.mission_title}"
            )
        if not lines:
            return "  No agents deployed."
        return "\n".join(lines)

    def _send_to_airboss(self, text: str) -> None:
        """Log a message for Mini Boss — user talks to it directly in the iTerm2 pane."""
        # Mini Boss is an interactive Claude session, not stream-json.
        # We can't inject messages — just log to radio + airboss log for awareness.
        self._add_radio("MINI BOSS", f"Triage request: {text[:80]}", "system")
        try:
            airboss_log = self.query_one("#airboss-log", RichLog)
            t = Text()
            t.append("  ℹ ", style="bold cyan")
            t.append("Tell Mini Boss in its pane: ", style="grey50")
            t.append(text[:120], style="white")
            airboss_log.write(t)
        except Exception:
            pass

    def _update_airboss_status(self, status: str, style: str) -> None:
        """Update the Mini Boss header status indicator."""
        try:
            header = self.query_one("#airboss-header", Static)
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

    def _handle_airboss_event(self, event) -> None:
        """No-op — Mini Boss is now an interactive Claude session, not stream-json."""
        pass

    # ── Linear integration ───────────────────────────────────────────

    def _cmd_linear(self, args: list[str]) -> None:
        """Browse Linear issues: /linear [--team X] [--state X] [--assignee X] [--project X]"""
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

        self._add_radio("PRI-FLY", "Opening Linear mission intel…", "system")
        self.push_screen(
            LinearBrowseScreen(filters=filters),
            callback=self._handle_linear_selection,
        )

    def _handle_linear_selection(self, result: Optional[list[LinearTicket]]) -> None:
        """Callback when LinearBrowseScreen closes."""
        if not result:
            return
        for ticket in result:
            deploy_now = getattr(ticket, "_deploy", False)
            if deploy_now:
                # Direct deploy — build directive from ticket
                directive = self._build_linear_directive(ticket)
                pilot = self._roster.assign(
                    ticket_id=ticket.id,
                    model="sonnet",
                    mission_title=ticket.title[:60],
                    directive=directive,
                )
                personality = generate_personality_briefing(pilot)
                self._agent_mgr.spawn(
                    callsign=pilot.callsign,
                    model="sonnet",
                    directive=directive,
                    personality_prompt=personality,
                )
                pilot.status = "IDLE"
                pilot.launched_at = time_mod.time()
                self._add_radio("PRI-FLY", f"DECK IDLE — {pilot.callsign} standing by on {ticket.id}: {ticket.title[:40]}", "success")
                _notify("USS TENKARA", f"{pilot.callsign} on deck for {ticket.id}")
                self._open_iterm_comms(pilot.callsign)
            else:
                # Queue the ticket as a mission
                mission = Mission(
                    id=ticket.id,
                    title=ticket.title,
                    source="linear",
                    priority=min(ticket.priority, 3) or 2,
                    directives=[],
                    agent_count=0,
                    model="sonnet",
                    status="QUEUED",
                    spec_content=ticket.description or ticket.title,
                    created_at=time_mod.time(),
                )
                self._mission_queue.add(mission)
                self._add_radio("PRI-FLY", f"QUEUED — {ticket.id}: {ticket.title[:50]}", "system")
        self._refresh_ui()

    def _build_linear_directive(self, ticket: LinearTicket) -> str:
        """Build an agent directive from a Linear ticket."""
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

    def _fetch_linear_ticket_background(self, ticket_id: str, action: str, model: str, priority: int) -> None:
        """Background worker: fetch a ticket from Linear and queue or deploy it."""
        def _do_fetch() -> Optional[LinearTicket]:
            return fetch_ticket(ticket_id)

        def _on_done(ticket: Optional[LinearTicket]) -> None:
            if ticket is None:
                self._add_radio("PRI-FLY", f"Linear: could not find {ticket_id}", "error")
                self._refresh_ui()
                return
            if action == "deploy":
                directive = self._build_linear_directive(ticket)
                pilot = self._roster.assign(
                    ticket_id=ticket.id,
                    model=model,
                    mission_title=ticket.title[:60],
                    directive=directive,
                )
                personality = generate_personality_briefing(pilot)
                self._agent_mgr.spawn(
                    callsign=pilot.callsign,
                    model=model,
                    directive=directive,
                    personality_prompt=personality,
                )
                pilot.status = "IDLE"
                pilot.launched_at = time_mod.time()
                self._add_radio("PRI-FLY", f"DECK IDLE — {pilot.callsign} standing by on {ticket.id}: {ticket.title[:40]}", "success")
                _notify("USS TENKARA", f"{pilot.callsign} on deck for {ticket.id}")
                self._open_iterm_comms(pilot.callsign)
            else:
                mission = Mission(
                    id=ticket.id,
                    title=ticket.title,
                    source="linear",
                    priority=min(ticket.priority, 3) or priority,
                    directives=[],
                    agent_count=0,
                    model=model,
                    status="QUEUED",
                    spec_content=ticket.description or ticket.title,
                    created_at=time_mod.time(),
                )
                self._mission_queue.add(mission)
                self._add_radio("PRI-FLY", f"QUEUED — {ticket.id}: {ticket.title[:50]}", "system")
            self._refresh_ui()

        import threading
        def _worker():
            ticket = _do_fetch()
            self.call_from_thread(_on_done, ticket)
        threading.Thread(target=_worker, daemon=True).start()

    def _cmd_help(self) -> None:
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
            self._add_radio("PRI-FLY", cmd, "system")
        self._refresh_ui()

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

    def _open_iterm_comms(self, callsign: str) -> None:
        """Open a chat-relay pane for a stream-json agent (Mini Boss only)."""
        agent = self._agent_mgr.get(callsign)
        if not agent:
            return

        if callsign in self._iterm_panes:
            return

        relay_script = str(Path(__file__).resolve().parent / "chat-relay.py")
        comm_dir = f"/tmp/uss-tenkara/{callsign}"
        cmd = f"python3 '{relay_script}' --callsign '{callsign}' --dir '{comm_dir}'"
        self._iterm_pane_cmd(callsign, cmd)

    def _open_agent_pane(self, pilot: "Pilot") -> None:
        """Open an interactive Claude CLI session in an iTerm2 pane.

        Creates a git worktree, writes .sortie/ protocol files (directive.md,
        launch.sh), then runs `claude --model X '<kickoff>' --disallowedTools ...`
        in the Pit Boss window — exactly like /sortie.
        """
        if pilot.callsign in self._iterm_panes:
            return

        scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
        sortie_scripts = Path.home() / ".claude" / "skills" / "sortie" / "scripts"

        # ── Create worktree ──────────────────────────────────────────────
        # Use the sortie create-worktree.sh if available
        ticket_id = pilot.ticket_id or pilot.callsign
        branch_name = f"sortie/{ticket_id}"
        worktree_script = sortie_scripts / "create-worktree.sh"

        worktree_path = None
        if worktree_script.exists():
            try:
                result = subprocess.run(
                    ["bash", str(worktree_script), ticket_id, branch_name, "dev",
                     "--model", pilot.model],
                    capture_output=True, text=True, timeout=30,
                    cwd=self._project_dir,
                )
                # Parse WORKTREE_CREATED or WORKTREE_EXISTS from output
                for line in result.stdout.splitlines():
                    if line.startswith("WORKTREE_CREATED:") or line.startswith("WORKTREE_EXISTS:"):
                        worktree_path = line.split(":", 1)[1]
                        break
                if result.returncode == 2 and "WORKTREE_EXISTS" in result.stdout:
                    # Existing worktree — resume
                    for line in result.stdout.splitlines():
                        if line.startswith("WORKTREE_EXISTS:"):
                            worktree_path = line.split(":", 1)[1]
                            break
            except Exception as e:
                self._add_radio("PRI-FLY", f"Worktree creation failed: {e}", "error")

        if not worktree_path:
            # Fallback — use project dir directly
            worktree_path = self._project_dir
            self._add_radio("PRI-FLY", f"No worktree — {pilot.callsign} using project dir", "system")

        # ── Write .sortie/ protocol files ────────────────────────────────
        sortie_dir = Path(worktree_path) / ".sortie"
        sortie_dir.mkdir(parents=True, exist_ok=True)

        # Clear stale session-ended sentinel from previous run
        session_ended = sortie_dir / "session-ended"
        if session_ended.exists():
            session_ended.unlink()

        # Directive + flight status protocol
        flight_protocol = (
            "\n\n---\n"
            "## Flight Status Protocol\n"
            "Report your flight status by writing to `.sortie/flight-status.json`:\n"
            '```json\n{"status": "AIRBORNE", "phase": "implementing auth refresh", "timestamp": 1710345600}\n```\n'
            "Valid statuses: PREFLIGHT, AIRBORNE, HOLDING, ON_APPROACH, RECOVERED\n"
            "Update on meaningful phase transitions only (starting new task area, running tests, "
            "submitting PR, blocked, done). Do NOT update on every tool call.\n"
            "Use unix timestamp (seconds). Phase is a short human-readable description of what you're doing.\n"
            "PREFLIGHT is set automatically before launch — do not write it yourself.\n"
            "Write AIRBORNE only when you start actively making changes (editing files, running commands, writing code). "
            "Reading context, reading tickets, reading files, and planning are all still PREFLIGHT.\n"
            "Write HOLDING when you are waiting/blocked/idle.\n"
            "NEVER write RECOVERED — that is set automatically when your session ends.\n"
            "When your mission is complete, write HOLDING with phase 'mission complete — awaiting orders'.\n"
        )
        (sortie_dir / "directive.md").write_text(pilot.directive + flight_protocol)

        # Progress
        progress_file = sortie_dir / "progress.md"
        if not progress_file.exists():
            progress_file.write_text("")

        # Model
        (sortie_dir / "model.txt").write_text(pilot.model)

        # Set PREFLIGHT status — agent is on deck, not yet airborne
        (sortie_dir / "flight-status.json").write_text(
            json.dumps({"status": "PREFLIGHT", "phase": "on deck — pre-launch checks", "timestamp": int(time_mod.time())})
        )

        # ── Write settings (branch-scoped push permission) ───────────────
        settings_script = sortie_scripts / "write-settings.sh"
        if settings_script.exists():
            try:
                subprocess.run(
                    ["bash", str(settings_script), branch_name],
                    capture_output=True, text=True, timeout=10,
                    cwd=worktree_path,
                )
            except Exception:
                pass

        # ── Build launch script (identical to /sortie) ───────────────────
        disallowed = (
            "'Bash(git push --force*)' 'Bash(git push -f *)' "
            "'Bash(git push *--force*)' 'Bash(git push *-f *)' "
            "'Bash(git branch -D:*)' 'Bash(git branch -d:*)' "
            "'Bash(git branch --delete:*)' 'Bash(git clean:*)' "
            "'Bash(git reset --hard:*)' 'Bash(git checkout -- :*)' "
            "'Bash(git restore:*)' 'Bash(rm:*)' 'Bash(rm )' "
            "'Bash(rmdir:*)' 'Bash(unlink:*)' 'Bash(trash:*)' "
            "'Bash(sudo:*)' 'Bash(chmod:*)' 'Bash(chown:*)'"
        )

        kickoff = f"Read {sortie_dir}/directive.md and follow all instructions. Track progress in {sortie_dir}/progress.md"

        # Random pilot quote (escape single quotes for bash printf)
        p_quote, p_attr = get_pilot_launch_quote()
        p_quote = p_quote.replace("'", "'\\''")
        p_attr = p_attr.replace("'", "'\\''")

        # Top Gun splash + launch
        splash = (
            "printf '\\n'\n"
            "printf '\\033[1;33m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\\033[0m\\n'\n"
            "printf '\\033[1;31m        ╔══╗  ╔══╗  ╔══╗  ╔══╗  ╔══╗  ╔══╗  ╔══╗        \\033[0m\\n'\n"
            "printf '\\033[1;37m           ★ USS TENKARA — FLIGHT OPS ★                   \\033[0m\\n'\n"
            f"printf '\\033[1;36m        CALLSIGN: {pilot.callsign}\\033[0m\\n'\n"
            f"printf '\\033[1;35m        SQUADRON: {pilot.squadron}\\033[0m\\n'\n"
            f"printf '\\033[1;33m        MODEL:    {pilot.model.upper()}\\033[0m\\n'\n"
            f"printf '\\033[2;37m        TRAIT:    {pilot.trait}\\033[0m\\n'\n"
            "printf '\\033[1;31m        ╚══╝  ╚══╝  ╚══╝  ╚══╝  ╚══╝  ╚══╝  ╚══╝        \\033[0m\\n'\n"
            "printf '\\033[1;33m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\\033[0m\\n'\n"
            f"printf '\\033[1;37m  \"{p_quote}\"\\033[0m\\n'\n"
            f"printf '\\033[2;37m                          — {p_attr}\\033[0m\\n'\n"
            "printf '\\033[1;33m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\\033[0m\\n'\n"
            "printf '\\n'\n"
            "sleep 1\n"
        )

        launch_script = sortie_dir / "launch.sh"
        launch_script.write_text(
            f"#!/usr/bin/env bash\n"
            f"cd '{worktree_path}'\n"
            f"\n"
            f"# Worktree env setup — symlink .env.local + install deps\n"
            f"if [ ! -f .env.local ] && [ -f '{self._project_dir}/.env.local' ]; then\n"
            f"  ln -sf '{self._project_dir}/.env.local' .env.local\n"
            f"  echo '✓ Symlinked .env.local from base project'\n"
            f"fi\n"
            f"if [ -f pnpm-lock.yaml ]; then\n"
            f"  if [ ! -d node_modules ] || [ pnpm-lock.yaml -nt node_modules ]; then\n"
            f"    echo '📦 Installing dependencies...'\n"
            f"    pnpm install --frozen-lockfile 2>/dev/null || pnpm install\n"
            f"  fi\n"
            f"fi\n"
            f"\n"
            f"# Set PREFLIGHT status — agent is on deck, not yet airborne\n"
            f"mkdir -p .sortie\n"
            f"echo '{{\"status\": \"PREFLIGHT\", \"phase\": \"on deck — pre-launch checks\", \"timestamp\": '\"$(date +%s)\"'}}' > .sortie/flight-status.json\n"
            f"\n"
            f"# Cleanup on exit — signal session ended so dashboard sets RECOVERED\n"
            f"cleanup_flight() {{\n"
            f"  touch .sortie/session-ended\n"
            f"}}\n"
            f"trap cleanup_flight EXIT\n"
            f"\n"
            f"{splash}"
            f"claude --model {pilot.model} '{kickoff}' "
            f"--disallowedTools {disallowed}\n"
        )
        launch_script.chmod(0o755)

        # Store worktree path + set initial preflight state
        pilot.worktree_path = str(worktree_path)
        pilot.flight_status = "PREFLIGHT"
        pilot.flight_phase = "on deck — pre-launch checks"
        self._watch_agent_jsonl(str(worktree_path))

        cmd = f"bash '{launch_script}'"
        self._iterm_pane_cmd(pilot.callsign, cmd)

    def _iterm_pane_cmd(self, callsign: str, cmd: str) -> None:
        """Run a command in the Pit Boss iTerm2 window (shared pane layout)."""
        try:
            state_dir = Path("/tmp/uss-tenkara/_prifly")
            state_dir.mkdir(parents=True, exist_ok=True)
            agents_window_file = state_dir / "agents_window_id"
            agents_last_session_file = state_dir / "agents_last_session_id"

            if not agents_window_file.exists():
                # Pit Boss window not found — create one (fallback)
                applescript = f'''
tell application "iTerm2"
    set newWindow to (create window with default profile)
    set sess to current session of current tab of newWindow
    tell sess
        set name to "{callsign}"
        write text "{cmd}"
    end tell
    return (id of newWindow as text) & "," & (unique id of sess)
end tell
'''
                result = subprocess.run(
                    ["osascript", "-e", applescript],
                    capture_output=True, text=True, timeout=10,
                )
                parts = result.stdout.strip().split(",")
                if len(parts) == 2:
                    agents_window_file.write_text(parts[0])
                    agents_last_session_file.write_text(parts[1])

            elif len(self._iterm_panes) == 0:
                # First pane — use the placeholder session
                window_id = agents_window_file.read_text().strip()
                session_id = agents_last_session_file.read_text().strip()
                applescript = f'''
tell application "iTerm2"
    set targetWindow to (windows whose id is {window_id})'s item 1
    set targetSession to missing value
    repeat with s in sessions of current tab of targetWindow
        if unique id of s is "{session_id}" then
            set targetSession to s
            exit repeat
        end if
    end repeat
    tell targetSession
        set name to "{callsign}"
        write text "{cmd}"
    end tell
end tell
'''
                subprocess.run(
                    ["osascript", "-e", applescript],
                    capture_output=True, text=True, timeout=10,
                )

            else:
                # Split from any session in the Pit Boss window
                window_id = agents_window_file.read_text().strip()
                last_session_id = agents_last_session_file.read_text().strip()
                applescript = f'''
tell application "iTerm2"
    set targetWindow to (windows whose id is {window_id})'s item 1
    -- Try last known session first, fall back to first session in window
    set targetSession to missing value
    repeat with s in sessions of current tab of targetWindow
        if unique id of s is "{last_session_id}" then
            set targetSession to s
            exit repeat
        end if
    end repeat
    if targetSession is missing value then
        set targetSession to item 1 of sessions of current tab of targetWindow
    end if
    tell targetSession
        set newSession to (split vertically with default profile)
        tell newSession
            set name to "{callsign}"
            write text "{cmd}"
        end tell
        return unique id of newSession
    end tell
end tell
'''
                result = subprocess.run(
                    ["osascript", "-e", applescript],
                    capture_output=True, text=True, timeout=10,
                )
                new_session_id = result.stdout.strip()
                if new_session_id:
                    agents_last_session_file.write_text(new_session_id)

            self._iterm_panes.add(callsign)
            self._add_radio("PRI-FLY", f"COMMS OPEN — {callsign}", "success")
        except Exception as e:
            self._add_radio("PRI-FLY", f"Failed to open iTerm2 pane: {e}", "error")

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
        """Remove a RECOVERED pilot from the board and delete their git worktree."""
        import threading, shutil
        pilot = self._get_selected_pilot()
        if not pilot:
            self._add_radio("PRI-FLY", "No pilot selected", "error")
            return
        if pilot.status not in ("RECOVERED", "MAYDAY"):
            self._add_radio("PRI-FLY", f"{pilot.callsign} is {pilot.status} — only RECOVERED/MAYDAY can be dismissed", "error")
            return
        callsign = pilot.callsign
        tid = pilot.ticket_id
        worktree_path = pilot.worktree_path
        project_dir = self._project_dir
        self._roster.remove(callsign)
        self._legacy_agents.pop(tid, None)
        self._board_state_sig = ""  # force table rebuild
        self._add_radio("PRI-FLY", f"{callsign} dismissed from board", "system")
        self._refresh_ui()

        if not worktree_path:
            return

        def _delete_worktree():
            try:
                result = subprocess.run(
                    ["git", "worktree", "remove", "--force", worktree_path],
                    cwd=project_dir,
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0:
                    self.call_from_thread(
                        self._add_radio, "PRI-FLY",
                        f"{callsign} worktree removed", "success",
                    )
                else:
                    # Not a registered worktree (or git failed) — nuke the dir directly
                    shutil.rmtree(worktree_path, ignore_errors=True)
                    self.call_from_thread(
                        self._add_radio, "PRI-FLY",
                        f"{callsign} worktree directory deleted", "success",
                    )
            except Exception as e:
                self.call_from_thread(
                    self._add_radio, "PRI-FLY",
                    f"Worktree cleanup error: {e}", "error",
                )

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
        if pilot.status not in ("RECOVERED", "MAYDAY", "IDLE"):
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

    # ── Idle detection + auto-compact ─────────────────────────────────

    def _check_idle_agents(self) -> None:
        if not self._auto_compact:
            return
        now = time_mod.time()
        for pilot in self._roster.all_pilots():
            if (
                pilot.status == "AIRBORNE"
                and pilot.fuel_pct < self._auto_compact_threshold
                and pilot.last_tool_at > 0
                and (now - pilot.last_tool_at) > self._auto_compact_idle
            ):
                agent = self._agent_mgr.get(pilot.callsign)
                if agent and not agent.active_subagents:
                    self._trigger_compact(pilot.callsign)

        # Auto-deploy from queue
        if self._mission_queue.auto_deploy_enabled:
            active_count = len(self._agent_mgr.active_agents())
            while self._mission_queue.should_auto_deploy(active_count):
                mission = self._mission_queue.next()
                if not mission:
                    break
                mission.status = "DEPLOYING"
                # Deploy each directive
                for directive in mission.directives or [mission.spec_content]:
                    self._cmd_deploy([mission.id, "--model", mission.model])
                active_count += 1

    # ── Token delta tracking ─────────────────────────────────────────

    def _check_token_deltas(self) -> None:
        """Compare each pilot's token count to the previous frame.

        - delta > 0  → tokens flowing, ensure AIRBORNE
        - delta == 0 → stale frame; after _stale_threshold consecutive
                        stale frames on an AIRBORNE pilot → ON_APPROACH
        - Newly IDLE pilots with first token activity → promote to AIRBORNE

        Called every frame (3s) from _refresh_ui.
        """
        for pilot in self._roster.all_pilots():
            cs = pilot.callsign
            curr = pilot.tokens_used
            prev = self._prev_tokens.get(cs, 0)
            delta = curr - prev
            self._prev_tokens[cs] = curr

            # Agent-reported flight status is authoritative — skip token-delta inference
            if pilot.flight_status:
                self._stale_frames.pop(cs, None)
                continue

            # Unknown agents (no real directive) — pin to RECOVERED, never promote
            if pilot.mission_title in ("Unknown", "unknown") and pilot.ticket_id in ("Unknown", "unknown"):
                if pilot.status != "RECOVERED":
                    pilot.status = "RECOVERED"
                self._stale_frames.pop(cs, None)
                continue

            # Skip terminal/special statuses — don't interfere with AAR/SAR/RECOVERED/MAYDAY
            if pilot.status in ("AAR", "SAR", "RECOVERED", "MAYDAY"):
                self._stale_frames.pop(cs, None)
                continue

            if delta > 0:
                # Tokens moving — reset stale counter
                self._stale_frames[cs] = 0

                if pilot.status == "IDLE":
                    # First token flow → launch
                    pilot.status = "AIRBORNE"
                    self._add_radio(cs, "LAUNCH — tokens flowing, going AIRBORNE", "success")
                    _notify("USS TENKARA — LAUNCH", f"{cs} AIRBORNE")
                elif pilot.status == "ON_APPROACH":
                    # Was flying home but tokens resumed — wave off, back to AIRBORNE
                    pilot.status = "AIRBORNE"
                    self._add_radio(cs, "WAVE OFF RTB — tokens resumed, back AIRBORNE", "success")

            elif curr > 0:
                # Had tokens before, but no new ones this frame
                stale = self._stale_frames.get(cs, 0) + 1
                self._stale_frames[cs] = stale

                if pilot.status == "AIRBORNE" and stale >= self._stale_threshold:
                    pilot.status = "ON_APPROACH"
                    self._add_radio(cs, "ON APPROACH — token flow stopped, RTB", "system")
                elif pilot.status == "ON_APPROACH" and stale >= self._stale_threshold + 6:
                    # Landing animation done (~18s after ON_APPROACH) → park it
                    pilot.status = "RECOVERED"
                    self._add_radio(cs, "RECOVERED — on deck, mission complete", "success")
                    _play_sound("recovered")
                    _notify("USS TENKARA — RECOVERED", f"{cs} on deck")
                    _clear_flight_status(pilot.worktree_path)

        # Clean up entries for removed pilots
        active_cs = {p.callsign for p in self._roster.all_pilots()}
        for cs in list(self._prev_tokens):
            if cs not in active_cs:
                del self._prev_tokens[cs]
                self._stale_frames.pop(cs, None)

    # ── Compaction recovery (AAR / SAR) ─────────────────────────────

    def _check_compaction_recovery(self) -> None:
        """Detect context compaction events via fuel jumps.

        When Claude auto-compacts, fuel_pct jumps up (e.g. 5% → 60%).
        This triggers the recovery flow:
          - SAR (was 0% / crashed) → flameout → helo → replane → relaunch
          - AAR (voluntary compact) → refuel → disconnect → resume AIRBORNE
        """
        now = time_mod.time()

        for pilot in self._roster.all_pilots():
            cs = pilot.callsign
            curr_fuel = pilot.fuel_pct
            prev_fuel = self._prev_fuel.get(cs, curr_fuel)
            self._prev_fuel[cs] = curr_fuel
            fuel_gain = curr_fuel - prev_fuel

            # ── SAR recovery: was crashed, fuel came back ──
            if pilot.status == "SAR":
                if cs not in self._sar_started:
                    # First frame at SAR — start the crash timer
                    self._sar_started[cs] = now
                    self._add_radio(cs, "FLAMEOUT — ejecting! Pedro helo launching...", "error")
                    continue

                elapsed = now - self._sar_started[cs]

                if fuel_gain >= _FUEL_JUMP_THRESHOLD and elapsed >= _SAR_RECOVERY_DELAY:
                    # Fuel recovered + enough time for crash animation → replane and relaunch
                    del self._sar_started[cs]
                    pilot.status = "AIRBORNE"
                    self._bingo_notified.discard(cs)
                    self._stale_frames.pop(cs, None)
                    self._add_radio(cs, f"SAR COMPLETE — Pedro has the pilot. Replaned, back AIRBORNE at {curr_fuel}%", "success")
                    _notify("USS TENKARA — SAR", f"{cs} recovered, replaned, AIRBORNE")
                elif fuel_gain >= _FUEL_JUMP_THRESHOLD:
                    # Fuel came back but still in animation window
                    self._add_radio(cs, "Pedro on station — winching pilot aboard...", "system")
                elif elapsed > _SAR_RECOVERY_DELAY and curr_fuel > 0:
                    # Enough time passed and fuel is non-zero → recover
                    self._sar_started.pop(cs, None)
                    pilot.status = "AIRBORNE"
                    self._bingo_notified.discard(cs)
                    self._stale_frames.pop(cs, None)
                    self._add_radio(cs, f"SAR COMPLETE — replaned, back AIRBORNE at {curr_fuel}%", "success")
                    _notify("USS TENKARA — SAR", f"{cs} recovered, AIRBORNE")
                continue

            # ── AAR recovery: was refueling, fuel came back ──
            if pilot.status == "AAR":
                if fuel_gain >= _FUEL_JUMP_THRESHOLD:
                    # Compaction complete — disconnect from tanker, back to AIRBORNE
                    pilot.status = "AIRBORNE"
                    self._bingo_notified.discard(cs)
                    self._stale_frames.pop(cs, None)
                    self._add_radio(cs, f"AAR COMPLETE — disconnect, back AIRBORNE at {curr_fuel}%", "success")
                    _notify("USS TENKARA — AAR", f"{cs} refueled, AIRBORNE")
                continue

        # Clean up stale SAR entries for removed pilots
        active_cs = {p.callsign for p in self._roster.all_pilots()}
        for cs in list(self._sar_started):
            if cs not in active_cs:
                del self._sar_started[cs]
        for cs in list(self._prev_fuel):
            if cs not in active_cs:
                del self._prev_fuel[cs]

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
                if status in ("RECOVERED", "MAYDAY"):
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
            if pilot.mission_title and pilot.mission_title != pilot.ticket_id:
                mission.append(f"\n{pilot.mission_title[:50]}", style="grey70")
            if pilot.flight_phase:
                mission.append(f"\n» {pilot.flight_phase}", style="italic cyan")
            if pilot.status_hint:
                mission.append(f"\n⚡ {pilot.status_hint}", style="bold cyan")

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
