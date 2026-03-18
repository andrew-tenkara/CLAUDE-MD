"""USS Tenkara — Interactive SDK agent.

Wraps the Claude Agent SDK for agents that support ongoing conversation
(like Mini Boss). Uses AsyncIterable prompt to stream user messages into
a running query() session.

The agent runs in a background thread. User messages are injected via
send_message(). The agent's responses stream back via on_event callback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

log = logging.getLogger(__name__)

# Lazy import
_sdk_available = False
try:
    from claude_agent_sdk import (
        query,
        ClaudeAgentOptions,
        AssistantMessage,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ToolUseBlock,
        ToolResultBlock,
    )
    _sdk_available = True
except ImportError:
    pass

from sdk_bridge import AgentEvent


class InteractiveAgent:
    """An SDK agent that supports ongoing conversation via streaming prompt.

    Unlike SdkAgent which takes a single directive and runs to completion,
    InteractiveAgent keeps the query() session alive and accepts new user
    messages via send_message(). Responses stream back through on_event.

    This is the foundation for Mini Boss as an SDK agent (Phase 3).
    """

    def __init__(
        self,
        callsign: str,
        model: str,
        cwd: str,
        system_prompt: str,
        initial_prompt: str,
        on_event: Callable[[AgentEvent], None],
        on_exit: Callable[[str, int], None],
        allowed_tools: list[str] | None = None,
        permission_mode: str = "default",
        max_budget_usd: float | None = None,
    ) -> None:
        self.callsign = callsign
        self.model = model
        self.cwd = cwd
        self.system_prompt = system_prompt
        self.initial_prompt = initial_prompt
        self.on_event = on_event
        self.on_exit = on_exit
        self.allowed_tools = allowed_tools
        self.permission_mode = permission_mode
        self.max_budget_usd = max_budget_usd

        # State
        self.session_id: str = ""
        self.is_alive: bool = False
        self.total_cost_usd: float = 0.0
        self.total_tokens_in: int = 0
        self.total_tokens_out: int = 0
        self.tool_calls: int = 0

        # Message queue — user messages injected here
        self._message_queue: deque[str] = deque()
        self._message_event: Optional[asyncio.Event] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_requested = False

    def start(self) -> None:
        """Start the interactive agent in a background thread."""
        if self.is_alive:
            return
        self.is_alive = True
        self._stop_requested = False
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Request the agent to stop."""
        self._stop_requested = True
        # Wake up the message queue so the prompt generator can exit
        if self._message_event and self._loop:
            self._loop.call_soon_threadsafe(self._message_event.set)

    def send_message(self, text: str) -> bool:
        """Inject a user message into the conversation.

        Thread-safe — can be called from the TUI main thread.
        """
        if not self.is_alive:
            return False
        self._message_queue.append(text)
        # Wake up the async prompt generator
        if self._message_event and self._loop:
            self._loop.call_soon_threadsafe(self._message_event.set)
        return True

    def _run_loop(self) -> None:
        """Run the async agent loop in a dedicated event loop."""
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._message_event = asyncio.Event()
        try:
            loop.run_until_complete(self._agent_loop())
        except Exception as e:
            log.error("Interactive agent %s crashed: %s", self.callsign, e)
            self._emit(AgentEvent(
                callsign=self.callsign, type="error", error=str(e),
            ))
            self.on_exit(self.callsign, 1)
        finally:
            self.is_alive = False
            loop.close()

    async def _prompt_stream(self) -> AsyncIterator[dict[str, Any]]:
        """Async generator that yields user messages as they arrive.

        The initial prompt is yielded first. Then the generator waits
        for new messages via send_message(). Yields conversation_turn
        dicts compatible with the SDK's prompt streaming.
        """
        # Yield initial prompt
        yield {"role": "user", "content": self.initial_prompt}

        # Then yield user messages as they arrive
        while not self._stop_requested:
            # Wait for a message
            if not self._message_queue:
                self._message_event.clear()
                try:
                    await asyncio.wait_for(self._message_event.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue

            if self._stop_requested:
                break

            while self._message_queue:
                msg = self._message_queue.popleft()
                yield {"role": "user", "content": msg}

    async def _agent_loop(self) -> None:
        """The async agent execution with streaming prompt."""
        if not _sdk_available:
            self.on_exit(self.callsign, 1)
            return

        options = ClaudeAgentOptions(
            model=self.model,
            cwd=self.cwd,
            system_prompt=self.system_prompt,
            permission_mode=self.permission_mode,
            allowed_tools=self.allowed_tools or [],
            max_budget_usd=self.max_budget_usd,
        )

        return_code = 0

        try:
            async for message in query(prompt=self._prompt_stream(), options=options):
                if self._stop_requested:
                    break

                if isinstance(message, AssistantMessage):
                    if message.usage:
                        self.total_tokens_in += message.usage.get("input_tokens", 0)
                        self.total_tokens_out += message.usage.get("output_tokens", 0)

                    if message.error:
                        self._emit(AgentEvent(
                            callsign=self.callsign, type="error",
                            error=f"API error: {message.error}",
                        ))
                        continue

                    for block in message.content:
                        if isinstance(block, TextBlock):
                            self._emit(AgentEvent(
                                callsign=self.callsign, type="text",
                                text=block.text, model=message.model or self.model,
                            ))
                        elif isinstance(block, ToolUseBlock):
                            self.tool_calls += 1
                            self._emit(AgentEvent(
                                callsign=self.callsign, type="tool_use",
                                tool_name=block.name,
                                tool_input=block.input if isinstance(block.input, dict) else {},
                            ))

                elif isinstance(message, ResultMessage):
                    self.session_id = message.session_id or ""
                    if message.total_cost_usd:
                        self.total_cost_usd = message.total_cost_usd
                    if message.is_error:
                        return_code = 1

                elif isinstance(message, SystemMessage):
                    if message.subtype == "init" and message.data:
                        self.session_id = message.data.get("session_id", "")

        except Exception as e:
            log.error("Interactive agent %s error: %s", self.callsign, e)
            return_code = 1
            self._emit(AgentEvent(
                callsign=self.callsign, type="error", error=str(e),
            ))

        self._emit(AgentEvent(
            callsign=self.callsign, type="done",
            session_id=self.session_id, cost_usd=self.total_cost_usd,
        ))
        self.on_exit(self.callsign, return_code)

    def _emit(self, event: AgentEvent) -> None:
        try:
            self.on_event(event)
        except Exception as e:
            log.warning("Event callback error for %s: %s", self.callsign, e)
