"""Rule-based agent activity classifier.

Parses raw JSONL event dicts from Claude Code sessions and classifies
the agent's current activity into flight status codes.

No LLM required — deterministic rules based on tool names and Bash commands.

Usage:
    from classify import classify
    status, phase = classify(events)   # events: list of raw JSONL dicts
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# ── Status constants ──────────────────────────────────────────────────

AIRBORNE    = "AIRBORNE"
ON_APPROACH = "ON_APPROACH"
HOLDING     = "HOLDING"
RECOVERED   = "RECOVERED"

_PRIORITY = {HOLDING: 0, ON_APPROACH: 1, AIRBORNE: 2}

# ── Tool sets ─────────────────────────────────────────────────────────

_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "TodoWrite"})
_READ_TOOLS  = frozenset({"Read", "Glob", "Grep", "LS", "WebFetch", "WebSearch", "TodoRead"})

# ── Bash patterns ─────────────────────────────────────────────────────

_RE_TEST = re.compile(
    r"\b("
    r"jest|vitest|pytest|mocha|karma|cypress|playwright|jasmine|"
    r"npm\s+(run\s+)?test|pnpm\s+(run\s+)?test|yarn\s+test|"
    r"npm\s+test|pnpm\s+test|npx\s+(jest|vitest|mocha)|"
    r"python\s+-m\s+pytest|python\s+-m\s+unittest|"
    r"go\s+test|cargo\s+test|rake\s+test|bundle\s+exec\s+rspec|"
    r"dotnet\s+test"
    r")\b"
)

_RE_BUILD = re.compile(
    r"\b("
    r"tsc\b|next\s+build|nuxt\s+build|"
    r"npm\s+run\s+build|pnpm\s+(run\s+)?build|yarn\s+build|"
    r"webpack|vite\s+build|rollup|esbuild|parcel\s+build|"
    r"cargo\s+build|go\s+build|make\b|cmake\b|gradle\b|mvn\s+package"
    r")\b"
)

_RE_GIT_FINISH = re.compile(
    r"\b("
    r"git\s+commit|git\s+push|git\s+tag|git\s+merge|git\s+add\b|"
    r"gh\s+pr\s+(create|merge|close|edit)|"
    r"git\s+rebase\b"
    r")\b"
)

_RE_GIT_INFO = re.compile(
    r"\b("
    r"git\s+(log|status|diff|show|blame|branch|remote|fetch|pull|"
    r"shortlog|rev-parse|ls-files|stash\s+list|describe|reflog|checkout)|"
    r"gh\s+pr\s+(list|view|status|checks|diff)"
    r")\b"
)

_RE_INSTALL = re.compile(
    r"\b("
    r"npm\s+(install|i\b|ci\b)|pnpm\s+(install|i\b|add\b)|"
    r"yarn\s+(install|add\b)|pip\s+install|pip3\s+install|"
    r"brew\s+install|apt(-get)?\s+install|cargo\s+add|gem\s+install"
    r")\b"
)

# ── Error loop detection ──────────────────────────────────────────────

ERROR_LOOP_THRESHOLD = 3  # Same tool+args failing N times = stuck in a loop


def _detect_loop(events: list[dict]) -> Optional[str]:
    """Detect retry loops: same tool call failing repeatedly.

    Walks the event stream pairing each assistant tool_use with its
    subsequent user tool_result. Counts consecutive failures per
    (tool_name, key_input). Returns a description if any key hits
    ERROR_LOOP_THRESHOLD, else None.
    """
    pending: Optional[tuple[str, str]] = None  # (tool_name, key_input) of last call
    loop_counts: dict[tuple[str, str], int] = {}

    for event in events:
        etype = event.get("type")

        if etype == "assistant":
            content = event.get("message", {}).get("content") or []
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name", "")
                inp  = block.get("input") or {}
                if name == "Bash":
                    key = (inp.get("command") or "")[:80]
                elif name in _WRITE_TOOLS:
                    key = inp.get("file_path") or inp.get("notebook_path", "")
                else:
                    key = str(list(sorted(inp.items())))[:80]
                pending = (name, str(key))

        elif etype == "user" and pending is not None:
            content = event.get("message", {}).get("content") or []
            if isinstance(content, list):
                errored = any(
                    isinstance(b, dict) and b.get("is_error") is True
                    for b in content
                )
                if errored:
                    loop_counts[pending] = loop_counts.get(pending, 0) + 1
                else:
                    loop_counts.pop(pending, None)  # success resets count
            pending = None

    if not loop_counts:
        return None

    worst, count = max(loop_counts.items(), key=lambda x: x[1])
    if count >= ERROR_LOOP_THRESHOLD:
        tool_name, tool_input = worst
        label = tool_input[:40] if tool_input else tool_name
        return f"stuck: {label} ({count}x)"

    return None


# ── Narration keyword sets ─────────────────────────────────────────────

_NARRATION_ON_APPROACH = (
    "all tests pass", "tests pass", "opening pr", "creating pr", "creating a pr",
    "git push", "git commit", "submitting pr", "final check",
    "verify nothing broke", "lgtm", "ready to merge",
)

# ── Core classifiers ──────────────────────────────────────────────────

def _bash_classify(cmd: str) -> tuple[str, str]:
    """Classify a Bash command string → (status, phase)."""
    c = cmd.lower().strip()

    if _RE_TEST.search(c):
        return ON_APPROACH, f"running: {cmd[:60].strip()}"
    if _RE_BUILD.search(c):
        return ON_APPROACH, f"building: {cmd[:50].strip()}"
    if _RE_GIT_FINISH.search(c):
        return ON_APPROACH, cmd[:60].strip()
    if _RE_GIT_INFO.search(c):
        return HOLDING, f"git: {cmd[:50].strip()}"
    if _RE_INSTALL.search(c):
        return AIRBORNE, "installing deps"

    # Generic active shell command
    return AIRBORNE, cmd[:60].strip()


def _tool_classify(name: str, inp: dict) -> tuple[str, str]:
    """Classify a single tool_use block → (status, phase)."""
    if name in _WRITE_TOOLS:
        fp = inp.get("file_path") or inp.get("notebook_path", "")
        label = Path(fp).name if fp else name.lower()
        return AIRBORNE, f"editing {label}"

    if name == "Bash":
        cmd = (inp.get("command") or "").strip()
        return _bash_classify(cmd)

    if name == "Agent":
        desc = (inp.get("description") or inp.get("prompt", ""))[:60]
        return AIRBORNE, f"sub-agent: {desc}"

    if name in _READ_TOOLS:
        fp = inp.get("file_path") or inp.get("pattern") or inp.get("url", "")
        label = Path(str(fp)).name if fp else name.lower()
        return HOLDING, f"reading {label}"

    # Unknown tool — treat as active
    return AIRBORNE, name.lower()


# ── Public API ────────────────────────────────────────────────────────

def classify(events: list[dict]) -> tuple[str, str]:
    """Classify a batch of raw JSONL event dicts → (status, phase).

    Uses last-significant-action-wins: whichever status bucket (AIRBORNE,
    ON_APPROACH, HOLDING) had its most recent event latest in the stream
    wins. This means an agent that wrote code 50 events ago but is currently
    reading shows HOLDING — the strip reflects what's happening right now.

    Within a single assistant turn, the last tool in content[] wins for
    that turn's classification.

    Error density: ≥2 tool errors → appends count to phase.
    Loop detection: same call failing 3× → "stuck: ..." phase.
    Narration: text blocks can promote HOLDING → ON_APPROACH for wrap-up.
    """
    if not events:
        return HOLDING, "idle"

    # Track last step where each status was seen. We use a per-tool-block
    # monotonic step (not per-event) so tools within the same assistant
    # message are ordered correctly — the last tool in content[] wins.
    last: dict[str, int]  = {AIRBORNE: -1, ON_APPROACH: -1, HOLDING: -1}
    phase: dict[str, str] = {AIRBORNE: "idle", ON_APPROACH: "idle", HOLDING: "idle"}
    error_count = 0
    step = 0  # increments per tool_use block and per text block

    for event in events:
        etype = event.get("type")

        if etype == "assistant":
            content = event.get("message", {}).get("content") or []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")

                if btype == "tool_use":
                    name = block.get("name", "")
                    inp  = block.get("input") or {}
                    s, p = _tool_classify(name, inp)
                    last[s]  = step
                    phase[s] = p
                    step += 1

                elif btype == "text":
                    text = (block.get("text") or "").lower()
                    if any(kw in text for kw in _NARRATION_ON_APPROACH):
                        last[ON_APPROACH]  = step
                        phase[ON_APPROACH] = "wrapping up"
                    step += 1

        elif etype == "user":
            content = event.get("message", {}).get("content") or []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("is_error") is True:
                        error_count += 1

    # Status with the most recent last-seen event wins
    best_status = max(last, key=lambda s: last[s])
    if last[best_status] == -1:
        best_status = HOLDING
    best_phase = phase[best_status]

    # Loop detection takes priority over generic error count
    loop = _detect_loop(events)
    if loop:
        best_phase = loop
        if best_status == HOLDING:
            best_status = AIRBORNE  # retrying = active, not idle
    elif error_count >= 2 and best_status == AIRBORNE:
        best_phase = f"{best_phase} ({error_count} errors)"

    return best_status, best_phase
