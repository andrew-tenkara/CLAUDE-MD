"""USS Tenkara — Status Observer.

Derives pilot status from evidence, not declarations. Simplified v2:

  ON_DECK     — pane open, no tokens flowing
  IN_FLIGHT   — pane open, tokens flowing
  ON_APPROACH — tokens stopped, landing sequence
  RECOVERED   — pane closed

The JSONL is the heartbeat. The session-ended file is the death certificate.
"""
from __future__ import annotations

import json
import logging
import time as time_mod
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# ── Evidence readers ─────────────────────────────────────────────────

def _jsonl_age(worktree_path: str) -> Optional[float]:
    """Seconds since the JSONL session file was last modified.

    Returns None if no JSONL file found.
    """
    try:
        from parse_jsonl_metrics import find_latest_session_file
        jsonl = find_latest_session_file(worktree_path)
        if jsonl and jsonl.exists():
            return time_mod.time() - jsonl.stat().st_mtime
    except Exception:
        pass
    return None


def _has_session_ended(worktree_path: str) -> bool:
    """Check if the session-ended marker file exists."""
    try:
        return (Path(worktree_path) / ".sortie" / "session-ended").exists()
    except OSError:
        return False


def _read_command_override(worktree_path: str) -> Optional[str]:
    """Read and consume a one-shot command.json override. Returns status or None."""
    cmd_path = Path(worktree_path) / ".sortie" / "command.json"
    try:
        if cmd_path.exists():
            data = json.loads(cmd_path.read_text(encoding="utf-8"))
            cmd_path.unlink()  # consume — one-shot
            status = data.get("set_status", "").upper()
            if status in ("ON_DECK", "IN_FLIGHT", "ON_APPROACH", "RECOVERED"):
                return status
    except (OSError, json.JSONDecodeError):
        pass
    return None


# ── Core status derivation ───────────────────────────────────────────

# Hysteresis thresholds — different thresholds for entering vs leaving IN_FLIGHT.
# This prevents oscillation at the freshness boundary.
JSONL_FRESH_ENTER = 10   # seconds — JSONL must be this fresh to START flying
JSONL_FRESH_STAY  = 25   # seconds — JSONL can be this old and we STAY flying


def derive_status(worktree_path: str, current_status: str = "") -> str:
    """Derive pilot status from filesystem evidence.

    Uses hysteresis: harder to leave IN_FLIGHT than to enter it.
    This prevents animation flickering from JSONL boundary oscillation.

    Priority:
      1. command.json override (XO escape hatch, consumed on read)
      2. session-ended file → RECOVERED (pane closed / agent exited)
      3. Fresh JSONL → IN_FLIGHT (with hysteresis)
      4. Stale JSONL → ON_APPROACH if was IN_FLIGHT, else ON_DECK
      5. No JSONL → ON_DECK
    """
    if not worktree_path:
        return current_status or "ON_DECK"

    wt = worktree_path

    # 1. Command override — XO can force any status (escape hatch)
    override = _read_command_override(wt)
    if override:
        return override

    # 2. Session-ended — pane closed / agent exited
    if _has_session_ended(wt):
        return "RECOVERED"

    # 3. JSONL evidence — the heartbeat
    age = _jsonl_age(wt)

    # No JSONL at all — never started or file deleted
    if age is None:
        return "ON_DECK"

    # Hysteresis: use a wider window if already IN_FLIGHT
    threshold = JSONL_FRESH_STAY if current_status == "IN_FLIGHT" else JSONL_FRESH_ENTER

    if age < threshold:
        return "IN_FLIGHT"

    # 4. Stale JSONL — not actively running right now
    if current_status == "IN_FLIGHT":
        # Was flying but JSONL went quiet → begin landing
        return "ON_APPROACH"
    if current_status == "ON_APPROACH":
        # Already landing, still no fresh evidence → stay on approach
        return "ON_APPROACH"

    return "ON_DECK"


# ── Haiku transition narrator ────────────────────────────────────────

