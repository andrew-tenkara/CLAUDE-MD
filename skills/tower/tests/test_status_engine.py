"""Tests for status_engine.py — StatusReconciler, status mapping, and transitions.

Run: python3 -m pytest tests/test_status_engine.py -v
"""
import sys
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from status_engine import (
    StatusReconciler,
    _ctx_remaining,
    _map_flight_status,
    _derive_legacy_status,
    _clear_flight_status,
)
from constants import _FUEL_JUMP_THRESHOLD


# ── Fakes ────────────────────────────────────────────────────────────

@dataclass
class FakePilot:
    callsign: str
    status: str
    fuel_pct: int = 50
    tokens_used: int = 0
    ticket_id: str = "ENG-100"
    mission_title: str = "Fix the thing"
    flight_status: str = ""
    flight_phase: str = ""
    worktree_path: str = ""
    tool_calls: int = 0
    error_count: int = 0
    last_tool_at: float = 0.0
    launched_at: float = 0.0
    mood: str = "steady"
    status_hint: str = ""


@dataclass
class FakeAgentState:
    status: str = "WORKING"
    context: dict = field(default_factory=dict)
    jsonl_metrics: object = None
    flight_status: str = ""
    flight_phase: str = ""
    worktree_path: str = ""


@dataclass
class FakeJsonlMetrics:
    total_tokens: int = 0
    last_activity_at: str = ""
    total_tool_calls: int = 0
    error_count: int = 0


class RadioLog:
    """Captures radio messages for assertions."""
    def __init__(self):
        self.messages = []

    def __call__(self, callsign, msg, msg_type="normal"):
        self.messages.append((callsign, msg, msg_type))

    def has(self, substring):
        return any(substring in m[1] for m in self.messages)


# ── Tests: Status mapping functions ──────────────────────────────────

class TestCtxRemaining(unittest.TestCase):
    def test_normal(self):
        assert _ctx_remaining({"used_percentage": 70}) == 30

    def test_zero(self):
        assert _ctx_remaining({"used_percentage": 100}) == 0

    def test_unknown(self):
        assert _ctx_remaining({}) == 50

    def test_negative_clamp(self):
        assert _ctx_remaining({"used_percentage": 110}) == 0


class TestMapFlightStatus(unittest.TestCase):
    def test_airborne(self):
        assert _map_flight_status("AIRBORNE") == "AIRBORNE"

    def test_holding_maps_to_idle(self):
        assert _map_flight_status("HOLDING") == "IDLE"

    def test_recovered_maps_to_idle(self):
        # Agents can't set themselves RECOVERED
        assert _map_flight_status("RECOVERED") == "IDLE"

    def test_preflight(self):
        assert _map_flight_status("PREFLIGHT") == "PREFLIGHT"

    def test_unknown(self):
        assert _map_flight_status("BOGUS") == ""

    def test_case_insensitive(self):
        assert _map_flight_status("airborne") == "AIRBORNE"


class TestDeriveLegacyStatus(unittest.TestCase):
    def test_working_with_fresh_context(self):
        agent = FakeAgentState(
            status="WORKING",
            context={"used_percentage": 50, "stale": False},
        )
        assert _derive_legacy_status(agent) == "AIRBORNE"

    def test_done_without_context(self):
        agent = FakeAgentState(status="DONE", context={})
        assert _derive_legacy_status(agent) == "RECOVERED"

    def test_working_stale_context(self):
        agent = FakeAgentState(
            status="WORKING",
            context={"used_percentage": 50, "stale": True},
        )
        # Stale context — fall through to legacy map
        assert _derive_legacy_status(agent) == "AIRBORNE"  # WORKING → AIRBORNE

    def test_unknown_status(self):
        agent = FakeAgentState(status="BOGUS", context={})
        assert _derive_legacy_status(agent) == "MAYDAY"


# ── Tests: StatusReconciler — token deltas ───────────────────────────

class TestTokenDeltas(unittest.TestCase):
    def setUp(self):
        self.reconciler = StatusReconciler(stale_threshold=3)
        self.radio = RadioLog()

    def test_idle_to_airborne_on_first_tokens(self):
        pilot = FakePilot(callsign="VIPER-1", status="IDLE", tokens_used=100)
        # First frame: establish baseline
        self.reconciler.check_token_deltas([pilot], self.radio)
        # Second frame: tokens increase
        pilot.tokens_used = 200
        transitions = self.reconciler.check_token_deltas([pilot], self.radio)
        assert pilot.status == "AIRBORNE"
        assert self.radio.has("LAUNCH")

    def test_airborne_to_on_approach_after_stale_threshold(self):
        pilot = FakePilot(callsign="VIPER-1", status="AIRBORNE", tokens_used=500)
        # Establish baseline
        self.reconciler.check_token_deltas([pilot], self.radio)
        # 3 stale frames (threshold=3)
        for _ in range(3):
            self.reconciler.check_token_deltas([pilot], self.radio)
        assert pilot.status == "ON_APPROACH"
        assert self.radio.has("ON APPROACH")

    def test_on_approach_back_to_airborne_on_new_tokens(self):
        pilot = FakePilot(callsign="VIPER-1", status="AIRBORNE", tokens_used=500)
        self.reconciler.check_token_deltas([pilot], self.radio)
        # Go stale → ON_APPROACH
        for _ in range(3):
            self.reconciler.check_token_deltas([pilot], self.radio)
        assert pilot.status == "ON_APPROACH"
        # Tokens resume
        pilot.tokens_used = 600
        self.reconciler.check_token_deltas([pilot], self.radio)
        assert pilot.status == "AIRBORNE"
        assert self.radio.has("WAVE OFF RTB")

    def test_on_approach_to_recovered_after_extended_stale(self):
        pilot = FakePilot(callsign="VIPER-1", status="AIRBORNE", tokens_used=500)
        self.reconciler.check_token_deltas([pilot], self.radio)
        # threshold + 6 = 9 stale frames
        for _ in range(9):
            self.reconciler.check_token_deltas([pilot], self.radio)
        assert pilot.status == "RECOVERED"

    def test_recovered_not_interfered_with(self):
        pilot = FakePilot(callsign="VIPER-1", status="RECOVERED", tokens_used=500)
        self.reconciler.check_token_deltas([pilot], self.radio)
        pilot.tokens_used = 600
        self.reconciler.check_token_deltas([pilot], self.radio)
        # RECOVERED is terminal — should NOT promote back
        assert pilot.status == "RECOVERED"

    def test_aar_not_interfered_with(self):
        pilot = FakePilot(callsign="VIPER-1", status="AAR", tokens_used=500)
        self.reconciler.check_token_deltas([pilot], self.radio)
        assert pilot.status == "AAR"

    def test_flight_status_skips_delta_inference(self):
        pilot = FakePilot(callsign="VIPER-1", status="AIRBORNE", tokens_used=500, flight_status="AIRBORNE")
        self.reconciler.check_token_deltas([pilot], self.radio)
        for _ in range(5):
            self.reconciler.check_token_deltas([pilot], self.radio)
        # Should stay AIRBORNE — flight_status is authoritative
        assert pilot.status == "AIRBORNE"

    def test_unknown_agent_pinned_to_recovered(self):
        pilot = FakePilot(
            callsign="VIPER-1", status="IDLE",
            mission_title="Unknown", ticket_id="Unknown",
            tokens_used=100,
        )
        self.reconciler.check_token_deltas([pilot], self.radio)
        assert pilot.status == "RECOVERED"

    def test_cleanup_removed_pilots(self):
        p1 = FakePilot(callsign="VIPER-1", status="AIRBORNE", tokens_used=100)
        p2 = FakePilot(callsign="VIPER-2", status="AIRBORNE", tokens_used=200)
        self.reconciler.check_token_deltas([p1, p2], self.radio)
        assert "VIPER-1" in self.reconciler.prev_tokens
        assert "VIPER-2" in self.reconciler.prev_tokens
        # Remove p1
        self.reconciler.check_token_deltas([p2], self.radio)
        assert "VIPER-1" not in self.reconciler.prev_tokens


# ── Tests: StatusReconciler — compaction recovery ────────────────────

class TestCompactionRecovery(unittest.TestCase):
    def setUp(self):
        self.reconciler = StatusReconciler()
        self.radio = RadioLog()

    def test_aar_completes_on_fuel_jump(self):
        pilot = FakePilot(callsign="VIPER-1", status="AAR", fuel_pct=20)
        # Establish baseline
        self.reconciler.check_compaction_recovery([pilot], self.radio)
        # Fuel jumps
        pilot.fuel_pct = 20 + _FUEL_JUMP_THRESHOLD + 1
        transitions = self.reconciler.check_compaction_recovery([pilot], self.radio)
        assert pilot.status == "AIRBORNE"
        assert self.radio.has("AAR COMPLETE")
        assert len(transitions) == 1
        assert transitions[0].old_status == "AAR"

    def test_aar_no_change_without_fuel_jump(self):
        pilot = FakePilot(callsign="VIPER-1", status="AAR", fuel_pct=20)
        self.reconciler.check_compaction_recovery([pilot], self.radio)
        pilot.fuel_pct = 22  # Small increase, below threshold
        self.reconciler.check_compaction_recovery([pilot], self.radio)
        assert pilot.status == "AAR"

    def test_sar_starts_with_flameout_message(self):
        pilot = FakePilot(callsign="VIPER-1", status="SAR", fuel_pct=0)
        self.reconciler.check_compaction_recovery([pilot], self.radio)
        assert self.radio.has("FLAMEOUT")
        assert "VIPER-1" in self.reconciler.sar_started

    def test_sar_recovers_after_delay_and_fuel(self):
        pilot = FakePilot(callsign="VIPER-1", status="SAR", fuel_pct=0)
        # Start SAR
        self.reconciler.check_compaction_recovery([pilot], self.radio)
        # Simulate time passing
        self.reconciler.sar_started["VIPER-1"] = time.time() - 20  # well past delay
        pilot.fuel_pct = 60
        transitions = self.reconciler.check_compaction_recovery([pilot], self.radio)
        assert pilot.status == "AIRBORNE"
        assert self.radio.has("SAR COMPLETE")

    def test_bingo_notified_cleared_on_recovery(self):
        self.reconciler.bingo_notified.add("VIPER-1")
        pilot = FakePilot(callsign="VIPER-1", status="AAR", fuel_pct=20)
        self.reconciler.check_compaction_recovery([pilot], self.radio)
        pilot.fuel_pct = 60
        self.reconciler.check_compaction_recovery([pilot], self.radio)
        assert "VIPER-1" not in self.reconciler.bingo_notified

    def test_cleanup_stale_sar(self):
        self.reconciler.sar_started["GHOST-1"] = time.time()
        self.reconciler.prev_fuel["GHOST-1"] = 50
        pilot = FakePilot(callsign="VIPER-1", status="AIRBORNE", fuel_pct=50)
        self.reconciler.check_compaction_recovery([pilot], self.radio)
        assert "GHOST-1" not in self.reconciler.sar_started
        assert "GHOST-1" not in self.reconciler.prev_fuel


# ── Tests: StatusReconciler — PREFLIGHT whitelist in sentinel ────────

class TestPreflightHandling(unittest.TestCase):
    def setUp(self):
        self.reconciler = StatusReconciler(stale_threshold=3)
        self.radio = RadioLog()

    def test_preflight_not_promoted_by_token_delta(self):
        """PREFLIGHT agents with flight_status set should not be overridden."""
        pilot = FakePilot(
            callsign="VIPER-1", status="IDLE",
            tokens_used=0, flight_status="PREFLIGHT",
        )
        self.reconciler.check_token_deltas([pilot], self.radio)
        pilot.tokens_used = 100
        self.reconciler.check_token_deltas([pilot], self.radio)
        # flight_status is set → delta inference skipped
        assert pilot.status == "IDLE"


# ── Tests: apply_transitions ─────────────────────────────────────────

class TestApplyTransitions(unittest.TestCase):
    def test_apply_fires_no_errors(self):
        """Smoke test — apply_transitions shouldn't crash even when sound/notify are no-ops."""
        from status_engine import StatusTransition
        reconciler = StatusReconciler()
        transitions = [
            StatusTransition(
                callsign="VIPER-1", old_status="AIRBORNE", new_status="RECOVERED",
                phase="done", message="on deck",
                sound_key="recovered",
                notify_title="USS TENKARA", notify_message="VIPER-1 on deck",
            ),
        ]
        # Should not raise
        reconciler.apply_transitions(transitions)


if __name__ == "__main__":
    unittest.main()
