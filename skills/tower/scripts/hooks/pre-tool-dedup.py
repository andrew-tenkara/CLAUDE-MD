#!/usr/bin/env python3
"""pre-tool-dedup.py — PreToolUse hook: block redundant large tool calls (CCR dedup).

Called by Claude Code before Read or Bash tool use.
stdin: JSON {session_id, tool_name, tool_input}
argv[1]: PROJECT_DIR (injected by write-settings.sh at deploy time)

On cache HIT: exits 2 with JSON block message visible to Claude.
On cache MISS or error: exits 0 (allow tool to proceed).
"""

import json
import os
import subprocess
import sys


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


def main() -> None:
    project_dir = sys.argv[1] if len(sys.argv) > 1 else ""
    if not project_dir:
        sys.exit(0)

    storage_db = os.path.join(os.path.dirname(__file__), "..", "storage-db.py")

    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    session_id = data.get("session_id", "") or ""
    tool_name  = data.get("tool_name", "")  or ""
    tool_input = data.get("tool_input", {}) or {}

    if not session_id or not tool_name:
        sys.exit(0)

    tool_key = compute_tool_key(tool_name, tool_input)
    if not tool_key:
        sys.exit(0)

    result = subprocess.run(
        ["python3", storage_db, "check-tool-cache",
         project_dir, session_id, tool_name, tool_key],
        capture_output=True,
        text=True,
        timeout=5,
    )

    if result.stdout.strip() != "HIT":
        sys.exit(0)

    # Cache hit — block the tool call and surface retrieval instructions to Claude
    reason = (
        f"[CCR HIT] {tool_name} '{tool_key[:80]}' was already read this session.\n"
        f"Retrieve full result:\n"
        f"  python3 '{storage_db}' get-cached-tool '{project_dir}' "
        f"{session_id} {tool_name} '{tool_key}'\n"
        f"Only re-run the tool if you need fresh/updated data."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
