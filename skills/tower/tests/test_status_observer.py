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
    _has_write_tools,
    _has_any_tools,
    _read_command_override,
)

# Helper: mock the internal evidence functions instead of the lazy imports
def _patch_evidence(jsonl_age=None, events=None):
    """Patch _jsonl_age and _tail_jsonl_events for testing."""
    return (
        patch("status_observer._jsonl_age", return_value=jsonl_age),
        patch("status_observer._tail_jsonl_events", return_value=events or []),
    )


class TestDeriveStatus(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sortie = Path(self.tmpdir) / ".sortie"
        self.sortie.mkdir()

    def test_no_worktree_returns_idle(self):
        assert derive_status("") == "IDLE"

    def test_session_ended_returns_recovered(self):
        (self.sortie / "session-ended").touch()
        assert derive_status(self.tmpdir) == "RECOVERED"

    def test_command_override_consumed(self):
        cmd = {"set_status": "MAYDAY", "reason": "test", "source": "test"}
        (self.sortie / "command.json").write_text(json.dumps(cmd))
        assert derive_status(self.tmpdir) == "MAYDAY"
        assert not (self.sortie / "command.json").exists()

    def test_command_override_beats_session_ended(self):
        (self.sortie / "session-ended").touch()
        cmd = {"set_status": "AIRBORNE", "reason": "forced", "source": "xo"}
        (self.sortie / "command.json").write_text(json.dumps(cmd))
        assert derive_status(self.tmpdir) == "AIRBORNE"

    def test_no_jsonl_returns_idle(self):
        p1, p2 = _patch_evidence(jsonl_age=None)
        with p1, p2:
            assert derive_status(self.tmpdir) == "IDLE"

    def test_fresh_jsonl_with_write_tools_returns_airborne(self):
        events = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {}}
        ]}}]
        p1, p2 = _patch_evidence(jsonl_age=5.0, events=events)
        with p1, p2:
            assert derive_status(self.tmpdir) == "AIRBORNE"

    def test_fresh_jsonl_with_read_tools_returns_preflight(self):
        events = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {}}
        ]}}]
        p1, p2 = _patch_evidence(jsonl_age=5.0, events=events)
        with p1, p2:
            assert derive_status(self.tmpdir) == "PREFLIGHT"

    def test_warm_jsonl_airborne_goes_on_approach(self):
        """JSONL 60s old, was AIRBORNE → ON_APPROACH."""
        p1, p2 = _patch_evidence(jsonl_age=60.0)
        with p1, p2:
            assert derive_status(self.tmpdir, current_status="AIRBORNE") == "ON_APPROACH"

    def test_stale_jsonl_idle_stays_idle(self):
        """JSONL 300s old, was IDLE → stays IDLE."""
        p1, p2 = _patch_evidence(jsonl_age=300.0)
        with p1, p2:
            assert derive_status(self.tmpdir, current_status="IDLE") == "IDLE"

    def test_airborne_stays_during_thinking(self):
        """Agent AIRBORNE, JSONL fresh, last events are text only → stay AIRBORNE."""
        events = [{"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Let me think about this..."}
        ]}}]
        p1, p2 = _patch_evidence(jsonl_age=3.0, events=events)
        with p1, p2:
            assert derive_status(self.tmpdir, current_status="AIRBORNE") == "AIRBORNE"

    def test_session_ended_from_airborne(self):
        (self.sortie / "session-ended").touch()
        assert derive_status(self.tmpdir, current_status="AIRBORNE") == "RECOVERED"

    def test_no_jsonl_airborne_goes_mayday(self):
        """Was AIRBORNE but JSONL disappeared → MAYDAY."""
        p1, p2 = _patch_evidence(jsonl_age=None)
        with p1, p2:
            assert derive_status(self.tmpdir, current_status="AIRBORNE") == "MAYDAY"

    def test_warm_jsonl_on_approach_goes_idle(self):
        """JSONL 60s old, was ON_APPROACH (from previous session) → IDLE."""
        p1, p2 = _patch_evidence(jsonl_age=60.0)
        with p1, p2:
            assert derive_status(self.tmpdir, current_status="ON_APPROACH") == "IDLE"

    def test_stale_jsonl_on_approach_goes_idle(self):
        """JSONL 300s old, was ON_APPROACH → IDLE (previous session leftover)."""
        p1, p2 = _patch_evidence(jsonl_age=300.0)
        with p1, p2:
            assert derive_status(self.tmpdir, current_status="ON_APPROACH") == "IDLE"

    def test_fresh_jsonl_with_mixed_tools_returns_airborne(self):
        """Write tool present among reads → AIRBORNE."""
        events = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read", "input": {}}
            ]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Edit", "input": {}}
            ]}},
        ]
        p1, p2 = _patch_evidence(jsonl_age=2.0, events=events)
        with p1, p2:
            assert derive_status(self.tmpdir) == "AIRBORNE"


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

    def test_has_write_tools_true(self):
        events = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {}}
        ]}}]
        assert _has_write_tools(events) is True

    def test_has_write_tools_false(self):
        events = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {}}
        ]}}]
        assert _has_write_tools(events) is False

    def test_has_any_tools_true(self):
        events = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Grep", "input": {}}
        ]}}]
        assert _has_any_tools(events) is True

    def test_has_any_tools_false(self):
        events = [{"type": "assistant", "message": {"content": [
            {"type": "text", "text": "thinking"}
        ]}}]
        assert _has_any_tools(events) is False

    def test_command_override_reads_and_deletes(self):
        d = tempfile.mkdtemp()
        s = Path(d, ".sortie")
        s.mkdir()
        (s / "command.json").write_text(json.dumps({"set_status": "SAR"}))
        assert _read_command_override(d) == "SAR"
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
