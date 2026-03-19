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

Each Tower session starts fresh — no agent continuity across restarts.
Stale evidence from previous sessions is ignored. Only fresh JSONL
(< 30s old) can promote a pilot out of IDLE.

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
        lines = []
        with open(jsonl, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
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

# Fresh threshold — JSONL modified within this many seconds = agent is alive
JSONL_FRESH_SECS = 30


def derive_status(worktree_path: str, current_status: str = "") -> str:
    """Derive pilot status from filesystem evidence.

    This is the single source of truth for status. Called once per pilot
    per refresh cycle. Nothing else should write pilot.status.

    Each Tower session starts fresh. Stale JSONL from previous sessions
    is treated the same as no JSONL — the pilot stays IDLE until fresh
    evidence proves otherwise.

    Priority:
      1. command.json override (XO escape hatch, consumed on read)
      2. session-ended file (terminal — agent exited)
      3. Fresh JSONL (< 30s) → AIRBORNE or PREFLIGHT
      4. Everything else → IDLE (or stay AIRBORNE if already live)
    """
    if not worktree_path:
        return current_status or "IDLE"

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

    # No JSONL at all — never started or file deleted
    if age is None:
        if current_status == "AIRBORNE":
            return "MAYDAY"  # was live but JSONL vanished — crash
        return "IDLE"

    if age < JSONL_FRESH_SECS:
        # JSONL is fresh — agent is alive right now
        events = _tail_jsonl_events(wt, n=15)

        if _has_write_tools(events):
            return "AIRBORNE"

        if _has_any_tools(events):
            if current_status == "AIRBORNE":
                return "AIRBORNE"  # stay airborne, don't flicker
            return "PREFLIGHT"

        # Fresh JSONL but no tool calls — thinking/responding
        if current_status == "AIRBORNE":
            return "AIRBORNE"  # stay airborne through thinking pauses
        return "PREFLIGHT"

    # 4. No fresh evidence — not actively running right now
    if current_status == "AIRBORNE":
        # Was live this session but JSONL went quiet → winding down
        return "ON_APPROACH"
    if current_status == "ON_APPROACH":
        # Already winding down, still no fresh evidence → stay on approach
        # (will eventually get session-ended → RECOVERED, or user dismisses)
        return "ON_APPROACH"

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
