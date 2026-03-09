"""
read_sortie_state.py — Read sortie agent state from all worktrees

Returns agent state with status, model, progress, context usage,
JSONL metrics, and sub-agent information. Used by dashboard-tui.py.
"""

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from parse_jsonl_metrics import JsonlMetrics, parse_jsonl_metrics


def _get_worktrees_root() -> Path:
    # This file lives at <repo>/.claude/skills/sortie/lib/read_sortie_state.py
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / ".claude" / "worktrees"


WORKTREES_ROOT = _get_worktrees_root()


@dataclass
class AgentState:
    ticket_id: str
    title: str
    model: str
    status: str  # WORKING, PRE-REVIEW, DONE
    last_progress: List[str] = field(default_factory=list)
    branch: str = ""
    elapsed_time: str = "0s"
    worktree_path: str = ""
    sub_name: str = ""
    is_sub_agent: bool = False
    parent_ticket: Optional[str] = None
    context: Optional[dict] = None
    jsonl_metrics: Optional[JsonlMetrics] = None


@dataclass
class DashboardState:
    agents: List[AgentState] = field(default_factory=list)
    total: int = 0
    working: int = 0
    pre_review: int = 0
    done: int = 0
    timestamp: str = ""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _read_json_safe(path: Path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _format_elapsed(seconds: float) -> str:
    s = int(seconds)
    minutes, secs = divmod(s, 60)
    hours, mins = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {mins}m"
    if mins > 0:
        return f"{mins}m"
    return f"{secs}s"


def _extract_field(directive: str, field_name: str) -> str:
    match = re.search(rf"\*\*{field_name}\*\*:\s*(.+)", directive)
    return match.group(1).strip() if match else "Unknown"


def _get_branch(worktree_path: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(worktree_path),
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _read_context(sortie_dir: Path) -> dict:
    """Read context usage from context.json (ENG-133 context % tracking)."""
    ctx = _read_json_safe(sortie_dir / "context.json")
    if ctx is None:
        return {
            "used_percentage": None,
            "context_window_size": None,
            "total_input_tokens": None,
            "total_output_tokens": None,
            "model": None,
            "timestamp": None,
            "stale": True,
        }

    age = int(time.time()) - (ctx.get("timestamp") or 0)
    ctx["stale"] = age > 60
    return ctx


def _read_agent(sortie_dir: Path, worktree_path: Path,
                is_sub_agent: bool = False, parent_ticket: Optional[str] = None,
                sub_name: str = "") -> Optional[AgentState]:
    directive_path = sortie_dir / "directive.md"
    if not directive_path.exists():
        return None

    directive = _read_text(directive_path)
    model = _read_text(sortie_dir / "model.txt") or "unknown"

    progress_raw = _read_text(sortie_dir / "progress.md")
    progress_lines = [l for l in progress_raw.split("\n") if l.strip()]
    last_progress = progress_lines[-5:]

    has_post_review = (sortie_dir / "post-review.done").exists()
    has_pre_review = (sortie_dir / "pre-review.done").exists()

    if has_post_review:
        status = "DONE"
    elif has_pre_review:
        status = "PRE-REVIEW"
    else:
        status = "WORKING"

    branch = _get_branch(worktree_path)

    elapsed_time = "0s"
    try:
        st = os.stat(str(sortie_dir))
        # Prefer birthtime (macOS), fall back to mtime
        origin = getattr(st, "st_birthtime", None) or st.st_mtime
        elapsed = datetime.now().timestamp() - origin
        elapsed_time = _format_elapsed(elapsed)
    except OSError:
        pass

    # ENG-133: context % tracking from statusline API
    context = _read_context(sortie_dir)

    # ENG-134: JSONL metrics (token usage, tool calls, errors, timeline)
    jsonl_metrics = parse_jsonl_metrics(str(worktree_path))

    return AgentState(
        ticket_id=_extract_field(directive, "ID"),
        title=_extract_field(directive, "Title"),
        model=model,
        status=status,
        last_progress=last_progress,
        branch=branch,
        elapsed_time=elapsed_time,
        worktree_path=str(worktree_path),
        sub_name=sub_name,
        is_sub_agent=is_sub_agent,
        parent_ticket=parent_ticket,
        context=context,
        jsonl_metrics=jsonl_metrics,
    )


def read_sortie_state(target_ticket: Optional[str] = None) -> DashboardState:
    """Read state for all active sortie agents.

    Args:
        target_ticket: optional ticket ID to filter to

    Returns:
        DashboardState with agents, summary counts, and timestamp
    """
    agents: List[AgentState] = []

    if not WORKTREES_ROOT.is_dir():
        return DashboardState(timestamp=datetime.now().isoformat())

    try:
        entries = sorted(WORKTREES_ROOT.iterdir())
    except OSError:
        return DashboardState(timestamp=datetime.now().isoformat())

    if target_ticket:
        entries = [e for e in entries if e.name == target_ticket]

    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue

        sortie_dir = entry / ".sortie"
        agent = _read_agent(sortie_dir, entry)
        if agent:
            agents.append(agent)

        # Check sub-agents
        try:
            sub_entries = sorted(entry.iterdir())
        except OSError:
            continue
        for sub in sub_entries:
            if not sub.name.startswith("sub-") or sub.is_symlink() or not sub.is_dir():
                continue
            sub_sortie = sub / ".sortie"
            sub_label = sub.name[4:]  # strip "sub-" prefix
            sub_agent = _read_agent(
                sub_sortie, sub,
                is_sub_agent=True,
                parent_ticket=agent.ticket_id if agent else None,
                sub_name=sub_label,
            )
            if sub_agent:
                agents.append(sub_agent)

    return DashboardState(
        agents=agents,
        total=len(agents),
        working=sum(1 for a in agents if a.status == "WORKING"),
        pre_review=sum(1 for a in agents if a.status == "PRE-REVIEW"),
        done=sum(1 for a in agents if a.status == "DONE"),
        timestamp=datetime.now().isoformat(),
    )


def get_all_progress_entries(agents: List[AgentState], max_entries: int = 20) -> List[dict]:
    """Collect recent progress entries across all agents, sorted by time.

    Uses (ticket_id, line_index) as secondary sort key since HH:MM timestamps
    don't carry date info — entries within the same agent are in file order.
    """
    entries = []
    for agent in agents:
        for idx, line in enumerate(agent.last_progress):
            time_match = re.match(r"\[(\d{2}:\d{2})\]\s*(.*)", line)
            if time_match:
                timestamp, message = time_match.group(1), time_match.group(2)
            else:
                timestamp, message = "--:--", line

            entry_type = "normal"
            lower = message.lower()
            if lower.startswith("issue:") or lower.startswith("error:"):
                entry_type = "error"
            elif lower.startswith("complete:") or lower.startswith("done:"):
                entry_type = "success"

            entries.append({
                "timestamp": timestamp,
                "ticket_id": agent.ticket_id,
                "message": message,
                "type": entry_type,
                "_sort_key": (timestamp, agent.ticket_id, idx),
            })

    entries.sort(key=lambda e: e["_sort_key"], reverse=True)
    return entries[:max_entries]
