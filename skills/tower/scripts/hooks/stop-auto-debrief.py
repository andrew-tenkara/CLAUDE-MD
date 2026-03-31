#!/usr/bin/env python3
"""stop-auto-debrief.py — Stop hook: auto-debrief if pilot forgot + prune expired cache.

Called either by:
  1. Claude Code Stop hook — stdin has {session_id, transcript_path}
  2. Shell trap on pane close — stdin has {} (no session info)

In both cases, argv[1] is PROJECT_DIR.

Enriches the auto-debrief by parsing the session transcript JSONL to extract
files touched, decisions, and error count. Falls back to progress.md if the
transcript is unavailable (race condition on pane kill).

Non-blocking: always exits 0.
"""

import glob
import json
import os
import sqlite3
import subprocess
import sys
import time


def get_db(project_dir: str) -> sqlite3.Connection:
    db_path = os.path.join(project_dir, ".sortie", "storage.db")
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def find_sortie_dir() -> str:
    parts = os.getcwd().split(os.sep)
    for i in range(len(parts), 0, -1):
        d = os.sep.join(parts[:i]) + "/.sortie"
        if os.path.isdir(d):
            return d
    return ""


def session_has_debrief(conn: sqlite3.Connection, session_id: str,
                        ticket_id: str) -> bool:
    """Check if a debrief was written for this ticket in the last 2 hours."""
    if not conn or not ticket_id:
        return True  # Can't check — don't write a debrief blindly
    cutoff = int(time.time()) - 7200
    row = conn.execute(
        "SELECT 1 FROM debriefs WHERE ticket_id = ? AND timestamp > ? LIMIT 1",
        (ticket_id, cutoff),
    ).fetchone()
    return row is not None


def find_ticket_id(sortie_dir: str) -> str:
    fst = os.path.join(sortie_dir, "flight-status.json")
    if os.path.exists(fst):
        try:
            with open(fst) as f:
                return json.load(f).get("ticket_id", "")
        except Exception:
            pass
    return ""


# ── Transcript discovery + parsing ──────────────────────────────────

def find_transcript() -> str:
    """Locate the most recent session JSONL for the current worktree.

    Claude Code stores transcripts at:
        ~/.claude/projects/<encoded-cwd>/sessions/<uuid>.jsonl
    where <encoded-cwd> replaces / with - in the absolute CWD path.
    """
    cwd = os.getcwd()
    encoded = cwd.replace("/", "-")
    sessions_dir = os.path.expanduser(f"~/.claude/projects/{encoded}/sessions")
    if not os.path.isdir(sessions_dir):
        return ""
    # Most recently modified JSONL = the session that just ended
    jsonls = glob.glob(os.path.join(sessions_dir, "*.jsonl"))
    if not jsonls:
        return ""
    return max(jsonls, key=os.path.getmtime)


def parse_transcript(path: str) -> dict:
    """Parse a session JSONL, extracting files touched, decisions, and errors.

    Reads line-by-line so a truncated file (pane killed mid-write) still
    yields everything up to the last complete line.
    """
    files_touched = set()
    assistant_messages = []
    error_count = 0

    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get("type", "")

                if msg_type == "assistant":
                    message = obj.get("message", {})
                    content = message.get("content", [])
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            text = block.get("text", "").strip()
                            if len(text) > 50:
                                assistant_messages.append(text)
                        elif block.get("type") == "tool_use":
                            name = block.get("name", "")
                            inp = block.get("input", {})
                            fp = inp.get("file_path", "")
                            if fp and name in ("Edit", "Write"):
                                # Keep last two path segments (e.g. src/utils.ts)
                                parts = fp.split("/")
                                short = "/".join(parts[-2:]) if len(parts) > 1 else fp
                                files_touched.add(short)

                elif msg_type == "user":
                    message = obj.get("message", {})
                    content = message.get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("is_error"):
                                error_count += 1
    except Exception:
        pass

    return {
        "files_touched": sorted(files_touched),
        "assistant_messages": assistant_messages,
        "error_count": error_count,
    }


def extract_decisions(messages: list) -> str:
    """Pull the last few substantive assistant messages as decisions context."""
    # Walk backwards, grab up to 3 meaty messages
    decisions = []
    for msg in reversed(messages):
        if len(msg) > 80:
            # First line is usually the key point
            first_line = msg.split("\n")[0].strip()
            decisions.append(first_line[:200])
            if len(decisions) >= 3:
                break
    decisions.reverse()
    return "; ".join(decisions)[:500] if decisions else ""


def get_branch() -> str:
    """Get the current git branch name."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def get_model(sortie_dir: str) -> str:
    """Read the model from .sortie/model.txt."""
    model_path = os.path.join(sortie_dir, "model.txt")
    if os.path.exists(model_path):
        try:
            with open(model_path) as f:
                return f.read().strip()
        except Exception:
            pass
    return "auto"


def main() -> None:
    project_dir = sys.argv[1] if len(sys.argv) > 1 else ""
    if not project_dir:
        sys.exit(0)

    storage_db = os.path.join(os.path.dirname(__file__), "..", "storage-db.py")

    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        data = {}

    session_id = data.get("session_id", "") or ""

    sortie_dir = find_sortie_dir()
    ticket_id  = find_ticket_id(sortie_dir) if sortie_dir else ""

    # Auto-debrief if pilot forgot
    if ticket_id:
        conn = get_db(project_dir)
        if not session_has_debrief(conn, session_id, ticket_id):
            # Read progress.md as baseline
            progress_path = os.path.join(sortie_dir, "progress.md") if sortie_dir else ""
            progress = ""
            if progress_path and os.path.exists(progress_path):
                try:
                    with open(progress_path) as f:
                        progress = f.read().strip()[:500]
                except Exception:
                    pass

            # Try to enrich from transcript
            transcript_path = data.get("transcript_path", "") or find_transcript()
            transcript_data = parse_transcript(transcript_path) if transcript_path else {}

            files = ", ".join(transcript_data.get("files_touched", []))[:300]
            decisions = extract_decisions(transcript_data.get("assistant_messages", []))
            error_count = transcript_data.get("error_count", 0)
            gotchas = f"{error_count} tool errors during session" if error_count > 0 else ""

            auto_debrief = {
                "ticket_id":     ticket_id,
                "branch":        get_branch(),
                "model":         get_model(sortie_dir) if sortie_dir else "auto",
                "what_done":     progress or "(auto-debrief: no progress recorded)",
                "whats_left":    "",
                "decisions":     decisions,
                "gotchas":       gotchas,
                "files_touched": files,
                "pr_url":        "",
                "pr_status":     "",
                "branch_status": "",
            }
            proc = subprocess.Popen(
                ["python3", storage_db, "write-debrief", project_dir, "-"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            proc.communicate(input=json.dumps(auto_debrief).encode())
        if conn:
            conn.close()

    # Prune expired tool cache
    subprocess.run(
        ["python3", storage_db, "prune-tool-cache", project_dir],
        capture_output=True,
        timeout=10,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
