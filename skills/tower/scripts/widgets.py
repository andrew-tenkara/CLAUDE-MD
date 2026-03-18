"""USS Tenkara PRI-FLY — custom Textual widgets.

PriFlyHeader, ChatInput, ChatPane, MissionQueuePanel, RadioChatter, DeckStatus.
All access self.app for roster/agent data (standard Textual pattern).
"""
from __future__ import annotations

import sys
import time as time_mod
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add lib to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from constants import STATUS_ICONS, STATUS_COLORS
from rendering import (
    fuel_gauge, _format_tokens, _format_elapsed, _tool_icon,
    _render_assistant_content, _render_tool_detail,
)

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, RichLog, Static, TextArea


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

class _QueueTable(DataTable):
    """DataTable subclass with deploy/remove via on_key override."""

    def _on_key(self, event) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self.action_deploy_mission()
            return
        if event.key in ("delete", "backspace", "x"):
            event.prevent_default()
            event.stop()
            self.action_remove_mission()
            return

    def action_deploy_mission(self) -> None:
        """Deploy selected mission."""
        try:
            app = self.app
            queued = app._mission_queue.queued()
            if self.row_count == 0 or self.cursor_row >= len(queued):
                return
            mission = queued[self.cursor_row]
            app._handle_command(f"/deploy {mission.id}")
            app._mission_queue.remove(mission.id)
            # Find parent panel and refresh
            for ancestor in self.ancestors:
                if isinstance(ancestor, MissionQueuePanel):
                    ancestor.refresh_queue()
                    break
        except Exception as e:
            self.app._add_radio("PRI-FLY", f"Deploy failed: {e}", "error")

    def action_remove_mission(self) -> None:
        """Remove selected mission from queue."""
        try:
            app = self.app
            queued = app._mission_queue.queued()
            if self.row_count == 0 or self.cursor_row >= len(queued):
                return
            mission = queued[self.cursor_row]
            app._mission_queue.remove(mission.id)
            app._add_radio("PRI-FLY", f"Removed {mission.id} from queue", "system")
            for ancestor in self.ancestors:
                if isinstance(ancestor, MissionQueuePanel):
                    ancestor.refresh_queue()
                    break
        except Exception as e:
            self.app._add_radio("PRI-FLY", f"Remove failed: {e}", "error")


class MissionQueuePanel(Vertical):
    """Scrollable, interactive mission queue with deploy and remove actions."""

    def compose(self) -> ComposeResult:
        yield Static(" MISSION QUEUE", id="queue-header")
        yield _QueueTable(id="queue-table")

    def on_mount(self) -> None:
        table = self.query_one("#queue-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("PRI", "ID", "TITLE", "MODEL")

    def refresh_queue(self) -> None:
        """Rebuild the queue table from current mission data."""
        try:
            app = self.app
            queue = app._mission_queue
            queued = queue.queued()

            table = self.query_one("#queue-table", DataTable)

            # Remember cursor position
            prev_row = table.cursor_row if table.row_count > 0 else 0

            table.clear()

            if not queued:
                return

            for m in queued:
                pri_style = {1: "bold red", 2: "yellow", 3: "grey70"}.get(m.priority, "white")
                pri_text = Text(f"P{m.priority}", style=pri_style)
                id_text = Text(m.id, style="bold white")
                title_text = Text(m.title[:45], style="white")
                model_text = Text(m.model, style="italic grey70")
                table.add_row(pri_text, id_text, title_text, model_text, key=m.id)

            # Restore cursor
            if table.row_count > 0:
                table.move_cursor(row=min(prev_row, table.row_count - 1))

            # Update header with count
            header = self.query_one("#queue-header", Static)
            header.update(Text(f" MISSION QUEUE ({len(queued)})", style="bold bright_white"))
        except Exception:
            pass

    def _get_selected_mission_id(self) -> str:
        """Get the mission ID of the currently selected row."""
        try:
            table = self.query_one("#queue-table", DataTable)
            if table.row_count == 0:
                return ""
            row_idx = table.cursor_row
            queued = self.app._mission_queue.queued()
            if row_idx < len(queued):
                return queued[row_idx].id
        except Exception:
            pass
        return ""

    def action_deploy_mission(self) -> None:
        """Deploy the selected mission — creates worktree, adds to board."""
        mission_id = self._get_selected_mission_id()
        if not mission_id:
            return
        try:
            app = self.app
            app._handle_command(f"/deploy {mission_id}")
            # Remove from queue after deploy
            app._mission_queue.remove(mission_id)
            self.refresh_queue()
        except Exception as e:
            self.app._add_radio("PRI-FLY", f"Deploy failed: {e}", "error")

    def action_remove_mission(self) -> None:
        """Remove the selected mission from the queue."""
        mission_id = self._get_selected_mission_id()
        if not mission_id:
            return
        try:
            self.app._mission_queue.remove(mission_id)
            self.app._add_radio("PRI-FLY", f"Removed {mission_id} from queue", "system")
            self.refresh_queue()
        except Exception as e:
            self.app._add_radio("PRI-FLY", f"Remove failed: {e}", "error")


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


# ── Board table renderer (standalone, takes app as ctx) ─────────────

def refresh_board_table(ctx) -> None:
    """Rebuild the agent board DataTable.

    Parameters
    ----------
    ctx : PriFlyCommander
        The app instance — all state is read through ``ctx``.
    """
    pilots = ctx._roster.all_pilots()

    # Skip full table rebuild when visible state hasn't changed — table.clear() +
    # add_row() on every 3s tick is the biggest source of UI jank with active agents.
    sig = "|".join(
        f"{p.callsign}:{p.status}:{p.fuel_pct}:{p.tokens_used}:{p.error_count}"
        f":{p.mood}:{p.flight_phase}:{p.status_hint}:{p.tool_calls}"
        for p in sorted(pilots, key=lambda p: p.callsign)
    )
    if sig == ctx._board_state_sig:
        return
    ctx._board_state_sig = sig

    table = ctx.query_one("#agent-table", DataTable)
    # Remember selected pilot by callsign (stable across refreshes)
    prev_callsign = ""
    if table.row_count > 0 and table.cursor_row < len(ctx._sorted_pilots):
        prev_callsign = ctx._sorted_pilots[table.cursor_row].callsign
    table.clear()

    ctx._sorted_pilots = sorted(pilots, key=lambda p: p.callsign)

    critical = []
    for pilot in ctx._sorted_pilots:
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
        bar = fuel_gauge(pilot.fuel_pct, blink=ctx._bingo_blink)
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
            tools.append(f" {pilot.error_count}\u2717", style="bold red")

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
            mission.append(f"\n\u00bb {pilot.flight_phase[:40]}", style="italic cyan")
        if pilot.status_hint:
            # Only show server URLs, not full paths
            hint = pilot.status_hint
            if "localhost:" in hint or "127.0.0.1:" in hint:
                mission.append(f"\n\u26a1 {hint}", style="bold cyan")

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
            (i for i, p in enumerate(ctx._sorted_pilots) if p.callsign == prev_callsign),
            0,
        )
        table.move_cursor(row=restored)
    elif table.row_count > 0:
        table.move_cursor(row=0)

    # Alert bar for critical fuel
    alert_bar = ctx.query_one("#alert-bar")
    if critical:
        names = ", ".join(critical)
        alert_bar.update(f"\u26a0 FUEL CRITICAL: {names} \u2014 BINGO RTB \u26a0")
        alert_bar.add_class("visible")
    else:
        alert_bar.remove_class("visible")
