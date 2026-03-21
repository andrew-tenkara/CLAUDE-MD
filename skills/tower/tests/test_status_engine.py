"""Tests for status_engine.py — StatusReconciler, status mapping, and transitions.

Run: python3 -m pytest tests/test_status_engine.py -v
"""
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from status_engine import (
    StatusReconciler,
    validate_transition,
    VALID_TRANSITIONS,
    _ctx_remaining,
    _map_flight_status,
    _derive_legacy_status,
    _clear_flight_status,
)


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
    def test_airborne_maps_to_in_flight(self):
        assert _map_flight_status("AIRBORNE") == "IN_FLIGHT"

    def test_holding_maps_to_on_deck(self):
        assert _map_flight_status("HOLDING") == "ON_DECK"

    def test_recovered_maps_to_on_deck(self):
        assert _map_flight_status("RECOVERED") == "ON_DECK"

    def test_preflight_maps_to_on_deck(self):
        assert _map_flight_status("PREFLIGHT") == "ON_DECK"

    def test_idle_maps_to_on_deck(self):
        assert _map_flight_status("IDLE") == "ON_DECK"

    def test_in_flight_passthrough(self):
        assert _map_flight_status("IN_FLIGHT") == "IN_FLIGHT"

    def test_on_deck_passthrough(self):
        assert _map_flight_status("ON_DECK") == "ON_DECK"

    def test_unknown(self):
        assert _map_flight_status("BOGUS") == ""

    def test_case_insensitive(self):
        assert _map_flight_status("airborne") == "IN_FLIGHT"


class TestDeriveLegacyStatus(unittest.TestCase):
    def test_working_with_fresh_context(self):
        agent = FakeAgentState(
            status="WORKING",
            context={"used_percentage": 50, "stale": False},
        )
        assert _derive_legacy_status(agent) == "IN_FLIGHT"

    def test_done_without_context(self):
        agent = FakeAgentState(status="DONE", context={})
        assert _derive_legacy_status(agent) == "RECOVERED"

    def test_working_stale_context(self):
        agent = FakeAgentState(
            status="WORKING",
            context={"used_percentage": 50, "stale": True},
        )
        assert _derive_legacy_status(agent) == "IN_FLIGHT"

    def test_unknown_status(self):
        agent = FakeAgentState(status="BOGUS", context={})
        assert _derive_legacy_status(agent) == "RECOVERED"


# ── Tests: StatusReconciler — token deltas ───────────────────────────

class TestTokenDeltas(unittest.TestCase):
    def setUp(self):
        self.reconciler = StatusReconciler(stale_threshold=3)
        self.radio = RadioLog()

    def test_on_deck_to_in_flight_on_first_tokens(self):
        pilot = FakePilot(callsign="VIPER-1", status="ON_DECK", tokens_used=100)
        # First frame: establish baseline
        self.reconciler.check_token_deltas([pilot], self.radio)
        # Second frame: tokens increase
        pilot.tokens_used = 200
        transitions = self.reconciler.check_token_deltas([pilot], self.radio)
        assert pilot.status == "IN_FLIGHT"
        assert self.radio.has("LAUNCH")

    def test_in_flight_to_on_approach_after_stale_threshold(self):
        pilot = FakePilot(callsign="VIPER-1", status="IN_FLIGHT", tokens_used=500)
        # Establish baseline
        self.reconciler.check_token_deltas([pilot], self.radio)
        # 3 stale frames (threshold=3)
        for _ in range(3):
            self.reconciler.check_token_deltas([pilot], self.radio)
        assert pilot.status == "ON_APPROACH"
        assert self.radio.has("ON APPROACH")

    def test_on_approach_back_to_in_flight_on_new_tokens(self):
        pilot = FakePilot(callsign="VIPER-1", status="IN_FLIGHT", tokens_used=500)
        self.reconciler.check_token_deltas([pilot], self.radio)
        # Go stale -> ON_APPROACH
        for _ in range(3):
            self.reconciler.check_token_deltas([pilot], self.radio)
        assert pilot.status == "ON_APPROACH"
        # Tokens resume
        pilot.tokens_used = 600
        self.reconciler.check_token_deltas([pilot], self.radio)
        assert pilot.status == "IN_FLIGHT"
        assert self.radio.has("WAVE OFF RTB")

    def test_on_approach_to_recovered_after_extended_stale(self):
        pilot = FakePilot(callsign="VIPER-1", status="IN_FLIGHT", tokens_used=500)
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

    def test_flight_status_skips_delta_inference(self):
        pilot = FakePilot(callsign="VIPER-1", status="IN_FLIGHT", tokens_used=500, flight_status="IN_FLIGHT")
        self.reconciler.check_token_deltas([pilot], self.radio)
        for _ in range(5):
            self.reconciler.check_token_deltas([pilot], self.radio)
        # Should stay IN_FLIGHT — flight_status is authoritative
        assert pilot.status == "IN_FLIGHT"

    def test_unknown_agent_pinned_to_recovered(self):
        pilot = FakePilot(
            callsign="VIPER-1", status="ON_DECK",
            mission_title="Unknown", ticket_id="Unknown",
            tokens_used=100,
        )
        self.reconciler.check_token_deltas([pilot], self.radio)
        assert pilot.status == "RECOVERED"

    def test_cleanup_removed_pilots(self):
        p1 = FakePilot(callsign="VIPER-1", status="IN_FLIGHT", tokens_used=100)
        p2 = FakePilot(callsign="VIPER-2", status="IN_FLIGHT", tokens_used=200)
        self.reconciler.check_token_deltas([p1, p2], self.radio)
        assert "VIPER-1" in self.reconciler.prev_tokens
        assert "VIPER-2" in self.reconciler.prev_tokens
        # Remove p1
        self.reconciler.check_token_deltas([p2], self.radio)
        assert "VIPER-1" not in self.reconciler.prev_tokens


# ── Tests: PREFLIGHT/ON_DECK whitelist in sentinel ───────────────────

class TestOnDeckHandling(unittest.TestCase):
    def setUp(self):
        self.reconciler = StatusReconciler(stale_threshold=3)
        self.radio = RadioLog()

    def test_on_deck_not_promoted_by_token_delta_when_flight_status_set(self):
        """ON_DECK agents with flight_status set should not be overridden."""
        pilot = FakePilot(
            callsign="VIPER-1", status="ON_DECK",
            tokens_used=0, flight_status="ON_DECK",
        )
        self.reconciler.check_token_deltas([pilot], self.radio)
        pilot.tokens_used = 100
        self.reconciler.check_token_deltas([pilot], self.radio)
        # flight_status is set -> delta inference skipped
        assert pilot.status == "ON_DECK"


# ── Tests: apply_transitions ─────────────────────────────────────────

class TestApplyTransitions(unittest.TestCase):
    def test_apply_fires_no_errors(self):
        """Smoke test — apply_transitions shouldn't crash even when sound/notify are no-ops."""
        from status_engine import StatusTransition
        reconciler = StatusReconciler()
        transitions = [
            StatusTransition(
                callsign="VIPER-1", old_status="IN_FLIGHT", new_status="RECOVERED",
                phase="done", message="on deck",
                sound_key="recovered",
                notify_title="USS TENKARA", notify_message="VIPER-1 on deck",
            ),
        ]
        # Should not raise
        reconciler.apply_transitions(transitions)


# ── Tests: Status transition validator ────────────────────────────────

class TestValidateTransition(unittest.TestCase):
    """Logical status transition enforcement."""

    def test_same_status_is_valid(self):
        assert validate_transition("IN_FLIGHT", "IN_FLIGHT") == "IN_FLIGHT"

    def test_in_flight_to_on_approach_is_valid(self):
        assert validate_transition("IN_FLIGHT", "ON_APPROACH") == "ON_APPROACH"

    def test_in_flight_to_recovered_goes_through_on_approach(self):
        """IN_FLIGHT can't jump directly to RECOVERED — must pass through ON_APPROACH."""
        result = validate_transition("IN_FLIGHT", "RECOVERED")
        assert result == "ON_APPROACH", f"Expected ON_APPROACH intermediate, got {result}"

    def test_on_approach_to_recovered_is_valid(self):
        assert validate_transition("ON_APPROACH", "RECOVERED") == "RECOVERED"

    def test_on_approach_to_in_flight_is_wave_off(self):
        assert validate_transition("ON_APPROACH", "IN_FLIGHT") == "IN_FLIGHT"

    def test_on_deck_to_in_flight_is_valid(self):
        assert validate_transition("ON_DECK", "IN_FLIGHT") == "IN_FLIGHT"

    def test_on_deck_to_on_approach_goes_through_in_flight(self):
        result = validate_transition("ON_DECK", "ON_APPROACH")
        assert result == "IN_FLIGHT"

    def test_on_deck_to_recovered_is_valid(self):
        assert validate_transition("ON_DECK", "RECOVERED") == "RECOVERED"

    def test_recovered_to_on_deck_is_valid(self):
        assert validate_transition("RECOVERED", "ON_DECK") == "ON_DECK"

    def test_case_insensitive(self):
        assert validate_transition("in_flight", "on_approach") == "ON_APPROACH"

    def test_all_statuses_have_transition_rules(self):
        """Every known status should have at least one valid transition."""
        known = {"ON_DECK", "IN_FLIGHT", "ON_APPROACH", "RECOVERED"}
        for status in known:
            assert status in VALID_TRANSITIONS, f"{status} has no transition rules"
            assert len(VALID_TRANSITIONS[status]) > 0, f"{status} has empty transitions"

    def test_bingo_notified_attribute_exists(self):
        """StatusReconciler should still have bingo_notified set."""
        reconciler = StatusReconciler()
        assert hasattr(reconciler, "bingo_notified")
        assert isinstance(reconciler.bingo_notified, set)


if __name__ == "__main__":
    unittest.main()
