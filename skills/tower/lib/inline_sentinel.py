"""USS Tenkara — Inline sentinel.

Replaces the sentinel.py subprocess with a lightweight background thread
that runs inside the TUI process. Same classification logic (classify.py),
same debounce counter, optional Haiku gate — but no subprocess management,
no heartbeat monitoring, no crash recovery, no orphaned PIDs.

The inline sentinel tails JSONL files for all managed worktrees, classifies
agent status using deterministic rules, and writes sentinel-status.json
to each worktree (same output as the subprocess sentinel).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from classify import classify, compress_events
from constants import TOKEN_BUDGET_DEFAULT, TOKEN_BUDGET_WARN_PCT, TOKEN_BUDGET_LAND_PCT
from gate import gate_transition
from parse_jsonl_metrics import encode_project_path, CLAUDE_PROJECTS_DIR, find_latest_session_file, parse_jsonl_metrics

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────

POLL_INTERVAL    = 2.0    # seconds between JSONL tail checks
IDLE_THRESHOLD   = 90     # seconds of silence → HOLDING
EVENT_WINDOW     = 100    # rolling window size per worktree
PROPOSE_THRESHOLD = 3     # consecutive proposals before gate check


# ── JSONL tail reader ─────────────────────────────────────────────────

@dataclass
class TailState:
    path: Path
    offset: int = 0


def _read_new_lines(state: TailState) -> list[str]:
    """Read only bytes appended since last call."""
    try:
        size = state.path.stat().st_size
    except OSError:
        return []
    if size < state.offset:
        state.offset = 0  # file rotated
    if size == state.offset:
        return []
    try:
        with state.path.open(encoding="utf-8", errors="replace") as fh:
            fh.seek(state.offset)
            new = fh.read()
            state.offset = fh.tell()
    except OSError:
        return []
    return [l for l in new.splitlines() if l.strip()]


# ── Per-worktree state ────────────────────────────────────────────────

@dataclass
class WatchState:
    ticket_id: str
    worktree_path: str
    tail: Optional[TailState] = None
    last_event_time: float = field(default_factory=time.monotonic)
    idle_notified: bool = False
    recent_events: deque = field(default_factory=lambda: deque(maxlen=EVENT_WINDOW))
    # Gate state
    confirmed_status: str = ""
    confirmed_phase: str = ""
    last_transition_time: float = field(default_factory=time.monotonic)
    proposed_count: int = 0
    last_proposed: str = ""


# ── Atomic write ──────────────────────────────────────────────────────

def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically via tempfile + os.rename()."""
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            os.write(fd, json.dumps(data).encode("utf-8"))
        finally:
            os.close(fd)
        os.rename(tmp, str(path))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        path.write_text(json.dumps(data))


# ── Inline sentinel ──────────────────────────────────────────────────

class InlineSentinel:
    """Background thread that classifies agent status from JSONL streams.

    Drop-in replacement for the sentinel.py subprocess. Runs inside the
    TUI process — no subprocess management needed.
    """

    def __init__(self, project_dir: str, on_status_change: Optional[Callable] = None) -> None:
        self._project_dir = project_dir
        self._watches: dict[str, WatchState] = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._on_status_change = on_status_change

    def start(self) -> None:
        """Start the sentinel background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("Inline sentinel started for %s", self._project_dir)

    def stop(self) -> None:
        """Stop the sentinel."""
        self._running = False

    @property
    def is_alive(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    @property
    def watching_count(self) -> int:
        with self._lock:
            return len(self._watches)

    def add_worktree(self, ticket_id: str, worktree_path: str) -> None:
        """Register a worktree to watch."""
        with self._lock:
            if ticket_id not in self._watches:
                self._watches[ticket_id] = WatchState(
                    ticket_id=ticket_id,
                    worktree_path=worktree_path,
                )
                log.info("Inline sentinel tracking %s", ticket_id)

    def remove_worktree(self, ticket_id: str) -> None:
        """Stop watching a worktree."""
        with self._lock:
            self._watches.pop(ticket_id, None)

    def _run(self) -> None:
        """Main loop — poll JSONL files and classify."""
        while self._running:
            try:
                self._tick()
            except Exception as e:
                log.warning("Inline sentinel tick error: %s", e)
            time.sleep(POLL_INTERVAL)

    def _tick(self) -> None:
        """One classification cycle."""
        with self._lock:
            watches = list(self._watches.values())

        now = time.monotonic()

        for ws in watches:
            # Resolve JSONL file if not yet found
            if ws.tail is None and ws.worktree_path:
                abs_wt = ws.worktree_path
                if not Path(abs_wt).is_absolute():
                    abs_wt = str(Path(self._project_dir) / abs_wt)
                jsonl = find_latest_session_file(abs_wt)
                if jsonl:
                    try:
                        offset = jsonl.stat().st_size
                    except OSError:
                        offset = 0
                    ws.tail = TailState(path=jsonl, offset=offset)

            if ws.tail is None:
                continue

            # Check for session-ended
            wt_path = ws.worktree_path
            if not Path(wt_path).is_absolute():
                wt_path = str(Path(self._project_dir) / wt_path)
            if (Path(wt_path) / ".sortie" / "session-ended").exists():
                if ws.confirmed_status != "RECOVERED":
                    ws.confirmed_status = "RECOVERED"
                    ws.confirmed_phase = "session ended"
                    self._write_status(wt_path, {"status": "RECOVERED", "phase": "session ended"})
                continue

            # Read new JSONL lines
            new_lines = _read_new_lines(ws.tail)
            if not new_lines:
                # Check idle timeout
                elapsed = now - ws.last_event_time
                if elapsed >= IDLE_THRESHOLD and not ws.idle_notified:
                    ws.idle_notified = True
                    ws.confirmed_status = "HOLDING"
                    ws.confirmed_phase = f"idle {int(elapsed)}s"
                    ws.recent_events.clear()
                    self._write_status(wt_path, {
                        "status": "HOLDING",
                        "phase": f"idle {int(elapsed)}s",
                    })
                continue

            # Parse and feed rolling window
            for line in new_lines:
                try:
                    ws.recent_events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

            ws.last_event_time = now
            ws.idle_notified = False

            # Classify
            events = list(ws.recent_events)
            status, phase = classify(events)

            # First classification — accept directly
            if not ws.confirmed_status:
                ws.confirmed_status = status
                ws.confirmed_phase = phase
                ws.last_transition_time = now
                self._write_status(wt_path, {"status": status, "phase": phase})
                continue

            # Same as confirmed — just update phase
            if status == ws.confirmed_status:
                ws.proposed_count = 0
                ws.last_proposed = ""
                ws.confirmed_phase = phase
                self._write_status(wt_path, {"status": status, "phase": phase})
                continue

            # Different — debounce counter
            if status == ws.last_proposed:
                ws.proposed_count += 1
            else:
                ws.proposed_count = 1
                ws.last_proposed = status

            if ws.proposed_count < PROPOSE_THRESHOLD:
                self._write_status(wt_path, {
                    "status": ws.confirmed_status,
                    "phase": phase,
                })
                continue

            # Debounce met — call gate (optional)
            time_in_current = now - ws.last_transition_time
            compressed = compress_events(events)

            result = gate_transition(
                current=ws.confirmed_status,
                proposed=status,
                time_in_current_secs=time_in_current,
                compressed=compressed,
            )

            if result.approved:
                old_status = ws.confirmed_status
                ws.confirmed_status = result.final_status
                ws.confirmed_phase = result.phase if result.phase else phase
                ws.last_transition_time = now
                ws.proposed_count = 0
                ws.last_proposed = ""
                self._write_status(wt_path, {
                    "status": ws.confirmed_status,
                    "phase": ws.confirmed_phase,
                })
                if self._on_status_change:
                    try:
                        self._on_status_change(ws.ticket_id, old_status, ws.confirmed_status, ws.confirmed_phase)
                    except Exception:
                        pass
            else:
                ws.proposed_count = 0
                ws.last_proposed = ""
                self._write_status(wt_path, {
                    "status": ws.confirmed_status,
                    "phase": ws.confirmed_phase,
                })

            self._check_budget(ws)

    def _check_budget(self, ws: WatchState) -> None:
        """Check token budget and write warnings to sortie comm channel."""
        wt_path = ws.worktree_path
        if not Path(wt_path).is_absolute():
            wt_path = str(Path(self._project_dir) / wt_path)

        # Read budget (default if not set)
        budget_file = Path(wt_path) / ".sortie" / "budget.txt"
        try:
            budget = int(budget_file.read_text().strip())
        except (OSError, ValueError):
            budget = TOKEN_BUDGET_DEFAULT

        metrics = parse_jsonl_metrics(wt_path)
        if metrics is None:
            return

        pct = metrics.total_tokens / budget if budget > 0 else 0

        fuel_file = Path(wt_path) / ".sortie" / "fuel-warning.txt"
        if pct >= TOKEN_BUDGET_LAND_PCT:
            if not fuel_file.exists() or fuel_file.read_text().strip() != "BINGO":
                fuel_file.write_text("BINGO")
                log.info("BINGO FUEL for %s (%.0f%% of %d)", ws.ticket_id, pct * 100, budget)
        elif pct >= TOKEN_BUDGET_WARN_PCT:
            if not fuel_file.exists() or fuel_file.read_text().strip() != "WARNING":
                fuel_file.write_text("WARNING")
                log.info("FUEL WARNING for %s (%.0f%% of %d)", ws.ticket_id, pct * 100, budget)

    def _write_status(self, worktree_path: str, status: dict) -> None:
        """Write sentinel-status.json atomically."""
        path = Path(worktree_path) / ".sortie" / "sentinel-status.json"
        try:
            status["timestamp"] = int(time.time())
            status["source"] = "sentinel"
            _atomic_write_json(path, status)
        except OSError as e:
            log.warning("write_status failed for %s: %s", worktree_path, e)
