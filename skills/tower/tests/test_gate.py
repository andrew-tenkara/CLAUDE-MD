"""Tests for the Haiku transition gate."""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import pytest
from gate import gate_transition, GateResult, CONFIDENCE_THRESHOLD


# ── Helpers ───────────────────────────────────────────────────────────

def _mock_compressed():
    return {
        "recent_activity": [
            {"step": 0, "action": "read", "target": "main.ts"},
            {"step": 1, "action": "write", "target": "main.ts"},
        ],
        "session_summary": {
            "total_writes": 1,
            "total_reads": 3,
            "last_write_steps_ago": 2,
            "last_git_commit_steps_ago": -1,
            "errors_recent": 0,
        },
    }


def _make_response(approved=True, final_status="AIRBORNE", phase="coding",
                    confidence=0.9, reason="looks good"):
    """Build a mock Anthropic response with tool_use content."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "classify_transition"
    block.input = {
        "approved": approved,
        "final_status": final_status,
        "phase": phase,
        "confidence": confidence,
        "reason": reason,
    }
    response = MagicMock()
    response.content = [block]
    return response


# ── No API key → auto-approve ────────────────────────────────────────

class TestGracefulDegradation:
    def test_no_api_key_auto_approves(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            result = gate_transition("HOLDING", "AIRBORNE", 10.0, _mock_compressed())
        assert result.approved is True
        assert result.final_status == "AIRBORNE"
        assert "no API key" in result.reason

    def test_missing_api_key_env(self):
        with patch.dict("os.environ", {}, clear=True):
            # Remove ANTHROPIC_API_KEY entirely
            import os
            env = dict(os.environ)
            env.pop("ANTHROPIC_API_KEY", None)
            with patch.dict("os.environ", env, clear=True):
                result = gate_transition("HOLDING", "AIRBORNE", 10.0, _mock_compressed())
            assert result.approved is True

    def test_sdk_import_error_auto_approves(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test-123"}):
            with patch.dict("sys.modules", {"anthropic": None}):
                # Force ImportError
                import importlib
                with patch("builtins.__import__", side_effect=ImportError("no module")):
                    result = gate_transition("HOLDING", "AIRBORNE", 10.0, _mock_compressed())
        assert result.approved is True

    def test_api_error_auto_approves(self):
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("timeout")
        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test-123"}):
            with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
                result = gate_transition("HOLDING", "AIRBORNE", 10.0, _mock_compressed())
        assert result.approved is True
        assert "API error" in result.reason


# ── Approve / deny logic ─────────────────────────────────────────────

class TestApproveLogic:
    def test_approved_transition(self):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_response(
            approved=True, final_status="AIRBORNE", confidence=0.95, reason="first write detected"
        )
        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test-123"}):
            with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
                result = gate_transition("PREFLIGHT", "AIRBORNE", 30.0, _mock_compressed())

        assert result.approved is True
        assert result.final_status == "AIRBORNE"
        assert result.confidence == 0.95

    def test_denied_transition(self):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_response(
            approved=False, final_status="PREFLIGHT", confidence=0.85,
            reason="reads between writes, not actually idle"
        )
        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test-123"}):
            with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
                result = gate_transition("AIRBORNE", "HOLDING", 15.0, _mock_compressed())

        assert result.approved is False
        # Denied → final_status stays at current
        assert result.final_status == "AIRBORNE"

    def test_low_confidence_auto_denied(self):
        """Even if Haiku says approved, confidence < 0.7 → denied."""
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_response(
            approved=True, final_status="HOLDING", confidence=0.4,
            reason="unsure"
        )
        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test-123"}):
            with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
                result = gate_transition("AIRBORNE", "HOLDING", 5.0, _mock_compressed())

        assert result.approved is False
        assert result.confidence == 0.4
        assert "low confidence" in result.reason

    def test_confidence_at_threshold_is_approved(self):
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_response(
            approved=True, final_status="AIRBORNE", confidence=CONFIDENCE_THRESHOLD,
            reason="exactly at threshold"
        )
        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test-123"}):
            with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
                result = gate_transition("PREFLIGHT", "AIRBORNE", 10.0, _mock_compressed())

        assert result.approved is True

    def test_no_tool_use_in_response_auto_approves(self):
        """If Haiku returns text instead of tool_use → auto-approve."""
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "I think this should be approved"
        response = MagicMock()
        response.content = [text_block]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = response
        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test-123"}):
            with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
                result = gate_transition("HOLDING", "AIRBORNE", 10.0, _mock_compressed())

        assert result.approved is True
        assert "no tool_use" in result.reason


# ── GateResult dataclass ─────────────────────────────────────────────

class TestGateResult:
    def test_fields(self):
        r = GateResult(
            approved=True, final_status="AIRBORNE",
            phase="coding", confidence=0.9, reason="looks good"
        )
        assert r.approved is True
        assert r.final_status == "AIRBORNE"
        assert r.phase == "coding"
        assert r.confidence == 0.9
        assert r.reason == "looks good"
