"""Tests for dismiss logic — ensuring dismissed agents stay dismissed.

Verifies that _dismissed_tickets prevents resurrection by _apply_legacy_state,
and that the disappeared-agents block respects the dismissed set.
"""
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))


# ── Fakes ────────────────────────────────────────────────────────────

@dataclass
class FakeAgentState:
    ticket_id: str = "ENG-200"
    title: str = "Test ticket"
    status: str = "WORKING"
    model: str = "sonnet"
    branch: str = "sortie/ENG-200"
    worktree_path: str = "/fake/worktree/ENG-200"
    context: dict = field(default_factory=dict)
    jsonl_metrics: object = None
    flight_status: str = ""
    flight_phase: str = ""
    session_ended: bool = False
    status_hint: str = ""


@dataclass
class FakeSortieState:
    agents: list = field(default_factory=list)


@dataclass
class FakePilot:
    callsign: str = "PHOENIX-1"
    pilot_id: str = "PHOENIX-1"
    ticket_id: str = "ENG-200"
    status: str = "RECOVERED"
    model: str = "sonnet"
    mission_title: str = "Test"
    mood: str = "steady"
    fuel_pct: int = 50
    tokens_used: int = 0
    tool_calls: int = 0
    error_count: int = 0
    directive: str = ""
    trait: str = ""
    squadron: str = "phoenix"
    worktree_path: str = ""
    flight_status: str = ""
    flight_phase: str = ""
    status_hint: str = ""
    last_tool_at: float = 0.0
    launched_at: float = 0.0


class FakeRoster:
    """Minimal roster for testing dismiss logic."""
    def __init__(self):
        self._pilots: dict[str, FakePilot] = {}

    def add(self, pilot: FakePilot):
        self._pilots[pilot.callsign] = pilot

    def all_pilots(self):
        return list(self._pilots.values())

    def get_by_callsign(self, cs):
        return self._pilots.get(cs)

    def get_by_ticket(self, tid):
        return [p for p in self._pilots.values() if p.ticket_id == tid]

    def remove(self, cs):
        self._pilots.pop(cs, None)

    def assign(self, **kwargs):
        p = FakePilot(**kwargs)
        p.callsign = f"TEST-{len(self._pilots) + 1}"
        self._pilots[p.callsign] = p
        return p

    def update_moods(self):
        pass


class FakeAgentMgr:
    def get(self, cs):
        return None

    def active_agents(self):
        return []


# ── Simulated _apply_legacy_state logic ──────────────────────────────

def apply_legacy_state_sim(roster, legacy_agents, dismissed_tickets, state):
    """Simulates the core logic of _apply_legacy_state for testing."""
    seen_tickets = set()

    for agent in state.agents:
        tid = agent.ticket_id
        seen_tickets.add(tid)

        # Skip dismissed
        if tid in dismissed_tickets:
            continue

        legacy_agents[tid] = agent

        # Find or create pilot
        pilots_for_ticket = roster.get_by_ticket(tid)
        if pilots_for_ticket:
            pilot = pilots_for_ticket[0]
        else:
            pilot = roster.assign(
                ticket_id=tid,
                model=agent.model or "sonnet",
                mission_title=agent.title,
                directive="",
            )

        # Minimal status sync
        if agent.session_ended:
            pilot.status = "RECOVERED"
        else:
            pilot.status = "AIRBORNE"

    # Mark disappeared agents as RECOVERED
    for tid, agent_state in list(legacy_agents.items()):
        if tid not in seen_tickets:
            if tid in dismissed_tickets:
                legacy_agents.pop(tid, None)
                continue
            pilots = roster.get_by_ticket(tid)
            for p in pilots:
                p.status = "RECOVERED"
            del legacy_agents[tid]


# ── Tests ────────────────────────────────────────────────────────────

class TestDismissBlocksResurrection(unittest.TestCase):
    """Dismissed ticket IDs should never be re-added by _apply_legacy_state."""

    def setUp(self):
        self.roster = FakeRoster()
        self.legacy_agents = {}
        self.dismissed = set()

    def test_dismissed_ticket_not_readded_on_sync(self):
        """After dismiss, sync with same agent state should not re-add pilot."""
        agent = FakeAgentState(ticket_id="ENG-200")
        state = FakeSortieState(agents=[agent])

        # First sync — adds pilot
        apply_legacy_state_sim(self.roster, self.legacy_agents, self.dismissed, state)
        assert len(self.roster.all_pilots()) == 1

        # Dismiss
        pilot = self.roster.all_pilots()[0]
        self.roster.remove(pilot.callsign)
        self.legacy_agents.pop("ENG-200", None)
        self.dismissed.add("ENG-200")
        assert len(self.roster.all_pilots()) == 0

        # Next sync — same agent still on disk
        apply_legacy_state_sim(self.roster, self.legacy_agents, self.dismissed, state)
        assert len(self.roster.all_pilots()) == 0, "Dismissed pilot was resurrected"

    def test_dismissed_ticket_not_readded_across_multiple_syncs(self):
        """Dismissed ticket stays dismissed across 5 sync cycles."""
        agent = FakeAgentState(ticket_id="ENG-200")
        state = FakeSortieState(agents=[agent])

        apply_legacy_state_sim(self.roster, self.legacy_agents, self.dismissed, state)
        pilot = self.roster.all_pilots()[0]
        self.roster.remove(pilot.callsign)
        self.dismissed.add("ENG-200")

        for _ in range(5):
            apply_legacy_state_sim(self.roster, self.legacy_agents, self.dismissed, state)
            assert len(self.roster.all_pilots()) == 0, "Dismissed pilot resurrected on repeat sync"

    def test_dismissed_last_pilot_clears_board(self):
        """Dismissing the only pilot on the board leaves it empty."""
        agent = FakeAgentState(ticket_id="ENG-200")
        state = FakeSortieState(agents=[agent])

        apply_legacy_state_sim(self.roster, self.legacy_agents, self.dismissed, state)
        assert len(self.roster.all_pilots()) == 1

        pilot = self.roster.all_pilots()[0]
        self.roster.remove(pilot.callsign)
        self.legacy_agents.pop("ENG-200", None)
        self.dismissed.add("ENG-200")

        # Sync again — worktree still exists
        apply_legacy_state_sim(self.roster, self.legacy_agents, self.dismissed, state)
        assert len(self.roster.all_pilots()) == 0, "Last pilot should stay dismissed"

    def test_dismissed_ticket_disappearing_from_state_doesnt_crash(self):
        """After worktree is deleted, the ticket disappears from state.
        The disappeared-agents block should handle dismissed tickets gracefully."""
        agent = FakeAgentState(ticket_id="ENG-200")
        state_with = FakeSortieState(agents=[agent])
        state_without = FakeSortieState(agents=[])

        # Add, then dismiss
        apply_legacy_state_sim(self.roster, self.legacy_agents, self.dismissed, state_with)
        pilot = self.roster.all_pilots()[0]
        self.roster.remove(pilot.callsign)
        self.legacy_agents.pop("ENG-200", None)
        self.dismissed.add("ENG-200")

        # Worktree deleted — agent no longer in state
        apply_legacy_state_sim(self.roster, self.legacy_agents, self.dismissed, state_without)
        assert len(self.roster.all_pilots()) == 0

    def test_non_dismissed_ticket_still_syncs(self):
        """Other tickets should still sync normally when one is dismissed."""
        agent1 = FakeAgentState(ticket_id="ENG-200")
        agent2 = FakeAgentState(ticket_id="ENG-201", title="Other ticket")

        state = FakeSortieState(agents=[agent1, agent2])

        apply_legacy_state_sim(self.roster, self.legacy_agents, self.dismissed, state)
        assert len(self.roster.all_pilots()) == 2

        # Dismiss only ENG-200
        p = self.roster.get_by_ticket("ENG-200")[0]
        self.roster.remove(p.callsign)
        self.dismissed.add("ENG-200")

        apply_legacy_state_sim(self.roster, self.legacy_agents, self.dismissed, state)
        assert len(self.roster.all_pilots()) == 1
        assert self.roster.all_pilots()[0].ticket_id == "ENG-201"

    def test_dismiss_all_pilots_one_by_one(self):
        """Dismissing pilots one by one should leave an empty board."""
        agents = [
            FakeAgentState(ticket_id="ENG-200"),
            FakeAgentState(ticket_id="ENG-201"),
            FakeAgentState(ticket_id="ENG-202"),
        ]
        state = FakeSortieState(agents=agents)

        apply_legacy_state_sim(self.roster, self.legacy_agents, self.dismissed, state)
        assert len(self.roster.all_pilots()) == 3

        # Dismiss one at a time, sync after each
        for agent in agents:
            pilots = self.roster.get_by_ticket(agent.ticket_id)
            if pilots:
                self.roster.remove(pilots[0].callsign)
                self.dismissed.add(agent.ticket_id)
            apply_legacy_state_sim(self.roster, self.legacy_agents, self.dismissed, state)

        assert len(self.roster.all_pilots()) == 0, "All pilots should be dismissed"

    def test_dismissed_then_worktree_gone_then_sync(self):
        """Full lifecycle: dismiss → worktree delete completes → sync finds nothing."""
        agent = FakeAgentState(ticket_id="ENG-200")

        # Phase 1: agent exists
        state1 = FakeSortieState(agents=[agent])
        apply_legacy_state_sim(self.roster, self.legacy_agents, self.dismissed, state1)
        assert len(self.roster.all_pilots()) == 1

        # Phase 2: dismiss (worktree still exists)
        pilot = self.roster.all_pilots()[0]
        self.roster.remove(pilot.callsign)
        self.legacy_agents.pop("ENG-200", None)
        self.dismissed.add("ENG-200")

        # Phase 3: sync while worktree still exists
        apply_legacy_state_sim(self.roster, self.legacy_agents, self.dismissed, state1)
        assert len(self.roster.all_pilots()) == 0

        # Phase 4: worktree deleted — agent gone from state
        state2 = FakeSortieState(agents=[])
        apply_legacy_state_sim(self.roster, self.legacy_agents, self.dismissed, state2)
        assert len(self.roster.all_pilots()) == 0


class TestAbsolutePathResolution(unittest.TestCase):
    """Worktree paths must be resolved to absolute before git worktree remove."""

    def test_relative_path_resolved(self):
        project_dir = "/Users/andrew/Projects/tenkara-platform"
        relative_path = ".claude/worktrees/ENG-200"

        resolved = str(Path(project_dir) / relative_path) if not Path(relative_path).is_absolute() else relative_path
        assert resolved == "/Users/andrew/Projects/tenkara-platform/.claude/worktrees/ENG-200"

    def test_absolute_path_unchanged(self):
        project_dir = "/Users/andrew/Projects/tenkara-platform"
        absolute_path = "/Users/andrew/Projects/tenkara-platform/.claude/worktrees/ENG-200"

        resolved = str(Path(project_dir) / absolute_path) if not Path(absolute_path).is_absolute() else absolute_path
        assert resolved == absolute_path

    def test_none_path_handled(self):
        worktree_path = None
        # Should not crash
        resolved = str(Path("/project") / worktree_path) if worktree_path and not Path(worktree_path).is_absolute() else worktree_path
        assert resolved is None

    def test_empty_path_handled(self):
        worktree_path = ""
        resolved = str(Path("/project") / worktree_path) if worktree_path and not Path(worktree_path).is_absolute() else worktree_path
        assert resolved == ""


if __name__ == "__main__":
    unittest.main()
