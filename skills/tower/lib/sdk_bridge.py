"""USS Tenkara — Claude Agent SDK bridge.

Wraps the Claude Agent SDK to spawn and manage agents programmatically.
Replaces the iTerm2 + shell script approach with in-process agent execution.

Agents run as async tasks, streaming events back to the TUI via callbacks.
Each agent gets its own query() call with a dedicated cwd (worktree).

Requires Python 3.10+ and claude-agent-sdk package.
The venv at .venv/ in the skill directory has the SDK installed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# Lazy import — SDK requires 3.10+ and may not be in the system Python
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


def _has_api_key() -> bool:
    """Check if an Anthropic API key is available (required for SDK).

    The Agent SDK with subscription OAuth tokens is a ToS violation per
    Anthropic's Feb 19 2026 legal docs. We only allow SDK usage with an
    explicit API key.
    """
    import os
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    try:
        key_file = Path.home() / ".config" / "anthropic" / "api_key"
        return key_file.exists() and len(key_file.read_text().strip()) > 0
    except OSError:
        return False


def sdk_available() -> bool:
    """Check if the Claude Agent SDK is usable.

    Requires BOTH the SDK package AND an API key. Using the SDK with
    subscription OAuth is explicitly banned by Anthropic's ToS.
    """
    return _sdk_available and _has_api_key()


# ── Event types for TUI consumption ─────────────────────────────────

@dataclass
class AgentEvent:
    """Normalized event from an SDK agent for the TUI to consume."""
    callsign: str
    type: str  # "text", "tool_use", "tool_result", "error", "done", "rate_limit"
    text: str = ""
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    tool_result: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    model: str = ""
    session_id: str = ""
    error: str = ""


# ── SDK Agent wrapper ────────────────────────────────────────────────

class SdkAgent:
    """Manages a single Claude Agent SDK session.

    Runs the agent's async query loop in a background thread with its own
    event loop. Streams events to the TUI via an on_event callback.
    """

    def __init__(
        self,
        callsign: str,
        model: str,
        cwd: str,
        directive: str,
        on_event: Callable[[AgentEvent], None],
        on_exit: Callable[[str, int], None],
        system_prompt: str = "",
        disallowed_tools: list[str] | None = None,
        max_budget_usd: float | None = None,
        permission_mode: str = "acceptEdits",
    ) -> None:
        self.callsign = callsign
        self.model = model
        self.cwd = cwd
        self.directive = directive
        self.on_event = on_event
        self.on_exit = on_exit
        self.system_prompt = system_prompt
        self.disallowed_tools = disallowed_tools or []
        self.max_budget_usd = max_budget_usd
        self.permission_mode = permission_mode

        # State
        self.session_id: str = ""
        self.is_alive: bool = False
        self.total_tokens_in: int = 0
        self.total_tokens_out: int = 0
        self.total_cost_usd: float = 0.0
        self.tool_calls: int = 0
        self.error_count: int = 0
        self.last_tool_at: float = 0.0
        self.started_at: float = 0.0

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_requested = False
        self._jsonl_file: Optional[Any] = None
        self._jsonl_path: Optional[Path] = None

    @property
    def total_tokens(self) -> int:
        return self.total_tokens_in + self.total_tokens_out

    @property
    def fuel_pct(self) -> int:
        """Estimate fuel from token usage. Rough heuristic until we get real context usage."""
        # Assume ~200k context window, tokens_in is cumulative
        # Prefer token-based estimate, fall back to cost-based.
        # Note: SDK v0.1.49 doesn't expose per-message token counts (usage is None).
        # Cost only updates at session end via ResultMessage. During active sessions,
        # fuel stays at 100% — this is a known SDK limitation.
        if self.total_tokens_in > 0:
            used_pct = min(100, int(self.total_tokens_in / 200_000 * 100))
            return max(0, 100 - used_pct)
        if self.total_cost_usd > 0:
            # Rough cost-based estimate: $0.20 ≈ full context for sonnet
            used_pct = min(100, int(self.total_cost_usd / 0.20 * 100))
            return max(0, 100 - used_pct)
        return 100

    def _init_jsonl(self) -> None:
        """Set up JSONL tee file mirroring Claude CLI's path structure."""
        try:
            # Mirror: ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl
            claude_projects = Path.home() / ".claude" / "projects"
            encoded = self.cwd.replace("/", "-")
            if encoded.startswith("-"):
                encoded = encoded  # keep leading dash like CLI does
            jsonl_dir = claude_projects / encoded
            jsonl_dir.mkdir(parents=True, exist_ok=True)
            session_id = f"sdk-{self.callsign.lower()}-{int(time.time())}"
            self._jsonl_path = jsonl_dir / f"{session_id}.jsonl"
            self._jsonl_file = open(self._jsonl_path, "a", encoding="utf-8")
            log.info("JSONL tee: %s", self._jsonl_path)
        except Exception as e:
            log.warning("Failed to init JSONL tee: %s", e)
            self._jsonl_file = None

    def _write_jsonl(self, data: dict) -> None:
        """Append a JSON line to the tee file."""
        if self._jsonl_file:
            try:
                self._jsonl_file.write(json.dumps(data) + "\n")
                self._jsonl_file.flush()
            except Exception:
                pass

    def _close_jsonl(self) -> None:
        """Close the JSONL tee file."""
        if self._jsonl_file:
            try:
                self._jsonl_file.close()
            except Exception:
                pass
            self._jsonl_file = None

    def start(self) -> None:
        """Start the agent in a background thread."""
        if self.is_alive:
            return
        self.is_alive = True
        self.started_at = time.time()
        self._stop_requested = False
        self._init_jsonl()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Request the agent to stop."""
        self._stop_requested = True

    def inject_message(self, text: str) -> bool:
        """Inject a user message into the agent's conversation.

        Note: The SDK's query() doesn't support mid-stream injection natively.
        For now, this is a placeholder. Phase 3 will add proper inter-agent messaging.
        """
        # TODO: Implement via SDK's continue_conversation or prompt streaming
        log.warning("inject_message not yet implemented for SDK agents")
        return False

    def _run_loop(self) -> None:
        """Run the async agent loop in a dedicated event loop on this thread."""
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._agent_loop())
        except Exception as e:
            log.error("Agent %s crashed: %s", self.callsign, e)
            self._emit(AgentEvent(
                callsign=self.callsign, type="error",
                error=str(e),
            ))
            self.on_exit(self.callsign, 1)
        finally:
            self.is_alive = False
            self._close_jsonl()
            loop.close()

    async def _agent_loop(self) -> None:
        """The actual async agent execution."""
        options = ClaudeAgentOptions(
            model=self.model,
            cwd=self.cwd,
            system_prompt=self.system_prompt or None,
            permission_mode=self.permission_mode,
            disallowed_tools=self.disallowed_tools if self.disallowed_tools else [],
            max_budget_usd=self.max_budget_usd,
        )

        return_code = 0

        try:
            async for message in query(prompt=self.directive, options=options):
                if self._stop_requested:
                    break

                if isinstance(message, AssistantMessage):
                    self._handle_assistant(message)

                elif isinstance(message, ResultMessage):
                    self._handle_result(message)

                elif isinstance(message, SystemMessage):
                    self._handle_system(message)

                # StreamEvent and RateLimitEvent
                elif hasattr(message, 'type'):
                    msg_type = getattr(message, 'type', '')
                    if msg_type == 'rate_limit':
                        self._emit(AgentEvent(
                            callsign=self.callsign, type="rate_limit",
                            text="Rate limited — backing off",
                        ))

        except Exception as e:
            log.error("Agent %s query error: %s", self.callsign, e)
            return_code = 1
            self._emit(AgentEvent(
                callsign=self.callsign, type="error",
                error=str(e),
            ))

        self._emit(AgentEvent(
            callsign=self.callsign, type="done",
            session_id=self.session_id,
            cost_usd=self.total_cost_usd,
        ))
        self.on_exit(self.callsign, return_code)

    def _handle_assistant(self, msg: "AssistantMessage") -> None:
        """Process an assistant message with text and/or tool calls."""
        # Tee to JSONL
        try:
            content_summary = []
            for block in msg.content:
                if isinstance(block, TextBlock):
                    content_summary.append({"type": "text", "text": block.text})
                elif isinstance(block, ToolUseBlock):
                    content_summary.append({"type": "tool_use", "name": block.name, "input": block.input if isinstance(block.input, dict) else {}})
            self._write_jsonl({
                "type": "assistant",
                "message": {"role": "assistant", "content": content_summary, "model": msg.model or self.model},
                "timestamp": time.time(),
            })
        except Exception:
            pass

        # Track usage
        if msg.usage:
            tokens_in = msg.usage.get("input_tokens", 0)
            tokens_out = msg.usage.get("output_tokens", 0)
            self.total_tokens_in += tokens_in
            self.total_tokens_out += tokens_out

        if msg.model:
            self.model = msg.model

        if msg.error:
            self.error_count += 1
            self._emit(AgentEvent(
                callsign=self.callsign, type="error",
                error=f"API error: {msg.error}",
            ))
            return

        for block in msg.content:
            if isinstance(block, TextBlock):
                self._emit(AgentEvent(
                    callsign=self.callsign, type="text",
                    text=block.text,
                    model=msg.model or self.model,
                ))

            elif isinstance(block, ToolUseBlock):
                self.tool_calls += 1
                self.last_tool_at = time.time()
                self._emit(AgentEvent(
                    callsign=self.callsign, type="tool_use",
                    tool_name=block.name,
                    tool_input=block.input if isinstance(block.input, dict) else {},
                    model=msg.model or self.model,
                ))

            elif isinstance(block, ToolResultBlock):
                result_text = ""
                if isinstance(block.content, str):
                    result_text = block.content[:200]
                elif isinstance(block.content, list):
                    for item in block.content:
                        if hasattr(item, 'text'):
                            result_text = item.text[:200]
                            break
                self._emit(AgentEvent(
                    callsign=self.callsign, type="tool_result",
                    tool_result=result_text,
                ))

    def _handle_result(self, msg: "ResultMessage") -> None:
        """Process the final result message."""
        self.session_id = msg.session_id or ""
        if msg.total_cost_usd:
            self.total_cost_usd = msg.total_cost_usd
        if msg.usage:
            self.total_tokens_in = msg.usage.get("input_tokens", self.total_tokens_in)
            self.total_tokens_out = msg.usage.get("output_tokens", self.total_tokens_out)

        # If SDK didn't provide token counts but we have cost, estimate tokens
        # so the JSONL is compatible with parse_jsonl_metrics and fuel gauge works
        estimated_in = self.total_tokens_in
        estimated_out = self.total_tokens_out
        if estimated_in == 0 and self.total_cost_usd > 0:
            # Rough estimate: sonnet ~$3/MTok in, ~$15/MTok out
            # Assume 80/20 in/out split, average $5/MTok blended
            estimated_total = int(self.total_cost_usd / 5.0 * 1_000_000)
            estimated_in = int(estimated_total * 0.8)
            estimated_out = int(estimated_total * 0.2)
            self.total_tokens_in = estimated_in
            self.total_tokens_out = estimated_out

        # Tee to JSONL — write a synthetic assistant message with usage
        # so parse_jsonl_metrics can extract token counts
        self._write_jsonl({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [],
                "usage": {
                    "input_tokens": estimated_in,
                    "output_tokens": estimated_out,
                },
            },
            "timestamp": time.time(),
        })
        self._write_jsonl({
            "type": "result",
            "session_id": self.session_id,
            "total_cost_usd": self.total_cost_usd,
            "num_turns": msg.num_turns,
            "duration_ms": msg.duration_ms,
            "is_error": msg.is_error,
            "timestamp": time.time(),
        })
        self._close_jsonl()

    def _handle_system(self, msg: "SystemMessage") -> None:
        """Process system messages (init, heartbeat, etc)."""
        if msg.subtype == "init" and msg.data:
            self.session_id = msg.data.get("session_id", "")
        self._write_jsonl({
            "type": "system",
            "subtype": msg.subtype,
            "data": msg.data,
            "timestamp": time.time(),
        })

    def _emit(self, event: AgentEvent) -> None:
        """Send an event to the TUI callback."""
        try:
            self.on_event(event)
        except Exception as e:
            log.warning("Event callback error for %s: %s", self.callsign, e)


# ── SDK Agent Manager ────────────────────────────────────────────────

class SdkAgentManager:
    """Manages multiple SDK agents. Drop-in alongside the existing AgentManager.

    The TUI creates this in __init__ and uses it for SDK-spawned agents.
    Legacy agents (iTerm2 panes) continue through the existing AgentManager.
    During migration, both managers coexist.
    """

    def __init__(
        self,
        on_event: Callable[[str, AgentEvent], None],
        on_exit: Callable[[str, int], None],
    ) -> None:
        self._agents: dict[str, SdkAgent] = {}
        self._on_event = on_event
        self._on_exit = on_exit

    def spawn(
        self,
        callsign: str,
        model: str,
        cwd: str,
        directive: str,
        system_prompt: str = "",
        disallowed_tools: list[str] | None = None,
        max_budget_usd: float | None = None,
    ) -> SdkAgent:
        """Spawn a new SDK agent. Requires an API key (not subscription OAuth).

        Raises RuntimeError if no API key is configured — using the Agent SDK
        with subscription OAuth tokens violates Anthropic's Consumer ToS.
        """
        if not _has_api_key():
            raise RuntimeError(
                "Agent SDK requires an API key (ANTHROPIC_API_KEY or ~/.config/anthropic/api_key). "
                "Using subscription OAuth with the SDK violates Anthropic's ToS. "
                "Get a key at https://console.anthropic.com"
            )
        agent = SdkAgent(
            callsign=callsign,
            model=model,
            cwd=cwd,
            directive=directive,
            on_event=lambda evt: self._on_event(callsign, evt),
            on_exit=self._on_exit,
            system_prompt=system_prompt,
            disallowed_tools=disallowed_tools,
            max_budget_usd=max_budget_usd,
        )
        self._agents[callsign] = agent
        agent.start()
        return agent

    def get(self, callsign: str) -> Optional[SdkAgent]:
        return self._agents.get(callsign)

    def wave_off(self, callsign: str) -> bool:
        """Hard stop an agent."""
        agent = self._agents.get(callsign)
        if agent and agent.is_alive:
            agent.stop()
            return True
        return False

    def recall(self, callsign: str) -> bool:
        """Graceful wind-down — inject a wrap-up message then stop."""
        agent = self._agents.get(callsign)
        if agent and agent.is_alive:
            # TODO: inject "wrap up" message via SDK when supported
            agent.stop()
            return True
        return False

    def active_agents(self) -> list[SdkAgent]:
        return [a for a in self._agents.values() if a.is_alive]

    def shutdown(self) -> None:
        """Stop all agents."""
        for agent in self._agents.values():
            if agent.is_alive:
                agent.stop()
