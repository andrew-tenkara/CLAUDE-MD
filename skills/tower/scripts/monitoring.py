"""Monitoring — file watchers, sentinel lifecycle, and idle-agent checks.

Extracted from commander-dashboard.py to keep the main TUI module focused
on rendering and interaction.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time as time_mod
import threading
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

from parse_jsonl_metrics import encode_project_path, CLAUDE_PROJECTS_DIR
from read_sortie_state import get_worktrees_root

log = logging.getLogger(__name__)


# ── Debounced file watcher ──────────────────────────────────────────

class _WorktreeFileHandler(FileSystemEventHandler):
    """Debounced file watcher for sortie worktree state changes."""
    DEBOUNCE_SECONDS = 0.5

    def __init__(self, app) -> None:
        super().__init__()
        self._app = app
        self._last_event: float = 0.0
        self._pending: bool = False
        self._lock = threading.Lock()

    def _should_trigger(self, path: str) -> bool:
        p = Path(path)
        return (
            p.suffix == ".jsonl"
            or p.name in (
                "context.json", "progress.md", "model.txt",
                "pre-review.done", "post-review.done", "directive.md",
                "status-hint.txt", "server-status.txt", "flight-status.json",
                "session-ended", "command.json",
            )
            # Mission queue + managed servers
            or (p.suffix == ".json" and ("mission-queue" in str(p) or p.name == "managed-servers.json"))
        )

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._should_trigger(event.src_path):
            self._debounced_refresh()

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._should_trigger(event.src_path):
            self._debounced_refresh()

    def _debounced_refresh(self) -> None:
        now = time_mod.monotonic()
        with self._lock:
            self._last_event = now
            if self._pending:
                return
            self._pending = True

        def _fire():
            while True:
                time_mod.sleep(self.DEBOUNCE_SECONDS)
                with self._lock:
                    elapsed = time_mod.monotonic() - self._last_event
                    if elapsed >= self.DEBOUNCE_SECONDS:
                        self._pending = False
                        break
            try:
                self._app.call_from_thread(self._app._sync_legacy_agents)
            except Exception:
                pass

        threading.Thread(target=_fire, daemon=True).start()


# ── Monitoring facade ───────────────────────────────────────────────

class Monitoring:
    """Groups file-watching, sentinel lifecycle, and idle-agent checks.

    All methods operate on the app instance (`ctx`) passed at construction
    time, keeping the same runtime behaviour as the original inline methods.
    """

    def __init__(self, ctx) -> None:
        self.ctx = ctx

    # ── Managed servers ─────────────────────────────────────────────

    def sync_managed_servers(self) -> None:
        """Read .sortie/managed-servers.json and map server URLs to pilots."""
        ctx = self.ctx
        servers_file = Path(ctx._project_dir) / ".sortie" / "managed-servers.json"
        try:
            entries = json.loads(servers_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(entries, list):
            return

        # Build ticket_id -> server info map
        server_map: dict[str, str] = {}
        for entry in entries:
            tid = entry.get("ticket_id", "")
            url = entry.get("url", "")
            note = entry.get("note", "")
            if tid and url:
                label = url if not note else f"{url} ({note})"
                server_map[tid] = label

        # Apply to pilots — append to existing hint, don't clobber
        for pilot in ctx._roster.all_pilots():
            server_label = server_map.get(pilot.ticket_id)
            if server_label:
                # Avoid duplicating if already present
                if server_label not in (pilot.status_hint or ""):
                    if pilot.status_hint:
                        pilot.status_hint = f"{pilot.status_hint} | {server_label}"
                    else:
                        pilot.status_hint = server_label

    # ── File watchers ───────────────────────────────────────────────

    def start_watchers(self) -> None:
        """Set up watchdog observers for worktree and JSONL directories."""
        ctx = self.ctx

        handler = _WorktreeFileHandler(ctx)
        ctx._observer = Observer()

        # Watch worktrees directory
        worktrees_root = get_worktrees_root(ctx._project_dir)
        if worktrees_root.is_dir():
            ctx._observer.schedule(handler, str(worktrees_root), recursive=True)

        # Watch project .sortie/ dir (mission-queue/, managed-servers.json)
        sortie_dir = Path(ctx._project_dir) / ".sortie"
        sortie_dir.mkdir(parents=True, exist_ok=True)
        (sortie_dir / "mission-queue").mkdir(parents=True, exist_ok=True)
        ctx._observer.schedule(handler, str(sortie_dir), recursive=True)

        # Watch JSONL directories for known agents
        for agent in ctx._legacy_agents.values():
            encoded = encode_project_path(agent.worktree_path)
            jsonl_dir = CLAUDE_PROJECTS_DIR / encoded
            dir_str = str(jsonl_dir)
            if jsonl_dir.is_dir() and dir_str not in ctx._watched_jsonl_dirs:
                ctx._observer.schedule(handler, dir_str, recursive=True)
                ctx._watched_jsonl_dirs.add(dir_str)

        try:
            ctx._observer.start()
        except Exception:
            ctx._observer = None

    # ── Sentinel lifecycle ──────────────────────────────────────────

    def start_sentinel(self) -> None:
        """Launch sentinel.py as a background subprocess tied to this TUI session.

        sentinel.py spawns a persistent claude --input-format stream-json Haiku
        subprocess and feeds it JSONL events from all managed worktrees. It writes
        .sortie/sentinel-status.json to each worktree so pilots don't need to
        self-report status.
        """
        ctx = self.ctx
        sentinel_script = Path(__file__).parent / "sentinel.py"
        if not sentinel_script.exists():
            ctx._add_radio("PRI-FLY", "SENTINEL — script not found, skipping", "system")
            return

        try:
            import subprocess
            proc = subprocess.Popen(
                [sys.executable, str(sentinel_script), "--project-dir", ctx._project_dir],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,   # isolated process group — not killed by TUI Ctrl-C
            )
            ctx._sentinel_pid = proc.pid
            ctx._add_radio("PRI-FLY", f"SENTINEL — Haiku classifier online (PID {proc.pid})", "system")
        except Exception as e:
            ctx._add_radio("PRI-FLY", f"SENTINEL — failed to launch: {e}", "system")

    def check_sentinel_health(self) -> None:
        """Watchdog: verify sentinel process is alive; relaunch if dead.

        Checks two ways:
        1. os.kill(pid, 0) — process still exists
        2. sentinel-heartbeat.json age < 60s — process is actually looping

        If either fails, relaunches the sentinel and logs to radio.
        Called from a timer — must not raise or the timer dies.
        """
        try:
            self.check_sentinel_health_inner()
        except Exception as e:
            log.warning("Sentinel health check error: %s", e)

    def check_sentinel_health_inner(self) -> None:
        import subprocess as _sp
        ctx = self.ctx

        dead = False
        reason = ""

        if ctx._sentinel_pid:
            try:
                os.kill(ctx._sentinel_pid, 0)
            except ProcessLookupError:
                dead = True
                reason = f"PID {ctx._sentinel_pid} gone"
            except (PermissionError, OSError):
                pass  # Process exists but we don't own it, or other OS error — treat as alive

        # Also check heartbeat file age
        hb_path = Path(ctx._project_dir) / ".sortie" / "sentinel-heartbeat.json"
        try:
            hb = json.loads(hb_path.read_text(encoding="utf-8"))
            age = int(time_mod.time()) - hb.get("ts", 0)
            if age > 60:
                dead = True
                reason = f"heartbeat stale ({age}s)"
            elif not hb.get("haiku_ok"):
                # Sentinel alive but Haiku dead — sentinel will self-heal, just log
                ctx._add_radio("SENTINEL", "Haiku sub-process restarting", "system")
        except (OSError, json.JSONDecodeError, KeyError):
            # No heartbeat yet (startup) — don't treat as dead until pid check fails
            pass

        if dead:
            ctx._add_radio("SENTINEL", f"dead ({reason}) — relaunching", "system")
            ctx._sentinel_pid = None
            self.start_sentinel()

    # ── JSONL directory watcher ─────────────────────────────────────

    def watch_agent_jsonl(self, worktree_path: str) -> None:
        """Register a watchdog on an agent's JSONL directory for immediate telemetry.

        Called right after opening an agent pane so we don't have to wait
        for the next _sync_legacy_agents cycle to start tracking JSONL updates.
        """
        ctx = self.ctx
        if not ctx._observer:
            return
        encoded = encode_project_path(worktree_path)
        jsonl_dir = CLAUDE_PROJECTS_DIR / encoded
        dir_str = str(jsonl_dir)
        if dir_str in ctx._watched_jsonl_dirs:
            return
        # The JSONL dir may not exist yet (Claude creates it on first write).
        # Watch the parent (project-level) dir to catch creation.
        watch_target = jsonl_dir if jsonl_dir.is_dir() else CLAUDE_PROJECTS_DIR
        target_str = str(watch_target)
        if target_str not in ctx._watched_jsonl_dirs:
            try:
                handler = _WorktreeFileHandler(ctx)
                ctx._observer.schedule(handler, target_str, recursive=True)
                ctx._watched_jsonl_dirs.add(target_str)
            except Exception as e:
                log.warning(f"Failed to watch JSONL dir {target_str}: {e}")
        ctx._watched_jsonl_dirs.add(dir_str)

    # ── Idle agent checks ───────────────────────────────────────────

    def check_idle_agents(self) -> None:
        """Called from a timer — must not raise or the timer dies."""
        try:
            self.check_idle_agents_inner()
        except Exception as e:
            log.warning("Idle check error: %s", e)

    def check_idle_agents_inner(self) -> None:
        ctx = self.ctx
        if not ctx._auto_compact:
            return
        now = time_mod.time()
        for pilot in ctx._roster.all_pilots():
            if (
                pilot.status == "AIRBORNE"
                and pilot.fuel_pct < ctx._auto_compact_threshold
                and pilot.last_tool_at > 0
                and (now - pilot.last_tool_at) > ctx._auto_compact_idle
            ):
                agent = ctx._agent_mgr.get(pilot.callsign)
                if agent and not agent.active_subagents:
                    ctx._trigger_compact(pilot.callsign)

        # Auto-deploy from queue
        if ctx._mission_queue.auto_deploy_enabled:
            active_count = len(ctx._agent_mgr.active_agents())
            while ctx._mission_queue.should_auto_deploy(active_count):
                mission = ctx._mission_queue.next()
                if not mission:
                    break
                mission.status = "DEPLOYING"
                # Deploy each directive
                for directive in mission.directives or [mission.spec_content]:
                    ctx._cmd_deploy([mission.id, "--model", mission.model])
                active_count += 1
