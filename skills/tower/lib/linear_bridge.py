"""Bridge to Linear via Claude MCP tools.

Spawns short-lived Claude subprocesses to query the Linear MCP server.
The user must have the Linear MCP configured in ~/.claude/.mcp.json.

Includes a session-level cache to avoid redundant fetches for the same ticket.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional


# Pattern to detect Linear ticket IDs: ENG-123, PROJ-456, etc.
TICKET_ID_RE = re.compile(r"^[A-Z]{2,}-\d+$")

# Session-level ticket cache — avoids re-fetching the same ticket
# within a single process lifetime (e.g., batch /tq runs)
_ticket_cache: dict[str, tuple[float, Optional["LinearTicket"]]] = {}
_CACHE_TTL = 300  # 5 minutes


@dataclass
class LinearTicket:
    id: str             # "ENG-113"
    title: str
    description: str = ""
    priority: int = 3   # 0=None, 1=Urgent, 2=High, 3=Normal, 4=Low
    state: str = ""     # "In Progress", "Todo", etc.
    labels: list[str] = field(default_factory=list)
    assignee: str = ""
    team: str = ""
    git_branch: str = ""  # gitBranchName from Linear


def is_ticket_id(text: str) -> bool:
    """Check if text looks like a Linear ticket ID (e.g., ENG-113)."""
    return bool(TICKET_ID_RE.match(text.strip()))


def fetch_ticket(ticket_id: str, timeout: int = 30) -> Optional[LinearTicket]:
    """Fetch a single Linear ticket by ID using Claude + Linear MCP.

    Uses a session-level cache to avoid redundant subprocess spawns.
    Returns None if the fetch fails for any reason.
    """
    ticket_id = ticket_id.strip().upper()

    # Check cache
    if ticket_id in _ticket_cache:
        cached_at, cached_ticket = _ticket_cache[ticket_id]
        if time.time() - cached_at < _CACHE_TTL:
            return cached_ticket

    prompt = (
        f'Use the mcp__linear__get_issue tool to fetch issue "{ticket_id}". '
        "Then output ONLY a JSON object (no markdown, no code fences, no explanation) "
        "with exactly these fields:\n"
        '{"id": "ENG-113", "title": "...", "description": "...", '
        '"priority": 3, "state": "...", "labels": ["..."], '
        '"assignee": "...", "team": "...", "gitBranchName": "..."}\n'
        "Output the raw JSON and nothing else."
    )
    try:
        result = subprocess.run(
            [
                "claude", "-p", prompt,
                "--output-format", "text",
                "--allowedTools", "mcp__linear__get_issue",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            _ticket_cache[ticket_id] = (time.time(), None)
            return None

        ticket = _parse_ticket_json(result.stdout.strip(), ticket_id)
        _ticket_cache[ticket_id] = (time.time(), ticket)
        return ticket
    except Exception:
        _ticket_cache[ticket_id] = (time.time(), None)
        return None


def fetch_tickets_batch(ticket_ids: list[str], timeout: int = 45) -> dict[str, Optional[LinearTicket]]:
    """Fetch multiple Linear tickets in a single subprocess call.

    More efficient than calling fetch_ticket() N times for batch /tq.
    Returns a dict of ticket_id -> LinearTicket (or None if fetch failed).
    """
    # Filter out cached tickets
    results: dict[str, Optional[LinearTicket]] = {}
    uncached = []
    for tid in ticket_ids:
        tid = tid.strip().upper()
        if tid in _ticket_cache:
            cached_at, cached_ticket = _ticket_cache[tid]
            if time.time() - cached_at < _CACHE_TTL:
                results[tid] = cached_ticket
                continue
        uncached.append(tid)

    if not uncached:
        return results

    # Fetch all uncached tickets in one subprocess
    ids_str = ", ".join(uncached)
    prompt = (
        f'For each of these Linear issue IDs: {ids_str}\n'
        "Use the mcp__linear__get_issue tool to fetch each one. "
        "Then output ONLY a JSON array (no markdown, no code fences, no explanation) "
        "of objects with exactly these fields:\n"
        '[{"id": "ENG-113", "title": "...", "description": "...", '
        '"priority": 3, "state": "...", "labels": ["..."], '
        '"assignee": "...", "team": "...", "gitBranchName": "..."}]\n'
        "Output the raw JSON array and nothing else."
    )
    try:
        result = subprocess.run(
            [
                "claude", "-p", prompt,
                "--output-format", "text",
                "--allowedTools", "mcp__linear__get_issue",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            tickets = _parse_issues_json(result.stdout.strip())
            for t in tickets:
                _ticket_cache[t.id] = (time.time(), t)
                results[t.id] = t

        # Mark any still-missing tickets as None
        for tid in uncached:
            if tid not in results:
                _ticket_cache[tid] = (time.time(), None)
                results[tid] = None
    except Exception:
        for tid in uncached:
            results[tid] = None

    return results


def list_issues(
    team: str | None = None,
    state: str | None = None,
    assignee: str = "me",
    project: str | None = None,
    limit: int = 25,
    timeout: int = 45,
) -> list[LinearTicket]:
    """List Linear issues using Claude + Linear MCP.

    Spawns a short-lived `claude -p` subprocess that calls mcp__linear__list_issues.
    Returns empty list if the fetch fails.
    """
    filters = []
    if assignee:
        filters.append(f'assignee: "{assignee}"')
    if team:
        filters.append(f'team: "{team}"')
    if state:
        filters.append(f'state: "{state}"')
    if project:
        filters.append(f'project: "{project}"')
    filters.append(f"limit: {limit}")
    filter_desc = ", ".join(filters)

    prompt = (
        f"Use the mcp__linear__list_issues tool with these filters: {filter_desc}. "
        "Then output ONLY a JSON array (no markdown, no code fences, no explanation) "
        "of objects with exactly these fields:\n"
        '[{"id": "ENG-113", "title": "...", "priority": 3, "state": "...", '
        '"labels": ["..."], "assignee": "...", "team": "...", "gitBranchName": "..."}]\n'
        "Output the raw JSON array and nothing else."
    )
    try:
        result = subprocess.run(
            [
                "claude", "-p", prompt,
                "--output-format", "text",
                "--allowedTools", "mcp__linear__list_issues",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return []

        return _parse_issues_json(result.stdout.strip())
    except Exception:
        return []


# ── JSON parsing helpers ─────────────────────────────────────────────


def _parse_ticket_json(text: str, fallback_id: str) -> Optional[LinearTicket]:
    """Extract a LinearTicket from Claude's JSON response."""
    data = _extract_json_object(text)
    if data is None:
        return None
    return LinearTicket(
        id=data.get("id", fallback_id),
        title=data.get("title", ""),
        description=data.get("description", ""),
        priority=data.get("priority", 3),
        state=data.get("state", ""),
        labels=data.get("labels", []),
        assignee=data.get("assignee", ""),
        team=data.get("team", ""),
        git_branch=data.get("gitBranchName", ""),
    )


def _parse_issues_json(text: str) -> list[LinearTicket]:
    """Extract a list of LinearTickets from Claude's JSON response."""
    data = _extract_json_array(text)
    if data is None:
        return []
    tickets = []
    for item in data:
        if not isinstance(item, dict):
            continue
        tickets.append(LinearTicket(
            id=item.get("id", "???"),
            title=item.get("title", ""),
            description=item.get("description", ""),
            priority=item.get("priority", 3),
            state=item.get("state", ""),
            labels=item.get("labels", []),
            assignee=item.get("assignee", ""),
            team=item.get("team", ""),
            git_branch=item.get("gitBranchName", ""),
        ))
    return tickets


def _extract_json_object(text: str) -> Optional[dict]:
    """Find and parse the first JSON object in text."""
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(text[start : i + 1])
                except (json.JSONDecodeError, ValueError):
                    start = -1
    return None


def _extract_json_array(text: str) -> Optional[list]:
    """Find and parse the first JSON array in text."""
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(text[start : i + 1])
                except (json.JSONDecodeError, ValueError):
                    start = -1
    return None


# ── Priority helpers ─────────────────────────────────────────────────

PRIORITY_LABELS = {0: "None", 1: "Urgent", 2: "High", 3: "Normal", 4: "Low"}
PRIORITY_STYLES = {0: "grey50", 1: "bold red", 2: "bold yellow", 3: "white", 4: "grey70"}


def priority_label(p: int) -> str:
    return PRIORITY_LABELS.get(p, f"P{p}")


def priority_style(p: int) -> str:
    return PRIORITY_STYLES.get(p, "white")
