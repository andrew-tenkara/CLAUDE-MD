"""USS Tenkara PRI-FLY — modal screens.

Self-contained ModalScreen subclasses: splash, confirmation, briefing,
deploy input, and Linear browse. Communicate via dismiss() return values.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

# Add lib to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from linear_bridge import (
    LinearTicket, priority_label, priority_style,
    list_issues as linear_list_issues,
)
from pilot_roster import Pilot

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static
from textual.worker import Worker, WorkerState


# ── Splash Screen ────────────────────────────────────────────────────

class SplashScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "Skip")]

    _CHECKS = [
        ("INITIALIZING RADAR", 0.3),
        ("COMMS CHECK", 0.5),
        ("FLIGHT OPS", 0.7),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._dismissed = False
        self._step = 0

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

        yield Static(content, id="splash-art")
        yield Static("", id="splash-status")

    def on_mount(self) -> None:
        self._advance_check()

    def _advance_check(self) -> None:
        if self._dismissed:
            return
        widget = self.query_one("#splash-status", Static)

        if self._step < len(self._CHECKS):
            label, delay = self._CHECKS[self._step]
            # Build all lines up to current step
            t = Text()
            for i in range(self._step):
                prev_label, _ = self._CHECKS[i]
                t.append(f"  {prev_label} {'.' * (22 - len(prev_label))} ", style="bold white")
                t.append("████████████", style="bold green")
                t.append(" READY\n", style="bold bright_green")
            # Current line — animating
            t.append(f"  {label} {'.' * (22 - len(label))} ", style="bold white")
            t.append("████░░░░░░░░", style="bold yellow")
            t.append(" . . .\n", style="bold yellow")
            widget.update(t)
            self._step += 1
            self.set_timer(delay, self._advance_check)
        else:
            # All done
            t = Text()
            for label, _ in self._CHECKS:
                t.append(f"  {label} {'.' * (22 - len(label))} ", style="bold white")
                t.append("████████████", style="bold green")
                t.append(" READY\n", style="bold bright_green")
            t.append("\n")
            t.append("  ALL STATIONS MANNED AND READY\n", style="bold bright_white")
            t.append("  Press any key or wait . . .", style="dim")
            widget.update(t)
            # Auto-dismiss after a short beat
            self.set_timer(0.8, self._auto_dismiss)

    def _auto_dismiss(self) -> None:
        if not self._dismissed:
            self._dismissed = True
            try:
                self.dismiss()
            except Exception:
                pass

    def on_key(self, event) -> None:
        event.stop()
        if self._dismissed:
            return
        self._dismissed = True
        try:
            self.dismiss()
        except Exception:
            pass

    CSS = """
    SplashScreen { align: center middle; }
    #splash-art { width: auto; height: auto; }
    #splash-status { width: auto; height: auto; min-height: 6; }
    SplashScreen > Vertical, SplashScreen > Static {
        width: auto; height: auto;
        padding: 0 4;
        background: $surface-darken-3;
    }
    SplashScreen { background: $surface-darken-3 80%; }
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
