#!/usr/bin/env python3
"""USS Tenkara — Chat Relay.

Runs in an iTerm2 pane, providing a real terminal experience for agent conversation.
Tails events.jsonl for agent output, writes user input to input.jsonl for the TUI
to pick up and inject into the agent's stdin.

Usage:
    python3 chat-relay.py --callsign Phoenix-1 [--dir /tmp/uss-tenkara/Phoenix-1]
"""
from __future__ import annotations

import argparse
import json
import os
import readline  # enables arrow keys, history, Ctrl+R in input()
import sys
import time
from datetime import datetime
from pathlib import Path

# ── ANSI colors ──────────────────────────────────────────────────────

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
BLUE = "\033[38;5;75m"
GREEN = "\033[38;5;114m"
YELLOW = "\033[38;5;180m"
RED = "\033[38;5;204m"
PURPLE = "\033[38;5;176m"
CYAN = "\033[38;5;80m"
GREY = "\033[38;5;242m"
WHITE = "\033[38;5;255m"
BG_DARK = "\033[48;5;235m"
BG_YELLOW = "\033[48;5;58m"


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _short_path(path: str) -> str:
    parts = path.split("/")
    return parts[-1] if len(parts) > 1 else path


# ── Renderers ────────────────────────────────────────────────────────

def render_assistant(text: str, callsign: str) -> None:
    """Render assistant text output."""
    print(f"\n  {BLUE}{BOLD}{callsign}{RESET}  {GREY}{_ts()}{RESET}")

    for line in text.split("\n"):
        stripped = line.strip()

        # Headings
        if stripped.startswith("###"):
            print(f"  {PURPLE}{BOLD}{stripped}{RESET}")
        elif stripped.startswith("##"):
            print(f"  {PURPLE}{BOLD}{stripped}{RESET}")
        elif stripped.startswith("#"):
            print(f"  {BLUE}{BOLD}{stripped}{RESET}")
        # Bullets
        elif stripped.startswith("- ") or stripped.startswith("* "):
            print(f"  {PURPLE}●{RESET} {WHITE}{stripped[2:]}{RESET}")
        # Numbered
        elif stripped and stripped[0].isdigit() and ". " in stripped[:5]:
            num, rest = stripped.split(". ", 1)
            print(f"  {YELLOW}{num}.{RESET} {WHITE}{rest}{RESET}")
        # Code blocks
        elif stripped.startswith("```"):
            print(f"  {BG_DARK}{GREY}{stripped}{RESET}")
        # Normal
        elif stripped:
            print(f"  {WHITE}{line}{RESET}")
        else:
            print()

    print(f"  {GREY}{'─' * 60}{RESET}")


def render_tool(tool_name: str, summary: str, tool_input: dict | None = None) -> None:
    """Render a tool call."""
    icon = _tool_icon(tool_name)
    print(f"  {CYAN}{icon} {BOLD}{tool_name}{RESET}  {GREY}{summary}{RESET}")

    if tool_input:
        if tool_name == "Edit":
            fp = _short_path(tool_input.get("file_path", "?"))
            old = tool_input.get("old_string", "")
            new = tool_input.get("new_string", "")
            if old:
                for line in old.split("\n")[:5]:
                    print(f"    {RED}- {line}{RESET}")
                if old.count("\n") > 5:
                    print(f"    {RED}  ... +{old.count(chr(10)) - 5} more{RESET}")
            if new:
                for line in new.split("\n")[:5]:
                    print(f"    {GREEN}+ {line}{RESET}")
                if new.count("\n") > 5:
                    print(f"    {GREEN}  ... +{new.count(chr(10)) - 5} more{RESET}")

        elif tool_name == "Bash":
            cmd = tool_input.get("command", "")
            print(f"    {DIM}$ {cmd[:200]}{RESET}")

        elif tool_name == "Read":
            fp = _short_path(tool_input.get("file_path", "?"))
            offset = tool_input.get("offset", "")
            limit = tool_input.get("limit", "")
            suffix = ""
            if offset or limit:
                suffix = f" L{offset or 1}"
                if limit:
                    suffix += f"-{(offset or 1) + limit}"
            print(f"    {DIM}{fp}{suffix}{RESET}")

        elif tool_name in ("Grep", "Glob"):
            pattern = tool_input.get("pattern", "?")
            print(f"    {DIM}\"{pattern}\"{RESET}")


def render_permission(tool_name: str, summary: str, reason: str) -> None:
    """Render a permission request."""
    print(f"\n  {BG_YELLOW}{YELLOW}{BOLD} ⚡ PERMISSION REQUEST {RESET}  {GREY}{_ts()}{RESET}")
    print(f"  {YELLOW}{BOLD}{tool_name}{RESET}: {WHITE}{summary}{RESET}")
    if reason:
        print(f"  {DIM}{reason}{RESET}")
    print(f"  {GREEN}{BOLD}y{RESET}{GREY} approve  {RED}{BOLD}n{RESET}{GREY} deny{RESET}")


def render_system(text: str) -> None:
    """Render a system message."""
    print(f"  {GREY}⚙ {text}{RESET}")


def render_user(text: str) -> None:
    """Render user message."""
    print(f"\n  {GREEN}{BOLD}YOU{RESET}  {GREY}{_ts()}{RESET}")
    for line in text.split("\n"):
        print(f"  {WHITE}{BOLD}{line}{RESET}")
    print(f"  {GREY}{'─' * 60}{RESET}")


def _tool_icon(name: str) -> str:
    icons = {
        "Edit": "✏",
        "Write": "📝",
        "Read": "📖",
        "Bash": "⚡",
        "Grep": "🔍",
        "Glob": "📂",
        "Agent": "🤖",
        "WebFetch": "🌐",
        "WebSearch": "🔎",
    }
    return icons.get(name, "⚙")


def _summarize_tool(tool_name: str, tool_input: dict) -> str:
    """Quick summary of a tool call."""
    if tool_name == "Edit":
        return _short_path(tool_input.get("file_path", "?"))
    if tool_name == "Write":
        return _short_path(tool_input.get("file_path", "?"))
    if tool_name == "Read":
        return _short_path(tool_input.get("file_path", "?"))
    if tool_name == "Bash":
        return tool_input.get("command", "?")[:80]
    if tool_name == "Grep":
        return f'"{tool_input.get("pattern", "?")}"'
    if tool_name == "Glob":
        return f'"{tool_input.get("pattern", "?")}"'
    if tool_name == "Agent":
        return tool_input.get("description", "subagent")
    return str(list(tool_input.keys())[:2])


# ── Event processing ─────────────────────────────────────────────────

def process_event(event: dict, callsign: str) -> str | None:
    """Process a single event from events.jsonl. Returns 'permission' if awaiting input."""
    etype = event.get("type", "")

    if etype == "assistant":
        text = event.get("text", "")
        tool_uses = event.get("tool_uses", [])

        if text:
            render_assistant(text, callsign)

        for tu in tool_uses:
            tool_name = tu.get("name", "unknown")
            tool_input = tu.get("input", {})
            summary = _summarize_tool(tool_name, tool_input)
            render_tool(tool_name, summary, tool_input)

    elif etype == "permission":
        tool_name = event.get("tool_name", "?")
        tool_input = event.get("tool_input", {})
        reason = event.get("reason", "")
        summary = _summarize_tool(tool_name, tool_input)
        render_permission(tool_name, summary, reason)
        return "permission"

    elif etype == "user":
        text = event.get("text", "")
        if text:
            render_user(text)

    elif etype == "system":
        text = event.get("text", "")
        if text:
            render_system(text)

    elif etype == "result":
        text = event.get("text", "")
        print(f"\n  {GREEN}{BOLD}✓ MISSION COMPLETE{RESET}")
        if text:
            print(f"  {GREY}{text[:200]}{RESET}")

    elif etype == "exit":
        code = event.get("return_code", -1)
        if code == 0:
            print(f"\n  {GREEN}{BOLD}✓ RECOVERED{RESET} — Agent exited cleanly")
        else:
            print(f"\n  {RED}{BOLD}⚠ MAYDAY{RESET} — Agent exited with code {code}")

    return None


# ── Main loop ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="USS Tenkara Chat Relay")
    parser.add_argument("--callsign", required=True, help="Agent callsign")
    parser.add_argument("--dir", help="Comm directory (default: /tmp/uss-tenkara/<callsign>)")
    args = parser.parse_args()

    callsign = args.callsign
    comm_dir = Path(args.dir) if args.dir else Path(f"/tmp/uss-tenkara/{callsign}")

    events_file = comm_dir / "events.jsonl"
    input_file = comm_dir / "input.jsonl"

    # Splash
    print(f"\n{YELLOW}{'━' * 58}{RESET}")
    if callsign == "MINI-BOSS":
        print(f"{RED}{BOLD}     ★ ★ ★  USS TENKARA — MINI BOSS  ★ ★ ★{RESET}")
        print(f"{PURPLE}{BOLD}       \"Talk to me, Goose.\"{RESET}")
        print(f"{GREY}                    — Maverick, 1986{RESET}")
    else:
        print(f"{BLUE}{BOLD}     ✈  USS TENKARA — {callsign}  ✈{RESET}")
        print(f"{PURPLE}{BOLD}       \"It's time to buzz the tower.\"{RESET}")
        print(f"{GREY}                    — Maverick, 1986{RESET}")
    print(f"{YELLOW}{'━' * 58}{RESET}\n")

    # Wait for comm directory to exist
    print(f"  {BLUE}{BOLD}USS TENKARA — COMMS{RESET}")
    print(f"  {GREY}Connecting to {callsign}...{RESET}")

    for _ in range(30):
        if events_file.exists():
            break
        time.sleep(0.5)
    else:
        print(f"  {RED}Timeout waiting for {events_file}{RESET}")
        sys.exit(1)

    print(f"  {GREEN}Connected.{RESET} Type messages below. Ctrl+C to disconnect.\n")
    print(f"  {GREY}{'═' * 60}{RESET}")

    # Tail events file
    file_pos = 0
    pending_permission = False

    try:
        while True:
            # Read new events
            try:
                with open(events_file, "r") as f:
                    f.seek(file_pos)
                    new_lines = f.readlines()
                    file_pos = f.tell()

                for line in new_lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        result = process_event(event, callsign)
                        if result == "permission":
                            pending_permission = True
                    except json.JSONDecodeError:
                        continue
            except FileNotFoundError:
                pass

            # Check for user input (non-blocking-ish via short timeout)
            if pending_permission:
                # Permission prompt — block for y/n
                try:
                    response = input(f"  {YELLOW}> {RESET}").strip().lower()
                    if response in ("y", "yes", "n", "no"):
                        _write_input(input_file, {
                            "type": "permission_response",
                            "response": response in ("y", "yes"),
                            "timestamp": time.time(),
                        })
                        action = "APPROVED" if response in ("y", "yes") else "DENIED"
                        color = GREEN if response in ("y", "yes") else RED
                        print(f"  {color}{BOLD}✓ {action}{RESET}")
                        pending_permission = False
                except EOFError:
                    break
            else:
                # Check for user input with a timeout
                import select
                if select.select([sys.stdin], [], [], 0.3)[0]:
                    try:
                        text = input(f"  {GREEN}> {RESET}").strip()
                        if text:
                            _write_input(input_file, {
                                "type": "user_message",
                                "text": text,
                                "timestamp": time.time(),
                            })
                            render_user(text)
                    except EOFError:
                        break
                else:
                    time.sleep(0.2)

    except KeyboardInterrupt:
        print(f"\n\n  {GREY}Disconnected from {callsign}. Agent continues running.{RESET}")
        print(f"  {GREY}Reopen: press 'c' on {callsign} in Pri-Fly{RESET}\n")


def _write_input(input_file: Path, data: dict) -> None:
    """Append a JSON line to the input file."""
    with open(input_file, "a") as f:
        f.write(json.dumps(data) + "\n")


if __name__ == "__main__":
    main()
