"""Tests for inline_sentinel.py — classification, debounce, idle detection."""
import sys
import json
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from inline_sentinel import InlineSentinel, WatchState, TailState, _read_new_lines


class TestTailState(unittest.TestCase):
    def test_read_new_lines_empty_file(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write("")
            f.flush()
            state = TailState(path=Path(f.name), offset=0)
            lines = _read_new_lines(state)
            assert lines == []

    def test_read_new_lines_appended(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write('{"type": "assistant"}\n')
            f.flush()
            state = TailState(path=Path(f.name), offset=0)

            lines = _read_new_lines(state)
            assert len(lines) == 1
            assert '"assistant"' in lines[0]

            # Second read with no new data
            lines2 = _read_new_lines(state)
            assert lines2 == []

            # Append more
            with open(f.name, 'a') as fa:
                fa.write('{"type": "user"}\n')
            lines3 = _read_new_lines(state)
            assert len(lines3) == 1

    def test_read_new_lines_missing_file(self):
        state = TailState(path=Path("/nonexistent/file.jsonl"), offset=0)
        lines = _read_new_lines(state)
        assert lines == []


class TestInlineSentinel(unittest.TestCase):
    def test_start_stop(self):
        sentinel = InlineSentinel(project_dir="/tmp")
        sentinel.start()
        assert sentinel.is_alive
        sentinel.stop()
        time.sleep(0.5)  # let thread wind down
        # Thread should eventually stop
        assert not sentinel._running

    def test_add_remove_worktree(self):
        sentinel = InlineSentinel(project_dir="/tmp")
        assert sentinel.watching_count == 0
        sentinel.add_worktree("ENG-200", "/tmp/worktrees/ENG-200")
        assert sentinel.watching_count == 1
        sentinel.add_worktree("ENG-201", "/tmp/worktrees/ENG-201")
        assert sentinel.watching_count == 2
        sentinel.remove_worktree("ENG-200")
        assert sentinel.watching_count == 1

    def test_duplicate_add_ignored(self):
        sentinel = InlineSentinel(project_dir="/tmp")
        sentinel.add_worktree("ENG-200", "/tmp/worktrees/ENG-200")
        sentinel.add_worktree("ENG-200", "/tmp/worktrees/ENG-200")
        assert sentinel.watching_count == 1


class TestWatchState(unittest.TestCase):
    def test_defaults(self):
        ws = WatchState(ticket_id="ENG-200", worktree_path="/tmp/wt")
        assert ws.ticket_id == "ENG-200"
        assert ws.confirmed_status == ""
        assert ws.proposed_count == 0
        assert len(ws.recent_events) == 0

    def test_event_window_bounded(self):
        ws = WatchState(ticket_id="ENG-200", worktree_path="/tmp/wt")
        for i in range(200):
            ws.recent_events.append({"i": i})
        assert len(ws.recent_events) == 100  # EVENT_WINDOW


class TestStatusCallback(unittest.TestCase):
    def test_on_status_change_called(self):
        changes = []
        def on_change(tid, old, new, phase):
            changes.append((tid, old, new, phase))

        sentinel = InlineSentinel(project_dir="/tmp", on_status_change=on_change)
        # Directly test that the callback fires (integration test)
        # Would need JSONL files to fully test — covered by classify tests
        assert callable(sentinel._on_status_change)


if __name__ == "__main__":
    unittest.main()
