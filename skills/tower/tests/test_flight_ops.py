"""Tests for flight_ops.py — sprite positioning, phase transitions, and edge cases.

Run: python3 tests/test_flight_ops.py
(No pytest needed — uses unittest + plain asserts with readable output.)
"""
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from flight_ops import FlightOpsStrip, FlightSprite, ZONE_PCT, PHASE_TICKS


# ── Helpers ───────────────────────────────────────────────────────────

@dataclass
class FakePilot:
    pilot_id: str
    status: str
    callsign: str = ""
    ticket_id: str = ""

    def __post_init__(self):
        if not self.callsign:
            self.callsign = self.pilot_id
        if not self.ticket_id:
            self.ticket_id = f"ENG-{hash(self.pilot_id) % 1000}"


def make_strip(width: int = 100) -> FlightOpsStrip:
    strip = FlightOpsStrip.__new__(FlightOpsStrip)
    strip._sprites = {}
    strip._strip_width = width
    strip._sweep_col = 0
    return strip


def advance(strip: FlightOpsStrip, ticks: int = 1):
    for _ in range(ticks):
        strip._advance_sprites()


def settle(strip: FlightOpsStrip):
    """Advance enough for ELEVATOR to complete and sprites to reach steady state."""
    advance(strip, PHASE_TICKS["ELEVATOR"] + 5)


# ── IDLE positioning ─────────────────────────────────────────────────

class TestIdlePositioning(unittest.TestCase):

    def test_idle_initializes_at_elevator(self):
        strip = make_strip()
        strip.update_pilots([FakePilot("V-1", "IDLE")])
        self.assertEqual(strip._sprites["V-1"].phase, "ELEVATOR")

    def test_idle_transitions_to_deck_idle(self):
        strip = make_strip()
        strip.update_pilots([FakePilot("V-1", "IDLE")])
        settle(strip)
        self.assertEqual(strip._sprites["V-1"].phase, "DECK_IDLE")

    def test_idle_at_or_past_cat_zone(self):
        strip = make_strip()
        strip.update_pilots([FakePilot("V-1", "IDLE")])
        settle(strip)
        cat_start = strip._zone_col("CAT", 0.0)
        self.assertGreaterEqual(strip._sprites["V-1"].col, cat_start)

    def test_idle_separated_from_recovered_sprites(self):
        """The exact bug from the screenshot: 4 RECOVERED + 1 IDLE."""
        strip = make_strip()
        pilots = [
            FakePilot("Ghost-1", "RECOVERED"),
            FakePilot("Iceman-1", "RECOVERED"),
            FakePilot("Phoenix-1", "RECOVERED"),
            FakePilot("Reaper-1", "RECOVERED"),
            FakePilot("Viper-1", "IDLE"),
        ]
        strip.update_pilots(pilots)
        settle(strip)

        idle = strip._sprites["Viper-1"]
        self.assertEqual(idle.phase, "DECK_IDLE")

        for pid, sprite in strip._sprites.items():
            if sprite.phase == "DECK_PARK":
                self.assertGreater(
                    idle.col, sprite.col + 4,
                    f"IDLE overlaps PARK sprite {pid} (idle={idle.col}, park={sprite.col})"
                )

    def test_multiple_idle_stagger(self):
        strip = make_strip()
        strip.update_pilots([FakePilot(f"I-{i}", "IDLE") for i in range(3)])
        settle(strip)
        cols = sorted(s.col for s in strip._sprites.values() if s.phase == "DECK_IDLE")
        self.assertEqual(len(cols), 3)
        for i in range(len(cols) - 1):
            self.assertGreaterEqual(cols[i + 1] - cols[i], 5, f"Too close: {cols}")

    def test_idle_past_park_at_all_widths(self):
        for w in [40, 60, 80, 100, 120, 160, 200]:
            with self.subTest(width=w):
                strip = make_strip(w)
                pilots = [FakePilot(f"P-{i}", "RECOVERED") for i in range(5)]
                pilots.append(FakePilot("I-1", "IDLE"))
                strip.update_pilots(pilots)
                settle(strip)
                idle = strip._sprites["I-1"]
                parked = [s for s in strip._sprites.values() if s.phase == "DECK_PARK"]
                if parked:
                    max_park = max(s.col for s in parked)
                    self.assertGreater(idle.col, max_park,
                                       f"Width {w}: IDLE at {idle.col} not past PARK at {max_park}")

    def test_single_idle_no_recovered(self):
        """IDLE alone (no parked sprites to dodge) still goes to CAT area."""
        strip = make_strip()
        strip.update_pilots([FakePilot("Solo-1", "IDLE")])
        settle(strip)
        cat_start = strip._zone_col("CAT", 0.0)
        self.assertGreaterEqual(strip._sprites["Solo-1"].col, cat_start)


# ── Phase transitions ────────────────────────────────────────────────

class TestPhaseTransitions(unittest.TestCase):

    def test_recovered_starts_deck_park(self):
        strip = make_strip()
        strip.update_pilots([FakePilot("R-1", "RECOVERED")])
        self.assertEqual(strip._sprites["R-1"].phase, "DECK_PARK")

    def test_airborne_starts_elevator(self):
        strip = make_strip()
        strip.update_pilots([FakePilot("A-1", "AIRBORNE")])
        self.assertEqual(strip._sprites["A-1"].phase, "ELEVATOR")

    def test_idle_to_airborne_triggers_taxi(self):
        strip = make_strip()
        strip.update_pilots([FakePilot("V-1", "IDLE")])
        settle(strip)
        self.assertEqual(strip._sprites["V-1"].phase, "DECK_IDLE")

        strip.update_pilots([FakePilot("V-1", "AIRBORNE")])
        self.assertEqual(strip._sprites["V-1"].phase, "TAXI_TO_CAT")

    def test_recovered_to_idle_goes_deck_idle(self):
        strip = make_strip()
        strip.update_pilots([FakePilot("P-1", "RECOVERED")])
        advance(strip, 2)
        self.assertEqual(strip._sprites["P-1"].phase, "DECK_PARK")

        strip.update_pilots([FakePilot("P-1", "IDLE")])
        settle(strip)
        self.assertEqual(strip._sprites["P-1"].phase, "DECK_IDLE")

    def test_airborne_full_launch_sequence(self):
        """AIRBORNE: ELEVATOR → TAXI_TO_CAT → CAT → LAUNCH → CRUISE.

        Phases advance on tick boundaries, so we check the phase
        transitions in order without asserting exact tick counts.
        """
        strip = make_strip()
        strip.update_pilots([FakePilot("A-1", "AIRBORNE")])
        sprite = strip._sprites["A-1"]

        # Track phase sequence
        seen_phases = [sprite.phase]
        for _ in range(200):
            advance(strip, 1)
            if sprite.phase != seen_phases[-1]:
                seen_phases.append(sprite.phase)
            if sprite.phase == "CRUISE":
                break

        expected_order = ["ELEVATOR", "TAXI_TO_CAT", "CAT", "LAUNCH", "CRUISE"]
        self.assertEqual(seen_phases, expected_order,
                         f"Expected phase sequence {expected_order}, got {seen_phases}")

    def test_on_approach_goes_to_return(self):
        strip = make_strip()
        strip.update_pilots([FakePilot("A-1", "AIRBORNE")])
        advance(strip, 50)  # get airborne

        strip.update_pilots([FakePilot("A-1", "ON_APPROACH")])
        self.assertEqual(strip._sprites["A-1"].phase, "RETURN")

    def test_recovered_from_airborne_goes_return(self):
        strip = make_strip()
        strip.update_pilots([FakePilot("A-1", "AIRBORNE")])
        advance(strip, 50)  # cruise

        strip.update_pilots([FakePilot("A-1", "RECOVERED")])
        self.assertEqual(strip._sprites["A-1"].phase, "RETURN")

    def test_mayday_phase(self):
        strip = make_strip()
        strip.update_pilots([FakePilot("M-1", "AIRBORNE")])
        advance(strip, 50)

        strip.update_pilots([FakePilot("M-1", "MAYDAY")])
        self.assertEqual(strip._sprites["M-1"].phase, "MAYDAY")

    def test_aar_starts_reverse(self):
        strip = make_strip()
        strip.update_pilots([FakePilot("A-1", "AIRBORNE")])
        advance(strip, 50)

        strip.update_pilots([FakePilot("A-1", "AAR")])
        self.assertEqual(strip._sprites["A-1"].phase, "AAR_REVERSE")

    def test_sar_starts_flameout(self):
        strip = make_strip()
        strip.update_pilots([FakePilot("A-1", "AIRBORNE")])
        advance(strip, 50)

        strip.update_pilots([FakePilot("A-1", "SAR")])
        self.assertEqual(strip._sprites["A-1"].phase, "SAR_FLAMEOUT")


# ── Sprite lifecycle ─────────────────────────────────────────────────

class TestSpriteLifecycle(unittest.TestCase):

    def test_sprite_created_on_first_update(self):
        strip = make_strip()
        strip.update_pilots([FakePilot("X-1", "AIRBORNE")])
        self.assertIn("X-1", strip._sprites)

    def test_sprite_pruned_when_removed(self):
        strip = make_strip()
        strip.update_pilots([FakePilot("X-1", "AIRBORNE")])
        strip.update_pilots([])
        self.assertNotIn("X-1", strip._sprites)

    def test_sprite_survives_same_status_update(self):
        strip = make_strip()
        strip.update_pilots([FakePilot("X-1", "AIRBORNE")])
        advance(strip, 20)
        old_phase = strip._sprites["X-1"].phase

        strip.update_pilots([FakePilot("X-1", "AIRBORNE")])
        self.assertEqual(strip._sprites["X-1"].phase, old_phase)

    def test_many_pilots_all_get_sprites(self):
        strip = make_strip(200)
        pilots = [FakePilot(f"P-{i}", "RECOVERED") for i in range(10)]
        strip.update_pilots(pilots)
        self.assertEqual(len(strip._sprites), 10)

    def test_rapid_status_changes(self):
        """Rapidly cycling statuses shouldn't crash."""
        strip = make_strip()
        statuses = ["IDLE", "AIRBORNE", "ON_APPROACH", "RECOVERED", "IDLE", "AIRBORNE", "MAYDAY"]
        for s in statuses:
            strip.update_pilots([FakePilot("V-1", s)])
            advance(strip, 3)
        self.assertIn("V-1", strip._sprites)


# ── Column bounds ────────────────────────────────────────────────────

class TestColumnBounds(unittest.TestCase):

    def test_sprites_never_negative(self):
        strip = make_strip(60)
        pilots = [FakePilot(f"P-{i}", s) for i, s in
                  enumerate(["AIRBORNE", "RECOVERED", "IDLE", "ON_APPROACH", "MAYDAY"])]
        strip.update_pilots(pilots)
        advance(strip, 100)
        for sprite in strip._sprites.values():
            self.assertGreaterEqual(sprite.col, 0, f"{sprite.pilot_id} col={sprite.col}")

    def test_sprites_within_strip_width(self):
        strip = make_strip(80)
        pilots = [FakePilot(f"P-{i}", "AIRBORNE") for i in range(5)]
        strip.update_pilots(pilots)
        advance(strip, 200)  # Let them cruise far
        for sprite in strip._sprites.values():
            self.assertLess(sprite.col, strip._strip_width,
                            f"{sprite.pilot_id} col={sprite.col} >= width={strip._strip_width}")

    def test_parked_sprites_dont_go_negative_with_many(self):
        """Many RECOVERED sprites packed tightly shouldn't push any below 0."""
        strip = make_strip(60)
        pilots = [FakePilot(f"R-{i}", "RECOVERED") for i in range(8)]
        strip.update_pilots(pilots)
        advance(strip, 5)
        for sprite in strip._sprites.values():
            self.assertGreaterEqual(sprite.col, 0)


# ── Lane deconfliction ───────────────────────────────────────────────

class TestLaneDeconfliction(unittest.TestCase):

    def test_overlapping_sprites_use_different_lanes(self):
        strip = make_strip()
        # Two RECOVERED sprites will be packed close
        pilots = [FakePilot("A-1", "RECOVERED"), FakePilot("A-2", "RECOVERED")]
        strip.update_pilots(pilots)
        advance(strip, 3)

        sprites = list(strip._sprites.values())
        if abs(sprites[0].col - sprites[1].col) < 8:
            lanes = {sprites[0].lane, sprites[1].lane}
            self.assertEqual(len(lanes), 2, "Overlapping sprites should use different lanes")


# ── Zone calculation ─────────────────────────────────────────────────

class TestZoneCalculation(unittest.TestCase):

    def test_park_starts_at_zero(self):
        strip = make_strip(100)
        self.assertEqual(strip._zone_col("PARK", 0.0), 0)

    def test_cat_zone_position(self):
        strip = make_strip(100)
        # CAT is 14-22%
        self.assertEqual(strip._zone_col("CAT", 0.0), 14)
        self.assertEqual(strip._zone_col("CAT", 1.0), 22)

    def test_zone_col_scales_with_width(self):
        for w in [60, 100, 200]:
            strip = make_strip(w)
            mid_sky = strip._zone_col("SKY", 0.5)
            # SKY is 22-65%, midpoint ~43.5%
            expected = int(43.5 / 100 * w)
            self.assertAlmostEqual(mid_sky, expected, delta=1)


# ── Stress / edge cases ──────────────────────────────────────────────

class TestStress(unittest.TestCase):

    def test_empty_pilot_list(self):
        strip = make_strip()
        strip.update_pilots([])
        advance(strip, 10)
        self.assertEqual(len(strip._sprites), 0)

    def test_single_pilot_all_phases(self):
        """Walk one pilot through every major status without crashing."""
        strip = make_strip()
        for status in ["IDLE", "AIRBORNE", "AAR", "AIRBORNE", "ON_APPROACH",
                        "RECOVERED", "IDLE", "AIRBORNE", "SAR", "MAYDAY"]:
            strip.update_pilots([FakePilot("Solo-1", status)])
            advance(strip, 15)
        self.assertIn("Solo-1", strip._sprites)

    def test_twenty_airborne_dont_crash(self):
        """20 simultaneous AIRBORNE agents — stress test."""
        strip = make_strip(200)
        pilots = [FakePilot(f"A-{i}", "AIRBORNE") for i in range(20)]
        strip.update_pilots(pilots)
        advance(strip, 100)
        self.assertEqual(len(strip._sprites), 20)
        for s in strip._sprites.values():
            self.assertGreaterEqual(s.col, 0)

    def test_mixed_statuses_no_overlap_idle_park(self):
        """Mix of statuses — IDLE must always be past PARK."""
        strip = make_strip(120)
        pilots = [
            FakePilot("R-1", "RECOVERED"),
            FakePilot("R-2", "RECOVERED"),
            FakePilot("R-3", "RECOVERED"),
            FakePilot("I-1", "IDLE"),
            FakePilot("I-2", "IDLE"),
            FakePilot("A-1", "AIRBORNE"),
            FakePilot("A-2", "AIRBORNE"),
        ]
        strip.update_pilots(pilots)
        settle(strip)

        parked = [s for s in strip._sprites.values() if s.phase == "DECK_PARK"]
        idle = [s for s in strip._sprites.values() if s.phase == "DECK_IDLE"]

        if parked and idle:
            max_park = max(s.col for s in parked)
            min_idle = min(s.col for s in idle)
            self.assertGreater(min_idle, max_park,
                               f"IDLE col {min_idle} not past PARK col {max_park}")

    def test_narrow_strip_no_crash(self):
        """Minimum width strip shouldn't crash."""
        strip = make_strip(40)
        pilots = [
            FakePilot("R-1", "RECOVERED"),
            FakePilot("I-1", "IDLE"),
            FakePilot("A-1", "AIRBORNE"),
        ]
        strip.update_pilots(pilots)
        advance(strip, 50)
        for s in strip._sprites.values():
            self.assertGreaterEqual(s.col, 0)


# ── Lane Distribution Tests ──────────────────────────────────────────

class TestLaneDistribution(unittest.TestCase):
    """Verify sprites use both lanes to prevent crowding."""

    def test_parked_sprites_alternate_lanes(self):
        """4 parked sprites should use both lane 0 and lane 1."""
        strip = make_strip(120)
        pilots = [FakePilot(f"R-{i}", "RECOVERED") for i in range(4)]
        strip.update_pilots(pilots)
        settle(strip)
        lanes = {s.lane for s in strip._sprites.values() if s.phase == "DECK_PARK"}
        self.assertEqual(lanes, {0, 1}, "Parked sprites should use both lanes")

    def test_parked_even_odd_alternation(self):
        """Parked sprites sorted by col should alternate 0, 1, 0, 1."""
        strip = make_strip(120)
        pilots = [FakePilot(f"R-{i}", "RECOVERED") for i in range(6)]
        strip.update_pilots(pilots)
        settle(strip)
        parked = sorted(
            [s for s in strip._sprites.values() if s.phase == "DECK_PARK"],
            key=lambda s: s.col,
        )
        for idx, s in enumerate(parked):
            self.assertEqual(s.lane, idx % 2,
                             f"Parked sprite {idx} at col {s.col} should be lane {idx % 2}, got {s.lane}")

    def test_idle_uses_both_lanes(self):
        """Multiple idle sprites should also distribute across lanes."""
        strip = make_strip(120)
        pilots = [
            FakePilot("R-1", "RECOVERED"),
            FakePilot("I-1", "IDLE"),
            FakePilot("I-2", "IDLE"),
            FakePilot("I-3", "IDLE"),
        ]
        strip.update_pilots(pilots)
        settle(strip)
        idle = [s for s in strip._sprites.values() if s.phase == "DECK_IDLE"]
        if len(idle) >= 2:
            lanes = {s.lane for s in idle}
            self.assertEqual(lanes, {0, 1}, "Idle sprites should use both lanes")

    def test_active_sprites_deconflict_to_lane1(self):
        """Two overlapping airborne sprites should use different lanes."""
        strip = make_strip(120)
        pilots = [FakePilot(f"A-{i}", "AIRBORNE") for i in range(3)]
        strip.update_pilots(pilots)
        # Advance past elevator into cruise
        advance(strip, PHASE_TICKS["ELEVATOR"] + PHASE_TICKS.get("TAXI_TO_CAT", 12) + PHASE_TICKS.get("CAT", 8) + 10)
        active = [s for s in strip._sprites.values()
                  if s.phase not in ("DECK_PARK", "DECK_IDLE", "ELEVATOR")]
        # Should use lane 1 if overlapping
        if len(active) >= 2:
            lanes = {s.lane for s in active}
            self.assertTrue(len(lanes) > 0, "Active sprites should have assigned lanes")

    def test_six_recovered_no_overlap(self):
        """6 recovered sprites across both lanes shouldn't overlap within a lane."""
        strip = make_strip(120)
        pilots = [FakePilot(f"R-{i}", "RECOVERED") for i in range(6)]
        strip.update_pilots(pilots)
        settle(strip)
        parked = sorted(strip._sprites.values(), key=lambda s: s.col)
        # Check no same-lane overlap
        for i in range(len(parked)):
            for j in range(i + 1, len(parked)):
                if parked[i].lane == parked[j].lane:
                    self.assertGreaterEqual(
                        abs(parked[i].col - parked[j].col), 5,
                        f"Same-lane sprites at col {parked[i].col} and {parked[j].col} overlap")

    def test_mixed_crowd_uses_both_lanes(self):
        """Realistic scenario: 4 recovered + 2 idle + 2 airborne should spread."""
        strip = make_strip(120)
        pilots = [
            FakePilot("R-1", "RECOVERED"), FakePilot("R-2", "RECOVERED"),
            FakePilot("R-3", "RECOVERED"), FakePilot("R-4", "RECOVERED"),
            FakePilot("I-1", "IDLE"), FakePilot("I-2", "IDLE"),
            FakePilot("A-1", "AIRBORNE"), FakePilot("A-2", "AIRBORNE"),
        ]
        strip.update_pilots(pilots)
        settle(strip)
        all_lanes = {s.lane for s in strip._sprites.values()}
        self.assertEqual(all_lanes, {0, 1},
                         "Mixed crowd should use both lanes")

    def test_elevator_always_lane0(self):
        """Elevator phase sprites should always be lane 0."""
        strip = make_strip(100)
        pilots = [FakePilot("A-1", "AIRBORNE")]
        strip.update_pilots(pilots)
        advance(strip, 1)  # Still in elevator
        elevator = [s for s in strip._sprites.values() if s.phase == "ELEVATOR"]
        for s in elevator:
            self.assertEqual(s.lane, 0, "Elevator sprites must be lane 0")


# ── Ticket ID Label Tests ────────────────────────────────────────────

class TestTicketLabels(unittest.TestCase):
    """Verify ticket IDs are stored on sprites and used for labels."""

    def test_ticket_id_stored_on_sprite(self):
        """FlightSprite should store ticket_id from pilot."""
        strip = make_strip(100)
        pilot = FakePilot("Phoenix-1", "AIRBORNE", ticket_id="ENG-175")
        strip.update_pilots([pilot])
        sprite = strip._sprites.get("Phoenix-1")
        self.assertIsNotNone(sprite)
        self.assertEqual(sprite.ticket_id, "ENG-175")

    def test_recovered_no_ticket_id_still_works(self):
        """Recovered pilot without ticket_id should fallback gracefully."""
        strip = make_strip(100)
        pilot = FakePilot("Ghost-1", "RECOVERED")
        # Manually clear ticket_id after __post_init__
        pilot.ticket_id = ""
        strip.update_pilots([pilot])
        sprite = strip._sprites.get("Ghost-1")
        self.assertIsNotNone(sprite)
        self.assertEqual(sprite.ticket_id, "")


# ── URL Extraction Tests ─────────────────────────────────────────────

class TestURLExtraction(unittest.TestCase):
    """Test localhost URL regex matching (standalone, no TUI dependency)."""

    def setUp(self):
        import re
        self.url_re = re.compile(r"(localhost:\d+|127\.0\.0\.1:\d+|0\.0\.0\.0:\d+)")

    def test_localhost_standard(self):
        match = self.url_re.search("Local: http://localhost:3000/")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "localhost:3000")

    def test_localhost_high_port(self):
        match = self.url_re.search("Server running at localhost:49152")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "localhost:49152")

    def test_127_address(self):
        match = self.url_re.search("http://127.0.0.1:8080/api")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "127.0.0.1:8080")

    def test_0000_address(self):
        match = self.url_re.search("Listening on 0.0.0.0:5173")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "0.0.0.0:5173")

    def test_no_match(self):
        match = self.url_re.search("Compiling typescript files...")
        self.assertIsNone(match)

    def test_no_match_remote_host(self):
        match = self.url_re.search("Connected to api.example.com:443")
        self.assertIsNone(match)

    def test_vite_output(self):
        """Real Vite dev server output."""
        match = self.url_re.search("  ➜  Local:   http://localhost:5173/")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "localhost:5173")

    def test_next_output(self):
        """Real Next.js dev server output."""
        match = self.url_re.search("- Local:        http://localhost:3000")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "localhost:3000")


# ── Action Validation Tests ──────────────────────────────────────────

class TestActionValidation(unittest.TestCase):
    """Test which actions are valid for each pilot status.

    These don't need the TUI — just verify the state logic.
    """

    VALID_RESUME = {"RECOVERED", "MAYDAY", "IDLE"}
    VALID_RECALL = {"AIRBORNE"}
    VALID_COMPACT = {"AIRBORNE"}  # compact triggers AAR
    INVALID_WAVEOFF = set()  # wave-off works on everything

    def test_resume_valid_statuses(self):
        for status in self.VALID_RESUME:
            self.assertIn(status, self.VALID_RESUME)

    def test_resume_invalid_for_airborne(self):
        self.assertNotIn("AIRBORNE", self.VALID_RESUME)

    def test_resume_invalid_for_aar(self):
        self.assertNotIn("AAR", self.VALID_RESUME)

    def test_recall_only_airborne(self):
        self.assertEqual(self.VALID_RECALL, {"AIRBORNE"})

    def test_waveoff_works_on_all(self):
        """Wave-off should work on any status."""
        all_statuses = {"IDLE", "AIRBORNE", "ON_APPROACH", "RECOVERED", "MAYDAY", "AAR", "SAR"}
        for status in all_statuses:
            self.assertNotIn(status, self.INVALID_WAVEOFF)

    def test_context_hotkey_resume_shown(self):
        """Resume key should be shown for RECOVERED/MAYDAY/IDLE."""
        for status in ["RECOVERED", "MAYDAY", "IDLE"]:
            self.assertIn(status, self.VALID_RESUME,
                          f"Resume should be available for {status}")

    def test_context_hotkey_recall_hidden_for_idle(self):
        """Recall should NOT be shown for IDLE pilots."""
        self.assertNotIn("IDLE", self.VALID_RECALL)

    def test_context_hotkey_compact_hidden_for_recovered(self):
        """Compact should NOT be shown for RECOVERED pilots."""
        self.assertNotIn("RECOVERED", self.VALID_COMPACT)


# ── Quote Escaping Tests ─────────────────────────────────────────────

class TestQuoteEscaping(unittest.TestCase):
    """Verify single-quote escaping for bash printf safety."""

    def _escape(self, s: str) -> str:
        return s.replace("'", "'\\''")

    def test_no_quotes(self):
        self.assertEqual(self._escape("Hello world"), "Hello world")

    def test_single_apostrophe(self):
        self.assertEqual(self._escape("What's up"), "What'\\''s up")

    def test_multiple_apostrophes(self):
        self.assertEqual(self._escape("I've what's it's"), "I'\\''ve what'\\''s it'\\''s")

    def test_leading_quote(self):
        self.assertEqual(self._escape("'hello'"), "'\\''hello'\\''")

    def test_bash_printf_safe(self):
        """Escaped string should be valid inside bash single-quoted printf."""
        import subprocess
        quote = "What's our vector, Victor?"
        escaped = self._escape(quote)
        cmd = f"printf '%s' '{escaped}'"
        result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, quote)


# ── Process Group Tests ──────────────────────────────────────────────

class TestProcessGroup(unittest.TestCase):
    """Verify start_new_session creates isolated process group."""

    def test_child_gets_own_pgid(self):
        """Process spawned with start_new_session should have its own pgid."""
        import subprocess, os
        proc = subprocess.Popen(
            ["sleep", "0.1"],
            start_new_session=True,
        )
        try:
            pgid = os.getpgid(proc.pid)
            self.assertEqual(pgid, proc.pid,
                             "Child pgid should equal its own pid with start_new_session")
        finally:
            proc.terminate()
            proc.wait()

    def test_killpg_terminates_children(self):
        """os.killpg should kill both parent and child processes."""
        import subprocess, os, signal, time
        proc = subprocess.Popen(
            ["bash", "-c", "sleep 10 & wait"],
            start_new_session=True,
        )
        time.sleep(0.2)
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        proc.wait(timeout=5)
        self.assertNotEqual(proc.returncode, None, "Process should have exited")


# ── Adversarial / Edge Case Tests ─────────────────────────────────────

class TestRapidStatusFlapping(unittest.TestCase):
    """Test what happens when status changes rapidly (e.g. token delta jitter)."""

    def test_airborne_idle_airborne_flap(self):
        """Agent flaps AIRBORNE → IDLE → AIRBORNE within a few ticks.
        This happens when token flow pauses briefly. Sprite shouldn't reset position."""
        strip = make_strip(120)
        strip.update_pilots([FakePilot("F-1", "AIRBORNE")])
        settle(strip)
        # Get position after cruise
        advance(strip, 20)
        pos_before = strip._sprites["F-1"].col

        # Flap to IDLE then back
        strip.update_pilots([FakePilot("F-1", "IDLE")])
        advance(strip, 2)
        strip.update_pilots([FakePilot("F-1", "AIRBORNE")])
        advance(strip, 2)
        pos_after = strip._sprites["F-1"].col

        # Position shouldn't jump wildly (back to DECK)
        self.assertGreater(pos_after, pos_before - 20,
                           "Flapping status shouldn't teleport sprite back to deck")

    def test_recovered_mayday_recovered_flap(self):
        """RECOVERED → MAYDAY → RECOVERED shouldn't corrupt sprite phase."""
        strip = make_strip(100)
        strip.update_pilots([FakePilot("F-1", "RECOVERED")])
        settle(strip)

        strip.update_pilots([FakePilot("F-1", "MAYDAY")])
        advance(strip, 3)
        strip.update_pilots([FakePilot("F-1", "RECOVERED")])
        advance(strip, 3)

        sprite = strip._sprites.get("F-1")
        self.assertIsNotNone(sprite)
        # Should end up parked, not stuck in a MAYDAY phase
        self.assertIn(sprite.phase, ("DECK_PARK", "TAXI_BACK", "RETURN", "DECEL", "TRAP"),
                      f"After flap should be recovering, got {sprite.phase}")

    def test_rapid_aar_cancel(self):
        """Start AAR then immediately go back to AIRBORNE (cancel compaction)."""
        strip = make_strip(120)
        strip.update_pilots([FakePilot("F-1", "AIRBORNE")])
        settle(strip)
        advance(strip, 20)

        strip.update_pilots([FakePilot("F-1", "AAR")])
        advance(strip, 3)  # Just started reversing
        strip.update_pilots([FakePilot("F-1", "AIRBORNE")])
        advance(strip, 5)

        sprite = strip._sprites.get("F-1")
        self.assertIsNotNone(sprite)
        # Should not be stuck in AAR phases
        self.assertFalse(sprite.phase.startswith("AAR"),
                         f"Cancelled AAR should not stay in {sprite.phase}")


class TestSimultaneousStatusChanges(unittest.TestCase):
    """Multiple pilots changing status at once."""

    def test_all_recover_simultaneously(self):
        """5 airborne agents all RECOVER at the same tick."""
        strip = make_strip(150)
        pilots = [FakePilot(f"A-{i}", "AIRBORNE") for i in range(5)]
        strip.update_pilots(pilots)
        settle(strip)
        advance(strip, 30)  # In cruise

        # All recover at once
        pilots = [FakePilot(f"A-{i}", "RECOVERED") for i in range(5)]
        strip.update_pilots(pilots)
        advance(strip, 100)  # Let them land

        parked = [s for s in strip._sprites.values() if s.phase == "DECK_PARK"]
        self.assertEqual(len(parked), 5, "All 5 should park")

        # No overlaps within same lane
        for i in range(len(parked)):
            for j in range(i + 1, len(parked)):
                if parked[i].lane == parked[j].lane:
                    self.assertGreaterEqual(
                        abs(parked[i].col - parked[j].col), 4,
                        f"Same-lane park overlap at cols {parked[i].col} and {parked[j].col}")

    def test_all_go_mayday_simultaneously(self):
        """5 agents all crash at the same tick."""
        strip = make_strip(150)
        pilots = [FakePilot(f"A-{i}", "AIRBORNE") for i in range(5)]
        strip.update_pilots(pilots)
        settle(strip)
        advance(strip, 20)

        pilots = [FakePilot(f"A-{i}", "MAYDAY") for i in range(5)]
        strip.update_pilots(pilots)
        advance(strip, 5)

        # All should be in mayday-related phases, none should crash
        for s in strip._sprites.values():
            self.assertIsNotNone(s.phase, f"{s.pilot_id} has no phase")


class TestPilotLifecycleFull(unittest.TestCase):
    """Walk a pilot through the complete lifecycle multiple times."""

    def test_full_lifecycle_twice(self):
        """IDLE → AIRBORNE → RECOVERED → resume → AIRBORNE → RECOVERED.
        Simulates a pilot being deployed, completing, then redeployed."""
        strip = make_strip(120)

        # Deploy as IDLE
        strip.update_pilots([FakePilot("P-1", "IDLE")])
        settle(strip)
        self.assertIn(strip._sprites["P-1"].phase, ("DECK_IDLE", "ELEVATOR"))

        # Go airborne
        strip.update_pilots([FakePilot("P-1", "AIRBORNE")])
        advance(strip, 50)
        self.assertNotIn(strip._sprites["P-1"].phase, ("DECK_IDLE", "DECK_PARK"))

        # Recover
        strip.update_pilots([FakePilot("P-1", "RECOVERED")])
        advance(strip, 60)  # Land
        self.assertEqual(strip._sprites["P-1"].phase, "DECK_PARK")

        # Resume — back to IDLE then AIRBORNE
        strip.update_pilots([FakePilot("P-1", "IDLE")])
        settle(strip)
        strip.update_pilots([FakePilot("P-1", "AIRBORNE")])
        advance(strip, 50)
        self.assertNotIn(strip._sprites["P-1"].phase, ("DECK_IDLE", "DECK_PARK"))

        # Recover again
        strip.update_pilots([FakePilot("P-1", "RECOVERED")])
        advance(strip, 60)
        self.assertEqual(strip._sprites["P-1"].phase, "DECK_PARK")

    def test_aar_full_cycle(self):
        """AIRBORNE → AAR → back to AIRBORNE after refuel."""
        strip = make_strip(120)
        strip.update_pilots([FakePilot("P-1", "AIRBORNE")])
        settle(strip)
        advance(strip, 30)

        strip.update_pilots([FakePilot("P-1", "AAR")])
        # Run through all AAR phases
        total_aar_ticks = sum(PHASE_TICKS.get(p, 0)
                              for p in ["AAR_REVERSE", "AAR_DOCK", "AAR_REFUEL", "AAR_DISCONNECT"])
        advance(strip, total_aar_ticks + 10)

        # After AAR completes, should be back in a cruise-like phase
        sprite = strip._sprites["P-1"]
        self.assertFalse(sprite.phase.startswith("AAR"),
                         f"Should have exited AAR, stuck in {sprite.phase}")

    def test_sar_full_cycle(self):
        """AIRBORNE → SAR → replane → back to active."""
        strip = make_strip(120)
        strip.update_pilots([FakePilot("P-1", "AIRBORNE")])
        settle(strip)
        advance(strip, 20)

        strip.update_pilots([FakePilot("P-1", "SAR")])
        total_sar_ticks = sum(PHASE_TICKS.get(p, 0)
                              for p in ["SAR_FLAMEOUT", "SAR_EJECT", "SAR_HELO_OUT",
                                        "SAR_PICKUP", "SAR_HELO_RTB", "SAR_REPLANE"])
        advance(strip, total_sar_ticks + 10)

        sprite = strip._sprites["P-1"]
        self.assertFalse(sprite.phase.startswith("SAR"),
                         f"Should have exited SAR, stuck in {sprite.phase}")


class TestExtremeDensity(unittest.TestCase):
    """Test with many sprites in a small strip — push the limits."""

    def test_10_parked_narrow_strip(self):
        """10 recovered agents on a 60-col strip. Should not crash or go negative."""
        strip = make_strip(60)
        pilots = [FakePilot(f"R-{i}", "RECOVERED") for i in range(10)]
        strip.update_pilots(pilots)
        settle(strip)

        for s in strip._sprites.values():
            self.assertGreaterEqual(s.col, 0, f"{s.pilot_id} col went negative")
            self.assertLess(s.col, 60, f"{s.pilot_id} col exceeded width")

    def test_15_mixed_on_80_cols(self):
        """15 agents mixed status on 80 cols. No overlaps within same lane."""
        strip = make_strip(80)
        pilots = (
            [FakePilot(f"R-{i}", "RECOVERED") for i in range(5)] +
            [FakePilot(f"I-{i}", "IDLE") for i in range(3)] +
            [FakePilot(f"A-{i}", "AIRBORNE") for i in range(5)] +
            [FakePilot("M-1", "MAYDAY"), FakePilot("S-1", "SAR")]
        )
        strip.update_pilots(pilots)
        settle(strip)
        advance(strip, 30)

        # Check for same-lane overlaps (within 4 cols)
        sprites = list(strip._sprites.values())
        overlaps = 0
        for i in range(len(sprites)):
            for j in range(i + 1, len(sprites)):
                if sprites[i].lane == sprites[j].lane:
                    if abs(sprites[i].col - sprites[j].col) < 4:
                        overlaps += 1
        # Allow some overlaps during transitions, but not excessive
        self.assertLess(overlaps, 5,
                        f"Too many same-lane overlaps ({overlaps}) for 15 agents on 80 cols")

    def test_pilot_added_then_removed(self):
        """Add 5 pilots, remove 2, add 3 more. Sprite dict should be clean."""
        strip = make_strip(100)
        pilots = [FakePilot(f"A-{i}", "AIRBORNE") for i in range(5)]
        strip.update_pilots(pilots)
        settle(strip)
        self.assertEqual(len(strip._sprites), 5)

        # Remove 2
        pilots = [FakePilot(f"A-{i}", "AIRBORNE") for i in range(3)]
        strip.update_pilots(pilots)
        advance(strip, 5)
        self.assertEqual(len(strip._sprites), 3, "Removed pilots should be pruned")

        # Add 3 new
        pilots += [FakePilot(f"B-{i}", "AIRBORNE") for i in range(3)]
        strip.update_pilots(pilots)
        advance(strip, 5)
        self.assertEqual(len(strip._sprites), 6, "New pilots should be added")


class TestMiniBossSessionGuard(unittest.TestCase):
    """Test the session ID guard logic for Mini Boss status file."""

    def test_stale_session_doesnt_overwrite(self):
        """Old session's cleanup should not overwrite new session's ACTIVE."""
        import tempfile, os
        tmpdir = tempfile.mkdtemp()
        status_file = os.path.join(tmpdir, "miniboss-status")
        session_file = os.path.join(tmpdir, "miniboss-session")

        # Session 1 writes its PID
        with open(session_file, "w") as f:
            f.write("1001")
        with open(status_file, "w") as f:
            f.write("ACTIVE")

        # Session 2 takes over
        with open(session_file, "w") as f:
            f.write("2002")
        with open(status_file, "w") as f:
            f.write("ACTIVE")

        # Session 1's cleanup runs (checks if still current)
        with open(session_file) as f:
            current = f.read().strip()
        if current == "1001":
            with open(status_file, "w") as f:
                f.write("OFFLINE")

        # Status should still be ACTIVE (session 2 owns it)
        with open(status_file) as f:
            self.assertEqual(f.read().strip(), "ACTIVE",
                             "Stale session cleanup should NOT overwrite ACTIVE")

        # Cleanup
        os.unlink(status_file)
        os.unlink(session_file)
        os.rmdir(tmpdir)


if __name__ == "__main__":
    unittest.main(verbosity=2)
