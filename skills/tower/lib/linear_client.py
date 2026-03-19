"""Direct Linear API client — no Claude subprocess, no MCP.

Replaces the claude -p middleman in linear_bridge.py with direct
GraphQL calls to Linear's API. Zero token cost, zero ban risk,
sub-second response times.

Setup: Create a personal API key at https://linear.app/settings/api
and store it in ~/.config/linear/api_key (one line, just the key).
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import re

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
TICKET_ID_RE = re.compile(r"^[A-Z]{2,}-\d+$")


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


def _get_api_key() -> str:
    """Read Linear API key from env or file."""
    key = os.environ.get("LINEAR_API_KEY", "")
    if not key:
        key_file = Path.home() / ".config" / "linear" / "api_key"
        try:
            key = key_file.read_text().strip()
        except OSError:
            pass
    return key


def _graphql(query: str, variables: dict | None = None, timeout: int = 15) -> Optional[dict]:
    """Execute a GraphQL query against Linear's API."""
    api_key = _get_api_key()
    if not api_key:
        return None

    payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        LINEAR_GRAPHQL_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "errors" in data:
                return None
            return data.get("data")
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def is_ticket_id(text: str) -> bool:
    """Check if text looks like a Linear ticket ID (e.g., ENG-113)."""
    return bool(TICKET_ID_RE.match(text.strip()))


def available() -> bool:
    """Check if we have a Linear API key configured."""
    return bool(_get_api_key())


# ── Single ticket fetch ──────────────────────────────────────────────

_ISSUE_QUERY = """
query IssueByIdentifier($id: String!) {
  issueByIdentifier(id: $id) {
    identifier
    title
    description
    priority
    branchName
    state { name }
    team { name }
    assignee { name }
    labels { nodes { name } }
  }
}
"""


def fetch_ticket(ticket_id: str, timeout: int = 15) -> Optional[LinearTicket]:
    """Fetch a single Linear ticket by ID. Direct API call, no Claude."""
    data = _graphql(_ISSUE_QUERY, {"id": ticket_id.strip()}, timeout=timeout)
    if not data or not data.get("issueByIdentifier"):
        return None
    return _parse_issue(data["issueByIdentifier"])


def fetch_tickets_batch(ticket_ids: list[str], timeout: int = 20) -> dict[str, Optional[LinearTicket]]:
    """Fetch multiple tickets. One API call per ticket (Linear doesn't batch by identifier)."""
    results: dict[str, Optional[LinearTicket]] = {}
    for tid in ticket_ids:
        results[tid.strip()] = fetch_ticket(tid, timeout=timeout)
    return results


# ── List issues ──────────────────────────────────────────────────────

_LIST_QUERY = """
query ListIssues($filter: IssueFilter, $first: Int) {
  issues(filter: $filter, first: $first) {
    nodes {
      identifier
      title
      description
      priority
      branchName
      state { name }
      team { name }
      assignee { name }
      labels { nodes { name } }
    }
  }
}
"""


def list_issues(
    team: str | None = None,
    state: str | None = None,
    assignee: str = "me",
    limit: int = 25,
    timeout: int = 20,
) -> list[LinearTicket]:
    """List Linear issues with filters. Direct API call."""
    issue_filter: dict = {}
    if team:
        issue_filter["team"] = {"name": {"eq": team}}
    if state:
        issue_filter["state"] = {"name": {"eq": state}}
    if assignee == "me":
        issue_filter["assignee"] = {"isMe": {"eq": True}}
    elif assignee:
        issue_filter["assignee"] = {"name": {"eq": assignee}}

    data = _graphql(_LIST_QUERY, {"filter": issue_filter, "first": limit}, timeout=timeout)
    if not data or not data.get("issues"):
        return []

    return [_parse_issue(node) for node in data["issues"]["nodes"]]


# ── Create issue ─────────────────────────────────────────────────────

_CREATE_MUTATION = """
mutation CreateIssue($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue {
      identifier
      title
      description
      priority
      branchName
      state { name }
      team { name }
      assignee { name }
      labels { nodes { name } }
    }
  }
}
"""


def create_issue(
    title: str,
    description: str = "",
    team_id: str = "",
    priority: int = 3,
    timeout: int = 15,
) -> Optional[LinearTicket]:
    """Create a new Linear issue. Returns the created ticket or None."""
    input_data: dict = {"title": title}
    if description:
        input_data["description"] = description
    if team_id:
        input_data["teamId"] = team_id
    if priority != 3:
        input_data["priority"] = priority

    data = _graphql(_CREATE_MUTATION, {"input": input_data}, timeout=timeout)
    if not data or not data.get("issueCreate", {}).get("success"):
        return None
    return _parse_issue(data["issueCreate"]["issue"])


# ── Parsing ──────────────────────────────────────────────────────────

def _parse_issue(node: dict) -> LinearTicket:
    """Convert a GraphQL issue node to a LinearTicket."""
    return LinearTicket(
        id=node.get("identifier", ""),
        title=node.get("title", ""),
        description=node.get("description", "") or "",
        priority=node.get("priority", 3) or 3,
        state=(node.get("state") or {}).get("name", ""),
        team=(node.get("team") or {}).get("name", ""),
        assignee=(node.get("assignee") or {}).get("name", ""),
        labels=[l["name"] for l in (node.get("labels", {}).get("nodes") or [])],
        git_branch=node.get("branchName", "") or "",
    )


# ── Priority helpers (same interface as linear_bridge.py) ────────────

PRIORITY_LABELS = {0: "None", 1: "Urgent", 2: "High", 3: "Normal", 4: "Low"}
PRIORITY_STYLES = {0: "grey50", 1: "bold red", 2: "bold yellow", 3: "white", 4: "grey70"}


def priority_label(p: int) -> str:
    return PRIORITY_LABELS.get(p, f"P{p}")


def priority_style(p: int) -> str:
    return PRIORITY_STYLES.get(p, "white")
