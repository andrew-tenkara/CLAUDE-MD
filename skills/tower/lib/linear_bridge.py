"""Bridge to Linear — direct API first, Claude MCP fallback.

Tries the direct GraphQL client (linear_client.py) first — zero tokens,
sub-second, no ban risk. Falls back to spawning claude -p subprocesses
if no Linear API key is configured.

Setup for direct mode: Create a personal API key at
https://linear.app/settings/api and save it to ~/.config/linear/api_key
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# Try direct client first
try:
    from linear_client import (
        available as _direct_available,
        fetch_ticket as _direct_fetch,
        fetch_tickets_batch as _direct_batch,
        list_issues as _direct_list,
        create_issue as _direct_create,
        LinearTicket,
        is_ticket_id,
        priority_label,
        priority_style,
        PRIORITY_LABELS,
        PRIORITY_STYLES,
    )
    _HAS_DIRECT = True
except ImportError:
    _HAS_DIRECT = False

# If direct client isn't importable, define types locally
if not _HAS_DIRECT:
    TICKET_ID_RE = re.compile(r"^[A-Z]{2,}-\d+$")
    PRIORITY_LABELS = {0: "None", 1: "Urgent", 2: "High", 3: "Normal", 4: "Low"}
    PRIORITY_STYLES = {0: "grey50", 1: "bold red", 2: "bold yellow", 3: "white", 4: "grey70"}

    @dataclass
    class LinearTicket:
        id: str
        title: str
        description: str = ""
        priority: int = 3
        state: str = ""
        labels: list[str] = field(default_factory=list)
        assignee: str = ""
        team: str = ""
        git_branch: str = ""

    def is_ticket_id(text: str) -> bool:
        return bool(TICKET_ID_RE.match(text.strip()))

    def priority_label(p: int) -> str:
        return PRIORITY_LABELS.get(p, f"P{p}")

    def priority_style(p: int) -> str:
        return PRIORITY_STYLES.get(p, "white")


def _use_direct() -> bool:
    """Check if we should use the direct Linear API client."""
    return _HAS_DIRECT and _direct_available()


# ── Public API (same interface regardless of backend) ────────────────

def fetch_ticket(ticket_id: str, timeout: int = 30) -> Optional[LinearTicket]:
    """Fetch a single Linear ticket by ID."""
    if _use_direct():
        return _direct_fetch(ticket_id, timeout=min(timeout, 15))

    # Fallback: claude -p subprocess
    return _claude_fetch_ticket(ticket_id, timeout=timeout)


def fetch_tickets_batch(ticket_ids: list[str], timeout: int = 45) -> dict[str, Optional[LinearTicket]]:
    """Fetch multiple tickets efficiently."""
    if _use_direct():
        return _direct_batch(ticket_ids, timeout=min(timeout, 20))

    # Fallback: one claude -p per ticket
    results = {}
    for tid in ticket_ids:
        results[tid] = _claude_fetch_ticket(tid, timeout=timeout)
    return results


def list_issues(
    team: str | None = None,
    state: str | None = None,
    assignee: str = "me",
    project: str | None = None,
    limit: int = 25,
    timeout: int = 45,
) -> list[LinearTicket]:
    """List Linear issues with filters."""
    if _use_direct():
        return _direct_list(team=team, state=state, assignee=assignee, limit=limit, timeout=min(timeout, 20))

    # Fallback: claude -p subprocess
    return _claude_list_issues(team=team, state=state, assignee=assignee, project=project, limit=limit, timeout=timeout)


# ── Claude -p fallback (legacy, higher risk) ────────────────────────

_ticket_cache: dict[str, tuple[float, Optional[LinearTicket]]] = {}
_CACHE_TTL = 300


def _claude_fetch_ticket(ticket_id: str, timeout: int = 30) -> Optional[LinearTicket]:
    """Fetch via claude -p subprocess. Legacy fallback."""
    ticket_id = ticket_id.strip().upper()

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
            ["claude", "-p", prompt, "--output-format", "text", "--allowedTools", "mcp__linear__get_issue"],
            capture_output=True, text=True, timeout=timeout,
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


def _claude_list_issues(
    team=None, state=None, assignee="me", project=None, limit=25, timeout=45,
) -> list[LinearTicket]:
    """List issues via claude -p subprocess. Legacy fallback."""
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

    prompt = (
        f"Use the mcp__linear__list_issues tool with these filters: {', '.join(filters)}. "
        "Then output ONLY a JSON array (no markdown, no code fences, no explanation) "
        "of objects with exactly these fields:\n"
        '[{"id": "ENG-113", "title": "...", "priority": 3, "state": "...", '
        '"labels": ["..."], "assignee": "...", "team": "...", "gitBranchName": "..."}]\n'
        "Output the raw JSON array and nothing else."
    )
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text", "--allowedTools", "mcp__linear__list_issues"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return []
        return _parse_issues_json(result.stdout.strip())
    except Exception:
        return []


# ── JSON parsing (only used by claude -p fallback) ───────────────────

def _parse_ticket_json(text: str, fallback_id: str) -> Optional[LinearTicket]:
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
    data = _extract_json_array(text)
    if data is None:
        return []
    return [
        LinearTicket(
            id=item.get("id", "???"),
            title=item.get("title", ""),
            description=item.get("description", ""),
            priority=item.get("priority", 3),
            state=item.get("state", ""),
            labels=item.get("labels", []),
            assignee=item.get("assignee", ""),
            team=item.get("team", ""),
            git_branch=item.get("gitBranchName", ""),
        )
        for item in data
        if isinstance(item, dict)
    ]


def _extract_json_object(text: str) -> Optional[dict]:
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
