#!/usr/bin/env python3
"""
USS Tenkara Sentinel — persistent stream-json Haiku classifier.

Tails JSONL event streams for all managed worktree agents and feeds
new events to a persistent Haiku subprocess via claude --input-format
stream-json. Haiku classifies each agent's current status and writes
.sortie/sentinel-status.json in each worktree.

No ANTHROPIC_API_KEY needed — uses the claude CLI's existing auth.
Agents do NOT need to self-report status; sentinel handles it all.

Usage:
    python3 sentinel.py --project-dir /path/to/project [--verbose]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from parse_jsonl_metrics import CLAUDE_PROJECTS_DIR, encode_project_path
from read_sortie_state import read_sortie_state

log = logging.getLogger("sentinel")

# ── Config ────────────────────────────────────────────────────────────

DEBOUNCE_SECS    = 3.0    # Seconds after last JSONL write before sending to Haiku
IDLE_THRESHOLD   = 90     # Seconds of silence → send IDLE event to Haiku
SYNC_INTERVAL    = 30     # Seconds between worktree rescans
TAIL_EVENTS      = 15     # Max recent events to include in each Haiku message
RESTART_AFTER    = 300    # Restart Haiku subprocess after N messages (context mgmt)
HEARTBEAT_SECS   = 10     # How often to write sentinel-heartbeat.json
HAIKU_PING_SECS  = 60     # How often to health-check the Haiku subprocess

# ── Haiku system prompt ───────────────────────────────────────────────

_SYSTEM = """You are the USS Tenkara Sentinel — a real-time flight status observer for AI coding agent sessions.

You watch multiple pilot sessions. Each pilot works on a separate git branch with a ticket ID.

When you receive an activity report for a pilot, respond with ONLY a JSON object — no markdown, no explanation:
{"ticket": "<ticket-id>", "status": "AIRBORNE|HOLDING|ON_APPROACH|RECOVERED", "phase": "<what they're doing, 8 words max>"}

Status rules:
- AIRBORNE    — actively writing/editing code, running shell commands, making real changes
- HOLDING     — reading files, planning, thinking, researching, idle, waiting, blocked
- ON_APPROACH — running tests, git commit/push, opening PR, cleanup/wrap-up
- RECOVERED   — mission complete, session ended, explicitly done

Activity events you'll receive per pilot:
  TOOL <name>: <input>   — the pilot used a tool (Write/Edit = coding, Bash = running commands)
  AGENT: <text>          — the pilot narrated what it's doing
  ERROR: <msg>           — a tool failed (usually means debugging)
  IDLE <N>s              — no activity for N seconds
  SESSION_ENDED          — the bash EXIT trap fired, session is over

Reply with the JSON object only."""


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
    if size <= state.offset:
        return []
    try:
        with state.path.open(encoding="utf-8", errors="replace") as fh:
            fh.seek(state.offset)
            new = fh.read()
            state.offset = fh.tell()
    except OSError:
        return []
    return [l for l in new.splitlines() if l.strip()]


def _format_event(raw: str) -> Optional[str]:
    """Convert one raw JSONL line to a short event string for Haiku."""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None

    t = obj.get("type")

    if t == "assistant":
        content = obj.get("message", {}).get("content") or []
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input") or {}
                # Summarize input compactly
                if name in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
                    fp = inp.get("file_path") or inp.get("notebook_path", "")
                    summary = Path(fp).name if fp else ""
                elif name == "Bash":
                    summary = (inp.get("command") or "")[:80]
                elif name in ("Read", "Glob"):
                    summary = (inp.get("file_path") or inp.get("pattern", ""))[:60]
                elif name == "Grep":
                    summary = f'"{inp.get("pattern","")[:40]}"'
                elif name == "Agent":
                    summary = (inp.get("description") or inp.get("prompt", ""))[:60]
                else:
                    summary = ""
                parts.append(f"TOOL {name}: {summary}".rstrip(": "))
            elif block.get("type") == "text":
                text = (block.get("text") or "").strip()
                if text:
                    parts.append(f"AGENT: {text[:200]}")
        return "\n".join(parts) if parts else None

    if t == "user":
        content = obj.get("message", {}).get("content") or []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                if block.get("is_error") is True:
                    inner = block.get("content") or ""
                    if isinstance(inner, list):
                        inner = " ".join(b.get("text", "") for b in inner if isinstance(b, dict))
                    return f"ERROR: {str(inner)[:100]}"

    return None


# ── Per-worktree state ────────────────────────────────────────────────

@dataclass
class WatchState:
    ticket_id: str
    worktree_path: str
    tail: Optional[TailState] = None
    last_event_mono: float = field(default_factory=time.monotonic)
    pending_timer: Optional[threading.Timer] = None
    idle_notified: bool = False


# ── Haiku subprocess wrapper ──────────────────────────────────────────

class HaikuAgent:
    """Persistent claude --input-format stream-json subprocess."""

    def __init__(self, on_result) -> None:
        self._on_result = on_result   # callback(ticket_id, status, phase)
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._msg_count = 0
        self._reader: Optional[threading.Thread] = None
        self._start()

    def _start(self) -> None:
        log.info("starting Haiku subprocess")
        try:
            self._proc = subprocess.Popen(
                [
                    "claude",
                    "--input-format",  "stream-json",
                    "--output-format", "stream-json",
                    "--model",         "claude-haiku-4-5-20251001",
                    "--allowedTools",  "[]",          # read-only sentinel, no tools
                    "-p",              _SYSTEM,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except FileNotFoundError:
            log.error("claude CLI not found — sentinel disabled")
            self._proc = None
            return

        self._msg_count = 0
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        log.info("Haiku subprocess PID %d", self._proc.pid)

    def _read_loop(self) -> None:
        proc = self._proc
        if not proc:
            return
        try:
            for raw in proc.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type")
                if etype == "result":
                    result_text = event.get("result", "")
                    self._parse_result(result_text)
                elif etype == "system" and event.get("subtype") == "init":
                    log.debug("Haiku session init: %s", event.get("session_id", "?"))
        except Exception as e:
            log.warning("Haiku reader error: %s", e)

    def _parse_result(self, text: str) -> None:
        """Parse Haiku's JSON response and call on_result."""
        text = text.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1].lstrip("json").strip() if len(parts) > 1 else text
        try:
            obj = json.loads(text)
            ticket = obj.get("ticket", "")
            status = obj.get("status", "").upper()
            phase = obj.get("phase", "")
            if status in ("AIRBORNE", "HOLDING", "ON_APPROACH", "RECOVERED") and ticket:
                self._on_result(ticket, status, phase)
        except (json.JSONDecodeError, KeyError) as e:
            log.debug("parse_result failed: %s — raw: %s", e, text[:80])

    def send(self, ticket_id: str, events: list[str]) -> None:
        """Send a batch of formatted event lines to Haiku."""
        with self._lock:
            proc = self._proc
            if not proc or proc.poll() is not None:
                log.warning("Haiku proc dead — restarting")
                self._start()
                proc = self._proc
            if not proc:
                return

            # Restart periodically to avoid context overflow
            if self._msg_count >= RESTART_AFTER:
                log.info("restarting Haiku after %d messages", self._msg_count)
                proc.terminate()
                self._start()
                proc = self._proc
                if not proc:
                    return

            body = "\n".join(events)
            content = f"{ticket_id}:\n{body}"
            msg = {"type": "user", "message": {"role": "user", "content": content}}
            try:
                proc.stdin.write(json.dumps(msg) + "\n")
                proc.stdin.flush()
                self._msg_count += 1
            except (BrokenPipeError, OSError) as e:
                log.warning("Haiku stdin write failed: %s", e)

    @property
    def haiku_pid(self) -> Optional[int]:
        with self._lock:
            return self._proc.pid if self._proc else None

    @property
    def is_alive(self) -> bool:
        with self._lock:
            return bool(self._proc and self._proc.poll() is None)

    def health_check(self) -> bool:
        """Verify Haiku subprocess is alive; restart if dead. Returns True if healthy."""
        with self._lock:
            alive = bool(self._proc and self._proc.poll() is None)
        if not alive:
            log.warning("Haiku health-check failed — restarting")
            with self._lock:
                self._start()
            return False
        return True

    def shutdown(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()


# ── Sentinel daemon ───────────────────────────────────────────────────

class Sentinel:
    def __init__(self, project_dir: str) -> None:
        self._project_dir = project_dir
        self._watches: dict[str, WatchState] = {}
        self._lock = threading.Lock()
        self._haiku = HaikuAgent(on_result=self._on_haiku_result)

    # ── Haiku result handler ──────────────────────────────────────────

    def _on_haiku_result(self, ticket_id: str, status: str, phase: str) -> None:
        """Called from the Haiku reader thread with a fresh classification."""
        with self._lock:
            ws = self._watches.get(ticket_id)
        worktree = ws.worktree_path if ws else self._find_worktree(ticket_id)
        if not worktree:
            return
        self._write_status(worktree, {"status": status, "phase": phase})
        log.info("[%s] %s — %s", ticket_id, status, phase)

    def _find_worktree(self, ticket_id: str) -> Optional[str]:
        with self._lock:
            ws = self._watches.get(ticket_id)
        return ws.worktree_path if ws else None

    # ── Status writer ─────────────────────────────────────────────────

    def _write_status(self, worktree_path: str, status: dict) -> None:
        path = Path(worktree_path) / ".sortie" / "sentinel-status.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            status["timestamp"] = int(time.time())
            status["source"] = "sentinel"
            path.write_text(json.dumps(status))
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

        # Resolve JSONL paths for watches that don't have them yet
        with self._lock:
            watches = list(self._watches.values())
        for ws in watches:
            if ws.worktree_path and ws.tail is None:
                jsonl = self._find_jsonl(ws.worktree_path)
                if jsonl:
                    # Start tail at current EOF — don't replay old history
                    try:
                        offset = jsonl.stat().st_size
                    except OSError:
                        offset = 0
                    ws.tail = TailState(path=jsonl, offset=offset)
                    log.info("%s: tailing %s", ws.ticket_id, jsonl.name)

    def _find_jsonl(self, worktree_path: str) -> Optional[Path]:
        encoded = encode_project_path(worktree_path)
        project_dir = CLAUDE_PROJECTS_DIR / encoded
        if not project_dir.is_dir():
            return None
        files = sorted(project_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime if f.exists() else 0, reverse=True)
        return files[0] if files else None

    # ── Debounced classify trigger ────────────────────────────────────

    def _schedule(self, ws: WatchState) -> None:
        if ws.pending_timer:
            ws.pending_timer.cancel()
        t = threading.Timer(DEBOUNCE_SECS, self._flush, args=[ws.ticket_id])
        ws.pending_timer = t
        t.start()

    def _flush(self, ticket_id: str) -> None:
        """Read new JSONL lines, format them, send to Haiku."""
        with self._lock:
            ws = self._watches.get(ticket_id)
        if not ws:
            return

        with self._lock:
            ws.pending_timer = None

        # Check session-ended
        session_ended = (Path(ws.worktree_path) / ".sortie" / "session-ended").exists()
        if session_ended:
            self._haiku.send(ticket_id, ["SESSION_ENDED"])
            return

        if ws.tail is None:
            jsonl = self._find_jsonl(ws.worktree_path)
            if jsonl:
                ws.tail = TailState(path=jsonl, offset=0)
            else:
                return

        new_lines = _read_new_lines(ws.tail)
        events = []
        for line in new_lines[-TAIL_EVENTS:]:
            fmt = _format_event(line)
            if fmt:
                events.append(fmt)

        if not events:
            return

        ws.last_event_mono = time.monotonic()
        ws.idle_notified = False
        self._haiku.send(ticket_id, events[-TAIL_EVENTS:])

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
                # Unknown file — rescan and pick it up
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
        """Write sentinel-heartbeat.json to project .sortie/ so Mini Boss and
        the TUI can verify the sentinel is alive and see what it's watching."""
        path = Path(self._project_dir) / ".sortie" / "sentinel-heartbeat.json"
        with self._lock:
            watching = list(self._watches.keys())
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "pid":       os.getpid(),
                "haiku_pid": self._haiku.haiku_pid,
                "haiku_ok":  self._haiku.is_alive,
                "watching":  watching,
                "ts":        int(time.time()),
            }))
        except OSError:
            pass

    # ── Main loop ─────────────────────────────────────────────────────

    def run(self) -> None:
        log.info("sentinel starting — project: %s", self._project_dir)
        self.sync_worktrees()
        observer = self._start_watchdog()

        last_heartbeat   = 0.0
        last_health_check = 0.0
        last_sync        = 0.0
        last_idle_check  = 0.0

        # Tight inner loop (HEARTBEAT_SECS) drives all periodic work.
        # Each task only fires when its own interval has elapsed — the loop
        # itself is cheap (just a sleep + monotonic comparisons).
        try:
            while True:
                time.sleep(HEARTBEAT_SECS)
                now = time.monotonic()

                # Heartbeat — every HEARTBEAT_SECS (10s)
                self._write_heartbeat()
                last_heartbeat = now

                # Haiku health-check — every HAIKU_PING_SECS (60s)
                if now - last_health_check >= HAIKU_PING_SECS:
                    self._haiku.health_check()
                    last_health_check = now

                # Worktree rescan — every SYNC_INTERVAL (30s)
                if now - last_sync >= SYNC_INTERVAL:
                    self.sync_worktrees()
                    last_sync = now

                # IDLE event check — every HEARTBEAT_SECS (keeps idle detection snappy)
                if now - last_idle_check >= HEARTBEAT_SECS:
                    with self._lock:
                        watches = list(self._watches.values())
                    for ws in watches:
                        if ws.pending_timer:
                            continue
                        idle_secs = int(time.monotonic() - ws.last_event_mono)
                        if idle_secs >= IDLE_THRESHOLD and not ws.idle_notified:
                            ws.idle_notified = True
                            self._haiku.send(ws.ticket_id, [f"IDLE {idle_secs}s"])
                    last_idle_check = now

        except KeyboardInterrupt:
            pass
        finally:
            # Clear heartbeat so stale file isn't misread after restart
            hb = Path(self._project_dir) / ".sortie" / "sentinel-heartbeat.json"
            try:
                hb.unlink(missing_ok=True)
            except OSError:
                pass
            observer.stop()
            observer.join()
            self._haiku.shutdown()
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
