"""Heavy action handlers — extracted from commander-dashboard.py.

Only contains methods that are genuinely large (50+ lines) and benefit
from living outside the main dashboard file. Small actions stay inline
in the dashboard where they're called.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import threading
import re as _re
from pathlib import Path
from typing import Optional

# Shared sortie lib — server detection
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "sortie" / "lib"))
from detect_server import detect_server_cmd

log = logging.getLogger(__name__)


class Actions:
    """Heavy action logic — only the stuff too big to inline."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx

    # ── Select mode (terminal escape sequences) ──────────────────────

    def toggle_select_mode(self) -> None:
        ctx = self.ctx
        from textual.widgets import Static
        if ctx._select_mode:
            sys.stdout.write("\x1b[?1000h\x1b[?1003h\x1b[?1006h")
            sys.stdout.flush()
            ctx._select_mode = False
            try:
                ctx.query_one("#select-mode-banner", Static).add_class("hidden")
            except Exception:
                pass
            ctx._add_radio("PRI-FLY", "SELECT MODE OFF — mouse restored", "system")
        else:
            sys.stdout.write("\x1b[?1000l\x1b[?1003l\x1b[?1006l")
            sys.stdout.flush()
            ctx._select_mode = True
            try:
                ctx.query_one("#select-mode-banner", Static).remove_class("hidden")
            except Exception:
                pass
            ctx._add_radio("PRI-FLY", "SELECT MODE ON — drag to select, Cmd+C to copy, F2 to exit", "system")

    # ── Dismiss (70+ lines — worktree cleanup in background thread) ──

    def dismiss_selected(self) -> None:
        ctx = self.ctx
        pilot = ctx._get_selected_pilot()
        if not pilot:
            try:
                from textual.widgets import DataTable
                table = ctx.query_one("#agent-table", DataTable)
                ctx._add_radio("PRI-FLY", f"No pilot selected (rows={table.row_count}, cursor={table.cursor_row}, sorted={len(ctx._sorted_pilots)})", "error")
            except Exception:
                ctx._add_radio("PRI-FLY", "No pilot selected (table error)", "error")
            return

        # Kill active agent if still running
        if pilot.status in ("IN_FLIGHT", "ON_APPROACH"):
            try:
                ctx._agent_mgr.wave_off(pilot.callsign)
            except Exception:
                pass
            try:
                if ctx._sdk_mgr:
                    ctx._sdk_mgr.wave_off(pilot.callsign)
            except Exception:
                pass

        callsign = pilot.callsign
        tid = pilot.ticket_id
        project_dir = ctx._project_dir
        worktree_path = pilot.worktree_path
        if worktree_path and not Path(worktree_path).is_absolute():
            worktree_path = str(Path(project_dir) / worktree_path)

        ctx._roster.remove(callsign)
        ctx._dismissed_tickets.add(tid)
        ctx._board_state_sig = "__force_rebuild__"
        ctx._add_radio("PRI-FLY", f"{callsign} dismissed from board", "system")

        # Remove sprite immediately
        try:
            from flight_ops import FlightOpsStrip
            strip = ctx.query_one("#flight-strip", FlightOpsStrip)
            if callsign in strip._sprites:
                del strip._sprites[callsign]
        except Exception:
            pass

        ctx._refresh_ui()

        if not worktree_path:
            return

        def _delete_worktree():
            try:
                wt_path = Path(worktree_path)
                if not wt_path.exists():
                    try:
                        ctx.call_from_thread(ctx._add_radio, "PRI-FLY", f"{callsign} worktree already gone", "system")
                    except Exception:
                        pass
                    return

                result = subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_path)],
                    cwd=project_dir, capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0:
                    try:
                        ctx.call_from_thread(ctx._add_radio, "PRI-FLY", f"{callsign} worktree removed", "success")
                    except Exception:
                        pass
                else:
                    err = result.stderr.strip()
                    try:
                        ctx.call_from_thread(ctx._add_radio, "PRI-FLY", f"{callsign} git remove failed ({err[:80]}), nuking dir", "system")
                    except Exception:
                        pass
                    shutil.rmtree(str(wt_path), ignore_errors=True)
                    try:
                        ctx.call_from_thread(ctx._add_radio, "PRI-FLY", f"{callsign} worktree directory deleted", "success")
                    except Exception:
                        pass

                if wt_path.exists():
                    try:
                        ctx.call_from_thread(ctx._add_radio, "PRI-FLY", f"WARNING: {callsign} worktree still exists at {wt_path}", "error")
                    except Exception:
                        pass
            except Exception as e:
                try:
                    ctx.call_from_thread(ctx._add_radio, "PRI-FLY", f"Worktree cleanup error: {e}", "error")
                except Exception:
                    pass

        threading.Thread(target=_delete_worktree, daemon=True).start()

    # ── Dev server (80+ lines — script generation) ───────────────────

    def start_server(self) -> None:
        ctx = self.ctx
        from status_engine import _notify
        pilot = ctx._get_selected_pilot()
        if not pilot:
            ctx._add_radio("PRI-FLY", "No pilot selected", "error")
            return
        if not pilot.worktree_path:
            ctx._add_radio("PRI-FLY", f"{pilot.callsign} has no worktree", "error")
            return

        wt = pilot.worktree_path
        tid = pilot.ticket_id
        pane_name = f"SRV-{pilot.callsign}"
        ctx._iterm_panes.discard(pane_name)

        all_pilots = ctx._roster.all_pilots()
        port = 3000 + next((i for i, p in enumerate(all_pilots) if p.callsign == pilot.callsign), 0)

        # Detect server command from project root
        server_cfg = detect_server_cmd(ctx._project_dir)
        if not server_cfg:
            ctx._add_radio(
                "PRI-FLY",
                "No server command detected — XO will ask on next startup, "
                "or create .sortie/server-cmd.json manually",
                "error",
            )
            return

        run_cmd = server_cfg["cmd"]
        install_cmd = server_cfg.get("install_cmd", "")
        pkg_mgr = server_cfg.get("pkg_mgr", "")
        detected_from = server_cfg.get("detected_from", "unknown")

        server_script = Path(wt) / ".sortie" / "start-server.sh"
        managed_servers = Path(ctx._project_dir) / ".sortie" / "managed-servers.json"

        # Build dep-install block based on detected package manager
        install_block = ""
        if pkg_mgr in ("pnpm", "npm", "yarn", "bun") and install_cmd:
            lock_files = {
                "pnpm": "pnpm-lock.yaml", "npm": "package-lock.json",
                "yarn": "yarn.lock", "bun": "bun.lockb",
            }
            lock = lock_files.get(pkg_mgr, "package.json")
            install_block = (
                f"if [ -f {lock} ]; then\n"
                f"  if [ ! -d node_modules ] || [ {lock} -nt node_modules ]; then\n"
                f"    printf '\\033[1;33m📦 Installing dependencies...\\033[0m\\n'\n"
                f"    {install_cmd}\n"
                f"  fi\n"
                f"fi\n\n"
            )
        elif install_cmd:
            install_block = (
                f"printf '\\033[1;33m📦 Installing dependencies...\\033[0m\\n'\n"
                f"{install_cmd}\n\n"
            )

        server_script.write_text(
            f"#!/usr/bin/env bash\n"
            f"cd '{wt}'\n\n"
            f"printf '\\033[1;36m⚡ USS TENKARA — DEV SERVER for {pilot.callsign}\\033[0m\\n'\n"
            f"printf '\\033[2;37m   Worktree: {wt}\\033[0m\\n'\n"
            f"printf '\\033[2;37m   Target port: {port}\\033[0m\\n'\n"
            f"printf '\\033[2;37m   Command: {run_cmd} (via {detected_from})\\033[0m\\n'\n"
            f"printf '\\n'\n\n"
            f"if [ ! -f .env.local ] && [ -f '{ctx._project_dir}/.env.local' ]; then\n"
            f"  ln -sf '{ctx._project_dir}/.env.local' .env.local\n"
            f"  printf '\\033[1;32m✓ Symlinked .env.local\\033[0m\\n'\n"
            f"fi\n\n"
            f"{install_block}"
            f"register_server() {{\n"
            f"  local file='{managed_servers}'\n"
            f"  mkdir -p \"$(dirname \"$file\")\"\n"
            f"  if [ ! -f \"$file\" ] || [ ! -s \"$file\" ]; then echo '[]' > \"$file\"; fi\n"
            f"  python3 -c \"\n"
            f"import json, pathlib\n"
            f"p = pathlib.Path('$file')\n"
            f"entries = json.loads(p.read_text()) if p.exists() else []\n"
            f"entries = [e for e in entries if e.get('ticket_id') != '{tid}']\n"
            f"entries.append({{'ticket_id': '{tid}', 'url': 'localhost:{port}', 'note': 'dev server', 'pid': $$}})\n"
            f"p.write_text(json.dumps(entries, indent=2) + '\\n')\n\"\n"
            f"}}\n\n"
            f"cleanup() {{\n"
            f"  python3 -c \"\n"
            f"import json, pathlib\n"
            f"p = pathlib.Path('{managed_servers}')\n"
            f"if p.exists():\n"
            f"    entries = json.loads(p.read_text())\n"
            f"    entries = [e for e in entries if e.get('ticket_id') != '{tid}']\n"
            f"    p.write_text(json.dumps(entries, indent=2) + '\\n')\n\"\n"
            f"}}\n"
            f"trap cleanup EXIT\n\n"
            f"printf '\\033[1;33m🚀 Starting dev server...\\033[0m\\n'\n"
            f"register_server\n"
            f"PORT={port} {run_cmd}\n"
        )
        server_script.chmod(0o755)

        ctx._iterm_pane_cmd(pane_name, f"bash '{server_script}'")
        ctx._add_radio("PRI-FLY", f"DEV SERVER — launching for {pilot.callsign} on port {port} ({detected_from})", "success")
        _notify("USS TENKARA", f"Dev server starting for {pilot.callsign} :{port}")

    # ── Linear browse (reads config, opens browser) ──────────────────

    def get_linear_org(self) -> str:
        try:
            config_path = Path(__file__).resolve().parent.parent / "config.json"
            return json.loads(config_path.read_text()).get("linear_org", "")
        except Exception:
            return ""

    def linear_browse(self) -> None:
        ctx = self.ctx
        org = self.get_linear_org()
        if not org:
            ctx._add_radio("PRI-FLY", "No linear_org configured — run with --linear-org <org>", "error")
            return
        pilot = ctx._get_selected_pilot()
        if pilot and pilot.ticket_id and pilot.ticket_id not in ("Unknown", "unknown"):
            url = f"https://linear.app/{org}/issue/{pilot.ticket_id}"
            try:
                subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                ctx._add_radio("PRI-FLY", f"Opening {pilot.ticket_id} in Linear", "system")
            except Exception:
                ctx._add_radio("PRI-FLY", "Failed to open browser", "error")
        else:
            try:
                subprocess.Popen(["open", f"https://linear.app/{org}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                ctx._add_radio("PRI-FLY", "Opening Linear inbox", "system")
            except Exception:
                ctx._add_radio("PRI-FLY", "Failed to open browser", "error")

    # ── BullBoard ────────────────────────────────────────────────────

    def open_bullboard(self) -> None:
        ctx = self.ctx
        pilot = ctx._get_selected_pilot()
        if not pilot:
            ctx._add_radio("PRI-FLY", "No pilot selected", "error")
            return
        if not pilot.worktree_path:
            ctx._add_radio("PRI-FLY", f"{pilot.callsign} has no worktree", "error")
            return
        wt = pilot.worktree_path
        if not Path(wt).is_absolute():
            wt = str(Path(ctx._project_dir) / wt)
        ports_file = Path(wt) / ".sortie" / "server-ports.json"
        try:
            if ports_file.exists():
                ports = json.loads(ports_file.read_text(encoding="utf-8"))
                bb_port = ports.get("bullboard")
                if bb_port:
                    url = f"http://localhost:{bb_port}"
                    subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    ctx._add_radio("PRI-FLY", f"Opening BullBoard at {url}", "system")
                    return
        except (OSError, json.JSONDecodeError):
            pass
        ctx._add_radio("PRI-FLY", f"{pilot.callsign} has no BullBoard port in .sortie/server-ports.json", "error")

    # ── Browser (server URL extraction + open) ───────────────────────

    def open_browser(self) -> None:
        ctx = self.ctx
        pilot = ctx._get_selected_pilot()
        if not pilot:
            ctx._add_radio("PRI-FLY", "No pilot selected", "error")
            return
        url = self.extract_server_url(pilot)
        if not url:
            ctx._add_radio("PRI-FLY", f"{pilot.callsign} has no active server", "error")
            return
        if not url.startswith("http"):
            url = f"http://{url}"
        try:
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ctx._add_radio("PRI-FLY", f"Opening {url} in browser", "system")
        except Exception:
            ctx._add_radio("PRI-FLY", "Failed to open browser", "error")

    def extract_server_url(self, pilot) -> str:
        """Multi-source server URL extraction — hint, managed-servers.json, conversation buffer."""
        ctx = self.ctx
        url_re = _re.compile(r"(localhost:\d+|127\.0\.0\.1:\d+|0\.0\.0\.0:\d+)")

        hint = pilot.status_hint or ""
        match = url_re.search(hint)
        if match:
            return match.group(1)

        try:
            servers_file = Path(ctx._project_dir) / ".sortie" / "managed-servers.json"
            if servers_file.exists():
                for entry in json.loads(servers_file.read_text(encoding="utf-8")):
                    if entry.get("ticket_id") == pilot.ticket_id and entry.get("url"):
                        return entry["url"]
        except (json.JSONDecodeError, OSError):
            pass

        session = ctx._agent_mgr.get(pilot.callsign) if hasattr(ctx, '_agent_mgr') else None
        if session:
            for entry in session.conversation[-200:][::-1]:
                match = url_re.search(entry.content)
                if match:
                    return match.group(1)
        return ""

    # ── PR (gh CLI lookup + fallback to repo URL) ────────────────────

    def open_pr(self) -> None:
        ctx = self.ctx
        pilot = ctx._get_selected_pilot()
        if not pilot:
            ctx._add_radio("PRI-FLY", "No pilot selected", "error")
            return
        if not pilot.worktree_path:
            ctx._add_radio("PRI-FLY", f"{pilot.callsign} has no worktree", "error")
            return
        try:
            result = subprocess.run(
                ["gh", "pr", "view", "--json", "number,url", "-q", ".url"],
                capture_output=True, text=True, timeout=10, cwd=pilot.worktree_path,
            )
            pr_url = result.stdout.strip()
            if result.returncode == 0 and pr_url:
                subprocess.Popen(["open", pr_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                ctx._add_radio("PRI-FLY", f"Opening PR for {pilot.callsign}", "system")
            else:
                repo_url = self.get_github_repo_url(pilot.worktree_path)
                if repo_url:
                    subprocess.Popen(["open", repo_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    ctx._add_radio("PRI-FLY", f"No PR found — opening repo", "system")
                else:
                    ctx._add_radio("PRI-FLY", f"No PR found for {pilot.callsign}", "error")
        except Exception:
            ctx._add_radio("PRI-FLY", "Failed to look up PR", "error")

    def get_github_repo_url(self, cwd: str) -> str:
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=5, cwd=cwd,
            )
            url = result.stdout.strip()
            if url.endswith(".git"):
                url = url[:-4]
            if url.startswith("git@github.com:"):
                url = url.replace("git@github.com:", "https://github.com/")
            return url
        except Exception:
            return ""

    # ── Chat pane management (slot allocation + grid layout) ─────────

    def open_chat_pane(self, callsign: str) -> None:
        ctx = self.ctx
        from textual.containers import Horizontal, Vertical
        from widgets import ChatInput, ChatPane

        if callsign in ctx._chat_panes:
            try:
                ctx.query_one(f"#chat-input-{callsign}", ChatInput).focus()
            except Exception:
                pass
            return

        if len(ctx._chat_panes) >= 4:
            ctx._add_radio("PRI-FLY", "Max 4 chat panes open. Close one first (Ctrl+C).", "system")
            return

        slot_idx = next((i for i in range(4) if i not in ctx._slot_map), None)
        if slot_idx is None:
            return

        pane = ChatPane(callsign, id=f"chat-pane-{callsign}")
        ctx._chat_panes[callsign] = pane
        ctx._slot_map[slot_idx] = callsign

        slot = ctx.query_one(f"#comms-slot-{slot_idx}", Vertical)
        slot.mount(pane)
        slot.remove_class("empty")

        row_id = "comms-row-top" if slot_idx < 2 else "comms-row-bot"
        ctx.query_one(f"#{row_id}", Horizontal).remove_class("empty")

        if not ctx._comms_active:
            ctx._comms_active = True
            ctx.query_one("#comms-grid", Vertical).add_class("active")
            ctx.query_one("#board-section", Vertical).add_class("compressed")

        try:
            ctx.query_one(f"#chat-input-{callsign}", ChatInput).focus()
        except Exception:
            pass
        try:
            ctx._update_keybind_hints()
        except Exception:
            pass

    def close_chat_pane(self, callsign: str) -> None:
        ctx = self.ctx
        from textual.containers import Horizontal, Vertical
        from textual.widgets import DataTable
        from widgets import ChatInput

        pane = ctx._chat_panes.pop(callsign, None)
        if pane is None:
            return

        slot_idx = next((idx for idx, cs in ctx._slot_map.items() if cs == callsign), None)

        if slot_idx is not None:
            del ctx._slot_map[slot_idx]
            try:
                pane.remove()
            except Exception:
                pass
            try:
                ctx.query_one(f"#comms-slot-{slot_idx}", Vertical).add_class("empty")
            except Exception:
                pass
            row_id = "comms-row-top" if slot_idx < 2 else "comms-row-bot"
            sibling_idx = slot_idx ^ 1
            if sibling_idx not in ctx._slot_map:
                try:
                    ctx.query_one(f"#{row_id}", Horizontal).add_class("empty")
                except Exception:
                    pass

        if not ctx._chat_panes:
            ctx._comms_active = False
            try:
                ctx.query_one("#comms-grid", Vertical).remove_class("active")
                ctx.query_one("#board-section", Vertical).remove_class("compressed")
            except Exception:
                pass
            ctx.query_one("#agent-table", DataTable).focus()
        else:
            next_callsign = next(iter(ctx._chat_panes))
            try:
                ctx.query_one(f"#chat-input-{next_callsign}", ChatInput).focus()
            except Exception:
                ctx.query_one("#agent-table", DataTable).focus()

        try:
            ctx._update_keybind_hints()
        except Exception:
            pass
