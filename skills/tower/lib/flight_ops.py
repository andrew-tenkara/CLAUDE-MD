"""USS Tenkara Pri-Fly — Flight Ops Strip widget.

Simplified v2: four statuses → clean animation phases.

  ON_DECK     → DECK_PARK / DECK_IDLE (parked on deck)
  IN_FLIGHT   → ELEVATOR → TAXI_TO_CAT → CAT → LAUNCH → CRUISE → ORDNANCE
  ON_APPROACH → RETURN → DECEL → TRAP → TAXI_BACK → DECK_PARK
  RECOVERED   → DECK_PARK (final resting state)

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
# Leftward (return/trap/decel)
F14_RTN    = ["◁==<", "<==<"]
F14_DECEL  = ["◁==|", "◁==-"]  # approach deceleration, speed brakes
F14_TRAP   = ["◁==|", "[-=]"]  # wire catch, abrupt stop
F14_TAXI_BACK = ["<-=]", "<=-]"]  # slow taxi back to line

# Elevator (rising from hangar deck)
F14_ELEVATOR = ["[__]", "[-=]"]  # hatch open → jet appears

# Phase → sprite list mapping
PHASE_SPRITES: dict[str, list[str] | str] = {
    "ELEVATOR":       F14_ELEVATOR,
    "DECK_PARK":      [F14_PARKED],
    "DECK_IDLE":      F14_IDLE,
    "TAXI_TO_CAT":    F14_TAXI,
    "CAT":            F14_CAT,
    "LAUNCH":         F14_LAUNCH,
    "CRUISE":         F14_CRUISE,
    "ORDNANCE":       F14_BOMB,
    "RETURN":         F14_RTN,
    "DECEL":          F14_DECEL,
    "TRAP":           F14_TRAP,
    "TAXI_BACK":      F14_TAXI_BACK,
}


# ── Zones ─────────────────────────────────────────────────────────────

# Zone boundaries as % of strip width
ZONE_PCT = {
    "PARK": (0, 8), "DECK": (8, 14), "CAT": (14, 22), "SKY": (22, 65),
    "TGT": (65, 72), "RTN": (72, 85), "DECEL": (85, 92), "TRAP": (92, 100),
}


# ── Phase timing ──────────────────────────────────────────────────────

# Ticks at ~0.15s interval
PHASE_TICKS = {
    "ELEVATOR":        8,  # rising from hangar deck
    "DECK_PARK":       0,  # indefinite
    "TAXI_TO_CAT":    12,  # slow taxi from parking to catapult
    "CAT":             8,  # spool up on the cat
    "LAUNCH":          5,  # afterburner acceleration off the bow
    "CRUISE":         40,
    "DECEL":          10,  # approach deceleration with speed brakes
    "TRAP":            6,  # wire catch
    "TAXI_BACK":      15,  # taxi back to parking spot
}

# Ticks for the radar sweep to cross the full strip width once (~3 s)
RADAR_SWEEP_TICKS = 20

# Tombstone TTL — ticks after a sprite leaves the roster before hard-removal.
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
    # Soft-prune: ticks since removed from roster (lets animations play out)
    tombstone_ticks: int = 0


# ── Widget ────────────────────────────────────────────────────────────

class FlightOpsStrip(Static):
    """NTDS-style horizontal flight ops display.

    Renders a 4-row animated strip:
      row 0  — zone labels + upper-lane sprites
      row 1  — main-lane sprites (lane 0)
      row 2  — (empty aux row for spacing)
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
        self._sweep_col = (self._sweep_col + max(1, self._strip_width // RADAR_SWEEP_TICKS)) % self._strip_width
        self.refresh()

    # ── Zone helpers ─────────────────────────────────────────────────

    def _zone_col(self, zone: str, offset_pct: float = 0.0) -> int:
        """Convert zone name + fractional offset within zone to column."""
        lo, hi = ZONE_PCT[zone]
        pct = lo + (hi - lo) * offset_pct
        return int(pct / 100 * self._strip_width)

    # ── Status → phase mapping ────────────────────────────────────────

    def _phase_from_status(self, status: str, sprite: FlightSprite) -> str:
        """Map pilot status string to flight phase, respecting current animation."""
        s = status.upper()

        if s == "RECOVERED":
            # Let the full landing sequence play out
            if sprite.phase in ("RETURN", "DECEL", "TRAP", "TAXI_BACK", "DECK_PARK"):
                return sprite.phase
            return "RETURN"

        if s == "ON_APPROACH":
            if sprite.phase in ("RETURN", "DECEL", "TRAP"):
                return sprite.phase
            return "RETURN"

        if s == "IN_FLIGHT":
            # Let the full launch sequence play out
            if sprite.phase in ("ELEVATOR", "DECK_IDLE", "DECK_PARK", "TAXI_TO_CAT", "CAT", "LAUNCH", "CRUISE", "ORDNANCE"):
                return sprite.phase
            return "ELEVATOR"

        if s == "ON_DECK":
            # Preserve current phase — never snap a sprite back to deck.
            # Real transitions are handled by the transition detector in update_pilots.
            return sprite.phase

        return sprite.phase

    # ── Public interface ─────────────────────────────────────────────

    def update_pilots(self, pilots: list) -> None:
        """Sync sprites with current pilot states.

        Each pilot object must expose:
          .pilot_id (str)  — unique id / callsign
          .status   (str)  — one of IN_FLIGHT / ON_APPROACH / RECOVERED / ON_DECK
        """
        seen: set[str] = set()

        for pilot in pilots:
            pid = pilot.pilot_id
            seen.add(pid)
            status = getattr(pilot, "status", "ON_DECK")

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
                elif s == "IN_FLIGHT":
                    sprite.phase = "ELEVATOR"
                    sprite.col = self._zone_col("DECK", 0.5)
                else:
                    # ON_DECK — elevator up to deck idle
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

                    if s == "IN_FLIGHT" and prev in ("ON_DECK", "RECOVERED", ""):
                        # Launch sequence from wherever they're sitting
                        if sprite.phase in ("DECK_PARK", "DECK_IDLE"):
                            new_phase = "TAXI_TO_CAT"
                            sprite.phase_ticks = 0
                            sprite.anim_frame = 0
                        elif sprite.phase not in ("ELEVATOR", "TAXI_TO_CAT", "CAT", "LAUNCH", "CRUISE", "ORDNANCE"):
                            new_phase = "ELEVATOR"
                            sprite.col = self._zone_col("DECK", 0.5)
                            sprite.phase_ticks = 0
                            sprite.anim_frame = 0

                    elif s == "ON_APPROACH" and sprite.phase not in ("RETURN", "DECEL", "TRAP"):
                        new_phase = "RETURN"
                        sprite.phase_ticks = 0

                    elif s == "RECOVERED" and sprite.phase not in ("RETURN", "DECEL", "TRAP", "TAXI_BACK", "DECK_PARK", "DECK_IDLE"):
                        new_phase = "RETURN"
                        sprite.phase_ticks = 0

                sprite.phase = new_phase
                sprite.prev_status = status

        # Soft-prune: keep sprites animating briefly after leaving roster
        to_prune = []
        for pid in set(self._sprites) - seen:
            sprite = self._sprites[pid]
            sprite.tombstone_ticks += 1
            if sprite.tombstone_ticks >= _TOMBSTONE_TTL or sprite.phase == "DECK_PARK":
                to_prune.append(pid)
        for pid in to_prune:
            del self._sprites[pid]

        self._deconflict_lanes()

    # ── State machine ─────────────────────────────────────────────────

    def _advance_sprites(self) -> None:
        """Advance each sprite one tick through its phase state machine."""
        sw = self._strip_width

        for sprite in self._sprites.values():
            sprite.anim_frame += 1
            sprite.phase_ticks += 1
            phase = sprite.phase

            if phase == "ELEVATOR":
                sprite.col = self._zone_col("DECK", 0.5)
                if sprite.phase_ticks >= PHASE_TICKS["ELEVATOR"]:
                    s = sprite.prev_status.upper()
                    if s == "ON_DECK":
                        sprite.phase = "DECK_IDLE"
                        sprite.col = self._zone_col("CAT", 0.0)
                    elif s == "IN_FLIGHT":
                        sprite.phase = "TAXI_TO_CAT"
                    else:
                        sprite.phase = "DECK_PARK"
                        sprite.col = self._zone_col("PARK", 0.0)
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "DECK_PARK":
                parked = sorted(
                    [s for s in self._sprites.values() if s.phase == "DECK_PARK"],
                    key=lambda s: s.pilot_id,
                )
                idx = next((i for i, s in enumerate(parked) if s.pilot_id == sprite.pilot_id), 0)
                sprite.col = self._zone_col("PARK", 0.0) + idx * 5

            elif phase == "DECK_IDLE":
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
                if sprite.phase_ticks % 3 == 0:
                    sprite.col += 1
                cat_start = self._zone_col("CAT", 0.3)
                if sprite.col >= cat_start or sprite.phase_ticks >= PHASE_TICKS["TAXI_TO_CAT"]:
                    sprite.phase = "CAT"
                    sprite.col = self._zone_col("CAT", 0.5)
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "CAT":
                if sprite.phase_ticks >= PHASE_TICKS["CAT"]:
                    sprite.phase = "LAUNCH"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "LAUNCH":
                accel = 1 + sprite.phase_ticks // 2
                sprite.col += accel
                sky_start = self._zone_col("SKY", 0.0)
                if sprite.col >= sky_start or sprite.phase_ticks >= PHASE_TICKS["LAUNCH"]:
                    sprite.phase = "CRUISE"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0
                    sprite.col = max(sprite.col, sky_start)

            elif phase == "CRUISE":
                pid_offset = sum(ord(c) for c in sprite.pilot_id) % 3
                move_interval = 3 + (pid_offset % 2)
                if sprite.phase_ticks % move_interval == 0:
                    sprite.col += 1
                tgt_start = self._zone_col("TGT", 0.0)
                if sprite.col >= tgt_start:
                    sprite.phase = "ORDNANCE"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "ORDNANCE":
                tgt_lo = self._zone_col("TGT", 0.0)
                tgt_hi = self._zone_col("TGT", 1.0) - 5
                mid = (tgt_lo + tgt_hi) // 2
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
                move_interval = 2 + sprite.phase_ticks // 3
                if sprite.phase_ticks % move_interval == 0:
                    sprite.col -= 1
                trap_start = self._zone_col("TRAP", 0.3)
                if sprite.col <= trap_start or sprite.phase_ticks >= PHASE_TICKS["DECEL"]:
                    sprite.phase = "TRAP"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "TRAP":
                if sprite.phase_ticks < 3:
                    sprite.col -= 1
                if sprite.phase_ticks >= PHASE_TICKS["TRAP"]:
                    sprite.phase = "TAXI_BACK"
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            elif phase == "TAXI_BACK":
                if sprite.phase_ticks % 3 == 0:
                    sprite.col -= 1
                park_spot = self._zone_col("PARK", 0.0)
                if sprite.col <= park_spot or sprite.phase_ticks >= PHASE_TICKS["TAXI_BACK"]:
                    sprite.phase = "DECK_PARK"
                    sprite.col = park_spot
                    sprite.phase_ticks = 0
                    sprite.anim_frame = 0

            # Column clamp
            sprite.col = max(0, min(sw - 8, sprite.col))

        self._deconflict_lanes()

    def _deconflict_lanes(self) -> None:
        """Assign sprite lanes to prevent visual overlap.

        Uses an O(n) left-to-right occupancy sweep.
        """
        CLEARANCE = 7

        # Deck sprites: stable index-based lane alternation
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

        # Active (airborne) sprites: occupancy-grid lane assignment
        DECK_PHASES = {"DECK_PARK", "TAXI_BACK", "DECK_IDLE", "ELEVATOR"}
        active = sorted(
            [s for s in self._sprites.values() if s.phase not in DECK_PHASES],
            key=lambda s: s.col,
        )

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
        frames = PHASE_SPRITES.get(sprite.phase)
        if frames:
            if isinstance(frames, str):
                return frames
            return frames[sprite.anim_frame % len(frames)]
        return F14_PARKED

    def _get_sprite_style(self, sprite: FlightSprite) -> str:
        phase = sprite.phase

        if phase in ("RETURN", "DECEL"):
            return "green"
        if phase == "TRAP":
            return "bold yellow"
        if phase in ("TAXI_BACK", "TAXI_TO_CAT"):
            return "dim green"
        if phase == "ELEVATOR":
            return "bold yellow"
        if phase == "DECK_PARK":
            return "grey50"
        if phase == "DECK_IDLE":
            return "bold dark_orange"
        if phase == "CAT":
            return "bold yellow"
        if phase == "LAUNCH":
            return "bold bright_white"
        return "bold green"

    # ── Render ────────────────────────────────────────────────────────

    def render(self) -> Text:
        """Build the flight ops strip as Rich Text."""
        try:
            w = self.size.width - 2
        except Exception:
            w = 80
        self._strip_width = max(40, w)
        sw = self._strip_width

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

        # Deck condensing
        DECK_CONDENSE_THRESHOLD = 4
        deck_phases = {"DECK_PARK", "TAXI_BACK", "DECK_IDLE"}
        deck_sprites = [s for s in self._sprites.values() if s.phase in deck_phases]
        condense_deck = len(deck_sprites) > DECK_CONDENSE_THRESHOLD
        condensed_count = len(deck_sprites) if condense_deck else 0

        # Collect sprite overlays
        sprite_overlays: list[tuple[int, int, str, str, str]] = []
        for sprite in self._sprites.values():
            if condense_deck and sprite.phase in deck_phases:
                continue
            txt   = self._get_sprite_text(sprite)
            style = self._get_sprite_style(sprite)
            sprite_overlays.append((sprite.col, sprite.lane, txt, style, sprite.pilot_id))

        if condense_deck and condensed_count > 0:
            badge = f"[{condensed_count} ON DECK]"
            badge_col = self._zone_col("PARK", 0.3)
            sprite_overlays.append((badge_col, 0, badge, "bold grey70", "__deck_badge__"))

        # Labels under sprites
        MAX_LABEL_ROWS = 3
        label_rows_chars: list[list[str]] = [[" "] * sw for _ in range(MAX_LABEL_ROWS)]
        label_rows_cells: list[list[tuple[int, str, str]]] = [[] for _ in range(MAX_LABEL_ROWS)]
        occupied: list[list[tuple[int, int]]] = [[] for _ in range(MAX_LABEL_ROWS)]

        for col, _lane, txt, _style, callsign in sprite_overlays:
            if callsign == "__deck_badge__":
                continue
            sprite = self._sprites.get(callsign)
            is_parked = sprite and sprite.phase in ("DECK_PARK", "TAXI_BACK")
            label = (sprite.ticket_id if sprite and sprite.ticket_id else callsign)
            style = "dim green" if is_parked else "bold bright_white"
            label_start = col + len(txt) // 2 - len(label) // 2
            label_end = label_start + len(label) - 1

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
                r = MAX_LABEL_ROWS - 1
                occ = occupied[r]
                check = label_start
                while check < sw:
                    blocked = any(s <= check <= e for s, e in occ)
                    if not blocked:
                        break
                    check += 1
                if check < sw:
                    gstart = check
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
        self._render_row(result, "".join(row_upper), sw, sprite_overlays, target_lane=1, sweep_col=self._sweep_col)
        result.append("║\n", style="dim green")

        # Main row: lane-0 sprites + radar sweep
        result.append(" ║", style="dim green")
        self._render_row(result, "".join(row_main), sw, sprite_overlays, target_lane=0, sweep_col=self._sweep_col)
        result.append("║\n", style="dim green")

        # Aux row (empty now — kept for spacing)
        result.append(" ║", style="dim green")
        for pos in range(sw):
            result.append(row_aux[pos], style=self._zone_style(pos, sw))
        result.append("║\n", style="dim green")

        # Label rows
        for r in range(MAX_LABEL_ROWS):
            if not label_rows_cells[r]:
                continue
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
        pct = pos / sw * 100 if sw > 0 else 0
        if pct < 8:       return "grey35"      # PARK
        if pct < 14:      return "grey30"      # DECK
        if pct < 22:      return "grey23"      # CAT
        if 65 <= pct < 72: return "dim red"    # TGT
        if 85 <= pct < 92: return "grey19"     # DECEL
        if pct >= 92:     return "grey30"      # TRAP
        return "grey15"                         # SKY / RTN

    def _radar_style(self, base_style: str) -> str:
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
        overlays: list[tuple[int, int, str, str]] = []
        for col, lane, txt, style, _callsign in sprites:
            if lane == target_lane:
                overlays.append((col, col + len(txt), txt, style))
        overlays.sort(key=lambda o: o[0])

        pos = 0
        for start, end, txt, style in overlays:
            if start < pos:
                continue
            while pos < start and pos < sw:
                ch = bg[pos] if pos < len(bg) else " "
                st = self._radar_style("") if pos == sweep_col else self._zone_style(pos, sw)
                result.append(ch, style=st)
                pos += 1
            for ch in txt:
                if pos < sw:
                    st = style if pos != sweep_col else (style + " on grey11")
                    result.append(ch, style=st)
                    pos += 1
        while pos < sw:
            ch = bg[pos] if pos < len(bg) else " "
            st = self._radar_style("") if pos == sweep_col else self._zone_style(pos, sw)
            result.append(ch, style=st)
            pos += 1
