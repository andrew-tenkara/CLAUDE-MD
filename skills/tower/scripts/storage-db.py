#!/usr/bin/env python3
"""storage-db.py — SQLite storage for Tower session debriefs and project insights.

Lives at PROJECT_DIR/.sortie/storage.db (shared across all worktrees).

Usage:
  storage-db.py init <project-dir>
  storage-db.py write-debrief <project-dir> '<json>'
      JSON keys: ticket_id, branch, model, what_done, whats_left,
                 decisions, gotchas, files_touched, pr_url, pr_status, branch_status
  storage-db.py write-insight <project-dir> <ticket-id> <category> <detail>
  storage-db.py write-session <project-dir> <ticket-id> <branch> <model> [--end]
  storage-db.py get-briefing <project-dir> <ticket-id>
  storage-db.py get-insights <project-dir> [--limit N]
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path


def get_db(project_dir: str) -> sqlite3.Connection:
    sortie_dir = Path(project_dir) / ".sortie"
    sortie_dir.mkdir(parents=True, exist_ok=True)
    db_path = sortie_dir / "storage.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def cmd_init(project_dir: str) -> None:
    conn = get_db(project_dir)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS debriefs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT NOT NULL,
            branch TEXT,
            model TEXT,
            timestamp INTEGER NOT NULL,
            what_done TEXT,
            whats_left TEXT,
            decisions TEXT,
            gotchas TEXT,
            files_touched TEXT,
            pr_url TEXT,
            pr_status TEXT,
            branch_status TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_debriefs_ticket ON debriefs(ticket_id);

        CREATE TABLE IF NOT EXISTS insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT,
            category TEXT NOT NULL,
            detail TEXT NOT NULL,
            timestamp INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_insights_category ON insights(category);

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT NOT NULL,
            branch TEXT,
            model TEXT,
            started_at INTEGER NOT NULL,
            ended_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_ticket ON sessions(ticket_id);
    """)
    conn.commit()
    conn.close()
    print("STORAGE:initialized")


def cmd_write_debrief(project_dir: str, ticket_id: str, branch: str,
                      model: str, what_done: str, whats_left: str,
                      decisions: str, gotchas: str, files_touched: str,
                      pr_url: str = "", pr_status: str = "",
                      branch_status: str = "") -> None:
    conn = get_db(project_dir)
    conn.execute(
        """INSERT INTO debriefs
           (ticket_id, branch, model, timestamp, what_done, whats_left,
            decisions, gotchas, files_touched, pr_url, pr_status, branch_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticket_id, branch, model, int(time.time()), what_done, whats_left,
         decisions, gotchas, files_touched, pr_url, pr_status, branch_status),
    )
    conn.commit()
    conn.close()
    print(f"STORAGE:debrief written for {ticket_id}")


def cmd_write_insight(project_dir: str, ticket_id: str, category: str,
                      detail: str) -> None:
    conn = get_db(project_dir)
    conn.execute(
        "INSERT INTO insights (ticket_id, category, detail, timestamp) VALUES (?, ?, ?, ?)",
        (ticket_id, category, detail, int(time.time())),
    )
    conn.commit()
    conn.close()
    print(f"STORAGE:insight logged [{category}]")


def cmd_write_session(project_dir: str, ticket_id: str, branch: str,
                      model: str, end: bool = False) -> None:
    conn = get_db(project_dir)
    if end:
        # Update the most recent open session for this ticket
        conn.execute(
            """UPDATE sessions SET ended_at = ?
               WHERE ticket_id = ? AND ended_at IS NULL
               ORDER BY started_at DESC LIMIT 1""",
            (int(time.time()), ticket_id),
        )
    else:
        conn.execute(
            "INSERT INTO sessions (ticket_id, branch, model, started_at) VALUES (?, ?, ?, ?)",
            (ticket_id, branch, model, int(time.time())),
        )
    conn.commit()
    conn.close()


def cmd_get_briefing(project_dir: str, ticket_id: str) -> None:
    conn = get_db(project_dir)

    # Last 3 debriefs for this ticket
    debriefs = conn.execute(
        """SELECT * FROM debriefs WHERE ticket_id = ?
           ORDER BY timestamp DESC LIMIT 3""",
        (ticket_id,),
    ).fetchall()

    # Recent project-wide insights (last 20)
    insights = conn.execute(
        """SELECT * FROM insights
           ORDER BY timestamp DESC LIMIT 20""",
    ).fetchall()

    # Session history for this ticket
    sessions = conn.execute(
        """SELECT * FROM sessions WHERE ticket_id = ?
           ORDER BY started_at DESC LIMIT 5""",
        (ticket_id,),
    ).fetchall()

    if not debriefs and not insights and not sessions:
        print("BRIEFING:none")
        return

    lines = ["## Prior Intelligence\n"]

    if debriefs:
        lines.append("### Previous Debriefs\n")
        for d in debriefs:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["timestamp"]))
            lines.append(f"**{ts}** (model: {d['model']}, branch: {d['branch']})")
            if d["what_done"]:
                lines.append(f"- **Done**: {d['what_done']}")
            if d["whats_left"]:
                lines.append(f"- **Remaining**: {d['whats_left']}")
            if d["decisions"]:
                lines.append(f"- **Decisions**: {d['decisions']}")
            if d["gotchas"]:
                lines.append(f"- **Gotchas**: {d['gotchas']}")
            if d["files_touched"]:
                lines.append(f"- **Files**: {d['files_touched']}")
            if d["pr_url"]:
                status = f" ({d['pr_status']})" if d["pr_status"] else ""
                lines.append(f"- **PR**: {d['pr_url']}{status}")
            if d["branch_status"]:
                lines.append(f"- **Branch**: {d['branch_status']}")
            lines.append("")

    if insights:
        lines.append("### Project Insights\n")
        for i in insights:
            tag = f"[{i['category']}]"
            src = f" (from {i['ticket_id']})" if i["ticket_id"] else ""
            lines.append(f"- {tag} {i['detail']}{src}")
        lines.append("")

    if sessions:
        lines.append(f"### Session History ({ticket_id})\n")
        for s in sessions:
            start = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["started_at"]))
            end = time.strftime("%H:%M", time.localtime(s["ended_at"])) if s["ended_at"] else "ongoing"
            lines.append(f"- {start}–{end} ({s['model']})")
        lines.append("")

    print("\n".join(lines))


def cmd_get_insights(project_dir: str, limit: int = 20) -> None:
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT * FROM insights ORDER BY timestamp DESC LIMIT ?", (limit,)
    ).fetchall()
    for r in rows:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["timestamp"]))
        src = f" ({r['ticket_id']})" if r["ticket_id"] else ""
        print(f"[{r['category']}] {r['detail']}{src} — {ts}")
    conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "init":
        cmd_init(sys.argv[2])
    elif cmd == "write-debrief":
        d = json.loads(sys.argv[3])
        cmd_write_debrief(
            sys.argv[2], d["ticket_id"], d.get("branch", ""),
            d.get("model", ""), d.get("what_done", ""), d.get("whats_left", ""),
            d.get("decisions", ""), d.get("gotchas", ""), d.get("files_touched", ""),
            d.get("pr_url", ""), d.get("pr_status", ""), d.get("branch_status", ""),
        )
    elif cmd == "write-insight":
        cmd_write_insight(*sys.argv[2:6])
    elif cmd == "write-session":
        end = "--end" in sys.argv
        cmd_write_session(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], end)
    elif cmd == "get-briefing":
        cmd_get_briefing(sys.argv[2], sys.argv[3])
    elif cmd == "get-insights":
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 20
        cmd_get_insights(sys.argv[2], limit)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
