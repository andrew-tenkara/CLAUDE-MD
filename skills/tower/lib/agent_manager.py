"""
agent_manager.py — Stream-JSON agent spawning, stdin/stdout pipe management.

Spawns Claude agents via:
    claude --input-format stream-json --output-format stream-json \
        --model <model> --worktree --allowedTools '...' -p "<directive>"

Owns the subprocess stdin/stdout pipes. Parses stream-json events into
a per-agent conversation buffer. Supports injecting user messages mid-flight.

Stream-JSON protocol (Claude → TUI on stdout):
    Each line is a JSON object with a "type" field:
    - {"type": "system", "subtype": "init", ...} — session started
    - {"type": "assistant", "message": {...}} — assistant turn (may contain tool_use)
    - {"type": "user", "message": {...}} — echoed user turn
    - {"type": "result", ...} — final result when agent exits
    - {"type": "error", ...} — error

Stream-JSON protocol (TUI → Claude on stdin):
    {"type": "user", "message": {"role": "user", "content": "..."}}
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


# ── Stream-JSON event types ──────────────────────────────────────────

@dataclass
class StreamEvent:
    """A parsed event from the stream-json stdout."""
    type: str               # system | assistant | user | result | error
    subtype: str = ""       # init, ping, etc. for system events
    raw: dict = field(default_factory=dict)
    text: str = ""          # Extracted text content (convenience)
    tool_uses: list = field(default_factory=list)    # tool_use blocks
    tool_results: list = field(default_factory=list)  # tool_result blocks
    usage: dict = field(default_factory=dict)
    timestamp: float = 0.0


@dataclass
class ConversationEntry:
    """A single entry in an agent's conversation history."""
    role: str           # "assistant" | "user" | "system" | "tool" | "permission"
    content: str        # Text content or summary
    raw: dict = field(default_factory=dict)
    timestamp: float = 0.0
    tool_name: str = ""  # For tool_use entries
    tool_input: dict = field(default_factory=dict)  # For rich rendering of edits
    is_error: bool = False


@dataclass
class SubagentInfo:
    """Tracks an active subagent spawned by the main agent."""
    agent_id: str
    description: str
    status: str = "RUNNING"  # RUNNING | COMPLETE | ERROR
    started_at: float = 0.0
    duration: float = 0.0


# ── Agent process wrapper ────────────────────────────────────────────

class AgentProcess:
    """Manages a single Claude agent subprocess via stream-json protocol."""

    def __init__(
        self,
        callsign: str,
        model: str,
        directive: str,
        project_dir: str,
        personality_prompt: str = "",
        allowed_tools: Optional[list[str]] = None,
        on_event: Optional[Callable[[str, StreamEvent], None]] = None,
        on_exit: Optional[Callable[[str, int], None]] = None,
        use_worktree: bool = True,
        cwd_override: str = "",
        permission_mode: str = "",
        auto_approve_permissions: bool = False,
    ) -> None:
        self.callsign = callsign
        self.model = model
        self.directive = directive
        self.project_dir = project_dir
        self.personality_prompt = personality_prompt
        self.allowed_tools = allowed_tools
        self.on_event = on_event  # callback(callsign, event)
        self.on_exit = on_exit    # callback(callsign, return_code)
        self.use_worktree = use_worktree
        self.cwd_override = cwd_override  # For resuming in an existing worktree
        self.permission_mode = permission_mode
        self.auto_approve_permissions = auto_approve_permissions

        self.process: Optional[subprocess.Popen] = None
        self.conversation: list[ConversationEntry] = []

        # Comm directory for chat relay (iTerm2 pane communication)
        self.comm_dir = Path(f"/tmp/uss-tenkara/{callsign}")
        self.comm_dir.mkdir(parents=True, exist_ok=True)
        self._events_file = self.comm_dir / "events.jsonl"
        self._input_file = self.comm_dir / "input.jsonl"
        # Clear previous session's files
        self._events_file.write_text("")
        self._input_file.write_text("")
        self._input_reader_thread: Optional[threading.Thread] = None
        self.subagents: list[SubagentInfo] = []
        self._reader_thread: Optional[threading.Thread] = None
        self._alive = False
        self._lock = threading.Lock()

        # Telemetry
        self.tokens_in: int = 0
        self.tokens_out: int = 0
        self.cache_read: int = 0
        self.cache_write: int = 0
        self.tool_calls: int = 0
        self.error_count: int = 0
        self.last_tool_at: float = 0.0
        self.launched_at: float = 0.0
        self.session_id: str = ""
        self.worktree_path: str = ""

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out + self.cache_read + self.cache_write

    @property
    def fuel_pct(self) -> int:
        """Estimate context remaining. Claude's context is ~200k tokens."""
        ctx_window = 200_000
        used_pct = min(100, int(self.tokens_in / ctx_window * 100)) if self.tokens_in > 0 else 0
        return max(0, 100 - used_pct)

    @property
    def is_alive(self) -> bool:
        if self.process is None:
            return False
        return self.process.poll() is None

    def spawn(self) -> None:
        """Launch the Claude subprocess."""
        full_prompt = self.directive
        if self.personality_prompt:
            full_prompt = f"{self.personality_prompt}\n\n---\n\n{self.directive}"

        cmd = [
            "claude",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--model", self.model,
            "--verbose",
            "--permission-prompt-tool", "stdio",
            "-p", full_prompt,
        ]

        if self.permission_mode:
            cmd.extend(["--permission-mode", self.permission_mode])

        if self.use_worktree:
            cmd.append("--worktree")

        if self.allowed_tools:
            cmd.extend(["--allowedTools", json.dumps(self.allowed_tools)])

        # Use cwd_override for resuming in an existing worktree
        spawn_cwd = self.cwd_override or self.project_dir
        if self.cwd_override:
            self.worktree_path = self.cwd_override

        env = os.environ.copy()
        env["CLAUDE_PROJECT_DIR"] = self.project_dir

        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=spawn_cwd,
            env=env,
            text=True,
            bufsize=1,  # Line-buffered
            start_new_session=True,  # Own process group so wave-off kills children too
        )
        self.launched_at = time.time()
        self._alive = True

        # Start stdout reader thread
        self._reader_thread = threading.Thread(
            target=self._read_stdout,
            daemon=True,
            name=f"reader-{self.callsign}",
        )
        self._reader_thread.start()

        # Start stderr reader (for logging only)
        threading.Thread(
            target=self._read_stderr,
            daemon=True,
            name=f"stderr-{self.callsign}",
        ).start()

        # Start input file watcher (reads from chat relay's input.jsonl)
        self._input_reader_thread = threading.Thread(
            target=self._read_input_file,
            daemon=True,
            name=f"input-{self.callsign}",
        )
        self._input_reader_thread.start()

        log.info(f"[{self.callsign}] Spawned PID {self.process.pid}: {self.model}")

    def inject_message(self, text: str) -> bool:
        """Send a user message to the agent via stdin (stream-json protocol)."""
        if not self.is_alive or self.process.stdin is None:
            return False

        message = {
            "type": "user",
            "message": {
                "role": "user",
                "content": text,
            },
        }

        try:
            line = json.dumps(message) + "\n"
            self.process.stdin.write(line)
            self.process.stdin.flush()

            # Record in conversation
            with self._lock:
                self.conversation.append(ConversationEntry(
                    role="user",
                    content=text,
                    raw=message,
                    timestamp=time.time(),
                ))

            log.info(f"[{self.callsign}] Injected user message: {text[:80]}...")
            return True
        except (BrokenPipeError, OSError) as e:
            log.warning(f"[{self.callsign}] Failed to inject message: {e}")
            return False

    def recall(self) -> None:
        """Graceful shutdown — send a wrap-up message then let agent finish."""
        self.inject_message(
            "CIC RECALL: Complete your current task, commit your work, "
            "and prepare for recovery. Do not start new work."
        )

    def wave_off(self) -> None:
        """Hard kill — SIGTERM the entire process group (agent + child servers)."""
        if self.process and self.is_alive:
            log.warning(f"[{self.callsign}] WAVE OFF — sending SIGTERM to process group")
            try:
                pgid = os.getpgid(self.process.pid)
                os.killpg(pgid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                # Fallback to just the process if pgid lookup fails
                try:
                    self.process.terminate()
                except OSError:
                    pass

    def kill(self) -> None:
        """Force kill — SIGKILL the entire process group."""
        if self.process and self.is_alive:
            log.warning(f"[{self.callsign}] KILL — sending SIGKILL to process group")
            try:
                pgid = os.getpgid(self.process.pid)
                os.killpg(pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                try:
                    self.process.kill()
                except OSError:
                    pass

    def _read_stdout(self) -> None:
        """Read stream-json events from stdout in a background thread."""
        assert self.process and self.process.stdout
        try:
            for line in self.process.stdout:
                line = line.strip()
                if not line:
                    continue
                event = self._parse_event(line)
                if event:
                    self._process_event(event)
                    if self.on_event:
                        try:
                            self.on_event(self.callsign, event)
                        except Exception as e:
                            log.error(f"[{self.callsign}] Event callback error: {e}")
        except Exception as e:
            log.error(f"[{self.callsign}] Stdout reader error: {e}")
        finally:
            self._alive = False
            rc = self.process.wait() if self.process else -1
            self._tee_exit(rc)
            log.info(f"[{self.callsign}] Process exited with code {rc}")
            if self.on_exit:
                try:
                    self.on_exit(self.callsign, rc)
                except Exception:
                    pass

    def _read_stderr(self) -> None:
        """Read stderr for logging (non-critical)."""
        assert self.process and self.process.stderr
        try:
            for line in self.process.stderr:
                line = line.strip()
                if not line:
                    continue
                # Look for worktree path in early stderr output
                if "worktree" in line.lower() and "/" in line:
                    # Try to extract path
                    for part in line.split():
                        if part.startswith("/"):
                            self.worktree_path = part.rstrip(".,;:")
                            break
                log.debug(f"[{self.callsign}] stderr: {line[:200]}")
        except Exception:
            pass

    def _tee_event(self, event: StreamEvent) -> None:
        """Write event to events.jsonl for the chat relay to pick up."""
        try:
            relay_event: dict = {"type": event.type, "timestamp": event.timestamp}

            if event.type == "assistant":
                relay_event["text"] = event.text
                relay_event["tool_uses"] = [
                    {"name": tu.get("name", "?"), "input": tu.get("input", {})}
                    for tu in event.tool_uses
                ]
            elif event.type == "control_request":
                request = event.raw.get("request", {})
                relay_event["type"] = "permission"
                relay_event["tool_name"] = request.get("tool_name", "?")
                relay_event["tool_input"] = request.get("input", {})
                relay_event["reason"] = request.get("decision_reason", "")
                relay_event["request_id"] = event.raw.get("request_id", "")
                relay_event["tool_use_id"] = request.get("tool_use_id", "")
            elif event.type == "user":
                relay_event["text"] = event.text
            elif event.type == "result":
                relay_event["text"] = event.text
            elif event.type == "error":
                relay_event["text"] = event.text
            else:
                return  # Skip system/init events

            with open(self._events_file, "a") as f:
                f.write(json.dumps(relay_event) + "\n")
        except Exception as e:
            log.debug(f"[{self.callsign}] Failed to tee event: {e}")

    def _tee_exit(self, return_code: int) -> None:
        """Write exit event to events.jsonl."""
        try:
            with open(self._events_file, "a") as f:
                f.write(json.dumps({
                    "type": "exit",
                    "return_code": return_code,
                    "timestamp": time.time(),
                }) + "\n")
        except Exception:
            pass

    def _read_input_file(self) -> None:
        """Watch input.jsonl for messages from the chat relay."""
        file_pos = 0
        while self.is_alive:
            try:
                if self._input_file.exists():
                    with open(self._input_file, "r") as f:
                        f.seek(file_pos)
                        new_lines = f.readlines()
                        file_pos = f.tell()

                    for line in new_lines:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            msg_type = data.get("type", "")

                            if msg_type == "user_message":
                                text = data.get("text", "").strip()
                                if text:
                                    self.inject_message(text)

                            elif msg_type == "permission_response":
                                # Find the pending permission and respond
                                allowed = data.get("response", False)
                                # Get from the last permission event in conversation
                                for entry in reversed(self.conversation):
                                    if entry.role == "permission":
                                        req = entry.raw
                                        request_id = req.get("request_id", "")
                                        tool_use_id = req.get("request", {}).get("tool_use_id", "")
                                        if request_id and tool_use_id:
                                            self._send_permission_response(
                                                request_id, tool_use_id, allowed
                                            )
                                        break
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                log.debug(f"[{self.callsign}] Input reader error: {e}")

            time.sleep(0.3)  # Poll interval

    def _parse_event(self, raw_line: str) -> Optional[StreamEvent]:
        """Parse a single stream-json line into a StreamEvent."""
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError:
            log.debug(f"[{self.callsign}] Non-JSON line: {raw_line[:100]}")
            return None

        event = StreamEvent(
            type=obj.get("type", "unknown"),
            subtype=obj.get("subtype", ""),
            raw=obj,
            timestamp=time.time(),
        )

        # Extract session ID from init
        if event.type == "system" and event.subtype == "init":
            self.session_id = obj.get("sessionId", "")
            return event

        # Extract content from assistant messages
        if event.type == "assistant":
            message = obj.get("message", {})
            content = message.get("content", [])
            usage = message.get("usage", {})
            event.usage = usage

            if isinstance(content, list):
                texts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            texts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            event.tool_uses.append(block)
                        elif block.get("type") == "tool_result":
                            event.tool_results.append(block)
                event.text = "\n".join(texts).strip()
            elif isinstance(content, str):
                event.text = content

        # Extract content from user messages (echoed back)
        if event.type == "user":
            message = obj.get("message", {})
            content = message.get("content", [])
            if isinstance(content, list):
                texts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            texts.append(block.get("text", ""))
                        elif block.get("type") == "tool_result":
                            event.tool_results.append(block)
                event.text = "\n".join(texts).strip()
            elif isinstance(content, str):
                event.text = content

        # Result type
        if event.type == "result":
            event.text = obj.get("result", "")

        # Permission / control request
        if event.type == "control_request":
            request = obj.get("request", {})
            event.subtype = request.get("subtype", "")
            # Store the full request for permission handling
            event.text = (
                f"Permission requested: {request.get('tool_name', '?')} — "
                f"{request.get('decision_reason', '')}"
            )

        return event

    def _process_event(self, event: StreamEvent) -> None:
        """Update internal state from a parsed event."""
        # Tee to events.jsonl for chat relay
        self._tee_event(event)

        with self._lock:
            # Update token counts
            if event.usage:
                self.tokens_in += _safe_int(event.usage.get("input_tokens"))
                self.tokens_out += _safe_int(event.usage.get("output_tokens"))
                self.cache_read += _safe_int(event.usage.get("cache_read_input_tokens"))
                self.cache_write += _safe_int(event.usage.get("cache_creation_input_tokens"))

            # Track tool calls
            for tool_use in event.tool_uses:
                self.tool_calls += 1
                self.last_tool_at = time.time()
                tool_name = tool_use.get("name", "unknown")

                # Track subagent spawns
                if tool_name == "Agent":
                    tool_input = tool_use.get("input", {})
                    desc = tool_input.get("description", "subagent")
                    agent_id = tool_use.get("id", "")
                    self.subagents.append(SubagentInfo(
                        agent_id=agent_id,
                        description=desc,
                        started_at=time.time(),
                    ))

            # Track tool result errors
            for result in event.tool_results:
                if result.get("is_error"):
                    self.error_count += 1
                # Track subagent completions
                tool_use_id = result.get("tool_use_id", "")
                for sa in self.subagents:
                    if sa.agent_id == tool_use_id and sa.status == "RUNNING":
                        sa.status = "COMPLETE"
                        sa.duration = time.time() - sa.started_at
                        break

            # Add to conversation history
            if event.type == "assistant" and (event.text or event.tool_uses):
                # Add text content if present
                if event.text:
                    self.conversation.append(ConversationEntry(
                        role="assistant",
                        content=event.text,
                        raw=event.raw,
                        timestamp=event.timestamp,
                    ))

                # Add individual tool call entries with rich detail
                for tool_use in event.tool_uses:
                    tool_name = tool_use.get("name", "unknown")
                    tool_input = tool_use.get("input", {})
                    summary = _summarize_tool_call(tool_name, tool_input)
                    self.conversation.append(ConversationEntry(
                        role="tool",
                        content=summary,
                        raw=tool_use,
                        timestamp=event.timestamp,
                        tool_name=tool_name,
                        tool_input=tool_input,
                    ))

            elif event.type == "user" and event.text:
                # Don't double-record injected messages (already added in inject_message)
                pass

            elif event.type == "result":
                self.conversation.append(ConversationEntry(
                    role="system",
                    content=f"[RESULT] {event.text[:200]}",
                    raw=event.raw,
                    timestamp=event.timestamp,
                ))

            elif event.type == "control_request":
                request = event.raw.get("request", {})
                request_id = event.raw.get("request_id", "")
                tool_name = request.get("tool_name", "?")
                tool_input = request.get("input", {})
                tool_use_id = request.get("tool_use_id", "")
                reason = request.get("decision_reason", "")

                self.conversation.append(ConversationEntry(
                    role="permission",
                    content=f"⚡ Permission: {tool_name} — {reason}",
                    raw=event.raw,
                    timestamp=event.timestamp,
                    tool_name=tool_name,
                    tool_input=tool_input,
                ))

                # Auto-approve if configured
                if self.auto_approve_permissions:
                    self._send_permission_response(request_id, tool_use_id, allowed=True)
                    log.info(f"[{self.callsign}] Auto-approved {tool_name}")

    def respond_permission(self, request_id: str, tool_use_id: str, allowed: bool) -> bool:
        """Respond to a permission request from the agent (public API)."""
        return self._send_permission_response(request_id, tool_use_id, allowed)

    def _send_permission_response(self, request_id: str, tool_use_id: str, allowed: bool) -> bool:
        """Send a control_response for a permission request."""
        if not self.is_alive or self.process.stdin is None:
            return False

        response = {
            "type": "control_response",
            "request_id": request_id,
            "permission_response": {
                "tool_use_id": tool_use_id,
                "allowed": allowed,
            },
        }

        try:
            line = json.dumps(response) + "\n"
            self.process.stdin.write(line)
            self.process.stdin.flush()
            log.info(f"[{self.callsign}] Permission {'granted' if allowed else 'denied'} for {tool_use_id} (req {request_id})")
            return True
        except (BrokenPipeError, OSError) as e:
            log.warning(f"[{self.callsign}] Failed to respond to permission: {e}")
            return False

    @property
    def active_subagents(self) -> list[SubagentInfo]:
        return [sa for sa in self.subagents if sa.status == "RUNNING"]

    def get_recent_radio(self, n: int = 5) -> list[str]:
        """Get recent assistant text messages for radio chatter display."""
        messages = []
        for entry in reversed(self.conversation):
            if entry.role == "assistant" and entry.content:
                first_line = entry.content.split("\n")[0].strip()
                if first_line and len(first_line) > 5:
                    messages.append(first_line[:120])
                    if len(messages) >= n:
                        break
        messages.reverse()
        return messages


def _summarize_tool_call(tool_name: str, tool_input: dict) -> str:
    """Create a human-readable summary of a tool call, similar to Claude Code's display."""
    if tool_name == "Edit":
        fp = tool_input.get("file_path", "?")
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        short_fp = fp.split("/")[-1] if "/" in fp else fp
        old_lines = len(old.split("\n")) if old else 0
        new_lines = len(new.split("\n")) if new else 0
        return f"{short_fp} — edit ({old_lines}→{new_lines} lines)"

    if tool_name == "Write":
        fp = tool_input.get("file_path", "?")
        content = tool_input.get("content", "")
        short_fp = fp.split("/")[-1] if "/" in fp else fp
        lines = len(content.split("\n")) if content else 0
        return f"{short_fp} — write ({lines} lines)"

    if tool_name == "Read":
        fp = tool_input.get("file_path", "?")
        short_fp = fp.split("/")[-1] if "/" in fp else fp
        offset = tool_input.get("offset")
        limit = tool_input.get("limit")
        suffix = ""
        if offset or limit:
            suffix = f" (L{offset or 1}"
            if limit:
                suffix += f"-{(offset or 1) + limit}"
            suffix += ")"
        return f"{short_fp}{suffix}"

    if tool_name == "Bash":
        cmd = tool_input.get("command", "?")
        return cmd[:120]

    if tool_name == "Grep":
        pattern = tool_input.get("pattern", "?")
        path = tool_input.get("path", ".")
        short_path = path.split("/")[-1] if "/" in path else path
        return f'grep "{pattern}" in {short_path}'

    if tool_name == "Glob":
        pattern = tool_input.get("pattern", "?")
        return f'glob "{pattern}"'

    if tool_name == "Agent":
        desc = tool_input.get("description", "subagent")
        subtype = tool_input.get("subagent_type", "")
        return f"spawn {subtype or 'agent'}: {desc}"

    # Generic fallback
    keys = list(tool_input.keys())[:3]
    return ", ".join(f"{k}={str(tool_input[k])[:30]}" for k in keys) if keys else ""


def _safe_int(v: object) -> int:
    try:
        if v is None:
            return 0
        import math
        n = float(v)
        return int(n) if math.isfinite(n) else 0
    except (TypeError, ValueError):
        return 0


# ── Agent Manager — orchestrates multiple agents ────────────────────

class AgentManager:
    """Manages all running agent processes."""

    def __init__(
        self,
        project_dir: str,
        on_event: Optional[Callable[[str, StreamEvent], None]] = None,
        on_exit: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        self.project_dir = project_dir
        self.on_event = on_event
        self.on_exit = on_exit
        self._agents: dict[str, AgentProcess] = {}
        self._lock = threading.Lock()

    def spawn(
        self,
        callsign: str,
        model: str,
        directive: str,
        personality_prompt: str = "",
        allowed_tools: Optional[list[str]] = None,
        use_worktree: bool = True,
        event_callback: Optional[Callable[[str, "StreamEvent"], None]] = None,
        permission_mode: str = "",
        auto_approve_permissions: bool = False,
    ) -> AgentProcess:
        """Spawn a new agent and register it.

        Args:
            event_callback: Optional override for event routing.
                            If provided, this callback is used instead of the
                            manager's default on_event handler.
            permission_mode: CLI permission mode (acceptEdits, bypassPermissions, etc.)
            auto_approve_permissions: Auto-approve all control_request permission prompts.
        """
        agent = AgentProcess(
            callsign=callsign,
            model=model,
            directive=directive,
            project_dir=self.project_dir,
            personality_prompt=personality_prompt,
            allowed_tools=allowed_tools,
            on_event=event_callback or self._on_agent_event,
            on_exit=self._on_agent_exit,
            use_worktree=use_worktree,
            permission_mode=permission_mode,
            auto_approve_permissions=auto_approve_permissions,
        )
        agent.spawn()
        with self._lock:
            self._agents[callsign] = agent
        return agent

    def resume(
        self,
        callsign: str,
        model: str,
        worktree_path: str,
        directive: str = "",
        personality_prompt: str = "",
    ) -> AgentProcess:
        """Resume work in an existing worktree (no --worktree flag)."""
        if not directive:
            directive = (
                "You are resuming work in an existing worktree. "
                "Check git status, review recent changes, and continue "
                "where the previous agent left off."
            )
        agent = AgentProcess(
            callsign=callsign,
            model=model,
            directive=directive,
            project_dir=self.project_dir,
            personality_prompt=personality_prompt,
            on_event=self._on_agent_event,
            on_exit=self._on_agent_exit,
            use_worktree=False,  # Don't create a new worktree
            cwd_override=worktree_path,
        )
        agent.spawn()
        with self._lock:
            self._agents[callsign] = agent
        return agent

    def get(self, callsign: str) -> Optional[AgentProcess]:
        with self._lock:
            return self._agents.get(callsign)

    def all_agents(self) -> list[AgentProcess]:
        with self._lock:
            return list(self._agents.values())

    def active_agents(self) -> list[AgentProcess]:
        with self._lock:
            return [a for a in self._agents.values() if a.is_alive]

    def inject_message(self, callsign: str, text: str) -> bool:
        agent = self.get(callsign)
        if agent:
            return agent.inject_message(text)
        return False

    def recall(self, callsign: str) -> bool:
        agent = self.get(callsign)
        if agent:
            agent.recall()
            return True
        return False

    def wave_off(self, callsign: str) -> bool:
        agent = self.get(callsign)
        if agent:
            agent.wave_off()
            return True
        return False

    def recall_all(self) -> None:
        for agent in self.active_agents():
            agent.recall()

    def wave_off_all(self) -> None:
        for agent in self.active_agents():
            agent.wave_off()

    def remove(self, callsign: str) -> None:
        with self._lock:
            agent = self._agents.pop(callsign, None)
        if agent and agent.is_alive:
            agent.wave_off()

    def _on_agent_event(self, callsign: str, event: StreamEvent) -> None:
        if self.on_event:
            try:
                self.on_event(callsign, event)
            except Exception as e:
                log.error(f"AgentManager event callback error: {e}")

    def _on_agent_exit(self, callsign: str, return_code: int) -> None:
        log.info(f"Agent {callsign} exited with code {return_code}")
        if self.on_exit:
            try:
                self.on_exit(callsign, return_code)
            except Exception as e:
                log.error(f"AgentManager exit callback error: {e}")

    def shutdown(self) -> None:
        """Gracefully shutdown all agents."""
        for agent in self.active_agents():
            agent.wave_off()
        # Give them a moment
        deadline = time.time() + 5
        while time.time() < deadline:
            if not self.active_agents():
                break
            time.sleep(0.2)
        # Force kill stragglers
        for agent in self.active_agents():
            agent.kill()
