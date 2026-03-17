"""USS Tenkara PRI-FLY — Status reconciliation engine.

Multi-source status priority engine: sentinel > command > session-ended >
flight-status > heuristic. This module is the clean seam for Phase 2 (SDK migration).

The reconciler currently reads sentinel-status.json from disk. After SDK
migration, it'll receive events from the SDK stream instead — same interface,
different input source.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time as time_mod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from constants import (
    _LEGACY_STATUS_MAP, _FLIGHT_STATUS_MAP, _FLIGHT_STATUS_MAX_AGE,
    _FUEL_JUMP_THRESHOLD, _SAR_RECOVERY_DELAY, _AAR_RECOVERY_DELAY,
    SOUNDS,
)

log = logging.getLogger(__name__)


# ── Notifications (disabled by default) ──────────────────────────────

_NOTIFICATIONS_ENABLED = False  # Disabled — too spammy during active sessions


def _play_sound(sound_key: str) -> None:
    """Sound effects disabled for now."""
    return


def _notify(title: str, message: str) -> None:
    """macOS notification via terminal-notifier or osascript fallback."""
    if not _NOTIFICATIONS_ENABLED:
        return
    icon = Path(__file__).resolve().parent.parent / "assets" / "uss-tenkara.png"
    try:
        cmd = ["terminal-notifier", "-title", title, "-message", message]
        if icon.exists():
            cmd.extend(["-appIcon", str(icon)])
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        script = f'display notification "{message}" with title "{title}"'
        try:
            subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass


# ── Status mapping helpers ───────────────────────────────────────────

def _ctx_remaining(ctx: dict) -> int:
    """Convert used_percentage to fuel remaining (0-100)."""
    used = ctx.get("used_percentage")
    if used is None:
        return 50  # unknown → assume half
    return max(0, 100 - int(used))


def _map_flight_status(reported: str) -> str:
    """Map agent-reported flight status to commander status."""
    return _FLIGHT_STATUS_MAP.get(reported.upper(), "")


def _flight_status_is_stale(agent_state) -> bool:
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


def _derive_legacy_status(agent) -> str:
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


# ── Status transition events ─────────────────────────────────────────

# ── Valid status transitions ──────────────────────────────────────────

# Maps each status to the set of statuses it can transition to.
# Prevents illogical jumps (e.g., AIRBORNE → RECOVERED without ON_APPROACH).
VALID_TRANSITIONS: dict[str, set[str]] = {
    "IDLE":         {"PREFLIGHT", "AIRBORNE", "RECOVERED", "MAYDAY"},
    "PREFLIGHT":    {"AIRBORNE", "IDLE", "MAYDAY"},
    "AIRBORNE":     {"ON_APPROACH", "AAR", "SAR", "MAYDAY"},
    "ON_APPROACH":  {"RECOVERED", "AIRBORNE", "MAYDAY"},  # AIRBORNE = wave off
    "RECOVERED":    {"IDLE", "MAYDAY"},  # IDLE = rearm/resume
    "MAYDAY":       {"RECOVERED", "IDLE", "SAR"},
    "AAR":          {"AIRBORNE", "MAYDAY"},
    "SAR":          {"AIRBORNE", "MAYDAY"},
}

# When a transition is invalid, this maps to an intermediate status to pass through.
# e.g., AIRBORNE → RECOVERED should go through ON_APPROACH first.
TRANSITION_INTERMEDIATES: dict[tuple[str, str], str] = {
    ("AIRBORNE", "RECOVERED"): "ON_APPROACH",
    ("AAR", "RECOVERED"): "AIRBORNE",       # AAR → AIRBORNE → ON_APPROACH → RECOVERED
    ("SAR", "RECOVERED"): "AIRBORNE",       # SAR → AIRBORNE → ON_APPROACH → RECOVERED
    ("IDLE", "ON_APPROACH"): "AIRBORNE",
    ("IDLE", "AAR"): "AIRBORNE",
    ("IDLE", "SAR"): "AIRBORNE",
    ("PREFLIGHT", "ON_APPROACH"): "AIRBORNE",
    ("PREFLIGHT", "RECOVERED"): "AIRBORNE",
}


def validate_transition(current: str, proposed: str) -> str:
    """Validate a status transition and return the actual next status.

    If the proposed transition is valid, returns proposed.
    If invalid but has an intermediate, returns the intermediate.
    If invalid with no intermediate, returns proposed anyway (permissive).
    """
    current = current.upper()
    proposed = proposed.upper()

    if current == proposed:
        return proposed

    valid = VALID_TRANSITIONS.get(current, set())
    if proposed in valid:
        return proposed

    # Check for intermediate
    intermediate = TRANSITION_INTERMEDIATES.get((current, proposed))
    if intermediate:
        return intermediate

    # Permissive fallback — allow it but log
    log.debug("Unusual transition: %s → %s (no intermediate defined)", current, proposed)
    return proposed


@dataclass
class StatusTransition:
    """Describes a status change event for a pilot."""
    callsign: str
    old_status: str
    new_status: str
    phase: str
    message: str
    sound_key: str | None = None
    notify_title: str | None = None
    notify_message: str | None = None


# ── StatusReconciler ─────────────────────────────────────────────────

class StatusReconciler:
    """Multi-source status priority engine.

    Priority cascade: sentinel > command > session-ended > flight-status > heuristic

    This class encapsulates the token-delta tracking, fuel-jump detection, and
    multi-source status reconciliation that was previously spread across
    _apply_legacy_state, _check_token_deltas, and _check_compaction_recovery.
    """

    def __init__(self, stale_threshold: int = 4) -> None:
        self.stale_frames: dict[str, int] = {}      # callsign → consecutive zero-delta frames
        self.prev_tokens: dict[str, int] = {}        # callsign → last known token count
        self.prev_fuel: dict[str, int] = {}          # callsign → last known fuel_pct
        self.sar_started: dict[str, float] = {}      # callsign → timestamp when SAR began
        self.bingo_notified: set[str] = set()
        self.stale_threshold = stale_threshold

    def check_token_deltas(self, pilots, add_radio, exclude_callsigns: set | None = None) -> list[StatusTransition]:
        """Compare each pilot's token count to the previous frame.

        - delta > 0  → tokens flowing, ensure AIRBORNE
        - delta == 0 → stale frame; after stale_threshold consecutive
                        stale frames on an AIRBORNE pilot → ON_APPROACH
        - Newly IDLE pilots with first token activity → promote to AIRBORNE

        exclude_callsigns: SDK-managed agents — their event stream handles status.
        Returns transition events. Caller applies sounds/notifications.
        """
        transitions = []
        _exclude = exclude_callsigns or set()

        for pilot in pilots:
            if pilot.callsign in _exclude:
                continue
            cs = pilot.callsign
            curr = pilot.tokens_used
            prev = self.prev_tokens.get(cs, 0)
            delta = curr - prev
            self.prev_tokens[cs] = curr

            # Agent-reported flight status is authoritative — skip token-delta inference
            if pilot.flight_status:
                self.stale_frames.pop(cs, None)
                continue

            # Unknown agents (no real directive) — pin to RECOVERED, never promote
            if pilot.mission_title in ("Unknown", "unknown") and pilot.ticket_id in ("Unknown", "unknown"):
                if pilot.status != "RECOVERED":
                    pilot.status = "RECOVERED"
                self.stale_frames.pop(cs, None)
                continue

            # Skip terminal/special statuses — don't interfere with AAR/SAR/RECOVERED/MAYDAY
            if pilot.status in ("AAR", "SAR", "RECOVERED", "MAYDAY"):
                self.stale_frames.pop(cs, None)
                continue

            if delta > 0:
                # Tokens moving — reset stale counter
                self.stale_frames[cs] = 0

                if pilot.status == "IDLE":
                    old = pilot.status
                    pilot.status = "AIRBORNE"
                    add_radio(cs, "LAUNCH — tokens flowing, going AIRBORNE", "success")
                    transitions.append(StatusTransition(
                        callsign=cs, old_status=old, new_status="AIRBORNE",
                        phase="launch", message="tokens flowing",
                        notify_title="USS TENKARA — LAUNCH",
                        notify_message=f"{cs} AIRBORNE",
                    ))
                elif pilot.status == "ON_APPROACH":
                    pilot.status = "AIRBORNE"
                    add_radio(cs, "WAVE OFF RTB — tokens resumed, back AIRBORNE", "success")

            elif curr > 0:
                # Had tokens before, but no new ones this frame
                stale = self.stale_frames.get(cs, 0) + 1
                self.stale_frames[cs] = stale

                if pilot.status == "AIRBORNE" and stale >= self.stale_threshold:
                    pilot.status = "ON_APPROACH"
                    add_radio(cs, "ON APPROACH — token flow stopped, RTB", "system")
                elif pilot.status == "ON_APPROACH" and stale >= self.stale_threshold + 6:
                    pilot.status = "RECOVERED"
                    add_radio(cs, "RECOVERED — on deck, mission complete", "success")
                    transitions.append(StatusTransition(
                        callsign=cs, old_status="ON_APPROACH", new_status="RECOVERED",
                        phase="recovered", message="on deck",
                        sound_key="recovered",
                        notify_title="USS TENKARA — RECOVERED",
                        notify_message=f"{cs} on deck",
                    ))
                    _clear_flight_status(pilot.worktree_path)

        # Clean up entries for removed pilots
        active_cs = {p.callsign for p in pilots}
        for cs in list(self.prev_tokens):
            if cs not in active_cs:
                del self.prev_tokens[cs]
                self.stale_frames.pop(cs, None)

        return transitions

    def check_compaction_recovery(self, pilots, add_radio, exclude_callsigns: set | None = None) -> list[StatusTransition]:
        """Detect context compaction events via fuel jumps.

        When Claude auto-compacts, fuel_pct jumps up (e.g. 5% → 60%).
        This triggers the recovery flow:
          - SAR (was 0% / crashed) → flameout → helo → replane → relaunch
          - AAR (voluntary compact) → refuel → disconnect → resume AIRBORNE

        exclude_callsigns: SDK-managed agents — their event stream handles status.
        """
        transitions = []
        now = time_mod.time()

        _exclude = exclude_callsigns or set()

        for pilot in pilots:
            cs = pilot.callsign
            if cs in _exclude:
                continue
            curr_fuel = pilot.fuel_pct
            prev_fuel = self.prev_fuel.get(cs, curr_fuel)
            self.prev_fuel[cs] = curr_fuel
            fuel_gain = curr_fuel - prev_fuel

            # ── SAR recovery: was crashed, fuel came back ──
            if pilot.status == "SAR":
                if cs not in self.sar_started:
                    self.sar_started[cs] = now
                    add_radio(cs, "FLAMEOUT — ejecting! Pedro helo launching...", "error")
                    continue

                elapsed = now - self.sar_started[cs]

                if fuel_gain >= _FUEL_JUMP_THRESHOLD and elapsed >= _SAR_RECOVERY_DELAY:
                    del self.sar_started[cs]
                    pilot.status = "AIRBORNE"
                    self.bingo_notified.discard(cs)
                    self.stale_frames.pop(cs, None)
                    add_radio(cs, f"SAR COMPLETE — Pedro has the pilot. Replaned, back AIRBORNE at {curr_fuel}%", "success")
                    transitions.append(StatusTransition(
                        callsign=cs, old_status="SAR", new_status="AIRBORNE",
                        phase="sar_complete", message=f"replaned at {curr_fuel}%",
                        notify_title="USS TENKARA — SAR",
                        notify_message=f"{cs} recovered, replaned, AIRBORNE",
                    ))
                elif fuel_gain >= _FUEL_JUMP_THRESHOLD:
                    add_radio(cs, "Pedro on station — winching pilot aboard...", "system")
                elif elapsed > _SAR_RECOVERY_DELAY and curr_fuel > 0:
                    self.sar_started.pop(cs, None)
                    pilot.status = "AIRBORNE"
                    self.bingo_notified.discard(cs)
                    self.stale_frames.pop(cs, None)
                    add_radio(cs, f"SAR COMPLETE — replaned, back AIRBORNE at {curr_fuel}%", "success")
                    transitions.append(StatusTransition(
                        callsign=cs, old_status="SAR", new_status="AIRBORNE",
                        phase="sar_complete", message=f"replaned at {curr_fuel}%",
                        notify_title="USS TENKARA — SAR",
                        notify_message=f"{cs} recovered, AIRBORNE",
                    ))
                continue

            # ── AAR recovery: was refueling, fuel came back ──
            if pilot.status == "AAR":
                if fuel_gain >= _FUEL_JUMP_THRESHOLD:
                    pilot.status = "AIRBORNE"
                    self.bingo_notified.discard(cs)
                    self.stale_frames.pop(cs, None)
                    add_radio(cs, f"AAR COMPLETE — disconnect, back AIRBORNE at {curr_fuel}%", "success")
                    transitions.append(StatusTransition(
                        callsign=cs, old_status="AAR", new_status="AIRBORNE",
                        phase="aar_complete", message=f"refueled at {curr_fuel}%",
                        notify_title="USS TENKARA — AAR",
                        notify_message=f"{cs} refueled, AIRBORNE",
                    ))
                continue

        # Clean up stale entries for removed pilots
        active_cs = {p.callsign for p in pilots}
        for cs in list(self.sar_started):
            if cs not in active_cs:
                del self.sar_started[cs]
        for cs in list(self.prev_fuel):
            if cs not in active_cs:
                del self.prev_fuel[cs]

        return transitions

    def apply_transitions(self, transitions: list[StatusTransition]) -> None:
        """Apply sound/notification side-effects for transitions."""
        for t in transitions:
            if t.sound_key:
                _play_sound(t.sound_key)
            if t.notify_title and t.notify_message:
                _notify(t.notify_title, t.notify_message)
