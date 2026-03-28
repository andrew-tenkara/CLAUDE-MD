"""USS Tenkara PRI-FLY — Legacy agent sync.

Reads worktree-based agent state from disk and reconciles it into the
pilot roster.  Extracted from commander-dashboard.py to keep the main
app module focused on UI orchestration.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time as time_mod
from pathlib import Path

# Add lib to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from pilot_roster import derive_mood
from read_sortie_state import read_sortie_state
from status_engine import (
    _play_sound, _notify, _ctx_remaining, _map_flight_status,
    _flight_status_is_stale, _clear_flight_status, _derive_legacy_status,
    validate_transition,
)
from constants import _FLIGHT_STATUS_MAP

log = logging.getLogger(__name__)


class LegacySync:
    """Handles discovery and reconciliation of legacy worktree agents.

    Parameters
    ----------
    ctx : PriFlyCommander
        The app instance.  All roster / queue / reconciler state is accessed
        through ``ctx`` so the dashboard class stays thin.
    """

    def __init__(self, ctx) -> None:
        self._ctx = ctx

    # ── Public API (called by thin delegates on PriFlyCommander) ─────

    def sync(self) -> None:
        """Spawn a background thread to read sortie state and sync into the pilot roster.

        read_sortie_state() hits the filesystem and spawns git subprocesses — running it
        on the main thread at 5s intervals causes noticeable UI jank. We offload the I/O
        to a daemon thread and apply results on the main thread via call_from_thread.
        """
        ctx = self._ctx
        if ctx._sync_in_progress:
            return
        ctx._sync_in_progress = True

        def _bg():
            try:
                state = read_sortie_state(project_dir=ctx._project_dir)
                ctx.call_from_thread(self.apply, state)
            except Exception as e:
                log.warning(f"Failed to read sortie state: {e}")
            finally:
                ctx._sync_in_progress = False

        threading.Thread(target=_bg, daemon=True).start()

    def dismiss_splash(self) -> None:
        """Dismiss the splash screen if still showing."""
        ctx = self._ctx
        splash = getattr(ctx, "_splash", None)
        if splash and not splash._dismissed:
            splash._dismissed = True
            try:
                ctx.pop_screen()
            except Exception:
                pass
        ctx._splash = None

    def apply(self, state) -> None:
        """Apply sortie state to the pilot roster (main thread)."""
        ctx = self._ctx
        seen_tickets: set[str] = set()
        for agent in state.agents:
            tid = agent.ticket_id
            seen_tickets.add(tid)

            # Skip dismissed agents — user hit Z, don't resurrect
            if tid in ctx._dismissed_tickets:
                continue

            ctx._legacy_agents[tid] = agent

            # Skip if this agent was spawned by us (stream-json managed)
            existing_pilot = ctx._roster.get_by_callsign(
                next(
                    (p.callsign for p in ctx._roster.get_by_ticket(tid)),
                    "",
                )
            )
            if existing_pilot and ctx._agent_mgr.get(existing_pilot.callsign):
                continue  # Managed by stream-json — don't overwrite
            if existing_pilot and ctx._sdk_mgr and ctx._sdk_mgr.get(existing_pilot.callsign):
                continue  # Managed by SDK — event stream is authoritative

            # Derive commander status from legacy state
            cic_status = _derive_legacy_status(agent)

            # Get or create pilot in roster
            pilots_for_ticket = ctx._roster.get_by_ticket(tid)
            if pilots_for_ticket:
                pilot = pilots_for_ticket[0]
            else:
                # New legacy agent — register in roster
                # If title == ticket_id, try Linear lookup
                title = agent.title
                # Enrich title from mission queue if available (no API call needed)
                if title == tid or title in ("Unknown", "unknown", ""):
                    queued_mission = ctx._mission_queue._missions.get(tid)
                    if queued_mission and queued_mission.title:
                        title = queued_mission.title[:60]
                pilot = ctx._roster.assign(
                    ticket_id=tid,
                    model=agent.model if agent.model not in ("unknown", "Unknown", "") else "sonnet",
                    mission_title=title,
                    directive=f"(legacy worktree agent)\nBranch: {agent.branch}",
                    parent_ticket_id=agent.parent_ticket if agent.is_sub_agent else None,
                )
                # Only announce if worktree has fresh evidence (not stale from prior sessions)
                if not agent.session_ended and agent.jsonl_metrics and agent.jsonl_metrics.total_tokens > 0:
                    ctx._add_radio(
                        pilot.callsign,
                        f"DETECTED — {tid}: {title} ({agent.model})",
                        "system",
                    )

            # Sync worktree path from legacy state
            if agent.worktree_path and not pilot.worktree_path:
                pilot.worktree_path = agent.worktree_path

            # Enrich pilot title from queue if still showing ticket ID
            if pilot.mission_title == tid or pilot.mission_title in ("Unknown", "unknown", ""):
                queued = ctx._mission_queue._missions.get(tid)
                if queued and queued.title and queued.title != tid:
                    pilot.mission_title = queued.title[:60]

            # Register with inline sentinel for JSONL classification
            if ctx._use_inline_sentinel and agent.worktree_path:
                ctx._inline_sentinel.add_worktree(tid, agent.worktree_path)

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
            delta_is_tracking = cs in ctx._reconciler.prev_tokens and ctx._reconciler.prev_tokens[cs] > 0

            if pilot.status == "ON_DECK" and not has_tokens:
                pass  # Stay on deck — no tokens yet
            elif pilot.status == "ON_APPROACH" and delta_is_tracking:
                pass  # Delta tracker said fly home — don't override
            elif cic_status == "IN_FLIGHT" and not has_tokens:
                pilot.status = "ON_DECK"  # No evidence of work — keep grounded
            else:
                pilot.status = cic_status
            ctx_remaining = agent.context or {}
            pilot.fuel_pct = _ctx_remaining(ctx_remaining)

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
                    if ss_age < 90 and ss_status in ("AIRBORNE", "HOLDING", "ON_APPROACH", "RECOVERED", "PREFLIGHT", "IN_FLIGHT", "ON_DECK"):
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
                        ctx._reconciler.stale_frames.pop(pilot.callsign, None)
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
                        if new_status in ("IN_FLIGHT", "ON_DECK", "RECOVERED", "ON_APPROACH"):
                            pilot.status = new_status
                            ctx._reconciler.stale_frames.pop(pilot.callsign, None)
                            reason = cmd_data.get("reason", "command override")
                            ctx._add_radio(pilot.callsign, f"{new_status} — {reason} (set by {cmd_data.get('source', 'command')})", "system")
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
                ctx._reconciler.stale_frames.pop(pilot.callsign, None)
                ctx._add_radio(pilot.callsign, "RECOVERED — session ended", "success")
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
                    ctx._reconciler.stale_frames.pop(pilot.callsign, None)
                    if mapped == "RECOVERED":
                        ctx._add_radio(pilot.callsign, f"RECOVERED — {agent.flight_phase or 'mission complete'}", "success")
                        _play_sound("recovered")
                        _clear_flight_status(pilot.worktree_path)
                    elif old != mapped:
                        phase_msg = f" — {agent.flight_phase}" if agent.flight_phase else ""
                        ctx._add_radio(pilot.callsign, f"{mapped}{phase_msg}", "system")
            else:
                # Stale or missing — clear so token-delta inference takes over
                pilot.flight_status = ""
                pilot.flight_phase = ""
                if agent.flight_status:
                    _clear_flight_status(agent.worktree_path)

            pilot.mood = derive_mood(pilot)

        # Mark legacy agents that disappeared as RECOVERED
        for tid, agent_state in list(ctx._legacy_agents.items()):
            if tid not in seen_tickets:
                if tid in ctx._dismissed_tickets:
                    # Already dismissed by user — just clean up tracking
                    ctx._legacy_agents.pop(tid, None)
                    continue
                pilots = ctx._roster.get_by_ticket(tid)
                for p in pilots:
                    if not ctx._agent_mgr.get(p.callsign):  # Not stream-json managed
                        p.status = "RECOVERED"
                del ctx._legacy_agents[tid]

        # Push roster changes to the flight strip immediately — don't wait
        # for the next _refresh_ui cycle (up to 3s away).
        try:
            from flight_ops import FlightOpsStrip
            strip = ctx.query_one("#flight-strip", FlightOpsStrip)
            strip.update_pilots(ctx._roster.all_pilots())
        except Exception:
            pass
