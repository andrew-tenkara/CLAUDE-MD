#!/usr/bin/env python3
"""session-start-restore.py — SessionStart hook: restore pre-compaction snapshot.

Called by Claude Code when a new session starts (including post-compaction resume).
stdin: JSON {session_id}
argv[1]: PROJECT_DIR (injected by write-settings.sh at deploy time)

Writes .sortie/context-anchor.md if a recent snapshot exists for this session.
Non-blocking: always exits 0.
"""

import json
import os
import subprocess
import sys


def find_sortie_dir() -> str:
    """Walk up from cwd to find nearest .sortie directory."""
    parts = os.getcwd().split(os.sep)
    for i in range(len(parts), 0, -1):
        d = os.sep.join(parts[:i]) + "/.sortie"
        if os.path.isdir(d):
            return d
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

    session_id = data.get("session_id", "") or ""
    if not session_id:
        sys.exit(0)

    result = subprocess.run(
        ["python3", storage_db, "get-latest-snapshot", project_dir, session_id],
        capture_output=True,
        text=True,
        timeout=5,
    )

    snapshot = result.stdout
    if not snapshot or snapshot.strip() == "SNAPSHOT:none":
        sys.exit(0)

    sortie_dir = find_sortie_dir()
    if not sortie_dir:
        sys.exit(0)

    anchor_path = os.path.join(sortie_dir, "context-anchor.md")
    with open(anchor_path, "w") as f:
        f.write(f"## Context Restored (post-compaction snapshot)\n\n")
        f.write(snapshot)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
