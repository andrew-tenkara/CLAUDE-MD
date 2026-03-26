"""USS Tenkara PRI-FLY — shared constants.

Status maps, thresholds, sounds, and display constants used by
the dashboard, status engine, and rendering modules.

Simplified state model (v2):
  ON_DECK     — pane open, no tokens flowing
  IN_FLIGHT   — pane open, tokens flowing
  ON_APPROACH — tokens stopped, landing sequence playing
  RECOVERED   — pane closed, on deck
"""
from __future__ import annotations

# ── Status display ────────────────────────────────────────────────────

STATUS_ICONS = {
    "ON_DECK": "⏸",
    "IN_FLIGHT": "✈",
    "ON_APPROACH": "🔄",
    "RECOVERED": "✓",
}

STATUS_COLORS = {
    "ON_DECK": "yellow",
    "IN_FLIGHT": "green",
    "ON_APPROACH": "dark_orange",
    "RECOVERED": "grey50",
}

STATUS_SORT_ORDER = {
    "IN_FLIGHT": 0,
    "ON_APPROACH": 1,
    "ON_DECK": 2,
    "RECOVERED": 3,
}

# ── Sounds ────────────────────────────────────────────────────────────

# macOS sounds (built-in, no deps)
SOUNDS = {
    "recovered": "/System/Library/Sounds/Glass.aiff",
    "squadron_complete": "/System/Library/Sounds/Hero.aiff",
}

# ── Status mapping ────────────────────────────────────────────────────

# Map sortie-state internal status → commander status
_LEGACY_STATUS_MAP = {
    "WORKING": "IN_FLIGHT",
    "PRE-REVIEW": "ON_APPROACH",
    "DONE": "RECOVERED",
}

_FLIGHT_STATUS_MAP = {
    "PREFLIGHT": "ON_DECK",
    "AIRBORNE": "IN_FLIGHT",
    "HOLDING": "ON_DECK",
    "ON_APPROACH": "ON_APPROACH",
    "RECOVERED": "ON_DECK",
    # Legacy compat — map old statuses to new
    "IDLE": "ON_DECK",
    "QUEUED": "ON_DECK",
    "MAYDAY": "RECOVERED",
    "AAR": "IN_FLIGHT",
    "SAR": "IN_FLIGHT",
    "IN_FLIGHT": "IN_FLIGHT",
    "ON_DECK": "ON_DECK",
}

# ── Thresholds ────────────────────────────────────────────────────────

# Max age (seconds) for flight-status.json before it's considered stale
_FLIGHT_STATUS_MAX_AGE = 60

# ── Token budget ─────────────────────────────────────────────────────

# Default token budget per agent (total tokens across all turns)
TOKEN_BUDGET_DEFAULT = 150_000
TOKEN_BUDGET_WARN_PCT = 0.70    # 70% — send "wrap up" advisory
TOKEN_BUDGET_LAND_PCT = 0.90    # 90% — trigger landing sequence
