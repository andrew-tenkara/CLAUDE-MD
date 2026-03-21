"""Agent event handlers — SDK and legacy stream event processing.

Extracted from commander-dashboard.py. All methods operate on
the app instance (ctx) passed at construction time.
"""
from __future__ import annotations

import logging
import time as time_mod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_manager import StreamEvent
    from sdk_bridge import AgentEvent

log = logging.getLogger(__name__)


class AgentEventHandler:
    """Routes agent events (SDK + legacy) to roster, chat panes, radio, and flight strip."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx

    # ── SDK event processing ─────────────────────────────────────────

    def handle_sdk_event(self, callsign: str, event: "AgentEvent") -> None:
        """Process SDK agent event on the main thread (try/except wrapper)."""
        try:
            self._handle_sdk_event_inner(callsign, event)
        except Exception as e:
            log.warning("SDK event handler error for %s: %s", callsign, e)

    def _handle_sdk_event_inner(self, callsign: str, event: "AgentEvent") -> None:
        ctx = self.ctx
        pilot = ctx._roster.get_by_callsign(callsign)
        if not pilot:
            return

        sdk_agent = ctx._sdk_mgr.get(callsign) if ctx._sdk_mgr else None

        # ── Always-immediate: telemetry sync (numbers only, no status change) ──
        if sdk_agent:
            pilot.tokens_used = sdk_agent.total_tokens
            pilot.tool_calls = sdk_agent.tool_calls
            pilot.error_count = sdk_agent.error_count
            pilot.fuel_pct = sdk_agent.fuel_pct
            pilot.last_tool_at = sdk_agent.last_tool_at

        # ── Always-immediate: chat pane routing ──
        if callsign in ctx._chat_panes:
            pane = ctx._chat_panes[callsign]
            if event.type == "text" and event.text:
                pane.add_message("assistant", event.text)
            elif event.type == "tool_use":
                pane.add_message("tool", event.tool_name, tool_name=event.tool_name, tool_input=event.tool_input)
            pane.refresh_header()

        # ── Always-immediate: radio chatter (throttled to first line only) ──
        if event.type == "text" and event.text:
            first_line = event.text.split("\n")[0].strip()
            if first_line and len(first_line) > 5:
                ctx._add_radio(callsign, first_line[:120])
        if event.type == "error":
            ctx._add_radio(callsign, f"ERROR — {event.error}", "error")

        # ── Debounced: status transitions ──
        now = time_mod.time()
        last_change = ctx._sdk_last_status_update.get(callsign, 0)
        can_transition = (now - last_change) >= ctx._sdk_status_debounce_secs

        if sdk_agent and can_transition:
            from status_engine import _play_sound, _notify, validate_transition

            prev_status = pilot.status
            new_status = prev_status  # default: no change

            # ON_DECK → IN_FLIGHT: first token flow
            if prev_status == "ON_DECK" and sdk_agent.total_tokens > 0:
                new_status = "IN_FLIGHT"

            # IN_FLIGHT fuel check (bingo warning only)
            if prev_status == "IN_FLIGHT":
                if pilot.fuel_pct <= 30 and callsign not in ctx._reconciler.bingo_notified:
                    ctx._reconciler.bingo_notified.add(callsign)
                    ctx._add_radio(callsign, f"BINGO FUEL — {pilot.fuel_pct}% remaining", "error")

            # Apply transition through validator
            if new_status != prev_status:
                validated = validate_transition(prev_status, new_status)
                pilot.status = validated
                ctx._sdk_last_status_update[callsign] = now

                if validated == "IN_FLIGHT" and prev_status == "ON_DECK":
                    ctx._add_radio(callsign, "LAUNCH — tokens flowing", "success")
                    _notify("USS TENKARA — LAUNCH", f"{callsign} IN FLIGHT")
                elif validated == "ON_APPROACH":
                    ctx._add_radio(callsign, "ON APPROACH — token flow stopped", "system")
                elif validated != new_status:
                    ctx._add_radio(callsign, f"{prev_status} → {validated} (intermediate for {new_status})", "system")

        # Update mood
        from pilot_roster import derive_mood
        pilot.mood = derive_mood(pilot)

        # Flight strip update throttled to status changes only
        if sdk_agent and pilot.status != getattr(ctx, '_sdk_last_strip_status_' + callsign, ''):
            setattr(ctx, '_sdk_last_strip_status_' + callsign, pilot.status)
            try:
                from flight_ops import FlightOpsStrip
                strip = ctx.query_one("#flight-strip", FlightOpsStrip)
                strip.update_pilots(ctx._roster.all_pilots())
            except Exception:
                pass

    # ── Legacy (stream-json) event processing ────────────────────────

    def handle_agent_event(self, callsign: str, event: "StreamEvent") -> None:
        """Process legacy agent event on the main thread (try/except wrapper)."""
        try:
            self._handle_agent_event_inner(callsign, event)
        except Exception as e:
            log.warning("Agent event handler error for %s: %s", callsign, e)

    def _handle_agent_event_inner(self, callsign: str, event: "StreamEvent") -> None:
        ctx = self.ctx
        pilot = ctx._roster.get_by_callsign(callsign)
        if not pilot:
            return

        agent = ctx._agent_mgr.get(callsign)
        if not agent:
            return

        from status_engine import _play_sound, _notify
        from pilot_roster import derive_mood

        # Sync telemetry from agent process to pilot
        prev_tokens = pilot.tokens_used
        pilot.tokens_used = agent.total_tokens
        pilot.tool_calls = agent.tool_calls
        pilot.error_count = agent.error_count
        pilot.fuel_pct = agent.fuel_pct
        pilot.last_tool_at = agent.last_tool_at

        # Update mood
        pilot.mood = derive_mood(pilot)

        # Token consumption trigger — ON_DECK → IN_FLIGHT when tokens start flowing
        if pilot.status == "ON_DECK" and pilot.tokens_used > prev_tokens:
            pilot.status = "IN_FLIGHT"
            ctx._add_radio(pilot.callsign, "LAUNCH — tokens flowing", "success")
            _notify("USS TENKARA — LAUNCH", f"{pilot.callsign} IN FLIGHT")

        # IN_FLIGHT bingo warning
        if pilot.status == "IN_FLIGHT":
            if pilot.fuel_pct <= 30 and callsign not in ctx._reconciler.bingo_notified:
                ctx._reconciler.bingo_notified.add(callsign)
                ctx._add_radio(callsign, f"BINGO FUEL — {pilot.fuel_pct}% remaining", "error")

        # Handle permission requests
        if event.type == "control_request":
            request = event.raw.get("request", {})
            request_id = event.raw.get("request_id", "")
            tool_name = request.get("tool_name", "?")
            tool_use_id = request.get("tool_use_id", "")
            tool_input = request.get("input", {})

            ctx._pending_permissions[callsign] = {
                "request_id": request_id,
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
            }

            if callsign not in ctx._iterm_panes:
                ctx._open_iterm_comms(callsign)

            _play_sound("bingo")
            ctx._add_radio(callsign, f"⚡ PERMISSION — {tool_name}: awaiting approval", "system")
            return

        # Route to chat pane if open
        if callsign in ctx._chat_panes:
            pane = ctx._chat_panes[callsign]
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

        # Add to radio chatter
        if event.type == "assistant" and event.text:
            first_line = event.text.split("\n")[0].strip()
            if first_line and len(first_line) > 5:
                ctx._add_radio(callsign, first_line[:120])

        # Refresh flight strip
        try:
            from flight_ops import FlightOpsStrip
            strip = ctx.query_one("#flight-strip", FlightOpsStrip)
            strip.update_pilots(ctx._roster.all_pilots())
        except Exception:
            pass

    # ── Agent exit handling ───────────────────────────────────────────

    def handle_agent_exit(self, callsign: str, return_code: int) -> None:
        """Handle agent process exit on main thread (try/except wrapper)."""
        try:
            self._handle_agent_exit_inner(callsign, return_code)
        except Exception as e:
            log.warning("Agent exit handler error for %s: %s", callsign, e)

    def _handle_agent_exit_inner(self, callsign: str, return_code: int) -> None:
        ctx = self.ctx
        pilot = ctx._roster.get_by_callsign(callsign)
        if not pilot:
            return

        from status_engine import _play_sound, _notify
        from rendering import _format_elapsed

        if return_code == 0:
            pilot.status = "RECOVERED"
            _play_sound("recovered")
            ctx._add_radio(callsign, "TRAP — RECOVERED. Mission complete.", "success")
            _notify("USS TENKARA — RECOVERED", f"{callsign} mission complete")

            # Pipeline handoff — check if there's a next stage to deploy
            try:
                ctx._check_pipeline_handoff(pilot)
            except Exception as e:
                log.warning("Pipeline handoff error for %s: %s", callsign, e)

            # Check if entire squadron is recovered
            squadron_pilots = ctx._roster.get_squadron(pilot.squadron)
            if squadron_pilots and all(p.status == "RECOVERED" for p in squadron_pilots):
                total_time = sum(time_mod.time() - p.launched_at for p in squadron_pilots)
                total_tools = sum(p.tool_calls for p in squadron_pilots)
                _play_sound("squadron_complete")
                ctx._add_radio(
                    pilot.squadron.upper(),
                    f"SQUADRON COMPLETE — {pilot.ticket_id}: {pilot.mission_title} "
                    f"— {len(squadron_pilots)} pilots | {_format_elapsed(total_time)} | {total_tools} tx",
                    "success",
                )
        else:
            pilot.status = "RECOVERED"
            ctx._add_radio(callsign, f"RECOVERED — process exited with code {return_code}", "error")

            # Pipeline failure — mark mission FAILED, trigger on_failure policy
            try:
                pipeline_mission = ctx._mission_queue.get(pilot.ticket_id)
                if pipeline_mission and pipeline_mission.pipeline_id:
                    ctx._mission_queue.fail_mission(pilot.ticket_id)
                    ctx._check_pipeline_handoff(pilot)
            except Exception as e:
                log.warning("Pipeline failure handling error for %s: %s", callsign, e)

        ctx._refresh_ui()
