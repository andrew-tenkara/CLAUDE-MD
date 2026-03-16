"""USS Tenkara PRI-FLY — shared constants.

Status maps, thresholds, sounds, and display constants used by
the dashboard, status engine, and rendering modules.
"""
from __future__ import annotations

# ── Status display ────────────────────────────────────────────────────

STATUS_ICONS = {
    "AIRBORNE": "✈",
    "ON_APPROACH": "🔄",
    "RECOVERED": "✓",
    "MAYDAY": "⚠",
    "IDLE": "⏸",
    "QUEUED": "◆",
    "AAR": "⛽",
    "SAR": "🚁",
}

STATUS_COLORS = {
    "AIRBORNE": "green",
    "ON_APPROACH": "dark_orange",
    "RECOVERED": "grey50",
    "MAYDAY": "bold red",
    "IDLE": "yellow",
    "QUEUED": "bright_cyan",
    "AAR": "cyan",
    "SAR": "bold magenta",
}

STATUS_SORT_ORDER = {
    "MAYDAY": 0,
    "AIRBORNE": 1,
    "IDLE": 2,
    "QUEUED": 3,
    "AAR": 4,
    "SAR": 5,
    "ON_APPROACH": 6,
    "RECOVERED": 7,
}

# ── Sounds ────────────────────────────────────────────────────────────

# macOS sounds (built-in, no deps)
SOUNDS = {
    "mayday": "/System/Library/Sounds/Submarine.aiff",
    "recovered": "/System/Library/Sounds/Glass.aiff",
    "squadron_complete": "/System/Library/Sounds/Hero.aiff",
    "bingo": "/System/Library/Sounds/Ping.aiff",
}

# ── Status mapping ────────────────────────────────────────────────────

# Map sortie-state internal status → commander status
_LEGACY_STATUS_MAP = {
    "WORKING": "AIRBORNE",
    "PRE-REVIEW": "ON_APPROACH",
    "DONE": "RECOVERED",
}

_FLIGHT_STATUS_MAP = {
    "PREFLIGHT": "PREFLIGHT",
    "AIRBORNE": "AIRBORNE",
    "HOLDING": "IDLE",
    "ON_APPROACH": "ON_APPROACH",
    # Agent-reported RECOVERED is downgraded to IDLE — agents should never
    # set RECOVERED themselves. Only the bash EXIT trap (which writes
    # .sortie/session-ended) triggers real RECOVERED status.
    "RECOVERED": "IDLE",
}

# ── Thresholds ────────────────────────────────────────────────────────

# Max age (seconds) for flight-status.json before it's considered stale
_FLIGHT_STATUS_MAX_AGE = 60

# Compaction recovery — fuel jump threshold and SAR animation timing
_FUEL_JUMP_THRESHOLD = 15   # fuel gain (%) to count as compaction event
_SAR_RECOVERY_DELAY = 8     # seconds to let crash/helo animation play before relaunch
_AAR_RECOVERY_DELAY = 5     # seconds for refueling animation before returning to AIRBORNE
