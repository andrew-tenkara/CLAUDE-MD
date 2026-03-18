"""USS Tenkara — Squadron Analyst.

Periodic headless Haiku agent that reviews all agent statuses and
provides squadron-level tactical intelligence. Catches things that
per-agent classification can't:

- Stuck agents (ON_APPROACH for too long)
- Conflict risk (multiple agents editing same areas)
- Context waste (burning context on reads without writes)
- Idle agents that should be recalled
- Mission completion patterns

Runs every N minutes as a background thread. Results posted to
radio chatter and optionally to a callback.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────

ANALYSIS_INTERVAL = 120  # seconds between analyses (2 minutes)


def _get_api_key() -> str:
    """Get Anthropic API key from env or file."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        key_file = Path.home() / ".config" / "anthropic" / "api_key"
        try:
            key = key_file.read_text().strip()
        except OSError:
            pass
    return key


class SquadronAnalyst:
    """Periodic Haiku-powered squadron status analyst.

    Takes a snapshot of all pilots and asks Haiku for tactical assessments.
    Results are delivered via on_assessment callback.
    """

    def __init__(
        self,
        on_assessment: Callable[[str], None],
        interval: float = ANALYSIS_INTERVAL,
    ) -> None:
        self._on_assessment = on_assessment
        self._interval = interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_snapshot: str = ""

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("Squadron analyst started (interval=%ds)", self._interval)

    def stop(self) -> None:
        self._running = False

    @property
    def is_alive(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def analyze(self, pilots_snapshot: list[dict]) -> Optional[str]:
        """Run a single analysis on the given pilot snapshot.

        Returns the assessment text, or None if analysis fails.
        """
        if not pilots_snapshot:
            return None

        api_key = _get_api_key()
        if not api_key:
            return None

        try:
            import anthropic
        except ImportError:
            return None

        # Build the sitrep for Haiku
        sitrep_lines = []
        for p in pilots_snapshot:
            sitrep_lines.append(
                f"  {p['callsign']} | {p['status']} | fuel:{p['fuel_pct']}% | "
                f"tools:{p['tool_calls']} | errors:{p['error_count']} | "
                f"ticket:{p['ticket_id']} | elapsed:{p['elapsed_mins']:.0f}m"
            )
        sitrep = "\n".join(sitrep_lines) if sitrep_lines else "  No agents deployed."

        # Skip if nothing changed since last analysis
        if sitrep == self._last_snapshot:
            return None
        self._last_snapshot = sitrep

        prompt = (
            "You are the Squadron Analyst on USS Tenkara. Review this flight deck status "
            "and provide a 2-3 line tactical assessment. Focus ONLY on actionable insights:\n\n"
            "- Stuck agents (same status too long)\n"
            "- Context waste (high tool calls but low progress)\n"
            "- Fuel warnings (below 30%)\n"
            "- Error spikes\n"
            "- Agents that should be recalled or redeployed\n\n"
            "If everything looks normal, say 'Flight deck nominal.' and nothing else.\n\n"
            f"CURRENT STATUS:\n{sitrep}"
        )

        try:
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    text += block.text
            return text.strip() if text.strip() else None
        except Exception as e:
            log.warning("Squadron analysis failed: %s", e)
            return None

    def _run(self) -> None:
        """Main loop — periodic analysis."""
        # Wait a bit on startup for agents to populate
        time.sleep(30)

        while self._running:
            try:
                # Get snapshot from callback
                snapshot = self._get_snapshot()
                if snapshot:
                    assessment = self.analyze(snapshot)
                    if assessment and assessment != "Flight deck nominal.":
                        self._on_assessment(assessment)
            except Exception as e:
                log.warning("Squadron analyst error: %s", e)
            time.sleep(self._interval)

    def _get_snapshot(self) -> Optional[list[dict]]:
        """Override point — subclass or set via set_snapshot_provider."""
        return self._snapshot_fn() if self._snapshot_fn else None

    _snapshot_fn: Optional[Callable] = None

    def set_snapshot_provider(self, fn: Callable[[], list[dict]]) -> None:
        """Set a function that returns the current pilot snapshot."""
        self._snapshot_fn = fn
