#!/usr/bin/env python3
"""storage-db.py — SQLite storage for Tower session debriefs and project insights.

Lives at PROJECT_DIR/.sortie/storage.db (shared across all worktrees).

Usage:
  storage-db.py init <project-dir>
  storage-db.py write-debrief <project-dir> -
      JSON via stdin. Keys: ticket_id, branch, model, what_done, whats_left,
                 decisions, gotchas, files_touched, pr_url, pr_status, branch_status
  storage-db.py write-insight <project-dir> <ticket-id> <category> <detail> [--valid-days N]
  storage-db.py write-session <project-dir> <ticket-id> <branch> <model> [--end]
  storage-db.py get-briefing <project-dir> <ticket-id>
  storage-db.py get-insights <project-dir> [--limit N]
  storage-db.py send-message <project-dir> -
      JSON via stdin. Keys: from_agent, to_agent (null=broadcast), type, payload
  storage-db.py get-messages <project-dir> <to-agent>
  storage-db.py get-events <project-dir> [--since N]
  storage-db.py health-check <project-dir>
  storage-db.py prune <project-dir> [--events-days N] [--messages-days N] [--vacuum]
  storage-db.py get-for-compression <project-dir> <ticket-id>
  storage-db.py write-summary <project-dir> <ticket-id|-|null>
      JSON via stdin. Keys: content, summary_type, level, debrief_count, source_ids, model
  storage-db.py get-summaries <project-dir> [--ticket <ticket-id>] [--level N]
  storage-db.py get-summaries-for-rollup <project-dir>

CCR (Compress-Cache-Retrieve) commands:
  storage-db.py cache-tool-result <project-dir> <session-id> <ticket-id> <tool-name> <tool-key> -
      stdin=full result text. Upserts into tool_cache (24h TTL).
  storage-db.py get-cached-tool <project-dir> <session-id> <tool-name> <tool-key>
      Returns full cached result, or CACHE:MISS if not found.
  storage-db.py check-tool-cache <project-dir> <session-id> <tool-name> <tool-key>
      Prints HIT or MISS only (fast path for dedup hooks).
  storage-db.py write-snapshot <project-dir> <session-id> <ticket-id> <remaining-pct> -
      stdin=snapshot text. Stores pre-compaction context snapshot.
  storage-db.py get-latest-snapshot <project-dir> <session-id>
      Returns latest snapshot text for this session, or SNAPSHOT:none.
  storage-db.py prune-tool-cache <project-dir>
      Deletes expired tool_cache entries (expires_at < now).
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional


def get_db(project_dir: str) -> sqlite3.Connection:
    sortie_dir = Path(project_dir) / ".sortie"
    sortie_dir.mkdir(parents=True, exist_ok=True)
    db_path = sortie_dir / "storage.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")  # Safe with WAL, faster writes
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def cmd_init(project_dir: str) -> None:
    conn = get_db(project_dir)
    conn.executescript("""
        -- Core tables
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
        CREATE INDEX IF NOT EXISTS idx_debriefs_timestamp ON debriefs(timestamp DESC);

        CREATE TABLE IF NOT EXISTS insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT,
            category TEXT NOT NULL,
            detail TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            valid_until INTEGER,
            superseded_by INTEGER REFERENCES insights(id)
        );
        CREATE INDEX IF NOT EXISTS idx_insights_category ON insights(category);
        CREATE INDEX IF NOT EXISTS idx_insights_timestamp ON insights(timestamp DESC);

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT NOT NULL,
            branch TEXT,
            model TEXT,
            started_at INTEGER NOT NULL,
            ended_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_ticket ON sessions(ticket_id);

        -- Event bus: CDC log for Tower polling and inter-agent coordination
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL DEFAULT (unixepoch()),
            consumed_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_events_unconsumed ON events(consumed_at) WHERE consumed_at IS NULL;

        -- Inter-agent message bus
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_agent TEXT,
            to_agent TEXT,
            type TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL DEFAULT (unixepoch()),
            read_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_messages_unread ON messages(to_agent, read_at) WHERE read_at IS NULL;

        -- FTS5: full-text search over debriefs (no data duplication via content table)
        CREATE VIRTUAL TABLE IF NOT EXISTS debriefs_fts USING fts5(
            what_done, gotchas, decisions, files_touched, ticket_id,
            content=debriefs, content_rowid=id
        );

        -- FTS5: full-text search over insights
        CREATE VIRTUAL TABLE IF NOT EXISTS insights_fts USING fts5(
            detail, category, ticket_id,
            content=insights, content_rowid=id
        );

        -- Triggers: keep FTS5 in sync + write events on meaningful writes

        CREATE TRIGGER IF NOT EXISTS debriefs_ai AFTER INSERT ON debriefs BEGIN
            INSERT INTO debriefs_fts(rowid, what_done, gotchas, decisions, files_touched, ticket_id)
            VALUES (new.id, new.what_done, new.gotchas, new.decisions, new.files_touched, new.ticket_id);
            INSERT INTO events(type, payload)
            VALUES ('debrief_written', json_object(
                'ticket_id', new.ticket_id,
                'branch', new.branch,
                'model', new.model,
                'debrief_id', new.id
            ));
        END;

        CREATE TRIGGER IF NOT EXISTS insights_ai AFTER INSERT ON insights BEGIN
            INSERT INTO insights_fts(rowid, detail, category, ticket_id)
            VALUES (new.id, new.detail, new.category, new.ticket_id);
            INSERT INTO events(type, payload)
            VALUES ('insight_logged', json_object(
                'ticket_id', new.ticket_id,
                'category', new.category,
                'insight_id', new.id
            ));
        END;

        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO events(type, payload)
            VALUES ('message_sent', json_object(
                'from_agent', new.from_agent,
                'to_agent', new.to_agent,
                'type', new.type,
                'message_id', new.id
            ));
        END;

        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT,              -- NULL for project-level rollups
            summary_type TEXT NOT NULL,  -- 'ticket' | 'project_rollup'
            level INTEGER NOT NULL DEFAULT 1, -- 1=ticket, 2=rollup of summaries
            content TEXT NOT NULL,
            debrief_count INTEGER NOT NULL DEFAULT 0,
            source_ids TEXT,             -- JSON array of source IDs (debrief or summary IDs)
            model TEXT,
            created_at INTEGER NOT NULL DEFAULT (unixepoch())
        );
        CREATE INDEX IF NOT EXISTS idx_summaries_ticket ON summaries(ticket_id);
        CREATE INDEX IF NOT EXISTS idx_summaries_level ON summaries(level, created_at DESC);

        -- CCR: full tool results cached before headroom compresses them
        CREATE TABLE IF NOT EXISTS tool_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            ticket_id TEXT,
            tool_name TEXT NOT NULL,
            tool_key TEXT NOT NULL,       -- file path / first 200 chars of command
            full_result TEXT NOT NULL,
            original_bytes INTEGER,
            accessed_at INTEGER,
            created_at INTEGER NOT NULL DEFAULT (unixepoch()),
            expires_at INTEGER NOT NULL   -- created_at + 86400 (24h TTL)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_cache_key
            ON tool_cache(session_id, tool_name, tool_key);
        CREATE INDEX IF NOT EXISTS idx_tool_cache_expiry ON tool_cache(expires_at);

        -- CCR: pre-compaction context snapshots (SessionStart restores these)
        CREATE TABLE IF NOT EXISTS context_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            ticket_id TEXT,
            remaining_pct REAL,
            snapshot TEXT NOT NULL,
            created_at INTEGER NOT NULL DEFAULT (unixepoch())
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_session
            ON context_snapshots(session_id, created_at DESC);
    """)

    # Backfill FTS5 for any existing rows that predate this schema
    conn.execute("""
        INSERT INTO debriefs_fts(rowid, what_done, gotchas, decisions, files_touched, ticket_id)
        SELECT d.id, d.what_done, d.gotchas, d.decisions, d.files_touched, d.ticket_id
        FROM debriefs d
        WHERE d.id NOT IN (SELECT rowid FROM debriefs_fts)
    """)
    conn.execute("""
        INSERT INTO insights_fts(rowid, detail, category, ticket_id)
        SELECT i.id, i.detail, i.category, i.ticket_id
        FROM insights i
        WHERE i.id NOT IN (SELECT rowid FROM insights_fts)
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
                      detail: str, valid_days: Optional[int] = None) -> None:
    valid_until = int(time.time()) + valid_days * 86400 if valid_days else None
    conn = get_db(project_dir)
    conn.execute(
        "INSERT INTO insights (ticket_id, category, detail, timestamp, valid_until) VALUES (?, ?, ?, ?, ?)",
        (ticket_id, category, detail, int(time.time()), valid_until),
    )
    conn.commit()
    conn.close()
    print(f"STORAGE:insight logged [{category}]")


def cmd_write_session(project_dir: str, ticket_id: str, branch: str,
                      model: str, end: bool = False) -> None:
    conn = get_db(project_dir)
    if end:
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


def cmd_send_message(project_dir: str, from_agent: str, to_agent: Optional[str],
                     msg_type: str, payload: str) -> None:
    conn = get_db(project_dir)
    conn.execute(
        "INSERT INTO messages (from_agent, to_agent, type, payload) VALUES (?, ?, ?, ?)",
        (from_agent, to_agent, msg_type, payload),
    )
    conn.commit()
    conn.close()
    target = to_agent or "broadcast"
    print(f"STORAGE:message sent [{msg_type}] to {target}")


def cmd_get_messages(project_dir: str, to_agent: str) -> None:
    conn = get_db(project_dir)
    # Fetch unread messages addressed to this agent or broadcast (NULL)
    rows = conn.execute(
        """SELECT * FROM messages
           WHERE (to_agent = ? OR to_agent IS NULL) AND read_at IS NULL
           ORDER BY created_at ASC""",
        (to_agent,),
    ).fetchall()

    if not rows:
        print("MESSAGES:none")
        conn.close()
        return

    # Mark as read
    ids = [r["id"] for r in rows]
    conn.execute(
        f"UPDATE messages SET read_at = ? WHERE id IN ({','.join('?' * len(ids))})",
        [int(time.time())] + ids,
    )
    conn.commit()

    lines = [f"## Messages for {to_agent}\n"]
    for r in rows:
        ts = time.strftime("%H:%M", time.localtime(r["created_at"]))
        src = r["from_agent"] or "system"
        lines.append(f"**[{ts}] {src} → [{r['type']}]**")
        if r["payload"]:
            lines.append(r["payload"])
        lines.append("")

    print("\n".join(lines))
    conn.close()


def cmd_get_events(project_dir: str, since: int = 0) -> None:
    conn = get_db(project_dir)
    cutoff = int(time.time()) - since if since else 0
    rows = conn.execute(
        """SELECT * FROM events
           WHERE created_at >= ? AND consumed_at IS NULL
           ORDER BY created_at ASC""",
        (cutoff,),
    ).fetchall()

    if not rows:
        print("EVENTS:none")
        conn.close()
        return

    for r in rows:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["created_at"]))
        print(f"[{ts}] {r['type']}: {r['payload']}")

    conn.close()


def _extract_fts_query(text: str, max_terms: int = 8) -> str:
    """Extract meaningful search terms from text for FTS5 query."""
    import re
    # Strip common stop words and short tokens
    stop = {"the", "a", "an", "and", "or", "in", "on", "at", "to", "for",
            "of", "with", "is", "was", "are", "be", "it", "this", "that",
            "we", "i", "by", "from", "as", "not", "but", "so", "if", "no"}
    words = re.findall(r'[a-zA-Z][a-zA-Z0-9_\-]{2,}', text)
    seen = set()
    terms = []
    for w in words:
        lw = w.lower()
        if lw not in stop and lw not in seen:
            seen.add(lw)
            terms.append(w)
        if len(terms) >= max_terms:
            break
    return " OR ".join(f'"{t}"' for t in terms) if terms else ""


def cmd_get_briefing(project_dir: str, ticket_id: str) -> None:
    conn = get_db(project_dir)
    now = int(time.time())

    # ── 1. Direct debriefs for this ticket ───────────────────────────────
    debriefs = conn.execute(
        """SELECT * FROM debriefs WHERE ticket_id = ?
           ORDER BY timestamp DESC LIMIT 3""",
        (ticket_id,),
    ).fetchall()

    # Check for a ticket summary (compressed from prior debriefs)
    summary_row = conn.execute(
        "SELECT * FROM summaries WHERE ticket_id = ? AND summary_type = 'ticket' ORDER BY created_at DESC LIMIT 1",
        (ticket_id,)
    ).fetchone()

    # ── 2. Cross-ticket related debriefs (files_touched overlap) ─────────
    related_debriefs = []
    if debriefs:
        # Collect all files touched on this ticket across recent debriefs
        all_files = " ".join(
            d["files_touched"] or "" for d in debriefs
        ).strip()
        fts_query = _extract_fts_query(all_files, max_terms=6)
        if fts_query:
            related_debriefs = conn.execute(
                """SELECT d.*, bm25(debriefs_fts) as rank
                   FROM debriefs_fts
                   JOIN debriefs d ON debriefs_fts.rowid = d.id
                   WHERE debriefs_fts MATCH ? AND d.ticket_id != ?
                   ORDER BY rank LIMIT 3""",
                (fts_query, ticket_id),
            ).fetchall()

    # ── 3. Insights — FTS-ranked by ticket context, excluding expired ─────
    # Build search context from ticket_id + debrief keywords
    context_text = ticket_id + " " + " ".join(
        f"{d['what_done'] or ''} {d['files_touched'] or ''}" for d in debriefs
    )
    fts_insight_query = _extract_fts_query(context_text, max_terms=8)

    if fts_insight_query:
        insights = conn.execute(
            """SELECT i.*, bm25(insights_fts) as rank
               FROM insights_fts
               JOIN insights i ON insights_fts.rowid = i.id
               WHERE insights_fts MATCH ?
                 AND (i.valid_until IS NULL OR i.valid_until > ?)
                 AND i.superseded_by IS NULL
               ORDER BY rank LIMIT 15""",
            (fts_insight_query, now),
        ).fetchall()
        # Fall back to recency if FTS found nothing
        if not insights:
            insights = conn.execute(
                """SELECT * FROM insights
                   WHERE (valid_until IS NULL OR valid_until > ?)
                     AND superseded_by IS NULL
                   ORDER BY timestamp DESC LIMIT 15""",
                (now,),
            ).fetchall()
    else:
        insights = conn.execute(
            """SELECT * FROM insights
               WHERE (valid_until IS NULL OR valid_until > ?)
                 AND superseded_by IS NULL
               ORDER BY timestamp DESC LIMIT 15""",
            (now,),
        ).fetchall()

    # ── 4. Session history ────────────────────────────────────────────────
    sessions = conn.execute(
        """SELECT * FROM sessions WHERE ticket_id = ?
           ORDER BY started_at DESC LIMIT 5""",
        (ticket_id,),
    ).fetchall()

    # ── 5. Unread messages for this ticket / agent ────────────────────────
    messages = conn.execute(
        """SELECT * FROM messages
           WHERE (to_agent = ? OR to_agent IS NULL) AND read_at IS NULL
           ORDER BY created_at ASC LIMIT 10""",
        (ticket_id,),
    ).fetchall()
    if messages:
        ids = [m["id"] for m in messages]
        conn.execute(
            f"UPDATE messages SET read_at = ? WHERE id IN ({','.join('?' * len(ids))})",
            [now] + ids,
        )
        conn.commit()

    # ── 5b. Recent compaction snapshot (any session on this ticket, last 2h) ──
    recent_snapshot = conn.execute(
        """SELECT snapshot FROM context_snapshots
           WHERE ticket_id = ? AND created_at > ?
           ORDER BY created_at DESC LIMIT 1""",
        (ticket_id, now - 7200),
    ).fetchone()

    if not debriefs and not summary_row and not insights and not sessions and not messages and not recent_snapshot:
        print("BRIEFING:none")
        conn.close()
        return

    lines = ["## Prior Intelligence\n"]

    if recent_snapshot:
        lines.append("## Last Session Snapshot (pre-compaction)\n")
        lines.append(recent_snapshot["snapshot"])
        lines.append("")

    if summary_row:
        lines.append(f"### Summary (compressed from {summary_row['debrief_count']} sessions)\n")
        lines.append(summary_row["content"])
        lines.append("")
    elif debriefs:
        lines.append("### Previous Debriefs\n")
        for d in debriefs:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["timestamp"]))
            lines.append(f"**{ts}** (model: {d['model']}, branch: {d['branch']})")
            if d["what_done"]:  lines.append(f"- **Done**: {d['what_done']}")
            if d["whats_left"]: lines.append(f"- **Remaining**: {d['whats_left']}")
            if d["decisions"]:  lines.append(f"- **Decisions**: {d['decisions']}")
            if d["gotchas"]:    lines.append(f"- **Gotchas**: {d['gotchas']}")
            if d["files_touched"]: lines.append(f"- **Files**: {d['files_touched']}")
            if d["pr_url"]:
                status = f" ({d['pr_status']})" if d["pr_status"] else ""
                lines.append(f"- **PR**: {d['pr_url']}{status}")
            if d["branch_status"]: lines.append(f"- **Branch**: {d['branch_status']}")
            lines.append("")

    if related_debriefs:
        lines.append("### Related Work (other tickets, similar files)\n")
        for d in related_debriefs:
            ts = time.strftime("%Y-%m-%d", time.localtime(d["timestamp"]))
            lines.append(f"**{d['ticket_id']}** ({ts}, {d['model']})")
            if d["what_done"]:     lines.append(f"- {d['what_done']}")
            if d["gotchas"]:       lines.append(f"- ⚠️ {d['gotchas']}")
            if d["files_touched"]: lines.append(f"- Files: {d['files_touched']}")
            lines.append("")

    if insights:
        lines.append("### Project Insights\n")
        for i in insights:
            tag = f"[{i['category']}]"
            src = f" (from {i['ticket_id']})" if i["ticket_id"] else ""
            expiry = f" [expires {time.strftime('%Y-%m-%d', time.localtime(i['valid_until']))}]" if i["valid_until"] else ""
            lines.append(f"- {tag} {i['detail']}{src}{expiry}")
        lines.append("")

    if sessions:
        lines.append(f"### Session History ({ticket_id})\n")
        for s in sessions:
            start = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["started_at"]))
            end = time.strftime("%H:%M", time.localtime(s["ended_at"])) if s["ended_at"] else "ongoing"
            lines.append(f"- {start}–{end} ({s['model']})")
        lines.append("")

    if messages:
        lines.append("### Incoming Messages\n")
        for m in messages:
            ts = time.strftime("%H:%M", time.localtime(m["created_at"]))
            src = m["from_agent"] or "system"
            lines.append(f"**[{ts}] {src} → [{m['type']}]**: {m['payload'] or ''}")
        lines.append("")

    print("\n".join(lines))
    conn.close()


def cmd_get_insights(project_dir: str, limit: int = 20) -> None:
    conn = get_db(project_dir)
    now = int(time.time())
    rows = conn.execute(
        """SELECT * FROM insights
           WHERE (valid_until IS NULL OR valid_until > ?)
             AND superseded_by IS NULL
           ORDER BY timestamp DESC LIMIT ?""",
        (now, limit),
    ).fetchall()
    for r in rows:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["timestamp"]))
        src = f" ({r['ticket_id']})" if r["ticket_id"] else ""
        expiry = f" [exp {time.strftime('%m-%d', time.localtime(r['valid_until']))}]" if r["valid_until"] else ""
        print(f"[{r['category']}] {r['detail']}{src}{expiry} — {ts}")
    conn.close()



def cmd_health_check(project_dir: str) -> None:
    """Print JSON health report for the DB."""
    db_path = Path(project_dir) / ".sortie" / "storage.db"
    wal_path = Path(str(db_path) + "-wal")
    size_bytes = db_path.stat().st_size if db_path.exists() else 0
    wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
    conn = get_db(project_dir)

    stats: dict = {
        "db_size_mb": round(size_bytes / 1024 / 1024, 2),
        "wal_size_mb": round(wal_bytes / 1024 / 1024, 2),
        "db_path": str(db_path),
        "tables": {},
        "warnings": [],
        "ok": True,
    }

    for table in ["debriefs", "insights", "sessions", "events", "messages",
                  "summaries", "tool_cache", "context_snapshots"]:
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            stats["tables"][table] = {"rows": n}
        except Exception:
            stats["tables"][table] = {"rows": 0}

    # Age of oldest debrief
    oldest = conn.execute("SELECT MIN(timestamp) FROM debriefs").fetchone()[0]
    if oldest:
        stats["tables"]["debriefs"]["oldest_days"] = round((time.time() - oldest) / 86400, 1)

    # Unconsumed events
    unc = conn.execute("SELECT COUNT(*) FROM events WHERE consumed_at IS NULL").fetchone()[0]
    stats["tables"]["events"]["unconsumed"] = unc

    # tool_cache: expired count
    now = int(time.time())
    expired = conn.execute("SELECT COUNT(*) FROM tool_cache WHERE expires_at < ?", (now,)).fetchone()[0]
    stats["tables"]["tool_cache"]["expired"] = expired

    # Warnings
    if size_bytes > 50 * 1024 * 1024:
        stats["warnings"].append(f"DB is {stats['db_size_mb']}MB — run: storage-db.py prune <project_dir> --vacuum")
    if wal_bytes > 10 * 1024 * 1024:
        stats["warnings"].append(f"WAL is {stats['wal_size_mb']}MB — run: storage-db.py prune <project_dir> (triggers PASSIVE checkpoint)")
    if stats["tables"]["events"]["rows"] > 10_000:
        stats["warnings"].append(f"events table has {stats['tables']['events']['rows']} rows — run prune")
    if stats["tables"]["messages"]["rows"] > 2_000:
        stats["warnings"].append(f"messages table has {stats['tables']['messages']['rows']} rows — run prune")
    if stats["tables"]["debriefs"]["rows"] > 200:
        uncompressed = conn.execute(
            "SELECT COUNT(DISTINCT ticket_id) FROM debriefs d WHERE NOT EXISTS (SELECT 1 FROM summaries s WHERE s.ticket_id = d.ticket_id)"
        ).fetchone()[0]
        if uncompressed > 10:
            stats["warnings"].append(f"{uncompressed} tickets have uncompressed debriefs — run compress-ticket.sh per ticket or rollup-summaries.sh")
    if stats["tables"]["summaries"]["rows"] > 50:
        stats["warnings"].append(f"{stats['tables']['summaries']['rows']} summaries — consider running rollup-summaries.sh to condense")
    if expired > 100:
        stats["warnings"].append(f"{expired} expired tool_cache entries — run: storage-db.py prune-tool-cache <project_dir>")

    stats["ok"] = len(stats["warnings"]) == 0
    print(json.dumps(stats, indent=2))


def cmd_prune(project_dir: str, events_days: int = 30, messages_days: int = 7, vacuum: bool = False) -> None:
    """Prune ephemeral tables. Never touches debriefs, insights, or summaries."""
    conn = get_db(project_dir)
    now = int(time.time())
    ev_cut = now - events_days * 86400
    msg_cut = now - messages_days * 86400

    r1 = conn.execute("DELETE FROM events WHERE created_at < ?", (ev_cut,))
    r2 = conn.execute("DELETE FROM messages WHERE created_at < ? AND read_at IS NOT NULL", (msg_cut,))
    r3 = conn.execute("DELETE FROM tool_cache WHERE expires_at < ?", (now,))
    conn.commit()  # Must commit before WAL checkpoint
    wal = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()

    result = {
        "events_deleted": r1.rowcount,
        "messages_deleted": r2.rowcount,
        "tool_cache_deleted": r3.rowcount,
        "wal_checkpoint": {"busy": wal[0], "log": wal[1], "checkpointed": wal[2]},
    }
    if vacuum:
        conn.execute("VACUUM")
        result["vacuumed"] = True

    print(json.dumps(result))


def cmd_get_for_compression(project_dir: str, ticket_id: str) -> None:
    """Dump debriefs for a ticket as JSON {ticket_id, debrief_ids, text} ready for summarization."""
    from datetime import datetime
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT * FROM debriefs WHERE ticket_id = ? ORDER BY timestamp ASC",
        (ticket_id,)
    ).fetchall()

    if not rows:
        print("DEBRIEFS:none")
        return

    ids = [r["id"] for r in rows]
    lines = [f"# Pilot Debriefs — {ticket_id}\n"]
    for r in rows:
        ts = datetime.fromtimestamp(r["timestamp"]).strftime("%Y-%m-%d %H:%M")
        lines.append(f"## Session [{r['model'] or 'unknown'} @ {ts}]")
        for field in ("what_done", "whats_left", "decisions", "gotchas", "files_touched"):
            val = r[field]
            if val:
                label = field.replace("_", " ").title()
                lines.append(f"**{label}:** {val}")
        if r["pr_url"]:
            lines.append(f"**PR:** {r['pr_url']} ({r['pr_status']})")
        lines.append("")

    print(json.dumps({"ticket_id": ticket_id, "debrief_ids": ids, "text": "\n".join(lines)}))


def cmd_write_summary(project_dir: str, ticket_id_or_none: str) -> None:
    """Write a summary from stdin JSON. ticket_id_or_none can be '-' for project rollups."""
    raw = sys.stdin.read().strip()
    data = json.loads(raw)
    ticket_id = None if ticket_id_or_none in ("-", "null", "") else ticket_id_or_none
    content = data["content"]
    summary_type = data.get("summary_type", "ticket")
    level = int(data.get("level", 1))
    debrief_count = int(data.get("debrief_count", 0))
    source_ids = json.dumps(data.get("source_ids", []))
    model = data.get("model", "")

    conn = get_db(project_dir)
    conn.execute(
        "INSERT INTO summaries (ticket_id, summary_type, level, content, debrief_count, source_ids, model) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, summary_type, level, content, debrief_count, source_ids, model)
    )
    conn.commit()
    label = ticket_id or "project"
    print(f"SUMMARY_WRITTEN:{label}:level{level}")


def cmd_get_summaries(project_dir: str, ticket_id: Optional[str] = None, level: Optional[int] = None) -> None:
    """List summaries. Optional --ticket and --level filters."""
    conn = get_db(project_dir)
    q = "SELECT id, ticket_id, summary_type, level, debrief_count, model, created_at, substr(content,1,120) as preview FROM summaries WHERE 1=1"
    params: list = []
    if ticket_id:
        q += " AND ticket_id = ?"
        params.append(ticket_id)
    if level is not None:
        q += " AND level = ?"
        params.append(level)
    q += " ORDER BY created_at DESC LIMIT 50"
    rows = conn.execute(q, params).fetchall()
    print(json.dumps([dict(r) for r in rows], indent=2))


def cmd_get_summaries_for_rollup(project_dir: str) -> None:
    """Dump all level-1 ticket summaries as text for XO to roll up into a project-level summary."""
    conn = get_db(project_dir)
    rows = conn.execute(
        "SELECT * FROM summaries WHERE level = 1 AND summary_type = 'ticket' ORDER BY created_at DESC LIMIT 100",
        []
    ).fetchall()
    if not rows:
        print("SUMMARIES:none")
        return
    ids = [r["id"] for r in rows]
    from datetime import datetime
    lines = ["# All Ticket Summaries — Project Rollup Input\n"]
    for r in rows:
        ts = datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d")
        lines.append(f"## {r['ticket_id'] or 'unknown'} [{ts}, {r['debrief_count']} sessions]")
        lines.append(r["content"])
        lines.append("")
    print(json.dumps({"summary_ids": ids, "count": len(rows), "text": "\n".join(lines)}))


# ── CCR commands ─────────────────────────────────────────────────────────────

def cmd_cache_tool_result(project_dir: str, session_id: str, ticket_id: str,
                          tool_name: str, tool_key: str) -> None:
    """Cache a full tool result from stdin. Upserts (INSERT OR REPLACE) with 24h TTL."""
    full_result = sys.stdin.read()
    now = int(time.time())
    conn = get_db(project_dir)
    conn.execute(
        """INSERT OR REPLACE INTO tool_cache
           (session_id, ticket_id, tool_name, tool_key, full_result, original_bytes, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, ticket_id or None, tool_name, tool_key,
         full_result, len(full_result.encode()), now, now + 86400),
    )
    conn.commit()
    conn.close()
    print(f"CACHE:stored {tool_name}:{tool_key[:60]}")


def cmd_get_cached_tool(project_dir: str, session_id: str,
                        tool_name: str, tool_key: str) -> None:
    """Return full cached result for a tool call, or CACHE:MISS."""
    now = int(time.time())
    conn = get_db(project_dir)
    row = conn.execute(
        """SELECT id, full_result FROM tool_cache
           WHERE session_id = ? AND tool_name = ? AND tool_key = ? AND expires_at > ?""",
        (session_id, tool_name, tool_key, now),
    ).fetchone()
    if not row:
        print("CACHE:MISS")
        conn.close()
        return
    conn.execute("UPDATE tool_cache SET accessed_at = ? WHERE id = ?", (now, row["id"]))
    conn.commit()
    conn.close()
    print(row["full_result"], end="")


def cmd_check_tool_cache(project_dir: str, session_id: str,
                         tool_name: str, tool_key: str) -> None:
    """Print HIT or MISS only — fast path for dedup hooks."""
    now = int(time.time())
    conn = get_db(project_dir)
    row = conn.execute(
        """SELECT 1 FROM tool_cache
           WHERE session_id = ? AND tool_name = ? AND tool_key = ? AND expires_at > ?""",
        (session_id, tool_name, tool_key, now),
    ).fetchone()
    conn.close()
    print("HIT" if row else "MISS")


def cmd_write_snapshot(project_dir: str, session_id: str, ticket_id: str,
                       remaining_pct: str) -> None:
    """Write a pre-compaction context snapshot from stdin."""
    snapshot = sys.stdin.read()
    conn = get_db(project_dir)
    conn.execute(
        """INSERT INTO context_snapshots (session_id, ticket_id, remaining_pct, snapshot)
           VALUES (?, ?, ?, ?)""",
        (session_id, ticket_id or None, float(remaining_pct) if remaining_pct else None, snapshot),
    )
    conn.commit()
    conn.close()
    print(f"SNAPSHOT:written for session {session_id[:16]}")


def cmd_get_latest_snapshot(project_dir: str, session_id: str) -> None:
    """Return latest snapshot text for this session, or SNAPSHOT:none."""
    conn = get_db(project_dir)
    row = conn.execute(
        """SELECT snapshot FROM context_snapshots
           WHERE session_id = ?
           ORDER BY created_at DESC, id DESC LIMIT 1""",
        (session_id,),
    ).fetchone()
    conn.close()
    if not row:
        print("SNAPSHOT:none")
        return
    print(row["snapshot"], end="")


def cmd_prune_tool_cache(project_dir: str) -> None:
    """Delete expired tool_cache entries."""
    now = int(time.time())
    conn = get_db(project_dir)
    r = conn.execute("DELETE FROM tool_cache WHERE expires_at < ?", (now,))
    conn.commit()
    conn.close()
    print(json.dumps({"tool_cache_deleted": r.rowcount}))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "init":
        cmd_init(sys.argv[2])

    elif cmd == "write-debrief":
        raw = sys.stdin.read() if sys.argv[3] == "-" else sys.argv[3]
        d = json.loads(raw)
        cmd_write_debrief(
            sys.argv[2], d["ticket_id"], d.get("branch", ""),
            d.get("model", ""), d.get("what_done", ""), d.get("whats_left", ""),
            d.get("decisions", ""), d.get("gotchas", ""), d.get("files_touched", ""),
            d.get("pr_url", ""), d.get("pr_status", ""), d.get("branch_status", ""),
        )

    elif cmd == "write-insight":
        valid_days = None
        args = sys.argv[2:]
        if "--valid-days" in args:
            idx = args.index("--valid-days")
            valid_days = int(args[idx + 1])
            args = [a for i, a in enumerate(args) if i not in (idx, idx + 1)]
        cmd_write_insight(*args[:4], valid_days=valid_days)

    elif cmd == "write-session":
        end = "--end" in sys.argv
        cmd_write_session(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], end)

    elif cmd == "send-message":
        raw = sys.stdin.read() if sys.argv[3] == "-" else sys.argv[3]
        d = json.loads(raw)
        cmd_send_message(
            sys.argv[2],
            d.get("from_agent", "unknown"),
            d.get("to_agent"),
            d.get("type", "message"),
            d.get("payload", ""),
        )

    elif cmd == "get-messages":
        cmd_get_messages(sys.argv[2], sys.argv[3])

    elif cmd == "get-events":
        since = 0
        if "--since" in sys.argv:
            idx = sys.argv.index("--since")
            since = int(sys.argv[idx + 1])
        cmd_get_events(sys.argv[2], since)

    elif cmd == "get-briefing":
        cmd_get_briefing(sys.argv[2], sys.argv[3])

    elif cmd == "get-insights":
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 20
        cmd_get_insights(sys.argv[2], limit)

    elif cmd == "health-check":
        cmd_health_check(sys.argv[2])

    elif cmd == "prune":
        import argparse as _ap
        p = _ap.ArgumentParser()
        p.add_argument("project_dir")
        p.add_argument("--events-days", type=int, default=30)
        p.add_argument("--messages-days", type=int, default=7)
        p.add_argument("--vacuum", action="store_true")
        a = p.parse_args(sys.argv[2:])
        cmd_prune(a.project_dir, a.events_days, a.messages_days, a.vacuum)

    elif cmd == "get-for-compression":
        cmd_get_for_compression(sys.argv[2], sys.argv[3])

    elif cmd == "write-summary":
        cmd_write_summary(sys.argv[2], sys.argv[3])

    elif cmd == "get-summaries":
        ticket = None
        level = None
        for i, arg in enumerate(sys.argv[4:], 4):
            if arg == "--ticket" and i+1 < len(sys.argv): ticket = sys.argv[i+1]
            if arg == "--level" and i+1 < len(sys.argv): level = int(sys.argv[i+1])
        cmd_get_summaries(sys.argv[2], ticket, level)

    elif cmd == "get-summaries-for-rollup":
        cmd_get_summaries_for_rollup(sys.argv[2])

    elif cmd == "cache-tool-result":
        # cache-tool-result <project_dir> <session_id> <ticket_id> <tool_name> <tool_key> -
        cmd_cache_tool_result(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6])

    elif cmd == "get-cached-tool":
        cmd_get_cached_tool(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])

    elif cmd == "check-tool-cache":
        cmd_check_tool_cache(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])

    elif cmd == "write-snapshot":
        # write-snapshot <project_dir> <session_id> <ticket_id> <remaining_pct> -
        cmd_write_snapshot(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])

    elif cmd == "get-latest-snapshot":
        cmd_get_latest_snapshot(sys.argv[2], sys.argv[3])

    elif cmd == "prune-tool-cache":
        cmd_prune_tool_cache(sys.argv[2])

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
