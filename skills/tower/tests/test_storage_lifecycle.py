"""Tests for storage-db.py lifecycle commands.

Covers: cmd_init, cmd_health_check, cmd_prune, cmd_get_for_compression,
        cmd_write_summary, cmd_get_summaries, cmd_get_summaries_for_rollup,
        and get-briefing summary-preference behavior.

Run with:
  python3 -m pytest tests/test_storage_lifecycle.py -v
  python3 tests/test_storage_lifecycle.py
"""
import io
import json
import sqlite3
import sys
import tempfile
import time
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

# Add scripts dir to path so we can import storage-db.py as a module.
# The file uses a hyphen, so we use importlib.
import importlib.util

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_STORAGE_SPEC = importlib.util.spec_from_file_location(
    "storage_db", _SCRIPTS_DIR / "storage-db.py"
)
storage_db = importlib.util.module_from_spec(_STORAGE_SPEC)
_STORAGE_SPEC.loader.exec_module(storage_db)

cmd_init = storage_db.cmd_init
cmd_health_check = storage_db.cmd_health_check
cmd_prune = storage_db.cmd_prune
cmd_get_for_compression = storage_db.cmd_get_for_compression
cmd_write_summary = storage_db.cmd_write_summary
cmd_get_summaries = storage_db.cmd_get_summaries
cmd_get_summaries_for_rollup = storage_db.cmd_get_summaries_for_rollup
cmd_get_briefing = storage_db.cmd_get_briefing
cmd_write_debrief = storage_db.cmd_write_debrief
get_db = storage_db.get_db


def _capture(fn, *args, **kwargs):
    """Call fn(*args, **kwargs) and return its stdout as a string."""
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        fn(*args, **kwargs)
    return buf.getvalue()


def _write_debrief(project_dir, ticket_id="ENG-1", branch="main", model="test-model",
                   what_done="Did stuff", whats_left="More stuff", decisions="Used X",
                   gotchas="Watch out", files_touched="src/foo.py", pr_url="", pr_status=""):
    """Helper that inserts a debrief directly via cmd_write_debrief."""
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        cmd_write_debrief(project_dir, ticket_id, branch, model, what_done,
                         whats_left, decisions, gotchas, files_touched,
                         pr_url, pr_status)


def _write_summary(project_dir, ticket_id_arg, content, summary_type="ticket",
                   level=1, debrief_count=2, source_ids=None, model="test-model"):
    """Helper that calls cmd_write_summary with stdin patched."""
    payload = json.dumps({
        "content": content,
        "summary_type": summary_type,
        "level": level,
        "debrief_count": debrief_count,
        "source_ids": source_ids or [],
        "model": model,
    })
    buf = io.StringIO()
    with patch("sys.stdin", io.StringIO(payload)), patch("sys.stdout", buf):
        cmd_write_summary(project_dir, ticket_id_arg)
    return buf.getvalue()


class TestCmdInit(unittest.TestCase):

    def setUp(self):
        self.project_dir = tempfile.mkdtemp()
        _capture(cmd_init, self.project_dir)

    def tearDown(self):
        shutil.rmtree(self.project_dir)

    def _conn(self):
        return get_db(self.project_dir)

    def test_init_prints_initialized(self):
        out = _capture(cmd_init, self.project_dir)
        self.assertIn("STORAGE:initialized", out)

    def test_summaries_table_exists(self):
        conn = self._conn()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='summaries'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        conn.close()

    def test_summaries_ticket_index_exists(self):
        conn = self._conn()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_summaries_ticket'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        conn.close()

    def test_summaries_level_index_exists(self):
        conn = self._conn()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_summaries_level'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        conn.close()

    def test_core_tables_exist(self):
        conn = self._conn()
        expected = {"debriefs", "insights", "sessions", "events", "messages", "summaries"}
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        actual = {r[0] for r in rows if not r[0].startswith("sqlite_")}
        self.assertTrue(expected.issubset(actual))
        conn.close()

    def test_init_is_idempotent(self):
        # Running init twice should not raise or duplicate structure
        out = _capture(cmd_init, self.project_dir)
        self.assertIn("STORAGE:initialized", out)


class TestCmdHealthCheck(unittest.TestCase):

    def setUp(self):
        self.project_dir = tempfile.mkdtemp()
        _capture(cmd_init, self.project_dir)

    def tearDown(self):
        shutil.rmtree(self.project_dir)

    def _health(self):
        out = _capture(cmd_health_check, self.project_dir)
        return json.loads(out)

    def test_empty_db_no_warnings(self):
        report = self._health()
        self.assertEqual(report["warnings"], [])

    def test_empty_db_ok_true(self):
        report = self._health()
        self.assertTrue(report["ok"])

    def test_many_events_triggers_warning(self):
        conn = get_db(self.project_dir)
        now = int(time.time())
        conn.executemany(
            "INSERT INTO events (type, payload, created_at) VALUES (?, ?, ?)",
            [("test_event", "{}", now) for _ in range(10_001)]
        )
        conn.commit()
        conn.close()
        report = self._health()
        warning_text = " ".join(report["warnings"])
        self.assertIn("events", warning_text)

    def test_many_events_sets_ok_false(self):
        conn = get_db(self.project_dir)
        now = int(time.time())
        conn.executemany(
            "INSERT INTO events (type, payload, created_at) VALUES (?, ?, ?)",
            [("test_event", "{}", now) for _ in range(10_001)]
        )
        conn.commit()
        conn.close()
        report = self._health()
        self.assertFalse(report["ok"])

    def test_many_uncompressed_debriefs_triggers_warning(self):
        # Need >200 debriefs with >10 distinct ticket_ids that have no summary
        conn = get_db(self.project_dir)
        now = int(time.time())
        rows = []
        for i in range(201):
            ticket = f"ENG-{i % 20}"  # 20 distinct tickets, all uncompressed
            rows.append((ticket, "main", "m", now, "did stuff", "", "", "", "", "", "", ""))
        conn.executemany(
            "INSERT INTO debriefs (ticket_id,branch,model,timestamp,what_done,whats_left,decisions,gotchas,files_touched,pr_url,pr_status,branch_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows
        )
        conn.commit()
        conn.close()
        report = self._health()
        warning_text = " ".join(report["warnings"])
        self.assertIn("uncompressed", warning_text)

    def test_many_summaries_triggers_warning(self):
        conn = get_db(self.project_dir)
        now = int(time.time())
        conn.executemany(
            "INSERT INTO summaries (ticket_id, summary_type, level, content, debrief_count, created_at) VALUES (?,?,?,?,?,?)",
            [(f"ENG-{i}", "ticket", 1, "summary text", 1, now) for i in range(51)]
        )
        conn.commit()
        conn.close()
        report = self._health()
        warning_text = " ".join(report["warnings"])
        self.assertIn("summaries", warning_text)

    def test_tables_key_present_for_all_tables(self):
        report = self._health()
        for t in ["debriefs", "insights", "sessions", "events", "messages", "summaries"]:
            self.assertIn(t, report["tables"])


class TestCmdPrune(unittest.TestCase):

    def setUp(self):
        self.project_dir = tempfile.mkdtemp()
        _capture(cmd_init, self.project_dir)

    def tearDown(self):
        shutil.rmtree(self.project_dir)

    def _conn(self):
        return get_db(self.project_dir)

    def _insert_event(self, created_at, consumed_at=None):
        conn = self._conn()
        conn.execute(
            "INSERT INTO events (type, payload, created_at, consumed_at) VALUES (?, ?, ?, ?)",
            ("test", "{}", created_at, consumed_at)
        )
        conn.commit()
        conn.close()

    def _insert_message(self, created_at, read_at=None):
        conn = self._conn()
        conn.execute(
            "INSERT INTO messages (from_agent, to_agent, type, payload, created_at, read_at) VALUES (?,?,?,?,?,?)",
            ("a", "b", "msg", "hi", created_at, read_at)
        )
        conn.commit()
        conn.close()

    def test_old_events_deleted(self):
        old_ts = int(time.time()) - (40 * 86400)  # 40 days ago
        self._insert_event(old_ts)
        out = _capture(cmd_prune, self.project_dir, events_days=30)
        result = json.loads(out)
        self.assertEqual(result["events_deleted"], 1)

    def test_recent_events_not_deleted(self):
        recent_ts = int(time.time()) - (5 * 86400)  # 5 days ago
        self._insert_event(recent_ts)
        out = _capture(cmd_prune, self.project_dir, events_days=30)
        result = json.loads(out)
        self.assertEqual(result["events_deleted"], 0)

    def test_read_messages_deleted(self):
        old_ts = int(time.time()) - (10 * 86400)
        read_ts = int(time.time()) - (9 * 86400)
        self._insert_message(old_ts, read_at=read_ts)
        out = _capture(cmd_prune, self.project_dir, messages_days=7)
        result = json.loads(out)
        self.assertEqual(result["messages_deleted"], 1)

    def test_unread_messages_not_deleted(self):
        old_ts = int(time.time()) - (10 * 86400)
        self._insert_message(old_ts, read_at=None)  # unread
        out = _capture(cmd_prune, self.project_dir, messages_days=7)
        result = json.loads(out)
        self.assertEqual(result["messages_deleted"], 0)

    def test_debriefs_never_touched_by_prune(self):
        _write_debrief(self.project_dir, ticket_id="ENG-safe")
        conn = self._conn()
        # Manually backdate the debrief timestamp to very old
        conn.execute("UPDATE debriefs SET timestamp = 1000000")
        conn.commit()
        conn.close()
        _capture(cmd_prune, self.project_dir, events_days=1)
        conn = self._conn()
        count = conn.execute("SELECT COUNT(*) FROM debriefs").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_insights_never_touched_by_prune(self):
        conn = self._conn()
        conn.execute(
            "INSERT INTO insights (ticket_id, category, detail, timestamp) VALUES (?,?,?,?)",
            ("ENG-1", "test", "some insight", 1000000)
        )
        conn.commit()
        conn.close()
        _capture(cmd_prune, self.project_dir, events_days=1)
        conn = self._conn()
        count = conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_summaries_never_touched_by_prune(self):
        _write_summary(self.project_dir, "ENG-1", "summary content")
        conn = self._conn()
        conn.execute("UPDATE summaries SET created_at = 1000000")
        conn.commit()
        conn.close()
        _capture(cmd_prune, self.project_dir, events_days=1)
        conn = self._conn()
        count = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_vacuum_flag_sets_key_in_result(self):
        out = _capture(cmd_prune, self.project_dir, vacuum=True)
        result = json.loads(out)
        self.assertTrue(result.get("vacuumed"))

    def test_no_vacuum_flag_omits_vacuumed_key(self):
        out = _capture(cmd_prune, self.project_dir, vacuum=False)
        result = json.loads(out)
        self.assertNotIn("vacuumed", result)


class TestCmdGetForCompression(unittest.TestCase):

    def setUp(self):
        self.project_dir = tempfile.mkdtemp()
        _capture(cmd_init, self.project_dir)

    def tearDown(self):
        shutil.rmtree(self.project_dir)

    def test_no_debriefs_returns_none_sentinel(self):
        out = _capture(cmd_get_for_compression, self.project_dir, "ENG-MISSING")
        self.assertEqual(out.strip(), "DEBRIEFS:none")

    def test_with_debriefs_returns_json(self):
        _write_debrief(self.project_dir, ticket_id="ENG-42")
        out = _capture(cmd_get_for_compression, self.project_dir, "ENG-42")
        data = json.loads(out)
        self.assertIn("ticket_id", data)
        self.assertIn("debrief_ids", data)
        self.assertIn("text", data)

    def test_ticket_id_in_result(self):
        _write_debrief(self.project_dir, ticket_id="ENG-42")
        out = _capture(cmd_get_for_compression, self.project_dir, "ENG-42")
        data = json.loads(out)
        self.assertEqual(data["ticket_id"], "ENG-42")

    def test_debrief_ids_is_list_of_ints(self):
        _write_debrief(self.project_dir, ticket_id="ENG-42")
        _write_debrief(self.project_dir, ticket_id="ENG-42")
        out = _capture(cmd_get_for_compression, self.project_dir, "ENG-42")
        data = json.loads(out)
        self.assertEqual(len(data["debrief_ids"]), 2)
        self.assertIsInstance(data["debrief_ids"][0], int)

    def test_text_contains_what_done(self):
        _write_debrief(self.project_dir, ticket_id="ENG-42", what_done="Implemented caching layer")
        out = _capture(cmd_get_for_compression, self.project_dir, "ENG-42")
        data = json.loads(out)
        self.assertIn("Implemented caching layer", data["text"])

    def test_text_contains_gotchas(self):
        _write_debrief(self.project_dir, ticket_id="ENG-42", gotchas="Watch the race condition")
        out = _capture(cmd_get_for_compression, self.project_dir, "ENG-42")
        data = json.loads(out)
        self.assertIn("Watch the race condition", data["text"])

    def test_text_contains_decisions(self):
        _write_debrief(self.project_dir, ticket_id="ENG-42", decisions="Chose SQLite over Redis")
        out = _capture(cmd_get_for_compression, self.project_dir, "ENG-42")
        data = json.loads(out)
        self.assertIn("Chose SQLite over Redis", data["text"])

    def test_text_contains_files_touched(self):
        _write_debrief(self.project_dir, ticket_id="ENG-42", files_touched="src/cache.py")
        out = _capture(cmd_get_for_compression, self.project_dir, "ENG-42")
        data = json.loads(out)
        self.assertIn("src/cache.py", data["text"])

    def test_text_contains_pr_url(self):
        _write_debrief(self.project_dir, ticket_id="ENG-42",
                       pr_url="https://github.com/org/repo/pull/99", pr_status="open")
        out = _capture(cmd_get_for_compression, self.project_dir, "ENG-42")
        data = json.loads(out)
        self.assertIn("https://github.com/org/repo/pull/99", data["text"])


class TestCmdWriteSummary(unittest.TestCase):

    def setUp(self):
        self.project_dir = tempfile.mkdtemp()
        _capture(cmd_init, self.project_dir)

    def tearDown(self):
        shutil.rmtree(self.project_dir)

    def _conn(self):
        return get_db(self.project_dir)

    def test_write_ticket_summary_prints_confirmation(self):
        out = _write_summary(self.project_dir, "ENG-1", "This is the summary.")
        self.assertIn("SUMMARY_WRITTEN", out)
        self.assertIn("ENG-1", out)

    def test_write_ticket_summary_stored_in_db(self):
        _write_summary(self.project_dir, "ENG-1", "Unique summary content XYZ")
        conn = self._conn()
        row = conn.execute("SELECT * FROM summaries WHERE ticket_id = 'ENG-1'").fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertIn("Unique summary content XYZ", row["content"])

    def test_write_project_rollup_with_dash(self):
        out = _write_summary(self.project_dir, "-", "Project rollup content", summary_type="project_rollup")
        self.assertIn("SUMMARY_WRITTEN", out)
        self.assertIn("project", out)

    def test_project_rollup_has_null_ticket_id(self):
        _write_summary(self.project_dir, "-", "Rollup content", summary_type="project_rollup")
        conn = self._conn()
        row = conn.execute("SELECT * FROM summaries WHERE ticket_id IS NULL").fetchone()
        conn.close()
        self.assertIsNotNone(row)

    def test_level_stored_correctly_level1(self):
        _write_summary(self.project_dir, "ENG-1", "Level 1 summary", level=1)
        conn = self._conn()
        row = conn.execute("SELECT level FROM summaries WHERE ticket_id = 'ENG-1'").fetchone()
        conn.close()
        self.assertEqual(row["level"], 1)

    def test_level_stored_correctly_level2(self):
        _write_summary(self.project_dir, "ENG-1", "Level 2 summary", level=2)
        conn = self._conn()
        row = conn.execute("SELECT level FROM summaries WHERE ticket_id = 'ENG-1'").fetchone()
        conn.close()
        self.assertEqual(row["level"], 2)

    def test_null_string_ticket_id_stored_as_null(self):
        _write_summary(self.project_dir, "null", "Null ticket summary")
        conn = self._conn()
        row = conn.execute("SELECT * FROM summaries WHERE ticket_id IS NULL").fetchone()
        conn.close()
        self.assertIsNotNone(row)


class TestCmdGetSummaries(unittest.TestCase):

    def setUp(self):
        self.project_dir = tempfile.mkdtemp()
        _capture(cmd_init, self.project_dir)
        _write_summary(self.project_dir, "ENG-1", "Summary for ENG-1", level=1)
        _write_summary(self.project_dir, "ENG-2", "Summary for ENG-2", level=1)
        _write_summary(self.project_dir, "ENG-1", "Level 2 rollup", level=2)

    def tearDown(self):
        shutil.rmtree(self.project_dir)

    def test_no_filter_returns_all_summaries(self):
        out = _capture(cmd_get_summaries, self.project_dir)
        rows = json.loads(out)
        self.assertEqual(len(rows), 3)

    def test_ticket_filter_returns_only_matching(self):
        out = _capture(cmd_get_summaries, self.project_dir, ticket_id="ENG-1")
        rows = json.loads(out)
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual(r["ticket_id"], "ENG-1")

    def test_ticket_filter_excludes_other_tickets(self):
        out = _capture(cmd_get_summaries, self.project_dir, ticket_id="ENG-2")
        rows = json.loads(out)
        ticket_ids = {r["ticket_id"] for r in rows}
        self.assertNotIn("ENG-1", ticket_ids)

    def test_level_filter_returns_only_matching_level(self):
        out = _capture(cmd_get_summaries, self.project_dir, level=1)
        rows = json.loads(out)
        for r in rows:
            self.assertEqual(r["level"], 1)

    def test_level_filter_excludes_other_levels(self):
        out = _capture(cmd_get_summaries, self.project_dir, level=2)
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)

    def test_combined_ticket_and_level_filter(self):
        out = _capture(cmd_get_summaries, self.project_dir, ticket_id="ENG-1", level=1)
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticket_id"], "ENG-1")
        self.assertEqual(rows[0]["level"], 1)

    def test_result_contains_preview_field(self):
        out = _capture(cmd_get_summaries, self.project_dir)
        rows = json.loads(out)
        self.assertIn("preview", rows[0])


class TestCmdGetSummariesForRollup(unittest.TestCase):

    def setUp(self):
        self.project_dir = tempfile.mkdtemp()
        _capture(cmd_init, self.project_dir)

    def tearDown(self):
        shutil.rmtree(self.project_dir)

    def test_no_summaries_returns_none_sentinel(self):
        out = _capture(cmd_get_summaries_for_rollup, self.project_dir)
        self.assertEqual(out.strip(), "SUMMARIES:none")

    def test_with_summaries_returns_json(self):
        _write_summary(self.project_dir, "ENG-1", "Summary content", level=1)
        out = _capture(cmd_get_summaries_for_rollup, self.project_dir)
        data = json.loads(out)
        self.assertIn("summary_ids", data)
        self.assertIn("count", data)
        self.assertIn("text", data)

    def test_count_matches_number_of_summaries(self):
        _write_summary(self.project_dir, "ENG-1", "Summary 1", level=1)
        _write_summary(self.project_dir, "ENG-2", "Summary 2", level=1)
        out = _capture(cmd_get_summaries_for_rollup, self.project_dir)
        data = json.loads(out)
        self.assertEqual(data["count"], 2)

    def test_summary_ids_is_list_of_ints(self):
        _write_summary(self.project_dir, "ENG-1", "Summary 1", level=1)
        out = _capture(cmd_get_summaries_for_rollup, self.project_dir)
        data = json.loads(out)
        self.assertIsInstance(data["summary_ids"], list)
        self.assertIsInstance(data["summary_ids"][0], int)

    def test_text_contains_ticket_ids(self):
        _write_summary(self.project_dir, "ENG-99", "Some work was done", level=1)
        out = _capture(cmd_get_summaries_for_rollup, self.project_dir)
        data = json.loads(out)
        self.assertIn("ENG-99", data["text"])

    def test_text_contains_summary_content(self):
        _write_summary(self.project_dir, "ENG-1", "Distinctive rollup content ZZZQ", level=1)
        out = _capture(cmd_get_summaries_for_rollup, self.project_dir)
        data = json.loads(out)
        self.assertIn("Distinctive rollup content ZZZQ", data["text"])

    def test_only_level1_ticket_summaries_included(self):
        # Level-2 rollup summaries should NOT appear in the for-rollup output
        _write_summary(self.project_dir, "ENG-1", "Level 1 ticket summary", level=1)
        _write_summary(self.project_dir, "ENG-1", "Level 2 rollup summary", level=2,
                       summary_type="project_rollup")
        out = _capture(cmd_get_summaries_for_rollup, self.project_dir)
        data = json.loads(out)
        self.assertEqual(data["count"], 1)


class TestGetBriefingSummaryPreference(unittest.TestCase):
    """get-briefing should prefer a summary over raw debrief content."""

    def setUp(self):
        self.project_dir = tempfile.mkdtemp()
        _capture(cmd_init, self.project_dir)

    def tearDown(self):
        shutil.rmtree(self.project_dir)

    def test_briefing_shows_summary_when_present(self):
        _write_debrief(self.project_dir, ticket_id="ENG-7",
                       what_done="Raw debrief what_done text RAW_MARKER")
        _write_summary(self.project_dir, "ENG-7",
                       "COMPRESSED_SUMMARY_MARKER: all sessions compressed here",
                       summary_type="ticket", level=1, debrief_count=1)
        out = _capture(cmd_get_briefing, self.project_dir, "ENG-7")
        self.assertIn("Summary", out)
        self.assertIn("COMPRESSED_SUMMARY_MARKER", out)

    def test_briefing_does_not_show_raw_debriefs_when_summary_exists(self):
        _write_debrief(self.project_dir, ticket_id="ENG-7",
                       what_done="RAW_DEBRIEF_CONTENT_SENTINEL")
        _write_summary(self.project_dir, "ENG-7",
                       "Compressed summary replaces raw debriefs",
                       summary_type="ticket", level=1, debrief_count=1)
        out = _capture(cmd_get_briefing, self.project_dir, "ENG-7")
        self.assertNotIn("RAW_DEBRIEF_CONTENT_SENTINEL", out)

    def test_briefing_shows_raw_debriefs_without_summary(self):
        _write_debrief(self.project_dir, ticket_id="ENG-8",
                       what_done="RAW_DEBRIEF_ONLY_SENTINEL")
        out = _capture(cmd_get_briefing, self.project_dir, "ENG-8")
        self.assertIn("RAW_DEBRIEF_ONLY_SENTINEL", out)

    def test_briefing_none_for_empty_ticket(self):
        out = _capture(cmd_get_briefing, self.project_dir, "ENG-NONEXISTENT")
        self.assertEqual(out.strip(), "BRIEFING:none")


if __name__ == "__main__":
    unittest.main()
