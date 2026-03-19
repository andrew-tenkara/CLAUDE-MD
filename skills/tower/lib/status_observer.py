"""USS Tenkara — Status Observer.

Derives pilot status from evidence, not declarations. Replaces the
multi-writer status system (sentinel + token-delta + flight-status.json)
with a single function that reads reality every refresh cycle.

Inspired by /etc/init.d status scripts:
  - PID file → session-ended (death certificate)
  - Process table → JSONL modification time (heartbeat)
  - Log content → JSONL events (what the agent is doing)

Nothing writes pilot.status except derive_status(). The JSONL is the
heartbeat. The session-ended file is the death certificate. This
function reads them and decides.

Optional Haiku narrator: observes transitions and explains them in
human terms via radio chatter. Informational, not authoritative.
"""
from __future__ import annotations

import json
import logging
import os
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
        # Import here to avoid circular deps
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


def _tail_jsonl_events(worktree_path: str, n: int = 10) -> list[dict]:
    """Read the last N events from the JSONL file."""
    try:
        from parse_jsonl_metrics import find_latest_session_file
        jsonl = find_latest_session_file(worktree_path)
        if not jsonl or not jsonl.exists():
            return []
        # Read last N lines efficiently
        lines = []
        with open(jsonl, "rb") as f:
            # Seek to end, read backwards
            f.seek(0, 2)
            size = f.tell()
            # Read last 50KB max
            read_size = min(size, 50_000)
            f.seek(max(0, size - read_size))
            raw = f.read().decode("utf-8", errors="replace")
            raw_lines = raw.strip().splitlines()
            for line in raw_lines[-n:]:
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return lines
    except Exception:
        return []


def _has_write_tools(events: list[dict]) -> bool:
    """Check if recent events contain write-type tool calls."""
    write_tools = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
    for evt in events:
        if evt.get("type") == "assistant":
            content = evt.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        if block.get("name") in write_tools:
                            return True
    return False


def _has_any_tools(events: list[dict]) -> bool:
    """Check if recent events contain any tool calls."""
    for evt in events:
        if evt.get("type") == "assistant":
            content = evt.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        return True
    return False


def _read_command_override(worktree_path: str) -> Optional[str]:
    """Read and consume a one-shot command.json override. Returns status or None."""
    cmd_path = Path(worktree_path) / ".sortie" / "command.json"
    try:
        if cmd_path.exists():
            data = json.loads(cmd_path.read_text(encoding="utf-8"))
            cmd_path.unlink()  # consume — one-shot
            status = data.get("set_status", "").upper()
            if status in ("AIRBORNE", "IDLE", "RECOVERED", "ON_APPROACH", "MAYDAY", "AAR", "SAR", "PREFLIGHT"):
                return status
    except (OSError, json.JSONDecodeError):
        pass
    return None


# ── Core status derivation ───────────────────────────────────────────

# Thresholds
JSONL_ACTIVE_SECS = 30      # JSONL modified within this → agent is alive
JSONL_IDLE_SECS = 120       # JSONL older than this → agent is idle/stalled
ON_APPROACH_SECS = 300      # No write tools for this long while alive → wrapping up


def derive_status(worktree_path: str, current_status: str = "") -> str:
    """Derive pilot status from filesystem evidence.

    This is the single source of truth for status. Called once per pilot
    per refresh cycle. Nothing else should write pilot.status.

    Priority:
      1. command.json override (XO escape hatch, consumed on read)
      2. session-ended file (terminal — agent exited)
      3. JSONL evidence (heartbeat + content)
      4. No evidence → maintain current status or IDLE
    """
    if not worktree_path:
        return current_status or "IDLE"

    # Resolve to absolute
    wt = worktree_path

    # 1. Command override — XO can force any status (escape hatch)
    override = _read_command_override(wt)
    if override:
        return override

    # 2. Session-ended — agent exited cleanly
    if _has_session_ended(wt):
        return "RECOVERED"

    # 3. JSONL evidence — the heartbeat
    age = _jsonl_age(wt)

    if age is None:
        # No JSONL at all — agent hasn't started writing yet
        if current_status in ("AIRBORNE", "ON_APPROACH"):
            # Was active but JSONL disappeared — something went wrong
            return "MAYDAY"
        return current_status or "IDLE"

    if age < JSONL_ACTIVE_SECS:
        # JSONL is fresh — agent is alive
        events = _tail_jsonl_events(wt, n=15)

        if _has_write_tools(events):
            return "AIRBORNE"  # actively writing code

        if _has_any_tools(events):
            # Using tools but not writing — reading, searching, planning
            if current_status == "AIRBORNE":
                # Was writing, now just reading — might be wrapping up
                return "AIRBORNE"  # stay airborne, don't flicker
            return "PREFLIGHT"

        # JSONL fresh but no tool calls in last 15 events — thinking/responding
        if current_status == "AIRBORNE":
            return "AIRBORNE"  # stay airborne through thinking pauses
        return "PREFLIGHT"

    if age < JSONL_IDLE_SECS:
        # JSONL is warm but not hot — agent is pausing
        if current_status == "AIRBORNE":
            return "ON_APPROACH"  # starting to wind down
        # Don't inherit ON_APPROACH from a previous session — if we never
        # saw this agent AIRBORNE in the current Tower session, it's IDLE
        if current_status == "ON_APPROACH":
            return "IDLE"
        return current_status or "IDLE"

    # JSONL is stale — agent stopped producing events long ago
    if current_status == "AIRBORNE":
        return "ON_APPROACH"  # was active this session, winding down
    # Stale JSONL + not currently AIRBORNE = previous session leftover
    return "IDLE"


# ── Haiku transition narrator ────────────────────────────────────────

def narrate_transition(
    callsign: str,
    old_status: str,
    new_status: str,
    worktree_path: str,
) -> Optional[str]:
    """Ask Haiku to explain a status transition in human terms.

    Returns a short narrative string, or None if Haiku unavailable.
    Non-blocking — caller should run this in a thread or fire-and-forget.
    """
    if old_status == new_status:
        return None

    api_key = _get_api_key()
    if not api_key:
        return None

    try:
        import anthropic
    except ImportError:
        return None

    # Get recent events for context
    events = _tail_jsonl_events(worktree_path, n=5)
    event_summary = []
    for evt in events:
        if evt.get("type") == "assistant":
            content = evt.get("message", {}).get("content", [])
            for block in (content if isinstance(content, list) else []):
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        event_summary.append(f"text: {block['text'][:80]}")
                    elif block.get("type") == "tool_use":
                        event_summary.append(f"tool: {block.get('name')}")

    if not event_summary:
        return None

    context = "\n".join(event_summary[-5:])

    prompt = (
        f"Agent {callsign} just transitioned from {old_status} to {new_status}.\n"
        f"Recent activity:\n{context}\n\n"
        "In one sentence, explain what the agent was doing that caused this transition. "
        "Be specific and tactical. No filler."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        for block in response.content:
            if hasattr(block, "text"):
                return block.text.strip()
    except Exception as e:
        log.debug("Haiku narration failed: %s", e)

    return None


def _get_api_key() -> str:
    """Get Anthropic API key from env or file."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        key_file = Path.home() / ".config" / "anthropic" / "api_key"
        try:
            key = key_file.read_text().strip()
        except OSError:
            pass
    return key
