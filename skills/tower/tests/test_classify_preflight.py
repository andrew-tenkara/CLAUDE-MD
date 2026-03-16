"""Tests for PREFLIGHT classification and compress_events()."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import pytest
from classify import (
    classify, compress_events, _categorize_tool,
    AIRBORNE, ON_APPROACH, HOLDING, PREFLIGHT,
)


# ── Helpers ───────────────────────────────────────────────────────────

def assistant(*tools: tuple[str, dict], text: str = "") -> dict:
    content = [{"type": "tool_use", "name": n, "input": i} for n, i in tools]
    if text:
        content.append({"type": "text", "text": text})
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def user_ok(msg: str = "ok") -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "is_error": False, "content": msg}
        ]},
    }


def user_error(msg: str = "error") -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "is_error": True, "content": msg}
        ]},
    }


# ── PREFLIGHT classification ─────────────────────────────────────────

class TestPreflightClassification:
    """PREFLIGHT: only reads, zero writes/airborne/on_approach in window."""

    def test_reads_only_returns_preflight(self):
        events = [
            assistant(("Read", {"file_path": "/src/main.ts"})),
            user_ok(),
            assistant(("Glob", {"pattern": "**/*.py"})),
            user_ok(),
            assistant(("Grep", {"pattern": "TODO"})),
            user_ok(),
        ]
        status, phase = classify(events)
        assert status == PREFLIGHT

    def test_single_read_returns_preflight(self):
        events = [assistant(("Read", {"file_path": "/README.md"})), user_ok()]
        status, _ = classify(events)
        assert status == PREFLIGHT

    def test_empty_events_returns_holding_not_preflight(self):
        """Empty window = HOLDING (idle), not PREFLIGHT."""
        status, _ = classify([])
        assert status == HOLDING

    def test_read_then_write_returns_airborne_not_preflight(self):
        """Once a write happens, it's AIRBORNE, not PREFLIGHT."""
        events = [
            assistant(("Read", {"file_path": "/src/main.ts"})),
            user_ok(),
            assistant(("Edit", {"file_path": "/src/main.ts", "old_string": "a", "new_string": "b"})),
            user_ok(),
        ]
        status, _ = classify(events)
        assert status == AIRBORNE

    def test_read_then_test_returns_on_approach_not_preflight(self):
        events = [
            assistant(("Read", {"file_path": "/src/main.ts"})),
            user_ok(),
            assistant(("Bash", {"command": "npm test"})),
            user_ok(),
        ]
        status, _ = classify(events)
        assert status == ON_APPROACH

    def test_multiple_read_tools_preflight(self):
        events = [
            assistant(("Read", {"file_path": "/a.ts"})),
            user_ok(),
            assistant(("WebFetch", {"url": "https://example.com"})),
            user_ok(),
            assistant(("Read", {"file_path": "/b.ts"})),
            user_ok(),
        ]
        status, _ = classify(events)
        assert status == PREFLIGHT

    def test_git_info_only_returns_preflight(self):
        """git log/status/diff are reads — should be PREFLIGHT."""
        events = [
            assistant(("Bash", {"command": "git status"})),
            user_ok(),
            assistant(("Bash", {"command": "git log --oneline -5"})),
            user_ok(),
        ]
        status, _ = classify(events)
        assert status == PREFLIGHT


# ── _categorize_tool ─────────────────────────────────────────────────

class TestCategorizeTool:
    def test_write_tool(self):
        action, target = _categorize_tool("Edit", {"file_path": "/src/foo.ts"})
        assert action == "write"
        assert target == "foo.ts"

    def test_read_tool(self):
        action, target = _categorize_tool("Read", {"file_path": "/src/bar.py"})
        assert action == "read"
        assert target == "bar.py"

    def test_bash_test(self):
        action, _ = _categorize_tool("Bash", {"command": "npm test"})
        assert action == "test"

    def test_bash_build(self):
        action, _ = _categorize_tool("Bash", {"command": "tsc --noEmit"})
        assert action == "build"

    def test_bash_git_finish(self):
        action, _ = _categorize_tool("Bash", {"command": "git commit -m 'fix'"})
        assert action == "git_finish"

    def test_bash_git_info(self):
        action, _ = _categorize_tool("Bash", {"command": "git status"})
        assert action == "git_info"

    def test_bash_install(self):
        action, _ = _categorize_tool("Bash", {"command": "npm install lodash"})
        assert action == "install"

    def test_bash_generic(self):
        action, _ = _categorize_tool("Bash", {"command": "ls -la"})
        assert action == "shell"

    def test_agent_tool(self):
        action, target = _categorize_tool("Agent", {"description": "search codebase"})
        assert action == "sub_agent"
        assert "search" in target

    def test_unknown_tool(self):
        action, target = _categorize_tool("FooBar", {})
        assert action == "other"
        assert target == "foobar"


# ── compress_events ──────────────────────────────────────────────────

class TestCompressEvents:
    def test_empty_events(self):
        result = compress_events([])
        assert result["recent_activity"] == []
        assert result["session_summary"]["total_writes"] == 0
        assert result["session_summary"]["total_reads"] == 0

    def test_structure(self):
        events = [
            assistant(("Read", {"file_path": "/a.ts"})),
            user_ok(),
            assistant(("Edit", {"file_path": "/a.ts", "old_string": "x", "new_string": "y"})),
            user_ok(),
        ]
        result = compress_events(events)
        assert "recent_activity" in result
        assert "session_summary" in result
        summary = result["session_summary"]
        assert summary["total_writes"] == 1
        assert summary["total_reads"] == 1
        assert summary["last_write_steps_ago"] >= 0

    def test_recent_activity_limited_to_15(self):
        events = []
        for i in range(20):
            events.append(assistant(("Read", {"file_path": f"/file{i}.ts"})))
            events.append(user_ok())
        result = compress_events(events)
        assert len(result["recent_activity"]) == 15

    def test_write_counting(self):
        events = [
            assistant(("Edit", {"file_path": "/a.ts", "old_string": "x", "new_string": "y"})),
            user_ok(),
            assistant(("Write", {"file_path": "/b.ts", "content": "hello"})),
            user_ok(),
            assistant(("Edit", {"file_path": "/c.ts", "old_string": "a", "new_string": "b"})),
            user_ok(),
        ]
        result = compress_events(events)
        assert result["session_summary"]["total_writes"] == 3

    def test_git_commit_tracking(self):
        events = [
            assistant(("Bash", {"command": "git commit -m 'fix: stuff'"})),
            user_ok(),
        ]
        result = compress_events(events)
        assert result["session_summary"]["last_git_commit_steps_ago"] >= 0

    def test_error_counting(self):
        events = [
            assistant(("Edit", {"file_path": "/a.ts", "old_string": "x", "new_string": "y"})),
            user_error("failed"),
            assistant(("Edit", {"file_path": "/a.ts", "old_string": "x", "new_string": "y"})),
            user_error("failed again"),
        ]
        result = compress_events(events)
        assert result["session_summary"]["errors_recent"] == 2

    def test_no_git_commit_returns_negative(self):
        events = [
            assistant(("Read", {"file_path": "/a.ts"})),
            user_ok(),
        ]
        result = compress_events(events)
        assert result["session_summary"]["last_git_commit_steps_ago"] == -1
