"""USS Tenkara Pri-Fly — Flight Ops Strip widget.

Dead simple: three visual states.
  PARKED       — on deck, not moving
  FLYING_RIGHT — in flight, moving rightward across the strip
  FLYING_LEFT  — returning, moving leftward back to deck

Tick interval: 0.15s.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static


# ── Sprites ───────────────────────────────────────────────────────────

SPRITE_PARKED = "[-=]"
SPRITE_RIGHT  = [">==▷", ">==>"]   # cruise rightward
SPRITE_LEFT   = ["◁==<", "<==<"]   # return leftward

# ── Zones ─────────────────────────────────────────────────────────────

# Park zone: left 10% of strip. Sky: the rest.
PARK_PCT = 10
TGT_PCT  = 75   # oscillate zone starts here

# ── Timing ────────────────────────────────────────────────────────────

RADAR_SWEEP_TICKS = 20
_TOMBSTONE_TTL = 20   # ticks before removing a sprite that left the roster
_DEBOUNCE_FRAMES = 3  # consecutive same-status frames before acting


# ── Dataclass ─────────────────────────────────────────────────────────

@dataclass
class FlightSprite:
    pilot_id: str
    col: int = 0
    phase: str = "PARKED"      # PARKED | FLYING_RIGHT | FLYING_LEFT
    lane: int = 0
    anim_frame: int = 0
    ticket_id: str = ""
    tombstone_ticks: int = 0
    # Debounce
    stable_status: str = ""
    stable_count: int = 0
    effective_status: str = "ON_DECK"


# ── Widget ────────────────────────────────────────────────────────────

class FlightOpsStrip(Static):
    """NTDS-style horizontal flight ops strip.

    Two rows inside a border: upper (lane 1) and main (lane 0),
    plus a label row and radar sweep.
    """

    frame: reactive[int] = reactive(0)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._sprites: dict[str, FlightSprite] = {}
        self._strip_width: int = 80
        self._sweep_col: int = 0

    # ── Lifecycle ────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.set_interval(0.15, self._tick)

    def _tick(self) -> None:
        self.frame += 1

    def watch_frame(self, value: int) -> None:
        self._advance_sprites()
        self._sweep_col = (self._sweep_col + max(1, self._strip_width // RADAR_SWEEP_TICKS)) % self._strip_width
        self.refresh()

    # ── Helpers ───────────────────────────────────────────────────────

    def _park_col(self) -> int:
        return int(PARK_PCT / 100 * self._strip_width)

    def _tgt_col(self) -> int:
        return int(TGT_PCT / 100 * self._strip_width)

    # ── Public interface ─────────────────────────────────────────────

    def update_pilots(self, pilots: list) -> None:
        """Sync sprites with pilot states. Debounces status flickers."""
        seen: set[str] = set()

        for pilot in pilots:
            pid = pilot.pilot_id
            seen.add(pid)
            status = getattr(pilot, "status", "ON_DECK")

            if pid not in self._sprites:
                tid = getattr(pilot, "ticket_id", "")
                sprite = FlightSprite(pilot_id=pid, ticket_id=tid)
                sprite.effective_status = status
                sprite.stable_status = status
                sprite.stable_count = _DEBOUNCE_FRAMES
                if status == "IN_FLIGHT":
                    sprite.phase = "FLYING_RIGHT"
                    sprite.col = self._park_col()
                else:
                    sprite.phase = "PARKED"
                    sprite.col = self._park_col()
                self._sprites[pid] = sprite
            else:
                sprite = self._sprites[pid]

                # Debounce: require N consecutive same-status frames
                if status == sprite.stable_status:
                    sprite.stable_count = min(sprite.stable_count + 1, _DEBOUNCE_FRAMES + 1)
                else:
                    sprite.stable_status = status
                    sprite.stable_count = 1

                if sprite.stable_count >= _DEBOUNCE_FRAMES:
                    sprite.effective_status = status

                # Map effective status to phase
                es = sprite.effective_status
                if es == "IN_FLIGHT":
                    if sprite.phase == "PARKED":
                        sprite.phase = "FLYING_RIGHT"
                        sprite.col = self._park_col()
                    elif sprite.phase == "FLYING_LEFT":
                        # Was landing, tokens resumed — go back to flying right
                        sprite.phase = "FLYING_RIGHT"
                elif es in ("ON_DECK", "ON_APPROACH", "RECOVERED"):
                    if sprite.phase == "FLYING_RIGHT":
                        sprite.phase = "FLYING_LEFT"
                    # FLYING_LEFT will auto-park when it reaches deck

        # Prune sprites no longer in roster
        to_prune = []
        for pid in set(self._sprites) - seen:
            sprite = self._sprites[pid]
            sprite.tombstone_ticks += 1
            if sprite.tombstone_ticks >= _TOMBSTONE_TTL or sprite.phase == "PARKED":
                to_prune.append(pid)
        for pid in to_prune:
            del self._sprites[pid]

        self._deconflict_lanes()

    # ── Animation ────────────────────────────────────────────────────

    def _advance_sprites(self) -> None:
        sw = self._strip_width
        park = self._park_col()
        tgt = self._tgt_col()

        for sprite in self._sprites.values():
            sprite.anim_frame += 1

            if sprite.phase == "FLYING_RIGHT":
                # Move right every other tick
                if sprite.anim_frame % 2 == 0:
                    sprite.col += 1
                # Oscillate at target zone
                if sprite.col >= tgt:
                    mid = (tgt + sw - 6) // 2
                    pid_hash = sum(ord(c) for c in sprite.pilot_id) % 20
                    offset = int(3 * (((sprite.anim_frame + pid_hash) % 20) - 10) / 10)
                    sprite.col = mid + offset

            elif sprite.phase == "FLYING_LEFT":
                # Move left every other tick
                if sprite.anim_frame % 2 == 0:
                    sprite.col -= 1
                # Reached deck — park
                if sprite.col <= park:
                    sprite.col = park
                    sprite.phase = "PARKED"

            elif sprite.phase == "PARKED":
                # Stack parked sprites
                parked = sorted(
                    [s for s in self._sprites.values() if s.phase == "PARKED"],
                    key=lambda s: s.pilot_id,
                )
                idx = next((i for i, s in enumerate(parked) if s.pilot_id == sprite.pilot_id), 0)
                sprite.col = max(0, idx * 5)

            # Clamp
            sprite.col = max(0, min(sw - 6, sprite.col))

        self._deconflict_lanes()

    def _deconflict_lanes(self) -> None:
        """Simple two-lane deconfliction."""
        CLEARANCE = 7

        # Parked: alternate lanes
        parked = sorted(
            [s for s in self._sprites.values() if s.phase == "PARKED"],
            key=lambda s: s.col,
        )
        for idx, s in enumerate(parked):
            s.lane = idx % 2

        # Active: occupancy sweep
        active = sorted(
            [s for s in self._sprites.values() if s.phase != "PARKED"],
            key=lambda s: s.col,
        )
        lane_end = {0: 0, 1: 0}
        for s in active:
            w = 5
            if s.col >= lane_end[0]:
                s.lane = 0
                lane_end[0] = s.col + w + CLEARANCE
            elif s.col >= lane_end[1]:
                s.lane = 1
                lane_end[1] = s.col + w + CLEARANCE
            else:
                if lane_end[0] <= lane_end[1]:
                    s.lane = 0
                    lane_end[0] = s.col + w + CLEARANCE
                else:
                    s.lane = 1
                    lane_end[1] = s.col + w + CLEARANCE

    # ── Sprite helpers ───────────────────────────────────────────────

    def _get_sprite_text(self, sprite: FlightSprite) -> str:
        if sprite.phase == "PARKED":
            return SPRITE_PARKED
        if sprite.phase == "FLYING_RIGHT":
            return SPRITE_RIGHT[sprite.anim_frame % len(SPRITE_RIGHT)]
        if sprite.phase == "FLYING_LEFT":
            return SPRITE_LEFT[sprite.anim_frame % len(SPRITE_LEFT)]
        return SPRITE_PARKED

    def _get_sprite_style(self, sprite: FlightSprite) -> str:
        if sprite.phase == "PARKED":
            return "grey50"
        if sprite.phase == "FLYING_RIGHT":
            return "bold green"
        if sprite.phase == "FLYING_LEFT":
            return "dark_orange"
        return "grey50"

    # ── Render ────────────────────────────────────────────────────────

    def render(self) -> Text:
        try:
            w = self.size.width - 2
        except Exception:
            w = 80
        self._strip_width = max(40, w)
        sw = self._strip_width

        # Background
        park_end = int(PARK_PCT / 100 * sw)

        def _build_bg() -> list[str]:
            buf = []
            for i in range(sw):
                if i < park_end:
                    buf.append("▓")
                else:
                    buf.append(" ")
            return buf

        row_upper = _build_bg()
        row_main = _build_bg()

        # Zone labels
        label = "DECK"
        mid = park_end // 2 - len(label) // 2
        for i, ch in enumerate(label):
            pos = mid + i
            if 0 <= pos < sw:
                row_upper[pos] = ch

        tgt_col = int(TGT_PCT / 100 * sw)
        label2 = "TGT"
        mid2 = tgt_col + 3
        for i, ch in enumerate(label2):
            pos = mid2 + i
            if 0 <= pos < sw:
                row_upper[pos] = ch

        # Deck condensing
        deck_sprites = [s for s in self._sprites.values() if s.phase == "PARKED"]
        condense = len(deck_sprites) > 4
        condensed_count = len(deck_sprites) if condense else 0

        # Sprite overlays: (col, lane, text, style, id)
        overlays: list[tuple[int, int, str, str, str]] = []
        for sprite in self._sprites.values():
            if condense and sprite.phase == "PARKED":
                continue
            txt = self._get_sprite_text(sprite)
            style = self._get_sprite_style(sprite)
            overlays.append((sprite.col, sprite.lane, txt, style, sprite.pilot_id))

        if condense and condensed_count > 0:
            badge = f"[{condensed_count} ON DECK]"
            overlays.append((1, 0, badge, "bold grey70", "__badge__"))

        # Labels
        label_chars = [" "] * sw
        label_styles: dict[int, str] = {}
        for col, _lane, txt, _style, pid in overlays:
            if pid == "__badge__":
                continue
            sprite = self._sprites.get(pid)
            label = sprite.ticket_id if sprite and sprite.ticket_id else pid
            style = "dim green" if sprite and sprite.phase == "PARKED" else "bold bright_white"
            start = col + len(txt) // 2 - len(label) // 2
            for i, ch in enumerate(label):
                pos = start + i
                if 0 <= pos < sw:
                    label_chars[pos] = ch
                    label_styles[pos] = style

        # ── Compose output ─────────────────────────────────────────
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

        # Upper row (lane 1)
        result.append(" ║", style="dim green")
        self._render_row(result, "".join(row_upper), sw, overlays, 1)
        result.append("║\n", style="dim green")

        # Main row (lane 0)
        result.append(" ║", style="dim green")
        self._render_row(result, "".join(row_main), sw, overlays, 0)
        result.append("║\n", style="dim green")

        # Label row
        result.append(" ║", style="dim green")
        for pos in range(sw):
            ch = label_chars[pos]
            style = label_styles.get(pos, "dim green")
            result.append(ch, style=style)
        result.append("║\n", style="dim green")

        # Bottom border
        result.append(" ╚", style="dim green")
        result.append("═" * sw, style="dim green")
        result.append("╝", style="dim green")

        return result

    # ── Row rendering ────────────────────────────────────────────────

    def _zone_style(self, pos: int, sw: int) -> str:
        pct = pos / sw * 100 if sw > 0 else 0
        if pct < PARK_PCT:
            return "grey35"
        if 70 <= pct < 80:
            return "dim red"
        return "grey15"

    def _render_row(
        self,
        result: Text,
        bg: str,
        sw: int,
        sprites: list[tuple[int, int, str, str, str]],
        target_lane: int,
    ) -> None:
        overlays = sorted(
            [(col, col + len(txt), txt, style) for col, lane, txt, style, _ in sprites if lane == target_lane],
            key=lambda o: o[0],
        )

        pos = 0
        for start, end, txt, style in overlays:
            if start < pos:
                continue
            while pos < start and pos < sw:
                ch = bg[pos] if pos < len(bg) else " "
                st = "bold dark_green on grey11" if pos == self._sweep_col else self._zone_style(pos, sw)
                result.append(ch, style=st)
                pos += 1
            for ch in txt:
                if pos < sw:
                    st = style if pos != self._sweep_col else (style + " on grey11")
                    result.append(ch, style=st)
                    pos += 1
        while pos < sw:
            ch = bg[pos] if pos < len(bg) else " "
            st = "bold dark_green on grey11" if pos == self._sweep_col else self._zone_style(pos, sw)
            result.append(ch, style=st)
            pos += 1
