#!/usr/bin/env python3
"""user-prompt-snapshot.py — UserPromptSubmit hook: snapshot progress.md before each turn.

Fires before every Claude turn in a pilot worktree. Writes current progress.md
content to context_snapshots so the latest state survives compaction.

stdin: JSON {session_id, ...}
argv[1]: PROJECT_DIR (injected by write-settings.sh at deploy time)

Non-blocking: always exits 0.
"""

import json
import os
import subprocess
import sys


def find_sortie_dir() -> str:
    parts = os.getcwd().split(os.sep)
    for i in range(len(parts), 0, -1):
        d = os.sep.join(parts[:i]) + "/.sortie"
        if os.path.isdir(d):
            return d
    return ""


def find_ticket_id(sortie_dir: str) -> str:
    fst = os.path.join(sortie_dir, "flight-status.json")
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

    session_id = data.get("session_id", "") or ""
    if not session_id:
        sys.exit(0)

    sortie_dir = find_sortie_dir()
    if not sortie_dir:
        sys.exit(0)

    progress_path = os.path.join(sortie_dir, "progress.md")
    if not os.path.exists(progress_path):
        sys.exit(0)

    try:
        with open(progress_path) as f:
            content = f.read().strip()
    except Exception:
        sys.exit(0)

    if not content:
        sys.exit(0)

    ticket_id = find_ticket_id(sortie_dir)

    proc = subprocess.Popen(
        ["python3", storage_db, "write-snapshot",
         project_dir, session_id, ticket_id, "0", "-"],
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
