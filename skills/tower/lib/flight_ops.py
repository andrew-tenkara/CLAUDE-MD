"""USS Tenkara Pri-Fly — Flight Ops Strip widget.

Extended FlightOpsStrip for tower: carries all CIC phases
(CAT → LAUNCH → CRUISE → ORDNANCE → RETURN → TRAP → PARKED, MAYDAY) and
adds AAR (Air-to-Air Refueling) and SAR (Search and Rescue) phases.

Tick interval: 0.15s.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static


# ── Sprites ───────────────────────────────────────────────────────────

# Rightward (launch/cruise/ordnance) — NTDS phosphor green style
F14_PARKED = "[-=]"
F14_IDLE   = ["[-=]", "[—=]"]  # warm idle on deck, engines ticking over
F14_TAXI   = [">-=]", ">=—]"]  # slow taxi, exhaust shimmer
F14_CAT    = ["o==>", "O==>"]  # spool up, engines cycling
F14_LAUNCH = [" >==>", "  >==>>"]  # afterburner acceleration
F14_CRUISE = [">==▷", ">==>"]
F14_BOMB   = [">==*", ">==>·", ">==▷"]
F14_MAYDAY = [">==x", "/==/", "x==x"]
# Leftward (return/trap/decel)
F14_RTN    = ["◁==<", "<==<"]
F14_DECEL  = ["◁==|", "◁==-"]  # approach deceleration, speed brakes
F14_TRAP   = ["◁==|", "[-=]"]  # wire catch, abrupt stop
F14_TAXI_BACK = ["<-=]", "<=-]"]  # slow taxi back to line

# AAR
F14_AAR_REVERSE = ["◁==<", "<==<"]   # reuses RTN sprites; reverse toward tanker
TANKER         = "<==>⊙"
AAR_DOCKED     = "◁==<==>⊙"

# SAR
F14_FLAMEOUT = ["x==/", "\\=x", "x==/"]
PARACHUTE    = "☂"
DEBRIS       = "···"
HELO_RIGHT   = ["-=H>", "-≡H>"]
HELO_LEFT    = ["<H=-", "<H≡-"]

# Elevator (rising from hangar deck)
F14_ELEVATOR = ["[__]", "[-=]"]  # hatch open → jet appears

# CAT_HOLD: like CAT but does NOT auto-advance to LAUNCH.
# Loops spool animation indefinitely until status transitions to AIRBORNE.
F14_CAT_HOLD = ["o==>", "O==>", "o==≫", "O==≫"]  # extended spool cycle

# Phase → sprite list mapping (for phases that share simple frame tables)
PHASE_SPRITES: dict[str, list[str] | str] = {
    "ELEVATOR":       F14_ELEVATOR,
    "DECK_PARK":      [F14_PARKED],
    "DECK_IDLE":      F14_IDLE,
    "TAXI_TO_CAT":    F14_TAXI,
    "CAT":            F14_CAT,
    "CAT_HOLD":       F14_CAT_HOLD,
    "LAUNCH":         F14_LAUNCH,
    "CRUISE":         F14_CRUISE,
    "ORDNANCE":       F14_BOMB,
    "MAYDAY":         F14_MAYDAY,
    "RETURN":         F14_RTN,
    "DECEL":          F14_DECEL,
    "TRAP":           F14_TRAP,
    "TAXI_BACK":      F14_TAXI_BACK,
    "AAR_REVERSE":    F14_AAR_REVERSE,
    "AAR_DISCONNECT": F14_CRUISE,    # separating from tanker, resuming cruise sprite
    "SAR_FLAMEOUT":   F14_FLAMEOUT,
    "SAR_REPLANE":    [F14_PARKED],  # fresh jet on deck
}


# ── Zones ─────────────────────────────────────────────────────────────

# Zone boundaries as % of strip width
ZONE_PCT = {
    "PARK": (0, 8), "DECK": (8, 14), "CAT": (14, 22), "SKY": (22, 65),
    "TGT": (65, 72), "RTN": (72, 85), "DECEL": (85, 92), "TRAP": (92, 100),
}

TANKER_PCT = 60  # Fixed tanker position as % of strip width


# ── Phase timing ──────────────────────────────────────────────────────

# Ticks at ~0.15s interval
PHASE_TICKS = {
    "ELEVATOR":        8,  # rising from hangar deck
    "DECK_PARK":       0,  # indefinite until AIRBORNE status
    "TAXI_TO_CAT":    12,  # slow taxi from parking to catapult
    "CAT":             8,  # spool up on the cat
    "LAUNCH":          5,  # afterburner acceleration off the bow
    "CRUISE":         40,
    "DECEL":          10,  # approach deceleration with speed brakes
    "TRAP":            6,  # wire catch
    "TAXI_BACK":      15,  # taxi back to parking spot
    "AAR_REVERSE":    15,
    "AAR_DOCK":       20,
    "AAR_REFUEL":     30,
    "AAR_DISCONNECT":  8,
    "SAR_FLAMEOUT":    8,
    "SAR_EJECT":       6,
    "SAR_HELO_OUT":   20,
    "SAR_PICKUP":      6,
    "SAR_HELO_RTB":   20,
    "SAR_REPLANE":    10,
}

# Ticks for the radar sweep to cross the full strip width once (~3 s)
RADAR_SWEEP_TICKS = 20

# Tombstone TTL — ticks after a sprite leaves the roster before hard-removal.
# Allows landing/death animations to play out before the sprite disappears.
_TOMBSTONE_TTL = 30  # ticks (~4.5 s at 0.15 s/tick)


# ── Dataclass ─────────────────────────────────────────────────────────

@dataclass
class FlightSprite:
    pilot_id: str         # callsign / short label
    col: int = 0
    phase: str = "CAT"
    lane: int = 0         # 0 = main, 1 = upper (deconfliction)
    anim_frame: int = 0
    phase_ticks: int = 0
    prev_status: str = ""
    ticket_id: str = ""   # e.g. "ENG-177" — shown as label when not recovered
    # SAR-specific state
    helo_col: int = 0
    parachute_col: int = 0
    # Soft-prune: ticks since removed from roster (lets animations play out)
    tombstone_ticks: int = 0


# ── Widget ────────────────────────────────────────────────────────────

class FlightOpsStrip(Static):
    """NTDS-style horizontal flight ops display with AAR and SAR phases.

    Renders a 4-row animated strip:
      row 0  — zone labels + upper-lane sprites
      row 1  — main-lane sprites (lane 0)
      row 2  — secondary actors: tanker, helo, parachute
      row 3  — callsign labels
    Enclosed in a box border. A dim green radar sweep scans left-to-right
    every ~3 s overlaid on both sprite rows.
    """

    frame: reactive[int] = reactive(0)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._sprites: dict[str, FlightSprite] = {}
        self._strip_width: int = 80
        self._sweep_col: int = 0   # radar sweep column position

    # ── Lifecycle ────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.set_interval(0.15, self._tick)

    def _tick(self) -> None:
        self.frame += 1

    def watch_frame(self, value: int) -> None:
        self._advance_sprites()
        # Advance radar sweep
        self._sweep_col = (self._sweep_col + max(1, self._strip_width // RADAR_SWEEP_TICKS)) % self._strip_width
        self.refresh()

    # ── Zone helpers ─────────────────────────────────────────────────

    def _zone_col(self, zone: str, offset_pct: float = 0.0) -> int:
        """Convert zone name + fractional offset within zone to column."""
        lo, hi = ZONE_PCT[zone]
        pct = lo + (hi - lo) * offset_pct
        return int(pct / 100 * self._strip_width)

    def _tanker_col(self) -> int:
        return int(TANKER_PCT / 100 * self._strip_width)

    # ── Status → phase mapping ────────────────────────────────────────

    def _phase_from_status(self, status: str, sprite: FlightSprite) -> str:
        """Map pilot status string to flight phase, respecting current animation."""
        s = status.upper()

        if s == "MAYDAY":
            return "MAYDAY"

        if s == "SAR":
            if sprite.phase.startswith("SAR_"):
                return sprite.phase
            return "SAR_FLAMEOUT"

        if s == "AAR":
            if sprite.phase.startswith("AAR_"):
                return sprite.phase
            return "AAR_REVERSE"

        if s == "RECOVERED":
            # Let the full landing sequence play out
            if sprite.phase in ("RETURN", "DECEL", "TRAP", "TAXI_BACK", "DECK_PARK"):
                return sprite.phase
            return "RETURN"

        if s == "ON_APPROACH":
            if sprite.phase in ("RETURN", "DECEL", "TRAP"):
                return sprite.phase
            return "RETURN"

        if s == "AIRBORNE":
            # Let the full launch sequence play out
            if sprite.phase in ("ELEVATOR", "DECK_IDLE", "DECK_PARK", "TAXI_TO_CAT", "CAT", "CAT_HOLD", "LAUNCH", "CRUISE", "ORDNANCE"):
                return sprite.phase
            return "ELEVATOR"  # start from elevator when going airborne

        if s == "PREFLIGHT":
            # On the cat, burners lit, holding for first write
            if sprite.phase in ("ELEVATOR", "TAXI_TO_CAT", "CAT_HOLD"):
                return sprite.phase
            # From deck states → taxi to catapult hold
            if sprite.phase in ("DECK_PARK", "DECK_IDLE"):
                return "TAXI_TO_CAT"
            return "TAXI_TO_CAT"

        if s == "IDLE":
            # Idle = on deck, engines warm, waiting for tasking
            if sprite.phase in ("ELEVATOR", "DECK_IDLE"):
                return sprite.phase
            # DECK_PARK → DECK_IDLE: warming up engines on resume
            return "DECK_IDLE"

        # Assigned to a task but not yet airborne — park on deck
        if s == "QUEUED":
            if sprite.phase in ("ELEVATOR", "DECK_PARK", "DECK_IDLE"):
                return sprite.phase
            return "DECK_PARK"

        return sprite.phase

    # ── Public interface ─────────────────────────────────────────────

    def update_pilots(self, pilots: list) -> None:
        """Sync sprites with current pilot states.

        Each pilot object must expose:
          .pilot_id (str)  — unique id / callsign
          .status   (str)  — one of AIRBORNE / ON_APPROACH / RECOVERED /
                             MAYDAY / AAR / SAR / IDLE
        """
        seen: set[str] = set()

        for pilot in pilots:
            pid = pilot.pilot_id
            seen.add(pid)
            status = getattr(pilot, "status", "IDLE")

            if pid not in self._sprites:
                tid = getattr(pilot, "ticket_id", "")
                sprite = FlightSprite(pilot_id=pid, prev_status=status, ticket_id=tid)
                s = status.upper()
                if s == "RECOVERED":
                    sprite.phase = "DECK_PARK"
                    sprite.col = self._zone_col("PARK", 0.0)
                elif s == "ON_APPROACH":
                    sprite.phase = "RETURN"
                    sprite.col = self._zone_col("RTN", 0.3)
                elif s == "SAR":
                    sprite.phase = "SAR_FLAMEOUT"
                    sprite.col = self._zone_col("TGT", 0.5)
                elif s == "AAR":
                    sprite.phase = "AAR_REVERSE"
                    sprite.col = self._zone_col("SKY", 0.5)
                elif s == "PREFLIGHT":
                    # PREFLIGHT — elevator up, taxi to cat hold
                    sprite.phase = "ELEVATOR"
                    sprite.col = self._zone_col("DECK", 0.5)
                elif s == "AIRBORNE":
                    # New/resumed — elevator up from hangar, then taxi to cat
                    sprite.phase = "ELEVATOR"
                    sprite.col = self._zone_col("DECK", 0.5)
                elif s == "IDLE":
                    # Idle — elevator up, then warm idle on deck
                    sprite.phase = "ELEVATOR"
                    sprite.col = self._zone_col("DECK", 0.5)
                else:
                    # Queued / new — elevator up to deck
                    sprite.phase = "ELEVATOR"
                    sprite.col = self._zone_col("DECK", 0.5)
                self._sprites[pid] = sprite
            else:
                sprite = self._sprites[pid]
                new_phase = self._phase_from_status(status, sprite)

                # Detect status transitions
                if status != sprite.prev_status:
                    s = status.upper()
                    prev = sprite.prev_status.upper()

                    if s == "PREFLIGHT" and prev in ("IDLE", "QUEUED", "RECOVERED", ""):
                        # PREFLIGHT: taxi to catapult hold
                        if sprite.phase in ("DECK_PARK", "DECK_IDLE"):
                            new_phase = "TAXI_TO_CAT"
                            sprite.phase_ticks = 0
                            sprite.anim_frame = 0
                        elif sprite.phase not in ("ELEVATOR", "TAXI_TO_CAT", "CAT_HOLD"):
                            new_phase = "ELEVATOR"
                            sprite.col = self._zone_col("DECK", 0.5)
                            sprite.phase_ticks = 0
                            sprite.anim_frame = 0

                    elif s == "AIRBORNE" and prev == "PREFLIGHT":
                        # PREFLIGHT → AIRBORNE: release from catapult hold
                        if sprite.phase == "CAT_HOLD":
                            new_phase = "CAT"
                            sprite.phase_ticks = 0
                            sprite.anim_frame = 0
                        elif sprite.phase == "TAXI_TO_CAT":
                            pass  # let taxi complete → CAT_HOLD → will catch next tick
                        elif sprite.phase not in ("CAT", "LAUNCH", "CRUISE", "ORDNANCE"):
                            new_phase = "TAXI_TO_CAT"
                            sprite.phase_ticks = 0
                            sprite.anim_frame = 0

                    elif s == "AIRBORNE" and prev in ("RECOVERED", "QUEUED", "IDLE", ""):
                        # Launch sequence from wherever they're sitting
                        if sprite.phase in ("DECK_PARK", "DECK_IDLE"):
                            new_phase = "TAXI_TO_CAT"
                            sprite.phase_ticks = 0
                            sprite.anim_frame = 0
                        elif sprite.phase not in ("ELEVATOR", "TAXI_TO_CAT", "CAT", "CAT_HOLD", "LAUNCH", "CRUISE", "ORDNANCE"):
                            new_phase = "ELEVATOR"
                            sprite.col = self._zone_col("DECK", 0.5)
                            sprite.phase_ticks = 0
                            sprite.anim_frame = 0

                    elif s == "ON_APPROACH" and sprite.phase not in ("RETURN", "DECEL", "TRAP"):
                        new_phase = "RETURN"
                        # Turn around from current position, not jump to RTN zone
                        sprite.phase_ticks = 0

                    elif s == "RECOVERED" and sprite.phase not in ("RETURN", "DECEL", "TRAP", "TAXI_BACK", "DECK_PARK", "DECK_IDLE"):
                        new_phase = "RETURN"
                        # Turn around from wherever they are — no position jump
                        sprite.phase_ticks = 0

                    elif s == "MAYDAY":
                        new_phase = "MAYDAY"

                    elif s == "AAR" and not sprite.phase.startswith("AAR_"):
                        new_phase = "AAR_REVERSE"
                        sprite.phase_ticks = 0
                        sprite.anim_frame = 0

                    elif s == "SAR" and not sprite.phase.startswith("SAR_"):
                        new_phase = "SAR_FLAMEOUT"
                        sprite.phase_ticks = 0
                        sprite.anim_frame = 0
                        sprite.parachute_col = sprite.col
                        sprite.helo_col = self._zone_col("DECK", 0.8)

                sprite.phase = new_phase
                sprite.prev_status = status

        # Soft-prune: keep sprites animating briefly after leaving roster so
        # landing/death animations play out before the sprite disappears.
        # Game-dev pattern: tombstone state with TTL.
        to_prune = []
        for pid in set(self._sprites) - seen:
            sprite = self._sprites[pid]
            sprite.tombstone_ticks += 1
            # Hard-remove once fully parked, in MAYDAY, or TTL expired
            if sprite.tombstone_ticks >= _TOMBSTONE_TTL or sprite.phase in ("DECK_PARK", "MAYDAY"):
                to_prune.append(pid)
        for pid in to_prune:
            del self._sprites[pid]

        # Deconflict lanes immediately so new sprites are placed correctly
        # before the next animation tick fires.
        self._deconflict_lanes()

    # ── State machine ─────────────────────────────────────────────────

    def _advance_sprites(self) -> None:
        """Advance each sprite one tick through its phase state machine."""
        sw = self._strip_width

        for sprite in self._sprites.values():
            sprite.anim_frame += 1
            sprite.phase_ticks += 1
            phase = sprite.phase

            # ── Standard phases ──────────────────────────────────────

            if phase == "ELEVATOR":
                # Rising from hangar deck — stationary, sprite alternates
                sprite.col = self._zone_col("DECK", 0.5)
                if sprite.phase_ticks >= PHASE_TICKS["ELEVATOR"]:
                    # Elevator up → next phase depends on status
                    s = sprite.prev_status.upper()
                    if s == "IDLE":
                        sprite.phase = "DECK_IDLE"
                        sprite.col = self._zone_col("CAT", 0.0)
                    elif s in ("AIRBORNE", "PREFLIGHT"):
                        sprite.phase = "TAXI_TO_CAT"
                    else:
                        sprite.phase = "DECK_PARK"
                        sprite.col = self._zone_col("PARK", 0.0)
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "DECK_PARK":
                # Cold parked on deck — stagger tightly in PARK zone
                # Each parked sprite gets a slot based on its index among parked sprites
                parked = sorted(
                    [s for s in self._sprites.values() if s.phase == "DECK_PARK"],
                    key=lambda s: s.pilot_id,
                )
                idx = next((i for i, s in enumerate(parked) if s.pilot_id == sprite.pilot_id), 0)
                # Pack at 5-col spacing (sprite width ~4 chars + 1 gap)
                sprite.col = self._zone_col("PARK", 0.0) + idx * 5

            elif phase == "DECK_IDLE":
                # Warm idle on deck — engines running, waiting for tasking
                # Position past ALL parked sprites (they can overflow the PARK zone)
                parked_cols = [s.col for s in self._sprites.values() if s.phase == "DECK_PARK"]
                base_col = max(
                    self._zone_col("CAT", 0.0),
                    (max(parked_cols) + 6) if parked_cols else 0,
                )
                idle_sprites = sorted(
                    [s for s in self._sprites.values() if s.phase == "DECK_IDLE"],
                    key=lambda s: s.pilot_id,
                )
                idx = next((i for i, s in enumerate(idle_sprites) if s.pilot_id == sprite.pilot_id), 0)
                sprite.col = base_col + idx * 6

            elif phase == "TAXI_TO_CAT":
                # Slow taxi from parking to catapult
                if sprite.phase_ticks % 3 == 0:
                    sprite.col += 1
                cat_start = self._zone_col("CAT", 0.3)
                if sprite.col >= cat_start or sprite.phase_ticks >= PHASE_TICKS["TAXI_TO_CAT"]:
                    # PREFLIGHT → hold on cat; AIRBORNE → spool and launch
                    if sprite.prev_status.upper() == "PREFLIGHT":
                        sprite.phase = "CAT_HOLD"
                    else:
                        sprite.phase = "CAT"
                    sprite.col = self._zone_col("CAT", 0.5)
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "CAT_HOLD":
                # Stationary on catapult, engine spool animation loops
                # indefinitely. Does NOT auto-advance to LAUNCH.
                # When status transitions to AIRBORNE, update_pilots sets
                # phase to CAT which then auto-advances to LAUNCH.
                pass

            elif phase == "CAT":
                # Stationary engine spool on the catapult
                if sprite.phase_ticks >= PHASE_TICKS["CAT"]:
                    sprite.phase = "LAUNCH"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "LAUNCH":
                # Afterburner acceleration — increasing speed off the bow
                accel = 1 + sprite.phase_ticks // 2  # 1, 1, 2, 2, 3...
                sprite.col += accel
                sky_start = self._zone_col("SKY", 0.0)
                if sprite.col >= sky_start or sprite.phase_ticks >= PHASE_TICKS["LAUNCH"]:
                    sprite.phase = "CRUISE"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0
                    sprite.col = max(sprite.col, sky_start)

            elif phase == "CRUISE":
                # Stagger cruise speed slightly per pilot to avoid stacking
                pid_offset = sum(ord(c) for c in sprite.pilot_id) % 3
                move_interval = 3 + (pid_offset % 2)  # 3 or 4 ticks per step
                if sprite.phase_ticks % move_interval == 0:
                    sprite.col += 1
                tgt_start = self._zone_col("TGT", 0.0)
                if sprite.col >= tgt_start:
                    sprite.phase = "ORDNANCE"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "ORDNANCE":
                # Oscillate in TGT zone — stagger per sprite to avoid overlap
                tgt_lo = self._zone_col("TGT", 0.0)
                tgt_hi = self._zone_col("TGT", 1.0) - 5
                mid = (tgt_lo + tgt_hi) // 2
                # Hash pilot_id to get a stable phase offset per sprite
                pid_hash = sum(ord(c) for c in sprite.pilot_id) % 20
                offset = int(3 * (((sprite.phase_ticks + pid_hash) % 20) - 10) / 10)
                sprite.col = mid + offset

            elif phase == "RETURN":
                pid_offset = sum(ord(c) for c in sprite.pilot_id) % 3
                move_interval = 3 + (pid_offset % 2)
                if sprite.phase_ticks % move_interval == 0:
                    sprite.col -= 1
                decel_start = self._zone_col("DECEL", 0.0)
                if sprite.col <= decel_start:
                    sprite.phase = "DECEL"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "DECEL":
                # Approach deceleration — decreasing speed
                move_interval = 2 + sprite.phase_ticks // 3  # slows: every 2, 3, 4... ticks
                if sprite.phase_ticks % move_interval == 0:
                    sprite.col -= 1
                trap_start = self._zone_col("TRAP", 0.3)
                if sprite.col <= trap_start or sprite.phase_ticks >= PHASE_TICKS["DECEL"]:
                    sprite.phase = "TRAP"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "TRAP":
                # Wire catch — abrupt deceleration
                if sprite.phase_ticks < 3:
                    sprite.col -= 1  # final momentum
                if sprite.phase_ticks >= PHASE_TICKS["TRAP"]:
                    sprite.phase = "TAXI_BACK"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "TAXI_BACK":
                # Slow taxi back from trap zone to parking
                if sprite.phase_ticks % 3 == 0:
                    sprite.col -= 1
                park_spot = self._zone_col("PARK", 0.0)
                if sprite.col <= park_spot or sprite.phase_ticks >= PHASE_TICKS["TAXI_BACK"]:
                    sprite.phase = "DECK_PARK"
                    sprite.col = park_spot
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "MAYDAY":
                # Tumble animation in place — no movement
                pass

            # ── AAR phases ───────────────────────────────────────────

            elif phase == "AAR_REVERSE":
                # Move left toward tanker (~25% back from current position)
                tanker = self._tanker_col()
                if sprite.phase_ticks % 2 == 0 and sprite.col > tanker + len(AAR_DOCKED):
                    sprite.col -= 1
                if sprite.col <= tanker + len(TANKER) or sprite.phase_ticks >= PHASE_TICKS["AAR_REVERSE"]:
                    sprite.phase = "AAR_DOCK"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0
                    sprite.col = tanker  # snap to tanker

            elif phase == "AAR_DOCK":
                # Docked: render combined sprite AAR_DOCKED, hold
                sprite.col = self._tanker_col()
                if sprite.phase_ticks >= PHASE_TICKS["AAR_DOCK"]:
                    sprite.phase = "AAR_REFUEL"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "AAR_REFUEL":
                # Still docked; fuel bar climbs during this phase
                sprite.col = self._tanker_col()
                if sprite.phase_ticks >= PHASE_TICKS["AAR_REFUEL"]:
                    sprite.phase = "AAR_DISCONNECT"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "AAR_DISCONNECT":
                # Separate from tanker, resume rightward cruise
                if sprite.phase_ticks % 2 == 0:
                    sprite.col += 1
                if sprite.phase_ticks >= PHASE_TICKS["AAR_DISCONNECT"]:
                    sprite.phase = "CRUISE"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            # ── SAR phases ───────────────────────────────────────────

            elif phase == "SAR_FLAMEOUT":
                # Tumble in place; no forward movement
                if sprite.phase_ticks >= PHASE_TICKS["SAR_FLAMEOUT"]:
                    sprite.phase = "SAR_EJECT"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0
                    sprite.parachute_col = sprite.col
                    sprite.helo_col = self._zone_col("DECK", 0.8)

            elif phase == "SAR_EJECT":
                # Parachute visible; debris scatters
                if sprite.phase_ticks >= PHASE_TICKS["SAR_EJECT"]:
                    sprite.phase = "SAR_HELO_OUT"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "SAR_HELO_OUT":
                # Helo flies right toward crash site (parachute_col)
                if sprite.phase_ticks % 2 == 0:
                    sprite.helo_col += 1
                if sprite.helo_col >= sprite.parachute_col or sprite.phase_ticks >= PHASE_TICKS["SAR_HELO_OUT"]:
                    sprite.helo_col = sprite.parachute_col
                    sprite.phase = "SAR_PICKUP"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "SAR_PICKUP":
                # Helo hovers at site; parachute disappears
                if sprite.phase_ticks >= PHASE_TICKS["SAR_PICKUP"]:
                    sprite.phase = "SAR_HELO_RTB"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "SAR_HELO_RTB":
                # Helo flies left back to deck
                deck_col = self._zone_col("DECK", 0.8)
                if sprite.phase_ticks % 2 == 0 and sprite.helo_col > deck_col:
                    sprite.helo_col -= 1
                if sprite.helo_col <= deck_col or sprite.phase_ticks >= PHASE_TICKS["SAR_HELO_RTB"]:
                    sprite.helo_col = deck_col
                    sprite.phase = "SAR_REPLANE"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "SAR_REPLANE":
                # New jet on deck; relaunch after delay
                sprite.col = self._zone_col("PARK", 0.5)
                if sprite.phase_ticks >= PHASE_TICKS["SAR_REPLANE"]:
                    sprite.phase = "TAXI_TO_CAT"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            # ── Column clamp ─────────────────────────────────────────
            sprite.col = max(0, min(sw - 8, sprite.col))

        self._deconflict_lanes()

    def _deconflict_lanes(self) -> None:
        """Assign sprite lanes to prevent visual overlap.

        Uses an O(n) left-to-right occupancy sweep — a simplified version of
        the OAM slot-table pattern from arcade hardware. Each lane tracks its
        rightmost occupied column; sprites are assigned the first free lane.
        When both lanes are blocked a sprite is nudged right to the lane that
        opens soonest, keeping it visible rather than stacking silently.

        Called from both _advance_sprites (every tick) and update_pilots (on
        roster change) so new agents are correctly placed immediately.
        """
        CLEARANCE = 7   # min column gap between adjacent sprites in a lane

        # ── Deck sprites: stable index-based lane alternation ─────────
        parked = sorted(
            [s for s in self._sprites.values() if s.phase in ("DECK_PARK", "TAXI_BACK")],
            key=lambda s: s.col,
        )
        for idx, s in enumerate(parked):
            s.lane = idx % 2

        idle = sorted(
            [s for s in self._sprites.values() if s.phase == "DECK_IDLE"],
            key=lambda s: s.col,
        )
        idle_start = len(parked) % 2
        for idx, s in enumerate(idle):
            s.lane = (idle_start + idx) % 2

        for s in self._sprites.values():
            if s.phase == "ELEVATOR":
                s.lane = 0

        # ── Active (airborne) sprites: occupancy-grid lane assignment ──
        DECK_PHASES = {"DECK_PARK", "TAXI_BACK", "DECK_IDLE", "ELEVATOR"}
        active = sorted(
            [s for s in self._sprites.values() if s.phase not in DECK_PHASES],
            key=lambda s: s.col,
        )

        # lane_end[lane] = first column available in that lane
        lane_end: dict[int, int] = {0: 0, 1: 0}
        sw = self._strip_width

        for s in active:
            w = max(4, len(self._get_sprite_text(s)))
            can0 = s.col >= lane_end[0]
            can1 = s.col >= lane_end[1]

            if can0:
                s.lane = 0
                lane_end[0] = s.col + w + CLEARANCE
            elif can1:
                s.lane = 1
                lane_end[1] = s.col + w + CLEARANCE
            else:
                # Both lanes blocked — nudge to the lane that opens soonest
                if lane_end[0] <= lane_end[1]:
                    s.lane = 0
                    s.col = min(lane_end[0], sw - w)
                    lane_end[0] = s.col + w + CLEARANCE
                else:
                    s.lane = 1
                    s.col = min(lane_end[1], sw - w)
                    lane_end[1] = s.col + w + CLEARANCE

    # ── Sprite text / style helpers ───────────────────────────────────

    def _get_sprite_text(self, sprite: FlightSprite) -> str:
        phase = sprite.phase

        if phase in ("AAR_DOCK", "AAR_REFUEL"):
            return AAR_DOCKED

        frames = PHASE_SPRITES.get(phase)
        if frames:
            if isinstance(frames, str):
                return frames
            return frames[sprite.anim_frame % len(frames)]

        # SAR phases without a sprite in main row (jet is gone/ejected)
        return F14_PARKED

    def _get_sprite_style(self, sprite: FlightSprite) -> str:
        phase = sprite.phase

        if phase == "MAYDAY":
            return "bold red"
        if phase.startswith("SAR_"):
            return "bold red"
        if phase.startswith("AAR_"):
            return "bold cyan"
        if phase in ("RETURN", "DECEL"):
            return "green"
        if phase == "TRAP":
            return "bold yellow"  # wire catch — attention
        if phase in ("TAXI_BACK", "TAXI_TO_CAT"):
            return "dim green"
        if phase == "ELEVATOR":
            return "bold yellow"  # rising from hangar
        if phase == "DECK_PARK":
            return "grey50"
        if phase == "DECK_IDLE":
            return "bold dark_orange"  # warm idle — waiting for tasking
        if phase == "CAT":
            return "bold yellow"  # spool up glow
        if phase == "CAT_HOLD":
            return "bold dark_orange"  # holding on cat, burners lit
        if phase == "LAUNCH":
            return "bold bright_white"  # afterburner
        if sprite.prev_status.upper() == "IDLE":
            return "dark_green"
        return "bold green"

    def _get_tanker_style(self) -> str:
        return "bold cyan"

    def _get_helo_style(self) -> str:
        return "bold yellow"

    # ── Render ────────────────────────────────────────────────────────

    def render(self) -> Text:
        """Build the flight ops strip as Rich Text.

        Output rows (inside border):
          upper  — zone labels + lane-1 sprites (with radar sweep)
          main   — lane-0 sprites (with radar sweep)
          aux    — secondary actors: tanker, helo, parachute/debris
          labels — callsign labels under main-lane sprites
        """
        try:
            w = self.size.width - 2   # subtract border columns
        except Exception:
            w = 80
        self._strip_width = max(40, w)
        sw = self._strip_width

        # Zone background characters
        zone_fills = {
            "PARK": "▓", "DECK": "▓", "CAT": "░", "SKY": " ",
            "TGT": "·", "DECEL": " ", "TRAP": "▓",
        }

        def _build_zone_bg() -> list[str]:
            buf = [" "] * sw
            for zone, (lo_pct, hi_pct) in ZONE_PCT.items():
                lo = int(lo_pct / 100 * sw)
                hi = int(hi_pct / 100 * sw)
                ch = zone_fills.get(zone, " ")
                for c in range(lo, min(hi, sw)):
                    buf[c] = ch
            return buf

        row_upper = _build_zone_bg()
        row_main  = _build_zone_bg()
        row_aux   = [" "] * sw
        row_label = [" "] * sw

        # Zone labels in upper row
        zone_label_text = {
            "PARK": "PARK", "CAT": "CAT", "TGT": "TGT", "TRAP": "TRAP",
        }
        for zone, label in zone_label_text.items():
            lo_pct, hi_pct = ZONE_PCT[zone]
            mid = int((lo_pct + hi_pct) / 2 / 100 * sw) - len(label) // 2
            for i, ch in enumerate(label):
                pos = mid + i
                if 0 <= pos < sw:
                    row_upper[pos] = ch

        # ── Deck condensing: when many sprites are parked/idle, show a count
        # badge instead of individual sprites to prevent visual overflow.
        DECK_CONDENSE_THRESHOLD = 4  # condense when more than this many on deck
        deck_phases = {"DECK_PARK", "TAXI_BACK", "DECK_IDLE"}
        deck_sprites = [s for s in self._sprites.values() if s.phase in deck_phases]
        condense_deck = len(deck_sprites) > DECK_CONDENSE_THRESHOLD
        condensed_count = len(deck_sprites) if condense_deck else 0

        # Collect main sprite overlay data: (col, lane, text, style, callsign)
        sprite_overlays: list[tuple[int, int, str, str, str]] = []
        for sprite in self._sprites.values():
            # Only render jet sprite when not in eject/pickup/helo phases
            hide_jet = sprite.phase in (
                "SAR_EJECT", "SAR_HELO_OUT", "SAR_PICKUP", "SAR_HELO_RTB"
            )
            # Skip individual deck sprites when condensed
            if condense_deck and sprite.phase in deck_phases:
                continue
            if not hide_jet:
                txt   = self._get_sprite_text(sprite)
                style = self._get_sprite_style(sprite)
                sprite_overlays.append((sprite.col, sprite.lane, txt, style, sprite.pilot_id))

        # Add deck count badge when condensed
        if condense_deck and condensed_count > 0:
            badge = f"[{condensed_count} ON DECK]"
            badge_col = self._zone_col("PARK", 0.3)
            sprite_overlays.append((badge_col, 0, badge, "bold grey70", "__deck_badge__"))

        # Collect aux overlays: (col, text, style)
        aux_overlays: list[tuple[int, str, str]] = []

        for sprite in self._sprites.values():
            phase = sprite.phase

            # Standing tanker during AAR (all sub-phases)
            if phase.startswith("AAR_"):
                tanker_col = self._tanker_col()
                # When not docked, show tanker separately; when docked the
                # combined sprite is on the main row and we still show TANKER
                # in aux for visual continuity (it's underneath the docked sprite).
                if phase not in ("AAR_DOCK", "AAR_REFUEL"):
                    aux_overlays.append((tanker_col, TANKER, self._get_tanker_style()))

                # Fuel bar under tanker during refuel
                if phase == "AAR_REFUEL":
                    refuel_pct = min(100, int(sprite.phase_ticks / PHASE_TICKS["AAR_REFUEL"] * 100))
                    bar_width = 8
                    filled = max(1, round(refuel_pct / 100 * bar_width))
                    fuel_bar = "█" * filled + "░" * (bar_width - filled)
                    aux_overlays.append((tanker_col, fuel_bar, "bold cyan"))

            # SAR secondary actors
            if phase == "SAR_EJECT":
                # Parachute + debris
                aux_overlays.append((sprite.parachute_col, PARACHUTE, "bold yellow"))
                debris_col = max(0, sprite.parachute_col + 2)
                aux_overlays.append((debris_col, DEBRIS, "dim red"))

            elif phase == "SAR_HELO_OUT":
                # Jet gone; parachute + helo flying right
                aux_overlays.append((sprite.parachute_col, PARACHUTE, "bold yellow"))
                helo_txt = HELO_RIGHT[sprite.anim_frame % len(HELO_RIGHT)]
                aux_overlays.append((sprite.helo_col, helo_txt, self._get_helo_style()))

            elif phase == "SAR_PICKUP":
                # Helo at site; parachute disappears this tick (omit it)
                helo_txt = HELO_RIGHT[sprite.anim_frame % len(HELO_RIGHT)]
                aux_overlays.append((sprite.helo_col, helo_txt, self._get_helo_style()))

            elif phase == "SAR_HELO_RTB":
                # Helo flying left back to deck
                helo_txt = HELO_LEFT[sprite.anim_frame % len(HELO_LEFT)]
                aux_overlays.append((sprite.helo_col, helo_txt, self._get_helo_style()))

            elif phase == "SAR_REPLANE":
                # Show new jet on deck (already on sprite_overlays via F14_PARKED)
                pass

        # Labels under sprites: ticket ID follows active sprites,
        # callsign for parked/recovered. Color-coded by status.
        # Up to 3 label rows — overlapping labels bump to next row.
        MAX_LABEL_ROWS = 3
        label_rows_chars: list[list[str]] = [[" "] * sw for _ in range(MAX_LABEL_ROWS)]
        label_rows_cells: list[list[tuple[int, str, str]]] = [[] for _ in range(MAX_LABEL_ROWS)]
        occupied: list[list[tuple[int, int]]] = [[] for _ in range(MAX_LABEL_ROWS)]

        for col, _lane, txt, _style, callsign in sprite_overlays:
            if callsign == "__deck_badge__":
                continue  # Badge has no label
            sprite = self._sprites.get(callsign)
            is_parked = sprite and sprite.phase in ("DECK_PARK", "TAXI_BACK")
            label = (sprite.ticket_id if sprite and sprite.ticket_id else callsign)
            style = "dim green" if is_parked else "bold bright_white"
            label_start = col + len(txt) // 2 - len(label) // 2
            label_end = label_start + len(label) - 1

            # Find first row without overlap
            placed = False
            for r in range(MAX_LABEL_ROWS):
                overlaps = any(not (label_end < s or label_start > e) for s, e in occupied[r])
                if not overlaps:
                    occupied[r].append((label_start, label_end))
                    for i, ch in enumerate(label):
                        pos = label_start + i
                        if 0 <= pos < sw:
                            label_rows_cells[r].append((pos, ch, style))
                            label_rows_chars[r][pos] = ch
                    placed = True
                    break
            if not placed:
                # All rows full — find a gap in the last row and truncate to fit
                r = MAX_LABEL_ROWS - 1
                # Find first unoccupied run of columns wide enough for at least "…"
                occ = occupied[r]
                gaps = []
                check = label_start
                # Scan from label_start rightward for the first free column
                while check < sw:
                    blocked = any(s <= check <= e for s, e in occ)
                    if not blocked:
                        gaps.append(check)
                        break
                    check += 1
                if gaps:
                    gstart = gaps[0]
                    # Find how many chars fit before the next occupied block
                    free = sw
                    for s, e in occ:
                        if s > gstart:
                            free = min(free, s - gstart)
                    trunc = label[:max(0, free - 1)] + "…" if len(label) > free else label
                    trunc = trunc[:free]
                    for i, ch in enumerate(trunc):
                        p = gstart + i
                        if 0 <= p < sw:
                            label_rows_cells[r].append((p, ch, style))
                            label_rows_chars[r][p] = ch
                    occupied[r].append((gstart, gstart + len(trunc) - 1))

        # Keep row_label for backwards compat (first row)
        row_label = label_rows_chars[0]

        # ── Compose Rich Text output ──────────────────────────────────

        result = Text()

        # Top border
        title = " FLIGHT OPS "
        border_pad = sw - len(title)
        left_b = border_pad // 2
        right_b = border_pad - left_b
        result.append(" ╔", style="dim green")
        result.append("═" * left_b, style="dim green")
        result.append(title, style="bold green")
        result.append("═" * right_b, style="dim green")
        result.append("╗\n", style="dim green")

        # Upper row: zone labels + lane-1 sprites + radar sweep
        result.append(" ║", style="dim green")
        self._render_row(
            result, "".join(row_upper), sw,
            sprite_overlays, target_lane=1,
            sweep_col=self._sweep_col,
        )
        result.append("║\n", style="dim green")

        # Main row: lane-0 sprites + radar sweep
        result.append(" ║", style="dim green")
        self._render_row(
            result, "".join(row_main), sw,
            sprite_overlays, target_lane=0,
            sweep_col=self._sweep_col,
        )
        result.append("║\n", style="dim green")

        # Aux row: tanker, helo, parachute, debris
        result.append(" ║", style="dim green")
        self._render_aux_row(result, row_aux, sw, aux_overlays)
        result.append("║\n", style="dim green")

        # Label rows: render each row that has content
        for r in range(MAX_LABEL_ROWS):
            if not label_rows_cells[r]:
                continue  # skip empty rows
            result.append(" ║", style="dim green")
            style_map: dict[int, str] = {}
            for pos, _ch, style in label_rows_cells[r]:
                style_map[pos] = style
            for pos in range(sw):
                ch = label_rows_chars[r][pos]
                style = style_map.get(pos, "dim green")
                result.append(ch, style=style)
            result.append("║\n", style="dim green")

        # Bottom border
        result.append(" ╚", style="dim green")
        result.append("═" * sw, style="dim green")
        result.append("╝", style="dim green")

        return result

    # ── Row rendering helpers ─────────────────────────────────────────

    def _zone_style(self, pos: int, sw: int) -> str:
        """Background character style by zone position."""
        pct = pos / sw * 100 if sw > 0 else 0
        if pct < 8:       # PARK
            return "grey35"
        if pct < 14:       # DECK
            return "grey30"
        if pct < 22:       # CAT
            return "grey23"
        if 65 <= pct < 72: # TGT
            return "dim red"
        if 85 <= pct < 92: # DECEL
            return "grey19"
        if pct >= 92:      # TRAP
            return "grey30"
        return "grey15"    # SKY / RTN

    def _radar_style(self, base_style: str) -> str:
        """Blend a dim green tint for the radar sweep column."""
        # Keep it simple: override background hint with dim green overlay
        return "bold dark_green on grey11"

    def _render_row(
        self,
        result: Text,
        bg: str,
        sw: int,
        sprites: list[tuple[int, int, str, str, str]],
        target_lane: int,
        sweep_col: int,
    ) -> None:
        """Render one content row with sprite overlays and radar sweep."""
        overlays: list[tuple[int, int, str, str]] = []
        for col, lane, txt, style, _callsign in sprites:
            if lane == target_lane:
                overlays.append((col, col + len(txt), txt, style))
        overlays.sort(key=lambda o: o[0])

        pos = 0
        for start, end, txt, style in overlays:
            # Skip sprites already overrun by a prior wider/overlapping sprite
            if start < pos:
                continue
            # Background before sprite
            while pos < start and pos < sw:
                ch = bg[pos] if pos < len(bg) else " "
                st = self._radar_style("") if pos == sweep_col else self._zone_style(pos, sw)
                result.append(ch, style=st)
                pos += 1
            # Sprite characters
            for ch in txt:
                if pos < sw:
                    st = style if pos != sweep_col else (style + " on grey11")
                    result.append(ch, style=st)
                    pos += 1
        # Remaining background
        while pos < sw:
            ch = bg[pos] if pos < len(bg) else " "
            st = self._radar_style("") if pos == sweep_col else self._zone_style(pos, sw)
            result.append(ch, style=st)
            pos += 1

    def _render_aux_row(
        self,
        result: Text,
        bg: list[str],
        sw: int,
        aux_overlays: list[tuple[int, str, str]],
    ) -> None:
        """Render the aux row (tanker, helo, parachute, debris)."""
        # Sort and deduplicate by column (first-placed wins)
        overlays = sorted(aux_overlays, key=lambda o: o[0])

        # Build a flat overlay map: col → (char, style)
        char_map: dict[int, tuple[str, str]] = {}
        for col, txt, style in overlays:
            for i, ch in enumerate(txt):
                p = col + i
                if 0 <= p < sw and p not in char_map:
                    char_map[p] = (ch, style)

        for pos in range(sw):
            if pos in char_map:
                ch, style = char_map[pos]
                result.append(ch, style=style)
            else:
                result.append(bg[pos], style=self._zone_style(pos, sw))
