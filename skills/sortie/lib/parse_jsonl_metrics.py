"""
Parse Claude Code JSONL session logs to extract agent activity metrics.

Session files live at:
    ~/.claude/projects/<encoded-path>/<session-uuid>.jsonl
    ~/.claude/projects/<encoded-path>/<session-uuid>/subagents/*.jsonl

Path encoding: replace all '/' and '.' with '-'.
Each line is a JSON record. We care about:
    - type:"assistant" — contains tool_use blocks (tool calls) and usage data
    - type:"user"      — contains tool_result blocks (errors when is_error=True)
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _safe_int(v: object) -> int:
    """Safely coerce a potentially-malformed token count value to an integer.

    Mirrors JS behaviour: parse as float first, then truncate — so "1.9" -> 1.
    """
    try:
        if v is None:
            return 0
        n = float(v)  # type: ignore[arg-type]
        return int(n) if math.isfinite(n) else 0
    except (TypeError, ValueError):
        return 0


def _safe_mtime(p: Path) -> float:
    """Return mtime for sorting; returns 0.0 on OSError (race with deletion)."""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def encode_project_path(abs_path: str) -> str:
    """Encode an absolute path the same way Claude Code does for its project dirs.

    Both '/' and '.' are replaced with '-'.
    """
    return abs_path.replace("/", "-").replace(".", "-")


def _find_all_jsonl_files(directory: Path) -> List[Path]:
    """Recursively find all .jsonl files under directory (for subagent logs)."""
    files: List[Path] = []
    try:
        for entry in directory.iterdir():
            if entry.is_file() and entry.suffix == ".jsonl":
                files.append(entry)
            elif entry.is_dir():
                files.extend(_find_all_jsonl_files(entry))
    except OSError:
        pass
    return files


def find_latest_session_file(worktree_path: str) -> Optional[Path]:
    """Find the most recently modified top-level JSONL session file for a given worktree path."""
    encoded = encode_project_path(worktree_path)
    project_dir = CLAUDE_PROJECTS_DIR / encoded

    if not project_dir.is_dir():
        return None

    jsonl_files = list(project_dir.glob("*.jsonl"))
    if not jsonl_files:
        return None

    # Pick the most recently modified (safe against deletion races)
    jsonl_files.sort(key=_safe_mtime, reverse=True)
    return jsonl_files[0]


@dataclass
class JsonlMetrics:
    session_id: Optional[str] = None
    session_file: str = ""
    tool_call_counts: Dict[str, int] = field(default_factory=dict)
    total_tool_calls: int = 0
    error_count: int = 0
    error_rate: float = 0.0
    agent_spawns: int = 0
    last_activity_at: Optional[str] = None
    recent_timeline: List[Dict[str, str]] = field(default_factory=list)
    # Token usage (aggregated across all sessions for the worktree)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    total_tokens: int = 0


def _parse_single_file(path: Path, tool_call_counts: Dict[str, int],
                       timeline: List[Dict[str, str]],
                       accumulator: Dict) -> None:
    """Parse one JSONL file, mutating the shared accumulators."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if accumulator["session_id"] is None and obj.get("sessionId"):
                    accumulator["session_id"] = obj["sessionId"]

                entry_type = obj.get("type")

                if entry_type == "assistant":
                    # Token usage is on the message object; coerce to int to guard
                    # against malformed values (strings, None, floats) in JSONL.
                    usage = obj.get("message", {}).get("usage") or {}
                    accumulator["input_tokens"]       += _safe_int(usage.get("input_tokens"))
                    accumulator["output_tokens"]      += _safe_int(usage.get("output_tokens"))
                    accumulator["cache_write_tokens"] += _safe_int(usage.get("cache_creation_input_tokens"))
                    accumulator["cache_read_tokens"]  += _safe_int(usage.get("cache_read_input_tokens"))

                    content = obj.get("message", {}).get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if (
                                isinstance(block, dict)
                                and block.get("type") == "tool_use"
                            ):
                                name = block.get("name") or "unknown"
                                tool_call_counts[name] = (
                                    tool_call_counts.get(name, 0) + 1
                                )
                                if name == "Agent":
                                    accumulator["agent_spawns"] += 1
                                timeline.append(
                                    {
                                        "tool": name,
                                        "timestamp": obj.get("timestamp"),
                                    }
                                )

                elif entry_type == "user":
                    content = obj.get("message", {}).get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if (
                                isinstance(block, dict)
                                and block.get("type") == "tool_result"
                                and block.get("is_error") is True
                            ):
                                accumulator["error_count"] += 1
    except OSError:
        pass


def parse_jsonl_metrics(worktree_path: str) -> Optional[JsonlMetrics]:
    """Load JSONL metrics for a worktree path.

    Scans ALL session files (including subagent logs) for token usage.
    Returns None if no session files exist.
    """
    encoded = encode_project_path(worktree_path)
    project_dir = CLAUDE_PROJECTS_DIR / encoded

    if not project_dir.is_dir():
        return None

    all_files = _find_all_jsonl_files(project_dir)
    if not all_files:
        return None

    # Latest top-level session for metadata (session_id, session_file)
    top_level = [f for f in all_files if f.parent == project_dir]
    latest_session_file: Optional[Path] = None
    if top_level:
        top_level.sort(key=_safe_mtime, reverse=True)
        latest_session_file = top_level[0]

    tool_call_counts: Dict[str, int] = {}
    timeline: List[Dict[str, str]] = []
    acc = {
        "session_id": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
        "agent_spawns": 0,
        "error_count": 0,
        "last_activity_at": None,
    }

    # Parse latest session file first so session_id comes from the correct session
    if latest_session_file:
        _parse_single_file(latest_session_file, tool_call_counts, timeline, acc)
    for path in all_files:
        if path == latest_session_file:
            continue
        _parse_single_file(path, tool_call_counts, timeline, acc)

    # Sort timeline by timestamp so cross-file ordering is correct, then derive
    # last_activity_at from the true latest event rather than traversal order.
    # Parse to epoch so ISO 8601 timestamps with offsets sort correctly.
    def _ts_epoch(ts: Optional[str]) -> float:
        if not ts:
            return 0.0
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            return 0.0

    timeline.sort(key=lambda e: _ts_epoch(e.get("timestamp")))
    if timeline:
        acc["last_activity_at"] = timeline[-1].get("timestamp")

    total_tool_calls = sum(tool_call_counts.values())
    error_rate = (
        round(acc["error_count"] / total_tool_calls, 2) if total_tool_calls > 0 else 0.0
    )
    total_tokens = (
        acc["input_tokens"] + acc["output_tokens"] +
        acc["cache_write_tokens"] + acc["cache_read_tokens"]
    )

    return JsonlMetrics(
        session_id=acc["session_id"],
        session_file=str(latest_session_file) if latest_session_file else "",
        tool_call_counts=tool_call_counts,
        total_tool_calls=total_tool_calls,
        error_count=acc["error_count"],
        error_rate=error_rate,
        agent_spawns=acc["agent_spawns"],
        last_activity_at=acc["last_activity_at"],
        recent_timeline=timeline[-10:],
        input_tokens=acc["input_tokens"],
        output_tokens=acc["output_tokens"],
        cache_write_tokens=acc["cache_write_tokens"],
        cache_read_tokens=acc["cache_read_tokens"],
        total_tokens=total_tokens,
    )
