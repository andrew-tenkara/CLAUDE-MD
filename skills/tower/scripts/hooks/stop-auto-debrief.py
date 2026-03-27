#!/usr/bin/env python3
"""stop-auto-debrief.py — Stop hook: auto-debrief if pilot forgot + prune expired cache.

Called by Claude Code when a session ends (Stop event).
stdin: JSON {session_id, transcript_path}
argv[1]: PROJECT_DIR (injected by write-settings.sh at deploy time)

- If no debrief was written this session, writes a minimal one from progress.md
- Prunes expired tool_cache entries
Non-blocking: always exits 0.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time


def get_db(project_dir: str) -> sqlite3.Connection:
    db_path = os.path.join(project_dir, ".sortie", "storage.db")
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def find_sortie_dir() -> str:
    parts = os.getcwd().split(os.sep)
    for i in range(len(parts), 0, -1):
        d = os.sep.join(parts[:i]) + "/.sortie"
        if os.path.isdir(d):
            return d
    return ""


def session_has_debrief(conn: sqlite3.Connection, session_id: str,
                        ticket_id: str) -> bool:
    """Check if a debrief was written for this ticket in the last 2 hours."""
    if not conn or not ticket_id:
        return True  # Can't check — don't write a debrief blindly
    cutoff = int(time.time()) - 7200
    row = conn.execute(
        "SELECT 1 FROM debriefs WHERE ticket_id = ? AND timestamp > ? LIMIT 1",
        (ticket_id, cutoff),
    ).fetchone()
    return row is not None


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
        data = {}

    session_id = data.get("session_id", "") or ""

    sortie_dir = find_sortie_dir()
    ticket_id  = find_ticket_id(sortie_dir) if sortie_dir else ""

    # Auto-debrief if pilot forgot
    if ticket_id and session_id:
        conn = get_db(project_dir)
        if not session_has_debrief(conn, session_id, ticket_id):
            # Read progress.md for minimal debrief content
            progress_path = os.path.join(sortie_dir, "progress.md") if sortie_dir else ""
            progress = ""
            if progress_path and os.path.exists(progress_path):
                try:
                    with open(progress_path) as f:
                        progress = f.read().strip()[:500]
                except Exception:
                    pass

            auto_debrief = {
                "ticket_id":     ticket_id,
                "branch":        "",
                "model":         "auto",
                "what_done":     progress or "(auto-debrief: no progress recorded)",
                "whats_left":    "",
                "decisions":     "",
                "gotchas":       "",
                "files_touched": "",
                "pr_url":        "",
                "pr_status":     "",
                "branch_status": "",
            }
            proc = subprocess.Popen(
                ["python3", storage_db, "write-debrief", project_dir, "-"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            proc.communicate(input=json.dumps(auto_debrief).encode())
        if conn:
            conn.close()

    # Prune expired tool cache
    subprocess.run(
        ["python3", storage_db, "prune-tool-cache", project_dir],
        capture_output=True,
        timeout=10,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
