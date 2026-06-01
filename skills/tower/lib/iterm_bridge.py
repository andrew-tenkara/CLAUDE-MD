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
            if launch_script.exists() and "claude" in launch_script.read_text():
                # Clear stale session-ended from previous run
                session_ended = Path(pilot.worktree_path) / ".sortie" / "session-ended"
                if session_ended.exists():
                    session_ended.unlink()
                # Write ON_DECK status so the board tracks this pilot
                (Path(pilot.worktree_path) / ".sortie" / "flight-status.json").write_text(
                    json.dumps({"status": "ON_DECK", "phase": "on deck — pre-launch checks", "timestamp": int(time_mod.time())})
                )
                # Write pilot identity so CLAUDE.md !cat gets real roster values
                (Path(pilot.worktree_path) / ".sortie" / "pilot-identity.md").write_text(
                    f"**Callsign:** {pilot.callsign}\n"
                    f"**Squadron:** {pilot.squadron}\n"
                    f"**Model:** {pilot.model.upper()}\n"
                    f"**Trait:** {pilot.trait}\n"
                )

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
                cmd = f"cd '{pilot.worktree_path}' && bash '{splash_script}' && bash '{launch_script}'"
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

        # Resolve the skill's templates dir for the pilot kickoff message.
        # The pilot reads plan-template.html from here and Writes
        # .sortie/plan.html directly — no copy/symlink at launch time.
        templates_dir = Path(__file__).resolve().parent.parent / "templates"

        # Clear stale session-ended sentinel from previous run
        session_ended = sortie_dir / "session-ended"
        if session_ended.exists():
            session_ended.unlink()

        # Directive + flight status protocol
        flight_protocol = (
            "\n\n---\n"
            "## Flight Status Protocol\n"
            "Report your flight status by writing to `.sortie/flight-status.json`:\n"
            '```json\n{"status": "IN_FLIGHT", "phase": "implementing auth refresh", "timestamp": 1710345600}\n```\n'
            "Valid statuses: ON_DECK, IN_FLIGHT, ON_APPROACH\n"
            "Update on meaningful phase transitions only (starting new task area, running tests, "
            "submitting PR, blocked, done). Do NOT update on every tool call.\n"
            "Use unix timestamp (seconds). Phase is a short human-readable description of what you're doing.\n"
            "ON_DECK is set automatically before launch — do not write it yourself.\n"
            "Write IN_FLIGHT when you start actively making changes (editing files, running commands, writing code). "
            "Reading context, reading tickets, reading files, and planning are all still ON_DECK.\n"
            "NEVER write RECOVERED — that is set automatically when your session ends.\n"
            "When your mission is complete, write ON_APPROACH with phase 'mission complete — awaiting orders'.\n"
            "\n"
            "## Server Port Protocol\n"
            "If you start any dev server, worker, or dashboard process, write the port to `.sortie/server-ports.json`:\n"
            '```json\n{"dev": 3001, "bullboard": 4502, "timestamp": 1710345600}\n```\n'
            "Include any port your worktree is serving on. The TUI reads this to show server URLs on the board "
            "and the O key opens them in the browser. Update the file whenever a new server starts or a port changes.\n"
            "\n"
            "## Sibling Coordination (pull-parent protocol)\n"
            "If you see a file at `.sortie/pull-parent.json`, a sibling agent has merged their work "
            "into the parent branch. Read the file for details, then:\n"
            "1. Run `git pull origin <branch>` (branch is in the JSON file)\n"
            "2. Resolve any merge conflicts\n"
            "3. Delete `.sortie/pull-parent.json`\n"
            "4. Continue your work with the updated code\n"
        )
        (sortie_dir / "directive.md").write_text(pilot.directive + flight_protocol)

        (sortie_dir / "pilot-identity.md").write_text(
            f"**Callsign:** {pilot.callsign}\n"
            f"**Squadron:** {pilot.squadron}\n"
            f"**Model:** {pilot.model.upper()}\n"
            f"**Trait:** {pilot.trait}\n"
        )

        # Progress
        progress_file = sortie_dir / "progress.md"
        if not progress_file.exists():
            progress_file.write_text("")

        # Model
        (sortie_dir / "model.txt").write_text(pilot.model)

        # Set PREFLIGHT status — agent is on deck, not yet airborne
        (sortie_dir / "flight-status.json").write_text(
            json.dumps({"status": "ON_DECK", "phase": "on deck — pre-launch checks", "timestamp": int(time_mod.time())})
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

        kickoff = (
            f"Read {sortie_dir}/directive.md and follow all instructions. "
            f"See CLAUDE.md for the full sortie protocol. DO NOT WRITE CODE YET. "
            f"Phase 1 (Plan): Read the canonical plan template at "
            f"{templates_dir}/plan-template.html (do NOT copy — read with the Read tool as "
            f"structural reference). Write {sortie_dir}/plan.html from scratch as a fully "
            f"self-contained HTML doc matching the template's head/styles/scripts, with this "
            f"sortie's real content in the <article id='doc'> body. No markdown file — HTML "
            f"is the only source of truth. Opens in any browser via file:// — no server needed. "
            f"Invoke superpowers:brainstorming, then superpowers:writing-plans. The plan MUST "
            f"include a Reuse Audit (find_symbol/grep/CGC) AND a Blast Radius per CHANGE block "
            f"(use CGC: analyze_code_relationships query_type=find_all_callers,find_all_callees "
            f"on each modified symbol — render as the Mermaid graph the template shows). MUST "
            f"verify library/API/framework choices via the exa-search skill during planning — "
            f"not after. Send {sortie_dir}/plan.html to Mini Boss via send-message and WAIT "
            f"for approval. Phase 2 (Implement): only after approval. Update {sortie_dir}/plan.html checkboxes "
            f"in-place — single source of truth for progress. Use superpowers throughout "
            f"(test-driven-development, systematic-debugging, verification-before-completion, "
            f"requesting-code-review). If a superpowers skill plausibly applies, invoke it."
        )

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
            # Escape literal ' inside kickoff so the outer single-quoted bash
            # arg doesn't terminate at "sortie's", embedded quoted paths, etc.
            f"claude --model {pilot.model} '{kickoff.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}' "
            f"--disallowedTools {disallowed}\n"
        )
        launch_script.chmod(0o755)

        # Store worktree path + set initial preflight state
        pilot.worktree_path = str(worktree_path)
        pilot.flight_status = "ON_DECK"
        pilot.flight_phase = "on deck — pre-launch checks"
        self.ctx._watch_agent_jsonl(str(worktree_path))

        cmd = f"cd '{worktree_path}' && bash '{launch_script}'"
        self.pane_cmd(pilot.callsign, cmd)

    def resume_agent_pane(self, pilot: "Pilot") -> None:
        """Open a bare Claude session in the worktree — no directive kickoff.

        The agent starts fresh, reads progress.md and git state on its own,
        and asks for instructions. Used by R (Resume) hotkey.
        """
        if pilot.callsign in self.ctx._iterm_panes:
            return

        worktree_path = pilot.worktree_path
        if not worktree_path or not Path(worktree_path).exists():
            self.ctx._add_radio("PRI-FLY", f"{pilot.callsign} has no worktree to resume in", "error")
            return

        sortie_dir = Path(worktree_path) / ".sortie"

        # Clear stale session-ended from previous run
        session_ended = sortie_dir / "session-ended"
        if session_ended.exists():
            session_ended.unlink()

        # Write a minimal resume script — cd + bare claude, no directive
        resume_prompt = (
            f"You are {pilot.callsign}, resuming work on {pilot.ticket_id}"
            f"{': ' + pilot.mission_title if pilot.mission_title and pilot.mission_title != pilot.ticket_id else ''}. "
            f"Read .sortie/progress.md and check git status + git log to understand where the previous agent left off. "
            f"Then report what you find and ask what to do next."
        )
        # Escape single quotes for bash
        resume_prompt_escaped = resume_prompt.replace("'", "'\\''")

        disallowed_file = Path(__file__).resolve().parent.parent / "scripts" / "disallowed-tools.txt"
        if disallowed_file.exists():
            disallowed = disallowed_file.read_text().replace("\n", " ").strip()
        else:
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

        resume_script = sortie_dir / "resume.sh"
        resume_script.write_text(
            f"#!/usr/bin/env bash\n"
            f"cd '{worktree_path}'\n\n"
            f"# Cleanup on exit — signal session ended\n"
            f"cleanup_flight() {{\n"
            f"  touch .sortie/session-ended\n"
            f"}}\n"
            f"trap cleanup_flight EXIT\n\n"
            f"claude --model {pilot.model} '{resume_prompt_escaped}' "
            f"--disallowedTools {disallowed}\n"
        )
        resume_script.chmod(0o755)

        pilot.status = "ON_DECK"
        pilot.launched_at = time_mod.time()
        self.ctx._watch_agent_jsonl(str(worktree_path))

        cmd = f"cd '{worktree_path}' && bash '{resume_script}'"
        self.pane_cmd(pilot.callsign, cmd)
        self.ctx._add_radio(pilot.callsign, f"RESUME — open session in {pilot.ticket_id} worktree", "success")

    def kill_pane(self, callsign: str) -> None:
        """Close the iTerm2 session for the given callsign (no-op if already gone)."""
        try:
            state_dir = Path("/tmp/uss-tenkara/_prifly")
            agents_window_file = state_dir / "agents_window_id"
            if agents_window_file.exists():
                window_id = agents_window_file.read_text().strip()
                # Collect matching sessions first, then close — avoids mutating
                # the list while iterating, which can cause AppleScript errors.
                applescript = f'''
tell application "iTerm2"
    set targetWindow to (windows whose id is {window_id})'s item 1
    set killList to {{}}
    repeat with s in sessions of current tab of targetWindow
        if name of s is "{callsign}" then
            set end of killList to s
        end if
    end repeat
    repeat with s in killList
        close s
    end repeat
end tell
'''
                subprocess.run(
                    ["osascript", "-e", applescript],
                    capture_output=True, text=True, timeout=10,
                )
        except Exception as e:
            self.ctx._add_radio("PRI-FLY", f"kill_pane {callsign}: {e}", "error")
        finally:
            self.ctx._iterm_panes.discard(callsign)

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
