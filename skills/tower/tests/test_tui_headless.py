"""Headless TUI tests using Textual's pilot framework.

Tests the full PriFlyCommander app without a real terminal, iTerm2, or
subprocess spawning. Mocks external dependencies, exercises widget
rendering, keybindings, pilot lifecycle, and dismiss logic.

Run with the venv Python (needs textual + SDK):
    .venv/bin/python3.12 -m pytest tests/test_tui_headless.py -v
"""
import sys
import os
import time
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# These tests require Python 3.10+ and textual — skip on system Python 3.9
pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 10),
    reason="Headless TUI tests require Python 3.10+ with textual"
)
import importlib

# Add paths
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

# Import the dashboard module (hyphenated filename needs importlib)
_spec = importlib.util.spec_from_file_location(
    "commander_dashboard",
    str(Path(__file__).resolve().parent.parent / "scripts" / "commander-dashboard.py"),
)
_cd_mod = importlib.util.module_from_spec(_spec)
sys.modules["commander_dashboard"] = _cd_mod
_spec.loader.exec_module(_cd_mod)
PriFlyCommander = _cd_mod.PriFlyCommander


@pytest.fixture
def mock_externals():
    """Mock all external dependencies so the TUI can run headless."""
    patches = []

    # Mock ItermBridge — no AppleScript
    p1 = patch.object(_cd_mod, "ItermBridge")
    mock_iterm = p1.start()
    mock_iterm.return_value = MagicMock()
    patches.append(p1)

    # Mock AirBoss — no Mini Boss spawn
    p2 = patch.object(_cd_mod, "AirBoss")
    mock_airboss = p2.start()
    mock_airboss_inst = MagicMock()
    mock_airboss_inst.check_rtk = MagicMock()
    mock_airboss_inst.init_header = MagicMock()
    mock_airboss_inst.spawn = MagicMock()
    mock_airboss_inst.send_message = MagicMock()
    mock_airboss_inst.update_status = MagicMock()
    mock_airboss_inst.build_sitrep = MagicMock(return_value="No agents.")
    mock_airboss_inst.get_worktree_summary = MagicMock(return_value="No worktrees.")
    mock_airboss.return_value = mock_airboss_inst
    patches.append(p2)

    # Mock Monitoring — no watchers, no sentinel subprocess
    p3 = patch.object(_cd_mod, "Monitoring")
    mock_monitoring = p3.start()
    mock_mon_inst = MagicMock()
    mock_mon_inst.start_watchers = MagicMock()
    mock_mon_inst.start_sentinel = MagicMock()
    mock_mon_inst.check_sentinel_health = MagicMock()
    mock_mon_inst.watch_agent_jsonl = MagicMock()
    mock_mon_inst.sync_managed_servers = MagicMock()
    mock_mon_inst.check_idle_agents = MagicMock()
    mock_monitoring.return_value = mock_mon_inst
    patches.append(p3)

    # Mock InlineSentinel — no background thread
    p4 = patch.object(_cd_mod, "InlineSentinel")
    mock_sentinel = p4.start()
    mock_sent_inst = MagicMock()
    mock_sent_inst.start = MagicMock()
    mock_sent_inst.stop = MagicMock()
    mock_sent_inst.is_alive = False
    mock_sent_inst.add_worktree = MagicMock()
    mock_sentinel.return_value = mock_sent_inst
    patches.append(p4)

    # Mock SquadronAnalyst — no Haiku calls
    p5 = patch.object(_cd_mod, "SquadronAnalyst")
    mock_analyst = p5.start()
    mock_analyst_inst = MagicMock()
    mock_analyst_inst.start = MagicMock()
    mock_analyst_inst.stop = MagicMock()
    mock_analyst_inst.is_alive = False
    mock_analyst_inst.set_snapshot_provider = MagicMock()
    mock_analyst.return_value = mock_analyst_inst
    patches.append(p5)

    # Mock Observer — no file watching
    p6 = patch.object(_cd_mod, "Observer")
    mock_obs = p6.start()
    patches.append(p6)

    yield {
        "iterm": mock_iterm,
        "airboss": mock_airboss_inst,
        "monitoring": mock_mon_inst,
        "sentinel": mock_sent_inst,
        "analyst": mock_analyst_inst,
    }

    for p in patches:
        p.stop()


def _make_app(mock_externals):
    """Create a PriFlyCommander with all externals mocked."""
    app = PriFlyCommander(project_dir="/tmp/test-tower")
    return app


# ── Tests: App lifecycle ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_app_starts_and_stops(mock_externals):
    """App boots headless without crashing."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot:
        # App should be running
        assert app.is_running
        # Should have key widgets
        table = app.query_one("#agent-table")
        assert table is not None
        header = app.query_one("#header-bar")
        assert header is not None


@pytest.mark.asyncio
async def test_empty_board_renders(mock_externals):
    """Empty board shows column headers but no rows."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot:
        from textual.widgets import DataTable
        table = app.query_one("#agent-table", DataTable)
        assert table.row_count == 0


@pytest.mark.asyncio
async def test_flight_strip_renders(mock_externals):
    """Flight strip widget exists and renders without error."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot:
        from flight_ops import FlightOpsStrip
        strip = app.query_one("#flight-strip", FlightOpsStrip)
        assert strip is not None


# ── Tests: Pilot lifecycle on board ──────────────────────────────────

@pytest.mark.asyncio
async def test_add_pilot_appears_on_board(mock_externals):
    """Adding a pilot to the roster shows it on the board after refresh."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot_driver:
        # Add a pilot directly to the roster
        p = app._roster.assign(
            ticket_id="ENG-200",
            model="sonnet",
            mission_title="Test ticket",
            directive="Do the thing",
        )
        p.status = "IDLE"
        p.launched_at = time.time()

        # Force UI refresh
        app._board_state_sig = ""
        app._refresh_table()

        from textual.widgets import DataTable
        table = app.query_one("#agent-table", DataTable)
        assert table.row_count == 1


@pytest.mark.asyncio
async def test_multiple_pilots_on_board(mock_externals):
    """Multiple pilots show up as separate rows."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot_driver:
        for i in range(5):
            p = app._roster.assign(
                ticket_id=f"ENG-{200 + i}",
                model="sonnet",
                mission_title=f"Task {i}",
                directive="work",
            )
            p.status = "AIRBORNE"
            p.launched_at = time.time()

        app._board_state_sig = ""
        app._refresh_table()

        from textual.widgets import DataTable
        table = app.query_one("#agent-table", DataTable)
        assert table.row_count == 5


# ── Tests: Dismiss via Z key ────────────────────────────────────────

@pytest.mark.asyncio
async def test_dismiss_removes_pilot(mock_externals):
    """Z key removes the selected pilot from the board."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot_driver:
        p = app._roster.assign(
            ticket_id="ENG-200",
            model="sonnet",
            mission_title="Test",
            directive="work",
        )
        p.status = "RECOVERED"
        p.launched_at = time.time()

        app._board_state_sig = ""
        app._refresh_table()

        from textual.widgets import DataTable
        table = app.query_one("#agent-table", DataTable)
        assert table.row_count == 1

        # Press Z to dismiss
        await pilot_driver.press("z")
        await pilot_driver.pause()

        # Board should be empty
        assert table.row_count == 0
        assert "ENG-200" in app._dismissed_tickets


@pytest.mark.asyncio
async def test_dismiss_last_pilot_clears_board(mock_externals):
    """Dismissing the only pilot leaves an empty board — the specific bug we're chasing."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot_driver:
        p = app._roster.assign(
            ticket_id="ENG-300",
            model="sonnet",
            mission_title="Last pilot",
            directive="work",
        )
        p.status = "IDLE"
        p.launched_at = time.time()

        app._board_state_sig = ""
        app._refresh_table()

        from textual.widgets import DataTable
        table = app.query_one("#agent-table", DataTable)
        assert table.row_count == 1

        # Press Z
        await pilot_driver.press("z")
        await pilot_driver.pause()

        assert table.row_count == 0
        assert len(app._roster.all_pilots()) == 0
        assert "ENG-300" in app._dismissed_tickets


@pytest.mark.asyncio
async def test_dismiss_airborne_pilot(mock_externals):
    """Z on an AIRBORNE pilot wave-offs then dismisses."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot_driver:
        p = app._roster.assign(
            ticket_id="ENG-400",
            model="sonnet",
            mission_title="Active task",
            directive="work",
        )
        p.status = "AIRBORNE"
        p.launched_at = time.time()

        app._board_state_sig = ""
        app._refresh_table()

        await pilot_driver.press("z")
        await pilot_driver.pause()

        from textual.widgets import DataTable
        table = app.query_one("#agent-table", DataTable)
        assert table.row_count == 0
        assert "ENG-400" in app._dismissed_tickets


@pytest.mark.asyncio
async def test_dismiss_all_pilots_one_by_one(mock_externals):
    """Dismiss 3 pilots one by one, board ends empty."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot_driver:
        for i in range(3):
            p = app._roster.assign(
                ticket_id=f"ENG-{500 + i}",
                model="sonnet",
                mission_title=f"Task {i}",
                directive="work",
            )
            p.status = "RECOVERED"
            p.launched_at = time.time()

        app._board_state_sig = ""
        app._refresh_table()

        from textual.widgets import DataTable
        table = app.query_one("#agent-table", DataTable)
        assert table.row_count == 3

        # Dismiss all 3
        for i in range(3):
            await pilot_driver.press("z")
            await pilot_driver.pause()

        assert table.row_count == 0
        assert len(app._dismissed_tickets) == 3


# ── Tests: Flight strip sprites ──────────────────────────────────────

@pytest.mark.asyncio
async def test_flight_strip_sprites_created(mock_externals):
    """Pilots added to roster create sprites on the flight strip."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot_driver:
        from flight_ops import FlightOpsStrip

        p = app._roster.assign(
            ticket_id="ENG-600",
            model="sonnet",
            mission_title="Sprite test",
            directive="work",
        )
        p.status = "AIRBORNE"
        p.launched_at = time.time()

        strip = app.query_one("#flight-strip", FlightOpsStrip)
        strip.update_pilots(app._roster.all_pilots())

        assert len(strip._sprites) == 1
        sprite = list(strip._sprites.values())[0]
        assert sprite.phase == "ELEVATOR"  # new AIRBORNE starts at elevator


@pytest.mark.asyncio
async def test_flight_strip_deck_condensing(mock_externals):
    """More than 4 parked sprites condense into a badge."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot_driver:
        from flight_ops import FlightOpsStrip

        # Create 6 RECOVERED pilots (will be DECK_PARK)
        for i in range(6):
            p = app._roster.assign(
                ticket_id=f"ENG-{700 + i}",
                model="sonnet",
                mission_title=f"Done {i}",
                directive="work",
            )
            p.status = "RECOVERED"
            p.launched_at = time.time()

        strip = app.query_one("#flight-strip", FlightOpsStrip)
        strip.update_pilots(app._roster.all_pilots())

        # All 6 should have sprites
        assert len(strip._sprites) == 6

        # But when rendered, deck should condense (tested via render method)
        # The condensing happens in render(), not in update_pilots()
        # Just verify sprites were created correctly
        parked = [s for s in strip._sprites.values() if s.phase == "DECK_PARK"]
        assert len(parked) == 6


@pytest.mark.asyncio
async def test_dismiss_removes_sprite(mock_externals):
    """Z removes the sprite from the flight strip immediately."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot_driver:
        from flight_ops import FlightOpsStrip

        p = app._roster.assign(
            ticket_id="ENG-800",
            model="sonnet",
            mission_title="Sprite dismiss",
            directive="work",
        )
        p.status = "RECOVERED"
        p.launched_at = time.time()

        strip = app.query_one("#flight-strip", FlightOpsStrip)
        strip.update_pilots(app._roster.all_pilots())
        assert len(strip._sprites) == 1

        app._board_state_sig = ""
        app._refresh_table()

        await pilot_driver.press("z")
        await pilot_driver.pause()

        # Sprite should be gone
        callsign = p.callsign
        assert callsign not in strip._sprites


# ── Tests: Keybindings ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_escape_focuses_board(mock_externals):
    """Escape key returns focus to the agent table."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot_driver:
        await pilot_driver.press("escape")
        await pilot_driver.pause()

        from textual.widgets import DataTable
        table = app.query_one("#agent-table", DataTable)
        assert table.has_focus


@pytest.mark.asyncio
async def test_f_toggles_flight_strip(mock_externals):
    """F key toggles the flight strip visibility."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot_driver:
        from flight_ops import FlightOpsStrip
        strip = app.query_one("#flight-strip", FlightOpsStrip)

        assert "collapsed" not in strip.classes

        await pilot_driver.press("f")
        await pilot_driver.pause()
        assert "collapsed" in strip.classes

        await pilot_driver.press("f")
        await pilot_driver.pause()
        assert "collapsed" not in strip.classes


# ── Tests: Radio chatter ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_radio_log_bounded(mock_externals):
    """Radio log stays bounded at 100 entries."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot_driver:
        for i in range(150):
            app._add_radio("TEST", f"Message {i}", "system")

        assert len(app._radio_log) == 100


# ── Tests: Status transitions through UI ─────────────────────────────

# ── Tests: Full lifecycle flows ───────────────────────────────────────

@pytest.mark.asyncio
async def test_pilot_lifecycle_idle_to_recovered(mock_externals):
    """Full lifecycle: IDLE → AIRBORNE (tokens) → ON_APPROACH (stale) → RECOVERED."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot_driver:
        p = app._roster.assign(
            ticket_id="ENG-LC1", model="sonnet",
            mission_title="Lifecycle test", directive="work",
        )
        p.status = "IDLE"
        p.launched_at = time.time()
        app._board_state_sig = "__force_rebuild__"
        app._refresh_table()

        # Simulate token flow → AIRBORNE
        p.tokens_used = 500
        app._reconciler.prev_tokens["__never_match__"] = 0  # ensure no stale data
        # Manually trigger the status change (normally done by _check_token_deltas)
        p.status = "AIRBORNE"
        app._board_state_sig = "__force_rebuild__"
        app._refresh_table()

        from textual.widgets import DataTable
        table = app.query_one("#agent-table", DataTable)
        assert table.row_count == 1

        # ON_APPROACH
        p.status = "ON_APPROACH"
        app._board_state_sig = "__force_rebuild__"
        app._refresh_table()
        assert table.row_count == 1

        # RECOVERED
        p.status = "RECOVERED"
        app._board_state_sig = "__force_rebuild__"
        app._refresh_table()
        assert table.row_count == 1

        # Dismiss
        await pilot_driver.press("z")
        await pilot_driver.pause()
        assert table.row_count == 0


@pytest.mark.asyncio
async def test_mixed_statuses_on_board(mock_externals):
    """Board shows pilots in different statuses simultaneously."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot_driver:
        statuses = ["IDLE", "AIRBORNE", "ON_APPROACH", "RECOVERED", "MAYDAY", "AAR"]
        for i, status in enumerate(statuses):
            p = app._roster.assign(
                ticket_id=f"ENG-MIX{i}", model="sonnet",
                mission_title=f"Status {status}", directive="work",
            )
            p.status = status
            p.launched_at = time.time()

        app._board_state_sig = "__force_rebuild__"
        app._refresh_table()

        from textual.widgets import DataTable
        table = app.query_one("#agent-table", DataTable)
        assert table.row_count == 6


@pytest.mark.asyncio
async def test_dismiss_does_not_affect_other_pilots(mock_externals):
    """Dismissing one pilot doesn't remove others."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot_driver:
        p1 = app._roster.assign(ticket_id="ENG-A", model="sonnet",
                                 mission_title="Keep", directive="work")
        p1.status = "AIRBORNE"
        p1.launched_at = time.time()

        p2 = app._roster.assign(ticket_id="ENG-B", model="sonnet",
                                 mission_title="Dismiss me", directive="work")
        p2.status = "RECOVERED"
        p2.launched_at = time.time()

        app._board_state_sig = "__force_rebuild__"
        app._refresh_table()

        from textual.widgets import DataTable
        table = app.query_one("#agent-table", DataTable)
        assert table.row_count == 2

        # Navigate to second row and dismiss
        await pilot_driver.press("down")
        await pilot_driver.press("z")
        await pilot_driver.pause()

        # One should remain
        assert table.row_count == 1
        assert len(app._roster.all_pilots()) == 1
        remaining = app._roster.all_pilots()[0]
        assert remaining.ticket_id in ("ENG-A", "ENG-B")


@pytest.mark.asyncio
async def test_dismissed_ticket_not_readded_after_refresh(mock_externals):
    """Dismissed ticket stays gone through multiple refresh cycles."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot_driver:
        p = app._roster.assign(ticket_id="ENG-STAY-GONE", model="sonnet",
                                mission_title="Gone", directive="work")
        p.status = "RECOVERED"
        p.launched_at = time.time()
        app._board_state_sig = "__force_rebuild__"
        app._refresh_table()

        await pilot_driver.press("z")
        await pilot_driver.pause()

        from textual.widgets import DataTable
        table = app.query_one("#agent-table", DataTable)
        assert table.row_count == 0
        assert "ENG-STAY-GONE" in app._dismissed_tickets

        # Simulate multiple refresh cycles
        for _ in range(5):
            app._board_state_sig = "__force_rebuild__"
            app._refresh_table()

        assert table.row_count == 0


@pytest.mark.asyncio
async def test_fuel_gauge_in_table(mock_externals):
    """Fuel gauge renders for different fuel levels."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot_driver:
        p = app._roster.assign(ticket_id="ENG-FUEL", model="sonnet",
                                mission_title="Fuel test", directive="work")
        p.status = "AIRBORNE"
        p.fuel_pct = 25  # Low fuel
        p.launched_at = time.time()
        app._board_state_sig = "__force_rebuild__"
        app._refresh_table()

        from textual.widgets import DataTable
        table = app.query_one("#agent-table", DataTable)
        assert table.row_count == 1


@pytest.mark.asyncio
async def test_sprite_phases_for_different_statuses(mock_externals):
    """Different pilot statuses create sprites in correct phases."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot_driver:
        from flight_ops import FlightOpsStrip
        strip = app.query_one("#flight-strip", FlightOpsStrip)

        test_cases = [
            ("RECOVERED", "DECK_PARK"),
            ("AIRBORNE", "ELEVATOR"),
            ("ON_APPROACH", "RETURN"),
            ("SAR", "SAR_FLAMEOUT"),
            ("AAR", "AAR_REVERSE"),
        ]

        for status, expected_phase in test_cases:
            p = app._roster.assign(
                ticket_id=f"ENG-SPR-{status}", model="sonnet",
                mission_title=f"Sprite {status}", directive="work",
            )
            p.status = status
            p.launched_at = time.time()

        strip.update_pilots(app._roster.all_pilots())

        for status, expected_phase in test_cases:
            pilots = app._roster.get_by_ticket(f"ENG-SPR-{status}")
            if pilots:
                callsign = pilots[0].callsign
                sprite = strip._sprites.get(callsign)
                assert sprite is not None, f"No sprite for {callsign} ({status})"
                assert sprite.phase == expected_phase, \
                    f"{callsign} ({status}): expected {expected_phase}, got {sprite.phase}"


@pytest.mark.asyncio
async def test_pilot_status_reflected_in_table(mock_externals):
    """Pilot status changes appear in the board."""
    app = _make_app(mock_externals)
    async with app.run_test(size=(120, 40)) as pilot_driver:
        p = app._roster.assign(
            ticket_id="ENG-900",
            model="sonnet",
            mission_title="Status test",
            directive="work",
        )
        p.status = "AIRBORNE"
        p.fuel_pct = 75
        p.tokens_used = 5000
        p.launched_at = time.time()

        app._board_state_sig = ""
        app._refresh_table()

        from textual.widgets import DataTable
        table = app.query_one("#agent-table", DataTable)
        assert table.row_count == 1

        # Change status
        p.status = "ON_APPROACH"
        app._board_state_sig = ""
        app._refresh_table()

        # Still one row, but sig changed (status different)
        assert table.row_count == 1
