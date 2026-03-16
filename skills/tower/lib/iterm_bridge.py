from __future__ import annotations
import json, os, subprocess, sys, time as time_mod
from pathlib import Path
from typing import TYPE_CHECKING
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pilot_roster import get_pilot_launch_quote
if TYPE_CHECKING:
    from pilot_roster import Pilot


class ItermBridge:
    """Thin bridge that manages iTerm2 pane lifecycle on behalf of the app."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx

    def open_comms(self, callsign: str) -> None:
        """Open a chat-relay pane for a stream-json agent (Mini Boss only)."""
        agent = self.ctx._agent_mgr.get(callsign)
        if not agent:
            return

        if callsign in self.ctx._iterm_panes:
            return

        relay_script = str(Path(__file__).resolve().parent.parent / "scripts" / "chat-relay.py")
        comm_dir = f"/tmp/uss-tenkara/{callsign}"
        cmd = f"python3 '{relay_script}' --callsign '{callsign}' --dir '{comm_dir}'"
        self.pane_cmd(callsign, cmd)

    def open_agent_pane(self, pilot: "Pilot") -> None:
        """Open an interactive Claude CLI session in an iTerm2 pane.

        If the worktree is already prepped (e.g., from /tq or deploy-agent.sh),
        just run the existing launch.sh. Otherwise creates a git worktree,
        writes .sortie/ protocol files, and launches claude.
        """
        if pilot.callsign in self.ctx._iterm_panes:
            return

        # ── Fast path: worktree already prepped (from /tq) ───────────
        if pilot.worktree_path:
            launch_script = Path(pilot.worktree_path) / ".sortie" / "launch.sh"
            if launch_script.exists():
                # Clear stale session-ended from previous run
                session_ended = Path(pilot.worktree_path) / ".sortie" / "session-ended"
                if session_ended.exists():
                    session_ended.unlink()
                # Build Top Gun splash for the iTerm pane
                p_quote, p_attr = get_pilot_launch_quote()
                p_quote = p_quote.replace("'", "'\\''")
                p_attr = p_attr.replace("'", "'\\''")
                splash_script = Path(pilot.worktree_path) / ".sortie" / "splash.sh"
                splash_script.write_text(
                    "#!/usr/bin/env bash\n"
                    "printf '\\n'\n"
                    "printf '\\033[1;33m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\\033[0m\\n'\n"
                    "printf '\\033[1;31m        ╔══╗  ╔══╗  ╔══╗  ╔══╗  ╔══╗  ╔══╗  ╔══╗        \\033[0m\\n'\n"
                    "printf '\\033[1;37m           ★ USS TENKARA — FLIGHT OPS ★                   \\033[0m\\n'\n"
                    f"printf '\\033[1;36m        CALLSIGN: {pilot.callsign}\\033[0m\\n'\n"
                    f"printf '\\033[1;35m        SQUADRON: {pilot.squadron}\\033[0m\\n'\n"
                    f"printf '\\033[1;33m        MODEL:    {pilot.model.upper()}\\033[0m\\n'\n"
                    f"printf '\\033[2;37m        TRAIT:    {pilot.trait}\\033[0m\\n'\n"
                    "printf '\\033[1;31m        ╚══╝  ╚══╝  ╚══╝  ╚══╝  ╚══╝  ╚══╝  ╚══╝        \\033[0m\\n'\n"
                    "printf '\\033[1;33m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\\033[0m\\n'\n"
                    f"printf '\\033[1;37m  \"{p_quote}\"\\033[0m\\n'\n"
                    f"printf '\\033[2;37m                          — {p_attr}\\033[0m\\n'\n"
                    "printf '\\033[1;33m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\\033[0m\\n'\n"
                    "printf '\\n'\n"
                    "sleep 1\n"
                )
                splash_script.chmod(0o755)
                cmd = f"bash '{splash_script}' && bash '{launch_script}'"
                self.pane_cmd(pilot.callsign, cmd)
                self.ctx._watch_agent_jsonl(pilot.worktree_path)
                self.ctx._add_radio(pilot.callsign, "Launching from prepped worktree", "success")
                return

        scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
        sortie_scripts = Path.home() / ".claude" / "skills" / "sortie" / "scripts"

        # ── Create worktree ──────────────────────────────────────────────
        # Use the sortie create-worktree.sh if available
        ticket_id = pilot.ticket_id or pilot.callsign
        branch_name = f"sortie/{ticket_id}"
        worktree_script = sortie_scripts / "create-worktree.sh"

        worktree_path = None
        if worktree_script.exists():
            try:
                result = subprocess.run(
                    ["bash", str(worktree_script), ticket_id, branch_name, "dev",
                     "--model", pilot.model],
                    capture_output=True, text=True, timeout=30,
                    cwd=self.ctx._project_dir,
                )
                # Parse WORKTREE_CREATED or WORKTREE_EXISTS from output
                for line in result.stdout.splitlines():
                    if line.startswith("WORKTREE_CREATED:") or line.startswith("WORKTREE_EXISTS:"):
                        worktree_path = line.split(":", 1)[1]
                        break
                if result.returncode == 2 and "WORKTREE_EXISTS" in result.stdout:
                    # Existing worktree — resume
                    for line in result.stdout.splitlines():
                        if line.startswith("WORKTREE_EXISTS:"):
                            worktree_path = line.split(":", 1)[1]
                            break
            except Exception as e:
                self.ctx._add_radio("PRI-FLY", f"Worktree creation failed: {e}", "error")

        if not worktree_path:
            # Fallback — use project dir directly
            worktree_path = self.ctx._project_dir
            self.ctx._add_radio("PRI-FLY", f"No worktree — {pilot.callsign} using project dir", "system")

        # ── Write .sortie/ protocol files ────────────────────────────────
        sortie_dir = Path(worktree_path) / ".sortie"
        sortie_dir.mkdir(parents=True, exist_ok=True)

        # Clear stale session-ended sentinel from previous run
        session_ended = sortie_dir / "session-ended"
        if session_ended.exists():
            session_ended.unlink()

        # Directive + flight status protocol
        flight_protocol = (
            "\n\n---\n"
            "## Flight Status Protocol\n"
            "Report your flight status by writing to `.sortie/flight-status.json`:\n"
            '```json\n{"status": "AIRBORNE", "phase": "implementing auth refresh", "timestamp": 1710345600}\n```\n'
            "Valid statuses: PREFLIGHT, AIRBORNE, HOLDING, ON_APPROACH, RECOVERED\n"
            "Update on meaningful phase transitions only (starting new task area, running tests, "
            "submitting PR, blocked, done). Do NOT update on every tool call.\n"
            "Use unix timestamp (seconds). Phase is a short human-readable description of what you're doing.\n"
            "PREFLIGHT is set automatically before launch — do not write it yourself.\n"
            "Write AIRBORNE only when you start actively making changes (editing files, running commands, writing code). "
            "Reading context, reading tickets, reading files, and planning are all still PREFLIGHT.\n"
            "Write HOLDING when you are waiting/blocked/idle.\n"
            "NEVER write RECOVERED — that is set automatically when your session ends.\n"
            "When your mission is complete, write HOLDING with phase 'mission complete — awaiting orders'.\n"
        )
        (sortie_dir / "directive.md").write_text(pilot.directive + flight_protocol)

        # Progress
        progress_file = sortie_dir / "progress.md"
        if not progress_file.exists():
            progress_file.write_text("")

        # Model
        (sortie_dir / "model.txt").write_text(pilot.model)

        # Set PREFLIGHT status — agent is on deck, not yet airborne
        (sortie_dir / "flight-status.json").write_text(
            json.dumps({"status": "PREFLIGHT", "phase": "on deck — pre-launch checks", "timestamp": int(time_mod.time())})
        )

        # ── Write settings (branch-scoped push permission) ───────────────
        settings_script = sortie_scripts / "write-settings.sh"
        if settings_script.exists():
            try:
                subprocess.run(
                    ["bash", str(settings_script), branch_name],
                    capture_output=True, text=True, timeout=10,
                    cwd=worktree_path,
                )
            except Exception:
                pass

        # ── Build launch script (identical to /sortie) ───────────────────
        disallowed = (
            "'Bash(git push --force*)' 'Bash(git push -f *)' "
            "'Bash(git push *--force*)' 'Bash(git push *-f *)' "
            "'Bash(git branch -D:*)' 'Bash(git branch -d:*)' "
            "'Bash(git branch --delete:*)' 'Bash(git clean:*)' "
            "'Bash(git reset --hard:*)' 'Bash(git checkout -- :*)' "
            "'Bash(git restore:*)' 'Bash(rm:*)' 'Bash(rm )' "
            "'Bash(rmdir:*)' 'Bash(unlink:*)' 'Bash(trash:*)' "
            "'Bash(sudo:*)' 'Bash(chmod:*)' 'Bash(chown:*)'"
        )

        kickoff = f"Read {sortie_dir}/directive.md and follow all instructions. Track progress in {sortie_dir}/progress.md"

        # Random pilot quote (escape single quotes for bash printf)
        p_quote, p_attr = get_pilot_launch_quote()
        p_quote = p_quote.replace("'", "'\\''")
        p_attr = p_attr.replace("'", "'\\''")

        # Top Gun splash + launch
        splash = (
            "printf '\\n'\n"
            "printf '\\033[1;33m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\\033[0m\\n'\n"
            "printf '\\033[1;31m        ╔══╗  ╔══╗  ╔══╗  ╔══╗  ╔══╗  ╔══╗  ╔══╗        \\033[0m\\n'\n"
            "printf '\\033[1;37m           ★ USS TENKARA — FLIGHT OPS ★                   \\033[0m\\n'\n"
            f"printf '\\033[1;36m        CALLSIGN: {pilot.callsign}\\033[0m\\n'\n"
            f"printf '\\033[1;35m        SQUADRON: {pilot.squadron}\\033[0m\\n'\n"
            f"printf '\\033[1;33m        MODEL:    {pilot.model.upper()}\\033[0m\\n'\n"
            f"printf '\\033[2;37m        TRAIT:    {pilot.trait}\\033[0m\\n'\n"
            "printf '\\033[1;31m        ╚══╝  ╚══╝  ╚══╝  ╚══╝  ╚══╝  ╚══╝  ╚══╝        \\033[0m\\n'\n"
            "printf '\\033[1;33m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\\033[0m\\n'\n"
            f"printf '\\033[1;37m  \"{p_quote}\"\\033[0m\\n'\n"
            f"printf '\\033[2;37m                          — {p_attr}\\033[0m\\n'\n"
            "printf '\\033[1;33m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\\033[0m\\n'\n"
            "printf '\\n'\n"
            "sleep 1\n"
        )

        launch_script = sortie_dir / "launch.sh"
        launch_script.write_text(
            f"#!/usr/bin/env bash\n"
            f"cd '{worktree_path}'\n"
            f"\n"
            f"# Worktree env setup — symlink .env.local + install deps\n"
            f"if [ ! -f .env.local ] && [ -f '{self.ctx._project_dir}/.env.local' ]; then\n"
            f"  ln -sf '{self.ctx._project_dir}/.env.local' .env.local\n"
            f"  echo '✓ Symlinked .env.local from base project'\n"
            f"fi\n"
            f"if [ -f pnpm-lock.yaml ]; then\n"
            f"  if [ ! -d node_modules ] || [ pnpm-lock.yaml -nt node_modules ]; then\n"
            f"    echo '📦 Installing dependencies...'\n"
            f"    pnpm install --frozen-lockfile 2>/dev/null || pnpm install\n"
            f"  fi\n"
            f"fi\n"
            f"\n"
            f"# Set PREFLIGHT status — agent is on deck, not yet airborne\n"
            f"mkdir -p .sortie\n"
            f"echo '{{\"status\": \"PREFLIGHT\", \"phase\": \"on deck — pre-launch checks\", \"timestamp\": '\"$(date +%s)\"'}}' > .sortie/flight-status.json\n"
            f"\n"
            f"# Cleanup on exit — signal session ended so dashboard sets RECOVERED\n"
            f"cleanup_flight() {{\n"
            f"  touch .sortie/session-ended\n"
            f"}}\n"
            f"trap cleanup_flight EXIT\n"
            f"\n"
            f"{splash}"
            f"claude --model {pilot.model} '{kickoff}' "
            f"--disallowedTools {disallowed}\n"
        )
        launch_script.chmod(0o755)

        # Store worktree path + set initial preflight state
        pilot.worktree_path = str(worktree_path)
        pilot.flight_status = "PREFLIGHT"
        pilot.flight_phase = "on deck — pre-launch checks"
        self.ctx._watch_agent_jsonl(str(worktree_path))

        cmd = f"bash '{launch_script}'"
        self.pane_cmd(pilot.callsign, cmd)

    def pane_cmd(self, callsign: str, cmd: str) -> None:
        """Run a command in the Pit Boss iTerm2 window (shared pane layout)."""
        try:
            state_dir = Path("/tmp/uss-tenkara/_prifly")
            state_dir.mkdir(parents=True, exist_ok=True)
            agents_window_file = state_dir / "agents_window_id"
            agents_last_session_file = state_dir / "agents_last_session_id"

            if not agents_window_file.exists():
                # Pit Boss window not found — create one (fallback)
                applescript = f'''
tell application "iTerm2"
    set newWindow to (create window with default profile)
    set sess to current session of current tab of newWindow
    tell sess
        set name to "{callsign}"
        write text "{cmd}"
    end tell
    return (id of newWindow as text) & "," & (unique id of sess)
end tell
'''
                result = subprocess.run(
                    ["osascript", "-e", applescript],
                    capture_output=True, text=True, timeout=10,
                )
                parts = result.stdout.strip().split(",")
                if len(parts) == 2:
                    agents_window_file.write_text(parts[0])
                    agents_last_session_file.write_text(parts[1])

            elif len(self.ctx._iterm_panes) == 0:
                # First pane — use the placeholder session
                window_id = agents_window_file.read_text().strip()
                session_id = agents_last_session_file.read_text().strip()
                applescript = f'''
tell application "iTerm2"
    set targetWindow to (windows whose id is {window_id})'s item 1
    set targetSession to missing value
    repeat with s in sessions of current tab of targetWindow
        if unique id of s is "{session_id}" then
            set targetSession to s
            exit repeat
        end if
    end repeat
    tell targetSession
        set name to "{callsign}"
        write text "{cmd}"
    end tell
end tell
'''
                subprocess.run(
                    ["osascript", "-e", applescript],
                    capture_output=True, text=True, timeout=10,
                )

            else:
                # Split from any session in the Pit Boss window
                window_id = agents_window_file.read_text().strip()
                last_session_id = agents_last_session_file.read_text().strip()
                applescript = f'''
tell application "iTerm2"
    set targetWindow to (windows whose id is {window_id})'s item 1
    -- Try last known session first, fall back to first session in window
    set targetSession to missing value
    repeat with s in sessions of current tab of targetWindow
        if unique id of s is "{last_session_id}" then
            set targetSession to s
            exit repeat
        end if
    end repeat
    if targetSession is missing value then
        set targetSession to item 1 of sessions of current tab of targetWindow
    end if
    tell targetSession
        set newSession to (split vertically with default profile)
        tell newSession
            set name to "{callsign}"
            write text "{cmd}"
        end tell
        return unique id of newSession
    end tell
end tell
'''
                result = subprocess.run(
                    ["osascript", "-e", applescript],
                    capture_output=True, text=True, timeout=10,
                )
                new_session_id = result.stdout.strip()
                if new_session_id:
                    agents_last_session_file.write_text(new_session_id)

            self.ctx._iterm_panes.add(callsign)
            self.ctx._add_radio("PRI-FLY", f"COMMS OPEN — {callsign}", "success")
        except Exception as e:
            self.ctx._add_radio("PRI-FLY", f"Failed to open iTerm2 pane: {e}", "error")
