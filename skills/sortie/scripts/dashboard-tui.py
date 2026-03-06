#!/usr/bin/env python3
"""Sortie TUI Dashboard — Real-time terminal UI for monitoring sortie agents."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional

# Add lib to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from read_sortie_state import read_sortie_state, get_all_progress_entries, AgentState

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, RichLog, Static


# ── Status color mapping ──────────────────────────────────────────────

STATUS_COLORS = {
    "WORKING": "dodger_blue1",
    "PRE-REVIEW": "dark_orange",
    "DONE": "green",
}

PROGRESS_BAR_COLORS = {
    "WORKING": "blue",
    "PRE-REVIEW": "yellow",
    "DONE": "green",
}

PROGRESS_BAR_WIDTH = 16


def make_progress_bar(status: str) -> Text:
    """Create a colored text-based progress bar based on status."""
    if status == "DONE":
        filled = PROGRESS_BAR_WIDTH
    elif status == "PRE-REVIEW":
        filled = int(PROGRESS_BAR_WIDTH * 0.75)
    else:
        filled = int(PROGRESS_BAR_WIDTH * 0.4)

    color = PROGRESS_BAR_COLORS.get(status, "white")
    bar = Text()
    bar.append("\u2588" * filled, style=color)
    bar.append("\u2591" * (PROGRESS_BAR_WIDTH - filled), style="grey37")
    return bar


def make_status_text(status: str) -> Text:
    color = STATUS_COLORS.get(status, "white")
    return Text(status, style=f"bold {color}")


# ── Modal for ticket ID input ─────────────────────────────────────────

class TicketInputScreen(ModalScreen[str]):
    """Modal screen that prompts for a ticket ID."""

    BINDINGS = [Binding("escape", "dismiss('')", "Cancel")]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self._prompt, id="modal-prompt"),
            Input(placeholder="e.g. ENG-103", id="modal-input"),
            id="modal-container",
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())


# ── Header bar ────────────────────────────────────────────────────────

class SortieHeader(Static):
    """Custom header showing SORTIE LIVE and summary stats."""

    def render(self) -> Text:
        state = self.app._state  # type: ignore[attr-defined]
        header = Text()
        header.append(" \u25cf ", style="bold green")
        header.append("SORTIE ", style="bold white")
        header.append("\u2022 ", style="grey50")
        header.append("LIVE", style="bold green")

        stats = Text()
        stats.append(f"  {state.total} agents  ", style="white")
        stats.append(f"{state.working} working  ", style="dodger_blue1")
        stats.append(f"{state.pre_review} pre-review  ", style="dark_orange")
        stats.append(f"{state.done} done", style="green")

        header.append(stats)
        return header


# ── Main App ──────────────────────────────────────────────────────────

class SortieDashboard(App):
    """Live TUI dashboard for sortie agents."""

    CSS = """
    Screen {
        background: $surface;
    }

    #header-bar {
        dock: top;
        height: 1;
        background: $surface-darken-1;
        padding: 0 1;
    }

    #agent-table {
        height: 1fr;
        min-height: 5;
    }

    #progress-section {
        height: auto;
        max-height: 12;
        border-top: solid $accent;
    }

    #progress-title {
        height: 1;
        padding: 0 1;
        background: $surface-darken-1;
        color: $text-muted;
    }

    #progress-log {
        height: auto;
        max-height: 10;
        padding: 0 1;
    }

    #modal-container {
        align: center middle;
        width: 50;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    #modal-prompt {
        margin-bottom: 1;
        text-style: bold;
    }
    """

    BINDINGS = [
        Binding("k", "kill_agent", "Kill agent"),
        Binding("r", "respawn_agent", "Respawn agent"),
        Binding("s", "refresh_state", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._state = read_sortie_state()

    def compose(self) -> ComposeResult:
        yield SortieHeader(id="header-bar")
        yield DataTable(id="agent-table")
        yield Vertical(
            Static(" LAST PROGRESS LOG", id="progress-title"),
            RichLog(id="progress-log", highlight=True, markup=True),
            id="progress-section",
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#agent-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("TICKET", "MODEL", "PROGRESS", "STATUS", "TIME", "LAST ACTION")
        self._do_refresh()
        self.set_interval(3.0, self._do_refresh)

    def _do_refresh(self) -> None:
        self._state = read_sortie_state()
        self._refresh_ui()

    def _refresh_ui(self) -> None:
        self._refresh_table()
        self._refresh_progress_log()
        self.query_one("#header-bar", SortieHeader).refresh()

    def _refresh_table(self) -> None:
        table = self.query_one("#agent-table", DataTable)
        table.clear()

        for agent in self._state.agents:
            ticket_display = agent.ticket_id
            if agent.is_sub_agent:
                label = agent.sub_name or agent.ticket_id
                ticket_display = f"  \u2514 {agent.ticket_id}/{label}"

            last_action = agent.last_progress[-1] if agent.last_progress else "(no progress)"
            # Strip timestamp prefix for display
            if last_action.startswith("[") and "]" in last_action:
                last_action = last_action[last_action.index("]") + 1:].strip()
            # Truncate
            if len(last_action) > 60:
                last_action = last_action[:57] + "..."

            table.add_row(
                Text(ticket_display, style="bold"),
                Text(agent.model, style="italic"),
                make_progress_bar(agent.status),
                make_status_text(agent.status),
                Text(f"[{agent.elapsed_time}]", style="grey70"),
                Text(last_action),
            )

    def _refresh_progress_log(self) -> None:
        log = self.query_one("#progress-log", RichLog)
        log.clear()

        entries = get_all_progress_entries(self._state.agents, max_entries=10)
        if not entries:
            log.write(Text("  No progress entries yet.", style="grey50"))
            return

        for entry in reversed(entries):  # oldest first
            ts = entry.get("timestamp", "--:--")
            ticket = entry.get("ticket_id", "???")
            msg = entry.get("message", "")
            entry_type = entry.get("type", "normal")

            line = Text()
            line.append(f" [{ts}] ", style="grey70")
            line.append(f"{ticket}: ", style="bold")

            if entry_type == "error":
                line.append(msg, style="bold red")
            elif entry_type == "success":
                line.append(msg, style="bold green")
            else:
                line.append(msg)

            log.write(line)

    def _find_agent(self, query: str) -> Optional[AgentState]:
        """Find agent by ticket ID, or ticket/sub-name for sub-agents."""
        q = query.upper().strip()
        # Try exact ticket/sub-name match first (e.g. "ENG-103/tui")
        if "/" in q:
            ticket_part, sub_part = q.split("/", 1)
            for agent in self._state.agents:
                if agent.ticket_id.upper() == ticket_part and (agent.sub_name or "").upper() == sub_part:
                    return agent
        # Fall back to ticket ID match (returns first match)
        for agent in self._state.agents:
            if agent.ticket_id.upper() == q:
                return agent
        return None

    def _find_claude_pid(self, worktree_path: str) -> Optional[int]:
        """Find the PID of the claude process running in a given worktree.

        Uses ps + exact path matching to avoid regex injection from path chars.
        """
        try:
            # Get all claude processes with their full command lines
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
                # Exact substring match on worktree path (no regex)
                if worktree_path in line:
                    parts = line.split(None, 1)
                    if parts:
                        try:
                            candidates.append(int(parts[0]))
                        except ValueError:
                            continue

            if len(candidates) == 1:
                return candidates[0]
            elif len(candidates) > 1:
                # Ambiguous — return None rather than killing the wrong process
                return None
            return None
        except (subprocess.TimeoutExpired, OSError):
            return None

    # ── Actions ───────────────────────────────────────────────────────

    def action_refresh_state(self) -> None:
        self._do_refresh()
        self.notify("State refreshed", timeout=2)

    async def action_kill_agent(self) -> None:
        ticket_id = await self.push_screen_wait(
            TicketInputScreen("Kill agent — enter ticket ID:")
        )
        if not ticket_id:
            return

        agent = self._find_agent(ticket_id)
        if not agent:
            self.notify(f"Agent {ticket_id} not found", severity="error", timeout=3)
            return

        pid = self._find_claude_pid(agent.worktree_path)
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                self.notify(f"Sent SIGTERM to {ticket_id} (PID {pid})", timeout=3)
            except OSError as e:
                self.notify(f"Failed to kill {ticket_id}: {e}", severity="error", timeout=3)
        else:
            self.notify(f"No running process found for {ticket_id}", severity="warning", timeout=3)

    async def action_respawn_agent(self) -> None:
        ticket_id = await self.push_screen_wait(
            TicketInputScreen("Respawn agent — enter ticket ID:")
        )
        if not ticket_id:
            return

        agent = self._find_agent(ticket_id)
        if not agent:
            self.notify(f"Agent {ticket_id} not found", severity="error", timeout=3)
            return

        spawn_script = Path.home() / ".claude" / "skills" / "sortie" / "scripts" / "spawn-pane.sh"

        if not spawn_script.exists():
            self.notify("spawn-pane.sh not found", severity="error", timeout=3)
            return

        try:
            subprocess.Popen(
                [str(spawn_script), agent.worktree_path, agent.model, ticket_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.notify(f"Respawning {ticket_id}", timeout=3)
        except (FileNotFoundError, PermissionError, OSError) as e:
            self.notify(f"Respawn failed: {e}", severity="error", timeout=3)


if __name__ == "__main__":
    app = SortieDashboard()
    app.run()
