"""Tests for status_observer.py — evidence-based status derivation."""
import sys
import json
import time
import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from status_observer import (
    derive_status,
    _has_session_ended,
    _read_command_override,
)

# Helper: mock the internal evidence function
def _patch_jsonl_age(jsonl_age=None):
    """Patch _jsonl_age for testing."""
    return patch("status_observer._jsonl_age", return_value=jsonl_age)


class TestDeriveStatus(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sortie = Path(self.tmpdir) / ".sortie"
        self.sortie.mkdir()

    def test_no_worktree_returns_on_deck(self):
        assert derive_status("") == "ON_DECK"

    def test_session_ended_returns_recovered(self):
        (self.sortie / "session-ended").touch()
        assert derive_status(self.tmpdir) == "RECOVERED"

    def test_command_override_consumed(self):
        cmd = {"set_status": "RECOVERED", "reason": "test", "source": "test"}
        (self.sortie / "command.json").write_text(json.dumps(cmd))
        assert derive_status(self.tmpdir) == "RECOVERED"
        assert not (self.sortie / "command.json").exists()

    def test_command_override_beats_session_ended(self):
        (self.sortie / "session-ended").touch()
        cmd = {"set_status": "IN_FLIGHT", "reason": "forced", "source": "xo"}
        (self.sortie / "command.json").write_text(json.dumps(cmd))
        assert derive_status(self.tmpdir) == "IN_FLIGHT"

    def test_no_jsonl_returns_on_deck(self):
        """No JSONL file → ON_DECK."""
        with _patch_jsonl_age(None):
            assert derive_status(self.tmpdir) == "ON_DECK"

    def test_fresh_jsonl_returns_in_flight(self):
        """Fresh JSONL (5s) → IN_FLIGHT."""
        with _patch_jsonl_age(5.0):
            assert derive_status(self.tmpdir) == "IN_FLIGHT"

    def test_warm_jsonl_in_flight_goes_on_approach(self):
        """JSONL 60s old, was IN_FLIGHT → ON_APPROACH (past hysteresis window)."""
        with _patch_jsonl_age(60.0):
            assert derive_status(self.tmpdir, current_status="IN_FLIGHT") == "ON_APPROACH"

    def test_stale_jsonl_on_deck_stays_on_deck(self):
        """JSONL 300s old, was ON_DECK → stays ON_DECK."""
        with _patch_jsonl_age(300.0):
            assert derive_status(self.tmpdir, current_status="ON_DECK") == "ON_DECK"

    def test_hysteresis_in_flight_stays_longer(self):
        """IN_FLIGHT uses JSONL_FRESH_STAY (25s) window, ON_DECK uses JSONL_FRESH_ENTER (10s)."""
        with _patch_jsonl_age(15.0):
            # 15s is stale for entering (>10s) but fresh for staying (<25s)
            assert derive_status(self.tmpdir, current_status="") == "ON_DECK"
            assert derive_status(self.tmpdir, current_status="IN_FLIGHT") == "IN_FLIGHT"

    def test_session_ended_from_in_flight(self):
        (self.sortie / "session-ended").touch()
        assert derive_status(self.tmpdir, current_status="IN_FLIGHT") == "RECOVERED"


class TestEvidenceReaders(unittest.TestCase):

    def test_has_session_ended_true(self):
        d = tempfile.mkdtemp()
        Path(d, ".sortie").mkdir()
        Path(d, ".sortie", "session-ended").touch()
        assert _has_session_ended(d) is True

    def test_has_session_ended_false(self):
        d = tempfile.mkdtemp()
        Path(d, ".sortie").mkdir()
        assert _has_session_ended(d) is False

    def test_command_override_reads_and_deletes(self):
        d = tempfile.mkdtemp()
        s = Path(d, ".sortie")
        s.mkdir()
        (s / "command.json").write_text(json.dumps({"set_status": "ON_APPROACH"}))
        assert _read_command_override(d) == "ON_APPROACH"
        assert not (s / "command.json").exists()

    def test_command_override_missing(self):
        d = tempfile.mkdtemp()
        Path(d, ".sortie").mkdir()
        assert _read_command_override(d) is None

    def test_command_override_invalid_status(self):
        d = tempfile.mkdtemp()
        s = Path(d, ".sortie")
        s.mkdir()
        (s / "command.json").write_text(json.dumps({"set_status": "BOGUS"}))
        assert _read_command_override(d) is None


if __name__ == "__main__":
    unittest.main()
