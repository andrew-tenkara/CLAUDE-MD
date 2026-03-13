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

    def __post_init__(self):
        if not self.callsign:
            self.callsign = self.pilot_id


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
