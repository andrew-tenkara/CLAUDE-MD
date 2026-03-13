"""
Parse Claude Code JSONL session logs to extract agent activity metrics.

v2: Incremental tail-read — tracks byte offset per file, only parses new lines.
Falls back to full re-parse if a file is truncated or replaced.

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


def encode_project_path(abs_path: str) -> str:
    """Encode an absolute path the same way Claude Code does for its project dirs."""
    return abs_path.replace("/", "-").replace(".", "-")


def _safe_int(v: object) -> int:
    """Safely coerce a potentially-malformed token count value to an integer."""
    try:
        if v is None:
            return 0
        n = float(v)
        return int(n) if math.isfinite(n) else 0
    except (TypeError, ValueError):
        return 0


def _safe_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


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
    """Find the most recently modified top-level JSONL session file."""
    encoded = encode_project_path(worktree_path)
    project_dir = CLAUDE_PROJECTS_DIR / encoded
    if not project_dir.is_dir():
        return None
    jsonl_files = list(project_dir.glob("*.jsonl"))
    if not jsonl_files:
        return None
    jsonl_files.sort(key=_safe_mtime, reverse=True)
    return jsonl_files[0]


# ── Incremental per-file state ───────────────────────────────────────

@dataclass
class _FileAccumulator:
    """Running totals from a single JSONL file, with byte offset for tail-read."""
    offset: int = 0
    size: int = 0
    mtime: float = 0.0
    session_id: Optional[str] = None
    tool_call_counts: Dict[str, int] = field(default_factory=dict)
    timeline: List[Dict[str, str]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    agent_spawns: int = 0
    agent_completions: int = 0
    _pending_agent_ids: set = field(default_factory=set)
    error_count: int = 0
    last_activity_at: Optional[str] = None
    recent_messages: List[Dict[str, str]] = field(default_factory=list)


# worktree_path -> {file_path_str -> _FileAccumulator}
_incremental_cache: Dict[str, Dict[str, _FileAccumulator]] = {}


def _parse_lines_into(acc: _FileAccumulator, lines: List[str]) -> None:
    """Parse JSONL lines into a file accumulator (same logic as before, just targeted)."""
    for raw_line in lines:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        if acc.session_id is None and obj.get("sessionId"):
            acc.session_id = obj["sessionId"]

        entry_type = obj.get("type")

        if entry_type == "assistant":
            usage = obj.get("message", {}).get("usage") or {}
            acc.input_tokens += _safe_int(usage.get("input_tokens"))
            acc.output_tokens += _safe_int(usage.get("output_tokens"))
            acc.cache_write_tokens += _safe_int(usage.get("cache_creation_input_tokens"))
            acc.cache_read_tokens += _safe_int(usage.get("cache_read_input_tokens"))

            content = obj.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name") or "unknown"
                        acc.tool_call_counts[name] = acc.tool_call_counts.get(name, 0) + 1
                        if name == "Agent":
                            acc.agent_spawns += 1
                            tool_id = block.get("id")
                            if tool_id:
                                acc._pending_agent_ids.add(tool_id)
                        acc.timeline.append({
                            "tool": name,
                            "timestamp": obj.get("timestamp", ""),
                        })
                        acc.last_activity_at = obj.get("timestamp")
                    elif isinstance(block, dict) and block.get("type") == "text":
                        text = (block.get("text") or "").strip()
                        if text:
                            acc.recent_messages.append({
                                "timestamp": obj.get("timestamp", ""),
                                "text": text,
                            })
                            # Keep only last 10 per file to bound memory
                            if len(acc.recent_messages) > 10:
                                acc.recent_messages = acc.recent_messages[-10:]

        elif entry_type == "user":
            content = obj.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        if block.get("is_error") is True:
                            acc.error_count += 1
                        # Track Agent tool completions
                        result_id = block.get("tool_use_id")
                        if result_id and result_id in acc._pending_agent_ids:
                            acc._pending_agent_ids.discard(result_id)
                            acc.agent_completions += 1


def _incremental_read_file(path: Path, acc: _FileAccumulator) -> bool:
    """Read only new bytes from a JSONL file. Returns True if new data was read."""
    try:
        st = path.stat()
    except OSError:
        return False

    current_size = st.st_size
    current_mtime = st.st_mtime

    # File unchanged — skip entirely
    if current_size == acc.size and current_mtime == acc.mtime:
        return False

    # File was truncated or replaced — reset and re-parse from 0
    if current_size < acc.offset:
        acc.offset = 0
        acc.size = 0
        acc.tool_call_counts.clear()
        acc.timeline.clear()
        acc.input_tokens = 0
        acc.output_tokens = 0
        acc.cache_write_tokens = 0
        acc.cache_read_tokens = 0
        acc.agent_spawns = 0
        acc.agent_completions = 0
        acc._pending_agent_ids.clear()
        acc.error_count = 0
        acc.last_activity_at = None
        acc.session_id = None
        acc.recent_messages.clear()

    # No new bytes to read
    if current_size <= acc.offset:
        acc.size = current_size
        acc.mtime = current_mtime
        return False

    # Seek to last known offset and read only new content
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            fh.seek(acc.offset)
            new_content = fh.read()
            new_offset = fh.tell()
    except OSError:
        return False

    new_lines = new_content.split("\n")
    _parse_lines_into(acc, new_lines)

    acc.offset = new_offset
    acc.size = current_size
    acc.mtime = current_mtime
    return True


# ── Public API ───────────────────────────────────────────────────────

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
    recent_messages: List[Dict[str, str]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    total_tokens: int = 0


def _ts_epoch(ts: Optional[str]) -> float:
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def parse_jsonl_metrics(worktree_path: str, model: str = "sonnet") -> Optional[JsonlMetrics]:
    """Load JSONL metrics for a worktree path using incremental tail-read.

    On first call: full parse (same as before).
    On subsequent calls: seeks to last byte offset per file, parses only new lines.
    If a file shrinks (truncated/replaced): resets and re-parses that file from 0.
    """
    encoded = encode_project_path(worktree_path)
    project_dir = CLAUDE_PROJECTS_DIR / encoded

    if not project_dir.is_dir():
        return None

    all_files = _find_all_jsonl_files(project_dir)
    if not all_files:
        return None

    # Get or create per-worktree file state map
    file_states = _incremental_cache.setdefault(worktree_path, {})

    # Prune state for files that no longer exist
    current_paths = {str(f) for f in all_files}
    for gone in set(file_states.keys()) - current_paths:
        del file_states[gone]

    # Incremental read each file
    for fpath in all_files:
        key = str(fpath)
        if key not in file_states:
            file_states[key] = _FileAccumulator()
        _incremental_read_file(fpath, file_states[key])

    # Merge all file accumulators into a single result
    merged_tools: Dict[str, int] = {}
    merged_timeline: List[Dict[str, str]] = []
    merged_messages: List[Dict[str, str]] = []
    session_id: Optional[str] = None
    input_tokens = 0
    output_tokens = 0
    cache_write = 0
    cache_read = 0
    agent_spawns = 0
    error_count = 0
    last_activity: Optional[str] = None

    # Find latest top-level session file for session_id
    top_level = [f for f in all_files if f.parent == project_dir]
    latest_session_file: Optional[Path] = None
    if top_level:
        top_level.sort(key=_safe_mtime, reverse=True)
        latest_session_file = top_level[0]

    for fpath_str, acc in file_states.items():
        # Prefer session_id from latest session file
        if session_id is None:
            if latest_session_file and fpath_str == str(latest_session_file):
                session_id = acc.session_id
            elif acc.session_id:
                session_id = acc.session_id

        for tool, count in acc.tool_call_counts.items():
            merged_tools[tool] = merged_tools.get(tool, 0) + count
        merged_timeline.extend(acc.timeline)
        merged_messages.extend(acc.recent_messages)
        input_tokens += acc.input_tokens
        output_tokens += acc.output_tokens
        cache_write += acc.cache_write_tokens
        cache_read += acc.cache_read_tokens
        agent_spawns += acc.agent_spawns - acc.agent_completions  # concurrent = spawned - completed
        error_count += acc.error_count
        if acc.last_activity_at:
            if last_activity is None or _ts_epoch(acc.last_activity_at) > _ts_epoch(last_activity):
                last_activity = acc.last_activity_at

    # Sort timeline and take last 10
    merged_timeline.sort(key=lambda e: _ts_epoch(e.get("timestamp")))
    if merged_timeline:
        last_activity = merged_timeline[-1].get("timestamp")

    # Sort messages by timestamp, keep last 10
    merged_messages.sort(key=lambda e: _ts_epoch(e.get("timestamp")))
    merged_messages = merged_messages[-10:]

    total_tool_calls = sum(merged_tools.values())
    error_rate = round(error_count / total_tool_calls, 2) if total_tool_calls > 0 else 0.0
    total_tokens = input_tokens + output_tokens + cache_write + cache_read

    return JsonlMetrics(
        session_id=session_id,
        session_file=str(latest_session_file) if latest_session_file else "",
        tool_call_counts=merged_tools,
        total_tool_calls=total_tool_calls,
        error_count=error_count,
        error_rate=error_rate,
        agent_spawns=max(0, agent_spawns),
        last_activity_at=last_activity,
        recent_timeline=merged_timeline[-10:],
        recent_messages=merged_messages,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_write_tokens=cache_write,
        cache_read_tokens=cache_read,
        total_tokens=total_tokens,
    )
