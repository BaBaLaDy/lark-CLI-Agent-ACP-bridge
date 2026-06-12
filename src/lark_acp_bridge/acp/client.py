"""ACP client wrapper built on the official agent-client-protocol SDK."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog
from acp import PROTOCOL_VERSION, Client, RequestError, spawn_agent_process, text_block
from acp.schema import (
    AgentMessageChunk,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AudioContentBlock,
    AvailableCommandsUpdate,
    ClientCapabilities,
    ConfigOptionUpdate,
    CreateTerminalResponse,
    CurrentModeUpdate,
    EmbeddedResourceContentBlock,
    EnvVariable,
    ImageContentBlock,
    Implementation,
    KillTerminalResponse,
    PermissionOption,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    ResourceContentBlock,
    SessionInfoUpdate,
    TerminalOutputResponse,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UsageUpdate,
    UserMessageChunk,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)

logger = structlog.get_logger()

SessionStatus = Literal["running", "done", "cancelled", "error", "timeout"]


@dataclass
class SessionState:
    """Accumulates streaming updates for one prompt turn.

    Supports both text delta listeners (on_text) and full-state listeners
    (on_state_change) so callers can react to every meaningful change —
    useful for streaming card updates in the Feishu bot.
    """

    text_chunks: list[str] = field(default_factory=list)
    thinking_chunks: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    status: SessionStatus = "running"
    _text_listeners: list[Callable[[str], None]] = field(default_factory=list)
    _state_listeners: list[Callable[["SessionState"], None]] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "".join(self.text_chunks)

    def on_text(self, listener: Callable[[str], None]) -> None:
        """Register a listener called with each new text delta."""
        self._text_listeners.append(listener)

    def on_state_change(self, listener: Callable[["SessionState"], None]) -> None:
        """Register a listener called whenever the state meaningfully changes."""
        self._state_listeners.append(listener)

    def emit_text(self, delta: str) -> None:
        self.text_chunks.append(delta)
        for listener in self._text_listeners:
            listener(delta)
        self._notify()

    def _notify(self) -> None:
        for listener in self._state_listeners:
            try:
                listener(self)
            except Exception as exc:
                logger.warning("state-listener-failed", error=str(exc), exc_info=True)


class BridgeClient(Client):
    """ACP client callbacks invoked by the agent process.

    State is tracked per session_id in ``_states`` so that concurrent
    prompts on different sessions (e.g. two chats routed to the same
    agent subprocess) do not overwrite each other.
    """

    def __init__(self) -> None:
        self._states: dict[str, SessionState] = {}

    def begin_turn(self, session_id: str) -> SessionState:
        """Create and register a new SessionState for the given session."""
        state = SessionState()
        self._states[session_id] = state
        return state

    def end_turn(self, session_id: str) -> None:
        """Remove the SessionState for a completed prompt turn."""
        self._states.pop(session_id, None)

    async def session_update(
        self,
        session_id: str,
        update: UserMessageChunk
        | AgentMessageChunk
        | AgentThoughtChunk
        | ToolCallStart
        | ToolCallProgress
        | AgentPlanUpdate
        | AvailableCommandsUpdate
        | CurrentModeUpdate
        | ConfigOptionUpdate
        | SessionInfoUpdate
        | UsageUpdate,
        **kwargs: Any,
    ) -> None:
        state = self._states.get(session_id)
        if state is None:
            return

        if isinstance(update, AgentMessageChunk):
            content = update.content
            if isinstance(content, TextContentBlock):
                state.emit_text(content.text)
            elif isinstance(content, ImageContentBlock):
                state.emit_text("[image]")
            elif isinstance(content, AudioContentBlock):
                state.emit_text("[audio]")
            elif isinstance(content, ResourceContentBlock):
                state.emit_text(f"[resource: {content.name}]")
            elif isinstance(content, EmbeddedResourceContentBlock):
                state.emit_text("[embedded resource]")
            return

        if isinstance(update, AgentThoughtChunk):
            content = update.content
            if isinstance(content, TextContentBlock):
                state.thinking_chunks.append(content.text)
                state._notify()
            return

        if isinstance(update, ToolCallStart):
            state.tool_calls.append(
                {
                    "id": update.tool_call_id,
                    "title": update.title,
                    "kind": update.kind,
                    "status": getattr(update, "status", "running"),
                }
            )
            state._notify()
            return

        if isinstance(update, ToolCallProgress):
            # Update existing tool_call by id with progress info.
            for tc in state.tool_calls:
                if tc.get("id") == update.tool_call_id:
                    tc["status"] = getattr(update, "status", tc.get("status", "running"))
                    if getattr(update, "title", None):
                        tc["title"] = update.title
                    break
            state._notify()
            return

        if isinstance(update, ToolCallUpdate):
            # ToolCallUpdate carries result/final status for a tool call.
            for tc in state.tool_calls:
                if tc.get("id") == update.tool_call_id:
                    tc["status"] = getattr(update, "status", "done")
                    if getattr(update, "title", None):
                        tc["title"] = update.title
                    break
            state._notify()
            return

        if isinstance(update, UsageUpdate):
            state.input_tokens = getattr(update, "input_tokens", 0) or 0
            state.output_tokens = getattr(update, "output_tokens", 0) or 0

    async def request_permission(
        self,
        options: list[PermissionOption],
        session_id: str,
        tool_call: ToolCallUpdate,
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        selected = options[0].option_id if options else "allow"
        return RequestPermissionResponse(outcome={"outcome": "selected", "option_id": selected})

    async def write_text_file(
        self, content: str, path: str, session_id: str, **kwargs: Any
    ) -> WriteTextFileResponse | None:
        raise RequestError.method_not_found("fs/write_text_file")

    async def read_text_file(
        self,
        path: str,
        session_id: str,
        limit: int | None = None,
        line: int | None = None,
        **kwargs: Any,
    ) -> ReadTextFileResponse:
        raise RequestError.method_not_found("fs/read_text_file")

    async def create_terminal(
        self,
        command: str,
        session_id: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: list[EnvVariable] | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> CreateTerminalResponse:
        raise RequestError.method_not_found("terminal/create")

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: Any) -> TerminalOutputResponse:
        raise RequestError.method_not_found("terminal/output")

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> ReleaseTerminalResponse | None:
        raise RequestError.method_not_found("terminal/release")

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> WaitForTerminalExitResponse:
        raise RequestError.method_not_found("terminal/wait_for_exit")

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> KillTerminalResponse | None:
        raise RequestError.method_not_found("terminal/kill")

    async def ext_method(self, method: str, params: dict) -> dict:
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict) -> None:
        return None


class ACPClient:
    """High-level lifecycle wrapper around SDK ClientSideConnection."""

    def __init__(self, command: list[str], cwd: str | None = None, env: dict[str, str] | None = None):
        self.command = command
        self.cwd = cwd or os.getcwd()
        self.env = env or {}
        self._bridge_client = BridgeClient()
        self._context_manager: Any = None
        self._conn: Any = None
        self._process: Any = None
        self._session_id: str | None = None

    async def start(self) -> None:
        merged_env = dict(os.environ)
        merged_env.update(self.env)
        logger.info("starting-acp-agent", command=self.command, cwd=self.cwd)

        self._context_manager = spawn_agent_process(
            self._bridge_client,
            *self.command,
            env=merged_env,
            cwd=self.cwd,
        )
        self._conn, self._process = await self._context_manager.__aenter__()
        await self._conn.initialize(
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(),
            client_info=Implementation(name="lark-acp-bridge", title="Lark ACP Bridge", version="0.1.0"),
        )

    async def new_session(self, cwd: str | None = None) -> str:
        if self._conn is None:
            raise RuntimeError("ACP client is not started")
        result = await self._conn.new_session(mcp_servers=[], cwd=cwd or self.cwd)
        self._session_id = result.session_id
        return self._session_id

    async def load_session(self, session_id: str, cwd: str | None = None) -> str:
        if self._conn is None:
            raise RuntimeError("ACP client is not started")
        # LoadSessionResponse does not carry session_id — it's the one we passed in.
        await self._conn.load_session(mcp_servers=[], cwd=cwd or self.cwd, session_id=session_id)
        self._session_id = session_id
        return self._session_id

    async def prompt(
        self,
        message: str,
        session_id: str | None = None,
        on_text: Callable[[str], None] | None = None,
        on_state_change: Callable[["SessionState"], None] | None = None,
    ) -> SessionState:
        if self._conn is None:
            raise RuntimeError("ACP client is not started")
        active_session_id = session_id or self._session_id
        if not active_session_id:
            raise RuntimeError("No active ACP session")

        state = self._bridge_client.begin_turn(active_session_id)
        if on_text:
            state.on_text(on_text)
        if on_state_change:
            state.on_state_change(on_state_change)

        try:
            await self._conn.prompt(session_id=active_session_id, prompt=[text_block(message)])
        finally:
            self._bridge_client.end_turn(active_session_id)
        return state

    async def cancel(self, session_id: str | None = None) -> None:
        if self._conn is None:
            return
        active_session_id = session_id or self._session_id
        if active_session_id:
            await self._conn.cancel(session_id=active_session_id)

    async def close_session(self, session_id: str | None = None) -> None:
        if self._conn is None:
            return
        active_session_id = session_id or self._session_id
        if active_session_id:
            await self._conn.close_session(session_id=active_session_id)

    async def stop(self) -> None:
        if self._context_manager is not None:
            await self._context_manager.__aexit__(None, None, None)
            if os.name == "nt":
                # Give ProactorEventLoop a tick to finalize subprocess pipes
                # before asyncio.run() closes the loop on Windows.
                await asyncio.sleep(0.25)
        self._context_manager = None
        self._conn = None
        self._process = None
        self._bridge_client._states.clear()

    @property
    def process(self) -> Any:
        """Return the underlying subprocess, or None if not running."""
        return self._process

    async def __aenter__(self) -> "ACPClient":
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.stop()
