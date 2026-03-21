"""USS Tenkara PRI-FLY — Status reconciliation engine.

Simplified v2: four states only.
  ON_DECK     — pane open, no tokens flowing
  IN_FLIGHT   — pane open, tokens flowing
  ON_APPROACH — tokens stopped, landing sequence
  RECOVERED   — pane closed / session ended

Token-delta tracking promotes ON_DECK → IN_FLIGHT when tokens start,
and demotes IN_FLIGHT → ON_APPROACH → RECOVERED when they stop.
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
    SOUNDS,
)

log = logging.getLogger(__name__)


# ── Notifications (disabled by default) ──────────────────────────────

_NOTIFICATIONS_ENABLED = False


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
        return "IN_FLIGHT"

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
                    return "IN_FLIGHT"
            except (ValueError, AttributeError):
                pass

    return _LEGACY_STATUS_MAP.get(internal, "RECOVERED")


# ── Valid status transitions ──────────────────────────────────────────

VALID_TRANSITIONS: dict[str, set[str]] = {
    "ON_DECK":      {"IN_FLIGHT", "RECOVERED"},
    "IN_FLIGHT":    {"ON_APPROACH", "RECOVERED"},
    "ON_APPROACH":  {"RECOVERED", "IN_FLIGHT"},  # IN_FLIGHT = wave off (tokens resume)
    "RECOVERED":    {"ON_DECK"},
}

TRANSITION_INTERMEDIATES: dict[tuple[str, str], str] = {
    ("IN_FLIGHT", "RECOVERED"): "ON_APPROACH",
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

    intermediate = TRANSITION_INTERMEDIATES.get((current, proposed))
    if intermediate:
        return intermediate

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
    """Token-delta status engine.

    Tracks token flow per pilot:
      - Tokens start flowing → ON_DECK becomes IN_FLIGHT
      - Tokens stop flowing → IN_FLIGHT becomes ON_APPROACH after stale_threshold frames
      - Stale long enough → ON_APPROACH becomes RECOVERED
    """

    def __init__(self, stale_threshold: int = 4) -> None:
        self.stale_frames: dict[str, int] = {}      # callsign → consecutive zero-delta frames
        self.prev_tokens: dict[str, int] = {}        # callsign → last known token count
        self.stale_threshold = stale_threshold

    def check_token_deltas(self, pilots, add_radio, exclude_callsigns: set | None = None) -> list[StatusTransition]:
        """Compare each pilot's token count to the previous frame.

        - delta > 0  → tokens flowing, ensure IN_FLIGHT
        - delta == 0 → stale frame; after stale_threshold → ON_APPROACH
        - Stale long enough on ON_APPROACH → RECOVERED
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

            # Unknown agents — pin to RECOVERED
            if pilot.mission_title in ("Unknown", "unknown") and pilot.ticket_id in ("Unknown", "unknown"):
                if pilot.status != "RECOVERED":
                    pilot.status = "RECOVERED"
                self.stale_frames.pop(cs, None)
                continue

            # Skip terminal status
            if pilot.status == "RECOVERED":
                self.stale_frames.pop(cs, None)
                continue

            if delta > 0:
                # Tokens moving — reset stale counter
                self.stale_frames[cs] = 0

                if pilot.status == "ON_DECK":
                    old = pilot.status
                    pilot.status = "IN_FLIGHT"
                    add_radio(cs, "LAUNCH — tokens flowing", "success")
                    transitions.append(StatusTransition(
                        callsign=cs, old_status=old, new_status="IN_FLIGHT",
                        phase="launch", message="tokens flowing",
                        notify_title="USS TENKARA — LAUNCH",
                        notify_message=f"{cs} IN FLIGHT",
                    ))
                elif pilot.status == "ON_APPROACH":
                    pilot.status = "IN_FLIGHT"
                    add_radio(cs, "WAVE OFF — tokens resumed, back IN FLIGHT", "success")

            elif curr > 0:
                # Had tokens before, but no new ones this frame
                stale = self.stale_frames.get(cs, 0) + 1
                self.stale_frames[cs] = stale

                if pilot.status == "IN_FLIGHT" and stale >= self.stale_threshold:
                    pilot.status = "ON_APPROACH"
                    add_radio(cs, "ON APPROACH — token flow stopped", "system")
                elif pilot.status == "ON_APPROACH" and stale >= self.stale_threshold + 6:
                    pilot.status = "RECOVERED"
                    add_radio(cs, "RECOVERED — on deck", "success")
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

    def apply_transitions(self, transitions: list[StatusTransition]) -> None:
        """Apply sound/notification side-effects for transitions."""
        for t in transitions:
            if t.sound_key:
                _play_sound(t.sound_key)
            if t.notify_title and t.notify_message:
                _notify(t.notify_title, t.notify_message)
