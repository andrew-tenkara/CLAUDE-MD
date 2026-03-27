#!/usr/bin/env python3
"""post-tool-cache.py — PostToolUse hook: cache large tool results in SQLite.

Called by Claude Code after Read, Bash, or Grep tool use.
stdin: JSON {session_id, tool_name, tool_input, tool_response}
argv[1]: PROJECT_DIR (injected by write-settings.sh at deploy time)

Non-blocking: always exits 0.
"""

import json
import os
import subprocess
import sys

THRESHOLD = 2048  # bytes — only cache results larger than this


def compute_tool_key(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Read":
        return tool_input.get("file_path", "")
    if tool_name == "Bash":
        return (tool_input.get("command", "") or "")[:200]
    if tool_name in ("Grep", "Glob"):
        pattern = tool_input.get("pattern", "") or ""
        path    = tool_input.get("path", "") or tool_input.get("glob", "") or ""
        return f"{pattern}:{path}"
    return json.dumps(tool_input)[:200]


def find_ticket_id() -> str:
    """Walk up from cwd to find .sortie/flight-status.json."""
    parts = os.getcwd().split(os.sep)
    for i in range(len(parts), 0, -1):
        fst = os.sep.join(parts[:i]) + "/.sortie/flight-status.json"
        if os.path.exists(fst):
            try:
                with open(fst) as f:
                    return json.load(f).get("ticket_id", "")
            except Exception:
                pass
    return ""


def main() -> None:
    project_dir = sys.argv[1] if len(sys.argv) > 1 else ""
    if not project_dir:
        sys.exit(0)

    storage_db = os.path.join(os.path.dirname(__file__), "..", "storage-db.py")

    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    session_id    = data.get("session_id", "") or ""
    tool_name     = data.get("tool_name", "")  or ""
    tool_input    = data.get("tool_input", {}) or {}
    tool_response = data.get("tool_response", {}) or {}

    if not session_id or not tool_name:
        sys.exit(0)

    # Extract result text from tool_response
    content = tool_response.get("content", "")
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content if isinstance(c, dict)]
        content = "\n".join(parts)
    elif not isinstance(content, str):
        content = str(content)

    if len(content.encode()) < THRESHOLD:
        sys.exit(0)

    tool_key = compute_tool_key(tool_name, tool_input)
    if not tool_key:
        sys.exit(0)

    ticket_id = find_ticket_id()

    proc = subprocess.Popen(
        ["python3", storage_db, "cache-tool-result",
         project_dir, session_id, ticket_id, tool_name, tool_key, "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.communicate(input=content.encode())


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
