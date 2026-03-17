"""Tests for sdk_bridge.py — SdkAgent, SdkAgentManager, AgentEvent.

Tests the bridge layer without making real SDK calls. Mocks the query()
function to simulate agent behavior.
"""
import sys
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from sdk_bridge import (
    SdkAgent, SdkAgentManager, AgentEvent, sdk_available,
)


# ── Helpers ──────────────────────────────────────────────────────────

class EventCollector:
    """Collects AgentEvents for assertions."""
    def __init__(self):
        self.events: list[AgentEvent] = []
        self.exit_calls: list[tuple[str, int]] = []

    def on_event(self, event: AgentEvent):
        self.events.append(event)

    def on_manager_event(self, callsign: str, event: AgentEvent):
        self.events.append(event)

    def on_exit(self, callsign: str, return_code: int):
        self.exit_calls.append((callsign, return_code))

    def events_of_type(self, t: str) -> list[AgentEvent]:
        return [e for e in self.events if e.type == t]

    def has_event(self, t: str) -> bool:
        return any(e.type == t for e in self.events)


# ── Tests: AgentEvent ────────────────────────────────────────────────

class TestAgentEvent(unittest.TestCase):
    def test_defaults(self):
        evt = AgentEvent(callsign="VIPER-1", type="text")
        assert evt.callsign == "VIPER-1"
        assert evt.type == "text"
        assert evt.text == ""
        assert evt.tool_name == ""
        assert evt.tokens_in == 0
        assert evt.cost_usd == 0.0

    def test_tool_event(self):
        evt = AgentEvent(
            callsign="VIPER-1", type="tool_use",
            tool_name="Edit", tool_input={"file_path": "/src/auth.ts"},
        )
        assert evt.tool_name == "Edit"
        assert evt.tool_input["file_path"] == "/src/auth.ts"


# ── Tests: SdkAgent state ───────────────────────────────────────────

class TestSdkAgentState(unittest.TestCase):
    def test_initial_state(self):
        collector = EventCollector()
        agent = SdkAgent(
            callsign="VIPER-1", model="sonnet", cwd="/tmp/test",
            directive="test", on_event=collector.on_event,
            on_exit=collector.on_exit,
        )
        assert agent.callsign == "VIPER-1"
        assert agent.model == "sonnet"
        assert agent.is_alive is False
        assert agent.total_tokens == 0
        assert agent.fuel_pct == 100
        assert agent.tool_calls == 0
        assert agent.error_count == 0

    def test_fuel_pct_calculation(self):
        collector = EventCollector()
        agent = SdkAgent(
            callsign="VIPER-1", model="sonnet", cwd="/tmp/test",
            directive="test", on_event=collector.on_event,
            on_exit=collector.on_exit,
        )
        # No tokens = full fuel
        assert agent.fuel_pct == 100

        # 50% through context window
        agent.total_tokens_in = 100_000
        assert agent.fuel_pct == 50

        # Maxed out
        agent.total_tokens_in = 200_000
        assert agent.fuel_pct == 0

        # Over max clamps to 0
        agent.total_tokens_in = 300_000
        assert agent.fuel_pct == 0

    def test_total_tokens(self):
        collector = EventCollector()
        agent = SdkAgent(
            callsign="VIPER-1", model="sonnet", cwd="/tmp/test",
            directive="test", on_event=collector.on_event,
            on_exit=collector.on_exit,
        )
        agent.total_tokens_in = 1000
        agent.total_tokens_out = 500
        assert agent.total_tokens == 1500


# ── Tests: SdkAgentManager ──────────────────────────────────────────

class TestSdkAgentManager(unittest.TestCase):
    def test_spawn_creates_agent(self):
        collector = EventCollector()
        mgr = SdkAgentManager(
            on_event=collector.on_manager_event,
            on_exit=collector.on_exit,
        )

        # Patch start() so we don't actually launch a thread
        with patch.object(SdkAgent, 'start'):
            agent = mgr.spawn(
                callsign="VIPER-1", model="sonnet",
                cwd="/tmp/test", directive="do stuff",
            )

        assert agent.callsign == "VIPER-1"
        assert agent.model == "sonnet"
        assert mgr.get("VIPER-1") is agent
        assert mgr.get("NONEXISTENT") is None

    def test_spawn_multiple_agents(self):
        collector = EventCollector()
        mgr = SdkAgentManager(
            on_event=collector.on_manager_event,
            on_exit=collector.on_exit,
        )

        with patch.object(SdkAgent, 'start'):
            a1 = mgr.spawn(callsign="VIPER-1", model="sonnet", cwd="/tmp/1", directive="task 1")
            a2 = mgr.spawn(callsign="VIPER-2", model="opus", cwd="/tmp/2", directive="task 2")
            a3 = mgr.spawn(callsign="GHOST-1", model="haiku", cwd="/tmp/3", directive="task 3")

        assert len(mgr._agents) == 3
        assert mgr.get("VIPER-2").model == "opus"

    def test_wave_off_stops_agent(self):
        collector = EventCollector()
        mgr = SdkAgentManager(
            on_event=collector.on_manager_event,
            on_exit=collector.on_exit,
        )

        with patch.object(SdkAgent, 'start'):
            agent = mgr.spawn(callsign="VIPER-1", model="sonnet", cwd="/tmp/test", directive="test")
            agent.is_alive = True  # simulate running

        assert mgr.wave_off("VIPER-1") is True
        assert agent._stop_requested is True
        assert mgr.wave_off("NONEXISTENT") is False

    def test_active_agents(self):
        collector = EventCollector()
        mgr = SdkAgentManager(
            on_event=collector.on_manager_event,
            on_exit=collector.on_exit,
        )

        with patch.object(SdkAgent, 'start'):
            a1 = mgr.spawn(callsign="VIPER-1", model="sonnet", cwd="/tmp/1", directive="task 1")
            a2 = mgr.spawn(callsign="VIPER-2", model="sonnet", cwd="/tmp/2", directive="task 2")
            a1.is_alive = True
            a2.is_alive = False

        active = mgr.active_agents()
        assert len(active) == 1
        assert active[0].callsign == "VIPER-1"

    def test_shutdown_stops_all(self):
        collector = EventCollector()
        mgr = SdkAgentManager(
            on_event=collector.on_manager_event,
            on_exit=collector.on_exit,
        )

        with patch.object(SdkAgent, 'start'):
            a1 = mgr.spawn(callsign="VIPER-1", model="sonnet", cwd="/tmp/1", directive="t1")
            a2 = mgr.spawn(callsign="VIPER-2", model="sonnet", cwd="/tmp/2", directive="t2")
            a1.is_alive = True
            a2.is_alive = True

        mgr.shutdown()
        assert a1._stop_requested is True
        assert a2._stop_requested is True

    def test_recall_stops_agent(self):
        collector = EventCollector()
        mgr = SdkAgentManager(
            on_event=collector.on_manager_event,
            on_exit=collector.on_exit,
        )

        with patch.object(SdkAgent, 'start'):
            agent = mgr.spawn(callsign="VIPER-1", model="sonnet", cwd="/tmp/test", directive="test")
            agent.is_alive = True

        assert mgr.recall("VIPER-1") is True
        assert agent._stop_requested is True


# ── Tests: Event emission ────────────────────────────────────────────

class TestEventEmission(unittest.TestCase):
    def test_emit_calls_callback(self):
        collector = EventCollector()
        agent = SdkAgent(
            callsign="VIPER-1", model="sonnet", cwd="/tmp/test",
            directive="test", on_event=collector.on_event,
            on_exit=collector.on_exit,
        )

        agent._emit(AgentEvent(callsign="VIPER-1", type="text", text="hello"))
        assert len(collector.events) == 1
        assert collector.events[0].text == "hello"

    def test_emit_swallows_callback_errors(self):
        """Event emission should not crash the agent if callback raises."""
        def bad_callback(evt):
            raise RuntimeError("callback exploded")

        agent = SdkAgent(
            callsign="VIPER-1", model="sonnet", cwd="/tmp/test",
            directive="test", on_event=bad_callback,
            on_exit=lambda cs, rc: None,
        )

        # Should not raise
        agent._emit(AgentEvent(callsign="VIPER-1", type="text", text="hello"))

    def test_manager_routes_events_with_callsign(self):
        collector = EventCollector()
        mgr = SdkAgentManager(
            on_event=collector.on_manager_event,
            on_exit=collector.on_exit,
        )

        with patch.object(SdkAgent, 'start'):
            agent = mgr.spawn(callsign="VIPER-1", model="sonnet", cwd="/tmp/test", directive="test")

        # Simulate agent emitting an event
        agent._emit(AgentEvent(callsign="VIPER-1", type="text", text="working"))
        assert len(collector.events) == 1
        assert collector.events[0].callsign == "VIPER-1"


# ── Tests: Assistant message handling ────────────────────────────────

class TestAssistantMessageHandling(unittest.TestCase):
    """Test _handle_assistant with mock SDK message objects."""

    def setUp(self):
        self.collector = EventCollector()
        self.agent = SdkAgent(
            callsign="VIPER-1", model="sonnet", cwd="/tmp/test",
            directive="test", on_event=self.collector.on_event,
            on_exit=self.collector.on_exit,
        )

    def _make_text_block(self, text):
        if sdk_available():
            from claude_agent_sdk import TextBlock
            return TextBlock(text=text)
        # Fallback mock
        mock = MagicMock()
        mock.text = text
        mock.__class__ = type('TextBlock', (), {})
        return mock

    def _make_tool_use_block(self, name, input_dict):
        if sdk_available():
            from claude_agent_sdk import ToolUseBlock
            return ToolUseBlock(id="test", name=name, input=input_dict)
        mock = MagicMock()
        mock.name = name
        mock.input = input_dict
        mock.__class__ = type('ToolUseBlock', (), {})
        return mock

    def _make_assistant_msg(self, content, model="sonnet", usage=None, error=None):
        if sdk_available():
            from claude_agent_sdk import AssistantMessage
            return AssistantMessage(
                content=content, model=model,
                parent_tool_use_id=None, error=error,
                usage=usage,
            )
        mock = MagicMock()
        mock.content = content
        mock.model = model
        mock.usage = usage
        mock.error = error
        return mock

    def test_text_block_emits_text_event(self):
        if not sdk_available():
            self.skipTest("SDK not available")
        from claude_agent_sdk import TextBlock, AssistantMessage
        msg = AssistantMessage(
            content=[TextBlock(text="Hello world")],
            model="sonnet", parent_tool_use_id=None,
            error=None, usage={"input_tokens": 100, "output_tokens": 50},
        )
        self.agent._handle_assistant(msg)
        assert len(self.collector.events) == 1
        assert self.collector.events[0].type == "text"
        assert self.collector.events[0].text == "Hello world"
        assert self.agent.total_tokens_in == 100
        assert self.agent.total_tokens_out == 50

    def test_tool_use_increments_counter(self):
        if not sdk_available():
            self.skipTest("SDK not available")
        from claude_agent_sdk import ToolUseBlock, AssistantMessage
        msg = AssistantMessage(
            content=[ToolUseBlock(id="t1", name="Edit", input={"file_path": "/src/a.ts"})],
            model="sonnet", parent_tool_use_id=None,
            error=None, usage=None,
        )
        self.agent._handle_assistant(msg)
        assert self.agent.tool_calls == 1
        assert self.agent.last_tool_at > 0
        assert self.collector.events[0].type == "tool_use"
        assert self.collector.events[0].tool_name == "Edit"

    def test_error_message_increments_error_count(self):
        if not sdk_available():
            self.skipTest("SDK not available")
        from claude_agent_sdk import AssistantMessage
        msg = AssistantMessage(
            content=[], model="sonnet", parent_tool_use_id=None,
            error="rate_limit", usage=None,
        )
        self.agent._handle_assistant(msg)
        assert self.agent.error_count == 1
        assert self.collector.events[0].type == "error"

    def test_usage_accumulates(self):
        if not sdk_available():
            self.skipTest("SDK not available")
        from claude_agent_sdk import TextBlock, AssistantMessage
        for i in range(3):
            msg = AssistantMessage(
                content=[TextBlock(text=f"msg {i}")],
                model="sonnet", parent_tool_use_id=None,
                error=None, usage={"input_tokens": 100, "output_tokens": 50},
            )
            self.agent._handle_assistant(msg)
        assert self.agent.total_tokens_in == 300
        assert self.agent.total_tokens_out == 150
        assert self.agent.total_tokens == 450


# ── Tests: Result message handling ───────────────────────────────────

class TestResultMessageHandling(unittest.TestCase):
    def test_result_captures_session_id_and_cost(self):
        if not sdk_available():
            self.skipTest("SDK not available")
        from claude_agent_sdk import ResultMessage
        collector = EventCollector()
        agent = SdkAgent(
            callsign="VIPER-1", model="sonnet", cwd="/tmp/test",
            directive="test", on_event=collector.on_event,
            on_exit=collector.on_exit,
        )
        msg = ResultMessage(
            subtype="result", duration_ms=5000, duration_api_ms=4000,
            is_error=False, num_turns=3, session_id="sess-123",
            stop_reason="end_turn", total_cost_usd=0.05,
            usage={"input_tokens": 5000, "output_tokens": 2000},
            result=None, structured_output=None,
        )
        agent._handle_result(msg)
        assert agent.session_id == "sess-123"
        assert agent.total_cost_usd == 0.05
        assert agent.total_tokens_in == 5000


# ── Tests: SDK availability ──────────────────────────────────────────

class TestSdkAvailability(unittest.TestCase):
    def test_sdk_available_returns_bool(self):
        result = sdk_available()
        assert isinstance(result, bool)


if __name__ == "__main__":
    unittest.main()
