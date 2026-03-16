#!/usr/bin/env python3
"""
USS Tenkara Sentinel — real-time agent activity classifier.

Tails JSONL event streams for all managed worktree agents, maintains a
100-event rolling window per worktree, and classifies agent state using
deterministic rules (no LLM required).

Writes .sortie/sentinel-status.json in each worktree and
.sortie/sentinel-heartbeat.json in the project root for TUI health checks.

Usage:
    python3 sentinel.py --project-dir /path/to/project [--verbose]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from classify import classify, compress_events
from gate import gate_transition
from parse_jsonl_metrics import CLAUDE_PROJECTS_DIR, encode_project_path, find_latest_session_file
from read_sortie_state import read_sortie_state

log = logging.getLogger("sentinel")

# ── Config ────────────────────────────────────────────────────────────

DEBOUNCE_SECS     = 3.0   # Seconds after last JSONL write before classifying
IDLE_THRESHOLD    = 90    # Seconds of silence → write HOLDING / idle
SYNC_INTERVAL     = 30    # Seconds between worktree rescans
EVENT_WINDOW      = 100   # Rolling window size (events) per worktree
HEARTBEAT_SECS    = 10    # How often to write sentinel-heartbeat.json
PROPOSE_THRESHOLD = 3     # Consecutive identical proposals before calling gate


# ── JSONL tail reader ─────────────────────────────────────────────────

@dataclass
class TailState:
    path: Path
    offset: int = 0


def _read_new_lines(state: TailState) -> list[str]:
    """Read only bytes appended since last call. Returns new non-empty lines."""
    try:
        size = state.path.stat().st_size
    except OSError:
        return []
    if size < state.offset:
        # File truncated / rotated — reset to start
        state.offset = 0
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
    # Protected by Sentinel._lock when read/written across threads
    last_event_mono: float = field(default_factory=time.monotonic)
    pending_timer: Optional[threading.Timer] = None
    idle_notified: bool = False
    # Rolling 100-event window — fed by _flush, read by classify()
    recent_events: deque = field(default_factory=lambda: deque(maxlen=EVENT_WINDOW))
    # Gate state — confirmed status persists across flushes
    confirmed_status: str = ""
    confirmed_phase: str = ""
    last_transition_time: float = field(default_factory=time.monotonic)
    # Debounce counter: classifier must propose same new state N times before gate
    proposed_count: int = 0
    last_proposed: str = ""


# ── Sentinel daemon ───────────────────────────────────────────────────

def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON to a file atomically via tempfile + os.rename().

    Prevents torn reads when the TUI reads sentinel-status.json mid-write.
    """
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
        # Fallback: if rename fails (e.g., cross-device), try direct write
        try:
            os.unlink(tmp)
        except OSError:
            pass
        path.write_text(json.dumps(data))


class Sentinel:
    def __init__(self, project_dir: str) -> None:
        self._project_dir = project_dir
        self._watches: dict[str, WatchState] = {}
        self._lock = threading.Lock()

    # ── Status writer ─────────────────────────────────────────────────

    def _write_status(self, worktree_path: str, status: dict) -> None:
        path = Path(worktree_path) / ".sortie" / "sentinel-status.json"
        try:
            status["timestamp"] = int(time.time())
            status["source"] = "sentinel"
            _atomic_write_json(path, status)
        except OSError as e:
            log.warning("write_status failed for %s: %s", worktree_path, e)

    # ── Worktree discovery ────────────────────────────────────────────

    def sync_worktrees(self) -> None:
        try:
            state = read_sortie_state(project_dir=self._project_dir)
        except Exception as e:
            log.warning("sync_worktrees: %s", e)
            return

        with self._lock:
            seen: set[str] = set()
            for agent in state.agents:
                tid = agent.ticket_id
                seen.add(tid)
                if tid not in self._watches:
                    self._watches[tid] = WatchState(
                        ticket_id=tid,
                        worktree_path=agent.worktree_path or "",
                    )
                    log.info("tracking %s", tid)
            for gone in set(self._watches) - seen:
                ws = self._watches.pop(gone)
                if ws.pending_timer:
                    ws.pending_timer.cancel()
                log.info("dropped %s", gone)

        # Resolve JSONL paths for watches that don't have one yet
        with self._lock:
            watches = list(self._watches.values())
        for ws in watches:
            if ws.worktree_path and ws.tail is None:
                jsonl = find_latest_session_file(ws.worktree_path)
                if jsonl:
                    try:
                        offset = jsonl.stat().st_size
                    except OSError:
                        offset = 0
                    ws.tail = TailState(path=jsonl, offset=offset)
                    log.info("%s: tailing %s", ws.ticket_id, jsonl.name)

    # ── Debounced classify trigger ────────────────────────────────────

    def _schedule(self, ws: WatchState) -> None:
        if ws.pending_timer:
            ws.pending_timer.cancel()
        t = threading.Timer(DEBOUNCE_SECS, self._flush, args=[ws.ticket_id])
        ws.pending_timer = t
        t.start()

    def _flush(self, ticket_id: str) -> None:
        """Read new JSONL lines, feed rolling window, classify, write status.

        Two-layer anti-flicker:
          1. Debounce counter: classifier must propose same new state 3 consecutive
             times before Haiku gate is called (filters transient noise).
          2. Haiku gate: approves/denies with narrative context + confidence.
        """
        with self._lock:
            ws = self._watches.get(ticket_id)
        if not ws:
            return

        with self._lock:
            ws.pending_timer = None

        # Session ended — bypass gate + debounce, write RECOVERED directly
        if (Path(ws.worktree_path) / ".sortie" / "session-ended").exists():
            ws.confirmed_status = "RECOVERED"
            ws.confirmed_phase = "session ended"
            ws.proposed_count = 0
            self._write_status(ws.worktree_path, {"status": "RECOVERED", "phase": "session ended"})
            return

        if ws.tail is None:
            jsonl = find_latest_session_file(ws.worktree_path)
            if jsonl:
                ws.tail = TailState(path=jsonl, offset=0)
            else:
                return

        new_lines = _read_new_lines(ws.tail)
        if not new_lines:
            return

        # Parse and feed rolling window
        for line in new_lines:
            try:
                ws.recent_events.append(json.loads(line))
            except json.JSONDecodeError:
                pass

        with self._lock:
            ws.last_event_mono = time.monotonic()
            ws.idle_notified = False

        events = list(ws.recent_events)
        status, phase = classify(events)

        # First classification — accept directly, no gate needed
        if not ws.confirmed_status:
            ws.confirmed_status = status
            ws.confirmed_phase = phase
            ws.last_transition_time = time.monotonic()
            self._write_status(ws.worktree_path, {"status": status, "phase": phase})
            log.info("[%s] initial: %s — %s", ticket_id, status, phase)
            return

        # Same as confirmed — just update phase text
        if status == ws.confirmed_status:
            ws.proposed_count = 0
            ws.last_proposed = ""
            ws.confirmed_phase = phase
            self._write_status(ws.worktree_path, {"status": status, "phase": phase})
            return

        # Different from confirmed — debounce counter
        if status == ws.last_proposed:
            ws.proposed_count += 1
        else:
            ws.proposed_count = 1
            ws.last_proposed = status

        if ws.proposed_count < PROPOSE_THRESHOLD:
            # Hold confirmed status, update phase text only
            self._write_status(ws.worktree_path, {
                "status": ws.confirmed_status,
                "phase": phase,
            })
            log.debug("[%s] debounce %d/%d: %s → %s",
                      ticket_id, ws.proposed_count, PROPOSE_THRESHOLD,
                      ws.confirmed_status, status)
            return

        # Debounce threshold met — call Haiku gate
        time_in_current = time.monotonic() - ws.last_transition_time
        compressed = compress_events(events)

        result = gate_transition(
            current=ws.confirmed_status,
            proposed=status,
            time_in_current_secs=time_in_current,
            compressed=compressed,
        )

        if result.approved:
            ws.confirmed_status = result.final_status
            ws.confirmed_phase = result.phase if result.phase else phase
            ws.last_transition_time = time.monotonic()
            ws.proposed_count = 0
            ws.last_proposed = ""
            self._write_status(ws.worktree_path, {
                "status": ws.confirmed_status,
                "phase": ws.confirmed_phase,
            })
            log.info("[%s] gate approved: %s — %s (%s)",
                     ticket_id, ws.confirmed_status, ws.confirmed_phase, result.reason)
        else:
            # Denied — reset counter, hold confirmed status
            ws.proposed_count = 0
            ws.last_proposed = ""
            self._write_status(ws.worktree_path, {
                "status": ws.confirmed_status,
                "phase": ws.confirmed_phase,
            })
            log.info("[%s] gate denied: %s → %s (%s)",
                     ticket_id, ws.confirmed_status, status, result.reason)

    # ── JSONL watchdog ────────────────────────────────────────────────

    def _start_watchdog(self):
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        sentinel = self

        class JsonlHandler(FileSystemEventHandler):
            def _handle(self, path: str) -> None:
                if not path.endswith(".jsonl"):
                    return
                p = Path(path)
                with sentinel._lock:
                    watches = list(sentinel._watches.values())
                for ws in watches:
                    if ws.tail and ws.tail.path == p:
                        sentinel._schedule(ws)
                        return
                sentinel.sync_worktrees()

            def on_modified(self, event):
                if not event.is_directory:
                    self._handle(event.src_path)

            def on_created(self, event):
                if not event.is_directory:
                    self._handle(event.src_path)

        observer = Observer()
        if CLAUDE_PROJECTS_DIR.is_dir():
            observer.schedule(JsonlHandler(), str(CLAUDE_PROJECTS_DIR), recursive=True)
        observer.start()
        return observer

    # ── Heartbeat writer ──────────────────────────────────────────────

    def _write_heartbeat(self) -> None:
        path = Path(self._project_dir) / ".sortie" / "sentinel-heartbeat.json"
        with self._lock:
            watching = list(self._watches.keys())
        try:
            _atomic_write_json(path, {
                "pid":      os.getpid(),
                "watching": watching,
                "ts":       int(time.time()),
            })
        except OSError:
            pass

    # ── Main loop ─────────────────────────────────────────────────────

    def run(self) -> None:
        log.info("sentinel starting — project: %s", self._project_dir)
        self.sync_worktrees()
        observer = self._start_watchdog()

        last_sync       = 0.0
        last_idle_check = 0.0

        try:
            while True:
                time.sleep(HEARTBEAT_SECS)
                now = time.monotonic()

                self._write_heartbeat()

                if now - last_sync >= SYNC_INTERVAL:
                    self.sync_worktrees()
                    last_sync = now

                if now - last_idle_check >= HEARTBEAT_SECS:
                    with self._lock:
                        idle_candidates = [
                            (ws.ticket_id, ws.worktree_path,
                             int(now - ws.last_event_mono), ws.idle_notified)
                            for ws in self._watches.values()
                            if not ws.pending_timer
                        ]
                    for tid, wpath, idle_secs, already in idle_candidates:
                        # Don't overwrite RECOVERED — session already ended
                        if (Path(wpath) / ".sortie" / "session-ended").exists():
                            continue
                        # Don't write HOLDING for worktrees with no active JSONL
                        with self._lock:
                            ws = self._watches.get(tid)
                        if ws and ws.tail is None:
                            continue
                        if idle_secs >= IDLE_THRESHOLD and not already:
                            with self._lock:
                                ws = self._watches.get(tid)
                                if ws:
                                    ws.idle_notified = True
                                    # Clear window so stale events don't bleed
                                    # into the next active phase classification
                                    ws.recent_events.clear()
                                    # Bypass gate + debounce for idle timeout
                                    ws.confirmed_status = "HOLDING"
                                    ws.confirmed_phase = f"idle {idle_secs}s"
                                    ws.proposed_count = 0
                                    ws.last_proposed = ""
                            self._write_status(wpath, {
                                "status": "HOLDING",
                                "phase": f"idle {idle_secs}s",
                            })
                    last_idle_check = now

        except KeyboardInterrupt:
            pass
        finally:
            hb = Path(self._project_dir) / ".sortie" / "sentinel-heartbeat.json"
            try:
                hb.unlink(missing_ok=True)
            except OSError:
                pass
            observer.stop()
            observer.join()
            log.info("sentinel stopped")


# ── Entry point ───────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [sentinel] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="USS Tenkara Sentinel")
    parser.add_argument("--project-dir", default=os.getcwd())
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    Sentinel(project_dir=args.project_dir).run()
