"""Haiku transition gate — LLM-backed approval for flight status changes.

Architecture: "Rule-based proposes, Haiku disposes."  The deterministic
classifier in classify.py proposes a new status; this module asks Haiku
whether the transition makes narrative sense given the compressed event
summary.  Structured output via tool_use guarantees parseable JSON.

Graceful degradation: no API key, API failure, parse error → auto-approve.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("gate")

# ── Result type ──────────────────────────────────────────────────────

@dataclass
class GateResult:
    approved: bool
    final_status: str
    phase: str
    confidence: float
    reason: str


# ── Haiku tool schema (structured output) ────────────────────────────

_GATE_TOOL = {
    "name": "classify_transition",
    "description": "Approve or deny a flight status transition",
    "input_schema": {
        "type": "object",
        "properties": {
            "approved": {"type": "boolean"},
            "final_status": {
                "type": "string",
                "enum": ["IDLE", "PREFLIGHT", "AIRBORNE", "ON_APPROACH", "RECOVERED"],
            },
            "phase": {"type": "string"},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["approved", "final_status", "confidence", "reason", "phase"],
        "additionalProperties": False,
    },
}

_SYSTEM_PROMPT = """\
You are a flight status transition validator for an agent monitoring system.
Agents go through this lifecycle:

  IDLE → PREFLIGHT → AIRBORNE → ON_APPROACH → RECOVERED

Normal transitions:
- IDLE → PREFLIGHT: agent starts reading files (reconnaissance before first write)
- PREFLIGHT → AIRBORNE: agent performs its first write/edit/build action
- AIRBORNE → ON_APPROACH: agent starts testing, committing, pushing (wrapping up)
- ON_APPROACH → RECOVERED: agent session ends
- Any → IDLE: agent goes silent for 90+ seconds

Rare but valid:
- AIRBORNE → PREFLIGHT: agent finished one sub-task and is reading for the next
- ON_APPROACH → AIRBORNE: tests failed, agent is fixing code
- RECOVERED → AIRBORNE: agent resumed after being marked done (stale events)

Should usually be DENIED:
- AIRBORNE → HOLDING/IDLE mid-flight: likely just a read between writes, not actually idle
- RECOVERED → AIRBORNE/PREFLIGHT: stale JSONL events after session ended
- ON_APPROACH → PREFLIGHT: unlikely regression, probably transient reads during wrap-up

Use the compressed event summary to judge whether the transition reflects a real
phase change or transient noise. Set confidence 0.0-1.0. Transitions below 0.7
confidence will be auto-denied regardless of your approved flag.\
"""

CONFIDENCE_THRESHOLD = 0.7
GATE_TIMEOUT = 3.0  # seconds
MODEL = "claude-haiku-4-5-20251001"


def gate_transition(
    current: str,
    proposed: str,
    time_in_current_secs: float,
    compressed: dict,
) -> GateResult:
    """Ask Haiku whether a status transition should be approved.

    Returns GateResult. On any failure, returns an auto-approved result
    so the system degrades to rule-based-only behavior.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        # Fall back to dedicated key file (avoids polluting shell env)
        key_file = Path.home() / ".config" / "anthropic" / "api_key"
        try:
            api_key = key_file.read_text().strip()
        except OSError:
            pass
    if not api_key:
        log.debug("no API key — auto-approving %s → %s", current, proposed)
        return GateResult(
            approved=True, final_status=proposed, phase="(no gate)",
            confidence=1.0, reason="no API key",
        )

    try:
        import anthropic
    except ImportError:
        log.debug("anthropic SDK not installed — auto-approving")
        return GateResult(
            approved=True, final_status=proposed, phase="(no gate)",
            confidence=1.0, reason="SDK not installed",
        )

    user_msg = json.dumps({
        "current_status": current,
        "proposed_status": proposed,
        "time_in_current_secs": round(time_in_current_secs, 1),
        "compressed": compressed,
    }, indent=2)

    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=GATE_TIMEOUT)
        response = client.messages.create(
            model=MODEL,
            max_tokens=256,
            system=_SYSTEM_PROMPT,
            tools=[_GATE_TOOL],
            tool_choice={"type": "tool", "name": "classify_transition"},
            messages=[{"role": "user", "content": user_msg}],
        )

        # Extract tool_use block from response
        for block in response.content:
            if block.type == "tool_use" and block.name == "classify_transition":
                inp = block.input
                approved = bool(inp.get("approved", False))
                confidence = float(inp.get("confidence", 0.0))
                final_status = inp.get("final_status", proposed)
                phase = inp.get("phase", "")
                reason = inp.get("reason", "")

                # Confidence threshold — low conviction = denied
                if confidence < CONFIDENCE_THRESHOLD:
                    approved = False
                    reason = f"low confidence ({confidence:.2f}): {reason}"

                log.info(
                    "gate %s → %s: %s (conf=%.2f, final=%s) — %s",
                    current, proposed,
                    "APPROVED" if approved else "DENIED",
                    confidence, final_status, reason,
                )
                return GateResult(
                    approved=approved,
                    final_status=final_status if approved else current,
                    phase=phase,
                    confidence=confidence,
                    reason=reason,
                )

        # No tool_use block found — auto-approve
        log.warning("gate response had no tool_use block — auto-approving")
        return GateResult(
            approved=True, final_status=proposed, phase="(parse fallback)",
            confidence=1.0, reason="no tool_use in response",
        )

    except Exception as e:
        log.warning("gate API error — auto-approving %s → %s: %s", current, proposed, e)
        return GateResult(
            approved=True, final_status=proposed, phase="(gate error)",
            confidence=1.0, reason=f"API error: {e}",
        )
