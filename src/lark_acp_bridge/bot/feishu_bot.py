"""Feishu bot that forwards messages to ACP Agent with streaming card updates.

Key features over the previous version:
- **Message deduplication**: prevents double-processing on WebSocket reconnect
- **Per-user concurrency guard**: rejects new messages while a run is active
- **Throttled streaming card updates**: card patches at ~400ms intervals
- **Idle timeout watchdog**: auto-cancels agent if no output in N seconds
- **`/stop` command**: cancels the active run via asyncio task cancellation
- **`/status` command**: shows bridge and active-run information
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import lark_oapi as lark
import structlog
from aiohttp import web
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.api.im.v1 import (
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    DeleteMessageReactionRequest,
    Emoji,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from ..acp.client import SessionState
from ..acp.codex_bridge import CodexACPBridge
from ..card import (
    agent_list_card,
    agent_switched_card,
    help_card,
    render_error_card,
    render_result_card,
    render_running_card,
    render_streaming_card,
    resume_card,
    session_reset_card,
    simple_text_card,
    status_card,
    workspaces_card,
)
from ..config.settings import Settings

# Optional import — available only after multi-agent-switching implementation
try:
    from ..acp.agent_manager import AgentManager
    from ..config.workspace_store import WorkspaceStore
    from ..config.session_store import SessionStore
except ImportError:
    AgentManager = None  # type: ignore[assignment,misc]
    WorkspaceStore = None  # type: ignore[assignment,misc]
    SessionStore = None  # type: ignore[assignment,misc]

logger = structlog.get_logger()

# WebSocket reconnects can replay events within a few seconds; 60s is generous.
_DEDUP_TTL_SECONDS = 60.0


def _build_blocked_paths() -> set[Path]:
    """Return a set of paths that ``/cd`` must reject as unsafe."""
    blocked = {
        Path("/"), Path("/etc"), Path("/bin"), Path("/usr"), Path("/sbin"),
        Path("/sys"), Path("/proc"), Path("/boot"), Path("/dev"),
    }
    try:
        blocked.add(Path.home())
    except Exception:
        pass
    if sys.platform == "win32":
        blocked.update({
            Path("C:\\"), Path("C:\\Windows"), Path("C:\\Program Files"),
            Path("C:\\Program Files (x86)"), Path("C:\\Users"),
        })
    return blocked


# Paths under these directories are always blocked (even as sub-directories).
_BLOCKED_PARENTS_UNIX = {
    Path("/etc"), Path("/bin"), Path("/usr"), Path("/sbin"),
    Path("/sys"), Path("/proc"), Path("/boot"), Path("/dev"),
}
_BLOCKED_PATHS = _build_blocked_paths()


def parse_prefix_routing(text: str, known_agents: set[str]) -> tuple[str | None, str]:
    """Parse message text for agent prefix routing: "<name>: <prompt>".

    Returns (agent_name, prompt) where agent_name is None if no prefix matched.
    The prefix format requires a colon followed by a space: ``"claude: do X"``.
    """
    if ": " in text:
        prefix, rest = text.split(": ", 1)
        if prefix.strip() in known_agents:
            return prefix.strip(), rest
    return None, text


# --------------------------------------------------------------------------- #
# Throttled card updater
# --------------------------------------------------------------------------- #

class _ThrottledCardUpdater:
    """Coalesces rapid card updates into patches at most every ``throttle_s``.

    When ``schedule(state)`` is called, it waits ``throttle_s`` seconds before
    sending.  If more ``schedule`` calls arrive during the wait, only the
    *latest* state is sent — earlier pending updates are discarded.
    ``final_flush`` bypasses the throttle and sends immediately.
    """

    def __init__(self, bot: "FeishuBot", card_message_id: str, throttle_s: float, show_tool_calls: bool = True):
        self._bot = bot
        self._card_message_id = card_message_id
        self._throttle_s = throttle_s
        self._show_tool_calls = show_tool_calls
        self._pending_task: asyncio.Task | None = None
        self._latest_state: SessionState | None = None

    def schedule(self, state: SessionState) -> None:
        """Enqueue a throttled card update with the given state."""
        self._latest_state = state
        if self._pending_task is None or self._pending_task.done():
            self._pending_task = asyncio.ensure_future(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        try:
            await asyncio.sleep(self._throttle_s)
        except asyncio.CancelledError:
            return  # final_flush cancelled us; it will send the card itself
        if self._latest_state is not None:
            card = render_streaming_card(self._latest_state, show_tool_calls=self._show_tool_calls)
            await self._bot._update_card(self._card_message_id, card)

    async def final_flush(self, state: SessionState, status: str = "done") -> None:
        """Immediately send the final card, cancelling any pending throttle."""
        if self._pending_task is not None and not self._pending_task.done():
            self._pending_task.cancel()
            try:
                await self._pending_task
            except asyncio.CancelledError:
                pass
            self._pending_task = None
        card = render_result_card(state, status=status, show_tool_calls=self._show_tool_calls)
        await self._bot._update_card(self._card_message_id, card)


# --------------------------------------------------------------------------- #
# Main bot class
# --------------------------------------------------------------------------- #

class FeishuBot:
    """Webhook / WebSocket Feishu message handler with streaming card updates.

    Accepts a ``Settings`` object so all tunables (timeout, throttle,
    concurrency, show_tool_calls) come from config rather than being hardcoded.
    """

    def __init__(
        self,
        settings: Settings,
        codex_bridge: CodexACPBridge | None = None,
        agent_name: str = "",
        agent_manager: "AgentManager | None" = None,
        workspace_store: "WorkspaceStore | None" = None,
        session_store: "SessionStore | None" = None,
    ):
        self._settings = settings
        self.codex_bridge = codex_bridge
        self._agent_manager = agent_manager
        self._workspace_store = workspace_store
        self._session_store = session_store
        self._agent_name = agent_name or (settings.agent_command[0] if settings.agent_command else "codex-acp")
        self.client = (
            lark.Client.builder()
            .app_id(settings.feishu_app_id)
            .app_secret(settings.feishu_app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )
        self.app = web.Application()
        self.app.router.add_post("/webhook/event", self.handle_event)
        self.app.router.add_post("/webhook/card", self._handle_card_action)
        self._loop: asyncio.AbstractEventLoop | None = None
        # message_id → monotonic timestamp; used to drop duplicate replays
        self._seen_message_ids: dict[str, float] = {}
        # user_id → asyncio.Task; tracks in-flight agent runs per user
        self._active_tasks: dict[str, asyncio.Task] = {}
        # Per-chat working directory cwd overrides (set via /cd command)
        # scope_key → cwd_path
        self._cwd_by_scope: dict[str, str] = {}
        # Bot's own open_id, fetched once on first use
        self._bot_open_id: str | None = None
        # Shutdown coordination
        self._shutdown_event = asyncio.Event()
        # Inbound message debounce: user_id → pending flush task / text buffer
        self._debounce_tasks: dict[str, asyncio.Task] = {}
        self._debounce_texts: dict[str, list[tuple[str, str, str, str]]] = {}
        # Each entry: (message_id, text, chat_id, chat_type)

    # ------------------------------------------------------------------ #
    # Bot identity
    # ------------------------------------------------------------------ #

    async def _get_bot_open_id(self) -> str | None:
        """Fetch and cache the bot's own open_id (used to detect @mentions).

        Uses the application/v6 API to get the app info, which includes the bot's open_id.
        """
        if self._bot_open_id is not None:
            return self._bot_open_id
        try:
            from lark_oapi.api.application.v6 import GetApplicationRequest
            request = (
                GetApplicationRequest.builder()
                .app_id(self._settings.feishu_app_id)
                .build()
            )
            response = self.client.application.v6.application.get(request)
            if response.success() and response.data:
                app_info = response.data
                bot = getattr(app_info, "bot", None)
                if bot:
                    self._bot_open_id = getattr(bot, "open_id", None)
                    logger.info("bot-open-id-resolved", open_id=self._bot_open_id)
                    return self._bot_open_id
            logger.warning("bot-open-id-not-found", code=response.code, msg=response.msg)
        except Exception as exc:
            logger.warning("bot-open-id-failed", error=str(exc))
        return None

    # ------------------------------------------------------------------ #
    # Webhook entry point
    # ------------------------------------------------------------------ #

    async def handle_event(self, request: web.Request) -> web.Response:
        data = await request.json()
        if "challenge" in data:
            return web.json_response({"challenge": data["challenge"]})
        if data.get("type") == "event_callback":
            await self._handle_message_event(data.get("event", {}))
        return web.json_response({"code": 0})

    # ------------------------------------------------------------------ #
    # WebSocket mode: thread ↔ asyncio bridge
    # ------------------------------------------------------------------ #

    def _build_event_handler(self) -> lark.EventDispatcherHandler:
        def on_message(event: P2ImMessageReceiveV1) -> None:
            if self._loop is None:
                logger.warning("ws-message-dropped-no-event-loop")
                return
            payload = self._event_to_dict(event)
            future = asyncio.run_coroutine_threadsafe(
                self._handle_message_event(payload), self._loop
            )

            def log_failure(done_future: Any) -> None:
                exc = done_future.exception()
                if exc:
                    logger.error("ws-message-handler-failed", error=str(exc), exc_info=exc)

            future.add_done_callback(log_failure)

        def on_card_action(event: Any) -> Any:
            """Handle card button clicks in WebSocket mode."""
            if self._loop is None:
                return None
            # Extract action value and operator from the card action event
            try:
                action_value = {}
                operator_id = "unknown"
                open_chat_id = ""
                open_message_id = ""

                # lark-oapi card action event structure
                if hasattr(event, "event"):
                    evt = event.event
                    if hasattr(evt, "action") and hasattr(evt.action, "value"):
                        action_value = evt.action.value or {}
                    if hasattr(evt, "operator") and hasattr(evt.operator, "open_id"):
                        operator_id = evt.operator.open_id or "unknown"
                    # context holds open_chat_id and open_message_id (not on evt directly)
                    if hasattr(evt, "context"):
                        ctx = evt.context
                        if hasattr(ctx, "open_chat_id"):
                            open_chat_id = ctx.open_chat_id or ""
                        if hasattr(ctx, "open_message_id"):
                            open_message_id = ctx.open_message_id or ""

                future = asyncio.run_coroutine_threadsafe(
                    self._dispatch_card_action(action_value, operator_id, open_chat_id, open_message_id),
                    self._loop,
                )
                future.result(timeout=10)  # Wait for completion
            except Exception as exc:
                logger.error("card-action-failed", error=str(exc), exc_info=True)

            # Return toast response to give user instant feedback
            return {"toast": {"type": "info", "content": "正在处理…"}}

        handler_builder = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(on_message)
        )

        # Try to register card action handler (may not be available in all SDK versions)
        try:
            handler_builder = handler_builder.register_p2_card_action_trigger(on_card_action)
        except AttributeError:
            logger.warning("card-action-not-supported", msg="lark-oapi SDK does not support card action trigger in WS mode")

        # Register no-op handlers for reaction events to suppress "processor not found" errors.
        # These events are triggered when we call _add_reaction / _remove_reaction APIs —
        # the Lark server echoes them back via WebSocket, but we don't need to process them.
        try:
            handler_builder = (
                handler_builder
                .register_p2_im_message_reaction_created_v1(lambda event: None)
                .register_p2_im_message_reaction_deleted_v1(lambda event: None)
            )
        except AttributeError:
            pass  # SDK version doesn't have these methods; errors will remain but are harmless

        return handler_builder.build()

    @staticmethod
    def _event_to_dict(event: P2ImMessageReceiveV1) -> dict[str, Any]:
        event_data = event.event
        message = event_data.message if event_data else None
        sender = event_data.sender if event_data else None
        sender_id = sender.sender_id if sender else None
        mentions_raw = getattr(message, "mentions", None) if message else None
        mentions: list[dict[str, Any]] = []
        if mentions_raw:
            for m in mentions_raw:
                mid = getattr(m, "id", None)
                mentions.append({
                    "open_id": getattr(mid, "open_id", "") if mid else "",
                    "user_id": getattr(mid, "user_id", "") if mid else "",
                    "key": getattr(m, "key", "") or "",
                    "name": getattr(m, "name", "") or "",
                })
        return {
            "message": {
                "message_id": getattr(message, "message_id", "") if message else "",
                "message_type": getattr(message, "message_type", "") if message else "",
                "content": getattr(message, "content", "{}") if message else "{}",
                "chat_id": getattr(message, "chat_id", "") if message else "",
                "chat_type": getattr(message, "chat_type", "") if message else "",
                "mentions": mentions,
            },
            "sender": {
                "sender_id": {
                    "user_id": getattr(sender_id, "user_id", None) if sender_id else None,
                    "open_id": getattr(sender_id, "open_id", None) if sender_id else None,
                    "union_id": getattr(sender_id, "union_id", None) if sender_id else None,
                }
            },
        }

    # ------------------------------------------------------------------ #
    # Dedup
    # ------------------------------------------------------------------ #

    def _is_duplicate(self, message_id: str) -> bool:
        """Return True if this message_id was already seen within the TTL window."""
        now = time.monotonic()
        cutoff = now - _DEDUP_TTL_SECONDS
        # Prune stale entries
        self._seen_message_ids = {k: v for k, v in self._seen_message_ids.items() if v > cutoff}
        if message_id in self._seen_message_ids:
            return True
        self._seen_message_ids[message_id] = now
        return False

    # ------------------------------------------------------------------ #
    # Core message handler
    # ------------------------------------------------------------------ #

    async def _handle_message_event(self, event: dict[str, Any]) -> None:
        message = event.get("message", {})
        if message.get("message_type") != "text":
            return

        text = self._extract_text(message.get("content", "{}"))
        if not text:
            return

        sender = event.get("sender", {}).get("sender_id", {})
        user_id = sender.get("user_id") or sender.get("open_id") or "unknown"
        message_id = message.get("message_id", "")
        chat_id = message.get("chat_id", "")
        chat_type = message.get("chat_type", "p2p")

        # Group mention check: in non-DM chats, only respond if bot is @mentioned
        if chat_type != "p2p":
            bot_open_id = await self._get_bot_open_id()
            mentions = message.get("mentions", [])
            bot_mentioned = any(
                m.get("open_id") == bot_open_id
                for m in mentions
                if bot_open_id
            )
            if not bot_mentioned:
                # Silently ignore messages where bot is not @mentioned
                return
            # Strip bot @mention placeholders from text
            text = self._strip_bot_mentions(text, mentions, bot_open_id)
            if not text:
                return

        # 1. Dedup — drop replays from WebSocket reconnect
        if self._is_duplicate(message_id):
            logger.debug("duplicate-message-skipped", message_id=message_id)
            return

        # 2. Slash commands are synchronous (no agent run)
        if text.startswith("/"):
            await self._handle_command(user_id, message_id, text, chat_id, chat_type)
            return

        # 3. Message debounce: accumulate rapid-fire messages into one agent run.
        debounce_ms = self._settings.debounce_ms
        if debounce_ms > 0:
            self._debounce_texts.setdefault(user_id, []).append(
                (message_id, text, chat_id, chat_type)
            )
            # Cancel any pending flush — we'll restart the timer with the new message.
            old = self._debounce_tasks.pop(user_id, None)
            if old and not old.done():
                old.cancel()
            self._debounce_tasks[user_id] = asyncio.create_task(
                self._debounce_flush(user_id, delay=debounce_ms / 1000.0),
                name=f"debounce:{user_id}",
            )
            return

        # No debounce: dispatch immediately.
        await self._dispatch_to_agent(user_id, [(message_id, text, chat_id, chat_type)])

    # ------------------------------------------------------------------ #
    # Message debounce flush
    # ------------------------------------------------------------------ #

    async def _debounce_flush(self, user_id: str, delay: float) -> None:
        """Wait for the debounce window to elapse, then dispatch accumulated messages."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return  # a newer message reset the timer; the new task will flush
        entries = self._debounce_texts.pop(user_id, [])
        self._debounce_tasks.pop(user_id, None)
        if not entries:
            return
        await self._dispatch_to_agent(user_id, entries)

    # ------------------------------------------------------------------ #
    # Agent dispatch (shared by immediate and debounced paths)
    # ------------------------------------------------------------------ #

    async def _dispatch_to_agent(
        self, user_id: str, entries: list[tuple[str, str, str, str]],
    ) -> None:
        """Run the agent for the given (message_id, text, chat_id, chat_type) entries.

        When multiple entries are present (debounced batch), their texts are
        joined with newlines and the last message_id is used for the reply card.
        """
        if not entries:
            return
        # Use the last message as the "primary" for the reply card.
        message_id, _, chat_id, chat_type = entries[-1]
        combined_text = "\n".join(text for _, text, _, _ in entries)

        # Concurrent-run guard — one run per user at a time
        existing_task = self._active_tasks.get(user_id)
        if existing_task is not None and not existing_task.done():
            await self._reply_message(
                message_id,
                "⏳ 你上一次的请求还在处理中，请稍等完成后再发送新消息。\n"
                "如需中断，请发送：/stop",
            )
            return

        # Typing reaction as instant visual ack
        reaction_id = await self._add_reaction(message_id, "Typing")

        # Post the initial "thinking" card
        card_message_id = await self._reply_card(message_id, render_running_card(combined_text))

        # Launch agent run as a cancellable task
        task = asyncio.create_task(
            self._run_agent(user_id, combined_text, card_message_id, message_id, reaction_id, chat_id, chat_type),
            name=f"agent-run:{user_id}",
        )
        self._active_tasks[user_id] = task
        try:
            await task
        finally:
            self._active_tasks.pop(user_id, None)

    # ------------------------------------------------------------------ #
    # Agent execution with streaming + timeout
    # ------------------------------------------------------------------ #

    async def _run_agent(
        self,
        user_id: str,
        text: str,
        card_message_id: str | None,
        message_id: str,
        reaction_id: str | None,
        chat_id: str = "",
        chat_type: str = "p2p",
    ) -> None:
        """Run the ACP agent, pushing throttled card updates as it streams."""
        settings = self._settings
        throttle_s = settings.card_update_throttle_ms / 1000.0
        show_tool_calls = settings.show_tool_calls

        updater: _ThrottledCardUpdater | None = None
        if card_message_id:
            updater = _ThrottledCardUpdater(
                self, card_message_id, throttle_s=throttle_s, show_tool_calls=show_tool_calls
            )

        state: SessionState | None = None
        status = "done"
        error_text: str | None = None

        def on_state_change(s: SessionState) -> None:
            nonlocal state
            state = s
            if updater is not None:
                updater.schedule(s)

        def on_session_reset() -> None:
            """Called when the session was silently recreated (context lost)."""
            asyncio.ensure_future(self._reply_card(message_id, session_reset_card()))
            # Record the auto-created session for /resume
            cwd = self._get_cwd(chat_id, chat_type, user_id)
            agent = agent_name or self._resolve_agent_name(user_id, chat_id, chat_type) or ""
            # Look up the session_id from the bridge
            sid = None
            if self._agent_manager is not None and agent:
                bridge = self._agent_manager._bridges.get(agent)
                sid = bridge._user_sessions.get(user_id) if bridge else None
            elif self.codex_bridge is not None:
                sid = self.codex_bridge._user_sessions.get(user_id)
            if sid:
                self._record_session(sid, agent_name=agent, cwd=cwd)

        # --- Resolve which agent to use --------------------------------
        # 1. Check prefix routing: "agent_name: prompt"
        agent_name: str | None = None
        actual_text = text
        if self._agent_manager is not None:
            agent_name, actual_text = parse_prefix_routing(
                text, set(self._agent_manager.registered_names)
            )

        # 2. If no prefix, use scope-based active agent
        if agent_name is None and self._agent_manager is not None:
            agent_name = self._resolve_agent_name(user_id, chat_id, chat_type)

        # --- Try to resume prior session before first message ------------
        await self._try_resume_prior_session(user_id, agent_name, chat_id, chat_type)

        # --- Run with timeout and cancellation handling -----------------
        try:
            if self._agent_manager is not None and agent_name:
                state = await asyncio.wait_for(
                    self._agent_manager.chat(
                        message=actual_text,
                        agent_name=agent_name,
                        user_id=user_id,
                        on_state_change=on_state_change,
                        on_session_reset=on_session_reset,
                    ),
                    timeout=float(settings.idle_timeout_seconds),
                )
            else:
                state = await asyncio.wait_for(
                    self.codex_bridge.chat(
                        message=text,
                        user_id=user_id,
                        on_state_change=on_state_change,
                        on_session_reset=on_session_reset,
                    ),
                    timeout=float(settings.idle_timeout_seconds),
                )
            status = "done"

        except asyncio.TimeoutError:
            logger.warning(
                "agent-timeout", user_id=user_id,
                timeout_seconds=settings.idle_timeout_seconds,
            )
            if self._agent_manager is not None and agent_name:
                await self._agent_manager.cancel(agent_name=agent_name, user_id=user_id)
            elif self.codex_bridge is not None:
                await self.codex_bridge.cancel(user_id=user_id)
            status = "timeout"
            if state is None:
                state = SessionState(status="timeout")
            else:
                state.status = "timeout"

        except asyncio.CancelledError:
            logger.info("agent-run-cancelled", user_id=user_id)
            status = "cancelled"
            if state is None:
                state = SessionState(status="cancelled")
            else:
                state.status = "cancelled"

        except Exception as exc:
            logger.error("agent-run-failed", user_id=user_id, error=str(exc), exc_info=True)
            status = "error"
            error_text = str(exc)
            if state is None:
                state = SessionState(status="error")
            else:
                state.status = "error"

        # --- Update session preview with the user's message -------------
        self._update_session_preview(user_id, agent_name, chat_id, chat_type, text)

        # --- Cleanup: always remove reaction and post final card ---------
        if reaction_id:
            await self._remove_reaction(message_id, reaction_id)

        if status == "error":
            # Error card replaces whatever was shown before
            if card_message_id:
                await self._update_card(card_message_id, render_error_card(error_text or "未知错误"))
            else:
                await self._reply_message(message_id, f"❌ 处理失败: {error_text}")
        elif updater is not None and state is not None:
            await updater.final_flush(state, status=status)
        elif card_message_id and state is not None:
            await self._update_card(
                card_message_id,
                render_result_card(state, status=status, show_tool_calls=show_tool_calls),
            )
        else:
            # Fallback: no card message id (reply_card failed), send plain text
            fallback = state.full_text if state and state.full_text else "⚠️ 出现异常，未能正常处理请求。"
            await self._reply_message(message_id, fallback)

    # ------------------------------------------------------------------ #
    # Command router
    # ------------------------------------------------------------------ #

    async def _handle_command(
        self, user_id: str, message_id: str, text: str,
        chat_id: str = "", chat_type: str = "p2p",
    ) -> None:
        parts = text.split(maxsplit=2)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        rest = parts[2] if len(parts) > 2 else ""

        if command in {"/new", "/newsession"}:
            if args.lower() == "chat" or args.lower().startswith("chat "):
                # /new chat [name]
                chat_name = rest.strip() or f"{self._agent_name} - {__import__('datetime').date.today().isoformat()}"
                await self._handle_new_chat(user_id, message_id, chat_name, chat_id, chat_type)
            else:
                agent_name = self._resolve_agent_name(user_id, chat_id, chat_type)
                if self._agent_manager is not None and agent_name:
                    await self._agent_manager.close_session(agent_name=agent_name, user_id=user_id)
                elif self.codex_bridge is not None:
                    await self.codex_bridge.close_session(user_id=user_id)
                else:
                    await self._reply_card(message_id, simple_text_card("⚠️ 未配置任何 agent。", "🆕 新会话"))
                    return
                session_id = await self._create_and_record_session(
                    user_id=user_id, agent_name=agent_name, chat_id=chat_id, chat_type=chat_type,
                )
                await self._reply_card(message_id, simple_text_card(
                    f"✅ 新会话已创建\n会话 ID: `{session_id}`", "🆕 新会话"
                ))

        elif command in {"/stop", "/cancel"}:
            stopped = await self._stop_user_run(user_id, message_id)
            if not stopped:
                await self._reply_card(message_id, simple_text_card("⚠️ 当前没有正在运行的操作。", "⏹️"))

        elif command == "/status":
            await self._handle_status(user_id, message_id, chat_id, chat_type)

        elif command == "/resume":
            current_cwd = self._get_cwd(chat_id, chat_type, user_id)
            entries: list[dict[str, Any]] = []
            if self._session_store is not None:
                raw = self._session_store.list_for_cwd(current_cwd)
                for s in raw:
                    entries.append({
                        "session_id": s.get("session_id", ""),
                        "preview": s.get("preview", "") or s.get("created_at", "")[:19],
                        "is_current": False,
                    })
            await self._reply_card(message_id, resume_card(entries=entries))

        elif command == "/agent":
            await self._handle_agent_command(user_id, message_id, args, rest, chat_id, chat_type)

        elif command == "/cd":
            await self._handle_cd_command(user_id, message_id, args, chat_id, chat_type)

        elif command in {"/ws", "/workspaces"}:
            await self._handle_ws_command(user_id, message_id, args, rest, chat_id, chat_type)

        elif command == "/help":
            await self._reply_card(message_id, help_card())

        else:
            await self._reply_card(message_id, simple_text_card(
                f"❓ 未知命令: `{command}`\n发送 `/help` 查看可用命令。", "❓"
            ))

    # ------------------------------------------------------------------ #
    # Scope-aware helpers
    # ------------------------------------------------------------------ #

    def _scope_key(self, chat_id: str, chat_type: str, user_id: str = "") -> str:
        """Compute scope key for cwd tracking."""
        if chat_type == "p2p":
            return f"user:{user_id}"
        return f"chat:{chat_id}"

    def _get_cwd(self, chat_id: str, chat_type: str, user_id: str = "") -> str:
        """Return cwd for the given scope, falling back to settings.working_dir."""
        key = self._scope_key(chat_id, chat_type, user_id)
        if self._workspace_store is not None:
            return self._workspace_store.get_cwd(key, str(self._settings.working_dir))
        return self._cwd_by_scope.get(key, str(self._settings.working_dir))

    def _resolve_agent_name(self, user_id: str, chat_id: str, chat_type: str) -> str | None:
        """Return the agent name to use for the given scope."""
        if self._agent_manager is not None:
            return self._agent_manager.active_agent_for(user_id, chat_id, chat_type)
        return None

    @staticmethod
    def _is_blocked_path(target: Path) -> bool:
        """Return True if ``target`` is a system directory that ``/cd`` should reject."""
        if target in _BLOCKED_PATHS:
            return True
        # Block sub-directories of sensitive Unix paths (/etc/nginx, /usr/local, etc.)
        for bp in _BLOCKED_PARENTS_UNIX:
            try:
                if bp in target.parents:
                    return True
            except Exception:
                pass
        # Windows: block if inside Windows or Program Files
        if sys.platform == "win32":
            win_roots = {Path("C:\\Windows"), Path("C:\\Program Files"),
                         Path("C:\\Program Files (x86)")}
            for wr in win_roots:
                try:
                    if target == wr or wr in target.parents:
                        return True
                except Exception:
                    pass
        return False

    def _record_session(self, session_id: str, agent_name: str = "", cwd: str = "", preview: str = "") -> None:
        """Record a session in the SessionStore (no-op if store is not configured)."""
        if self._session_store is not None:
            self._session_store.record(
                session_id=session_id,
                agent_name=agent_name or self._agent_name,
                cwd=cwd or str(self._settings.working_dir),
                preview=preview,
            )

    async def _create_and_record_session(
        self, user_id: str, agent_name: str | None = None,
        chat_id: str = "", chat_type: str = "p2p", preview: str = "",
    ) -> str:
        """Create a new session and record it in the SessionStore."""
        agent = agent_name or self._resolve_agent_name(user_id, chat_id, chat_type)
        cwd = self._get_cwd(chat_id, chat_type, user_id)
        if self._agent_manager is not None and agent:
            session_id = await self._agent_manager.create_session(agent_name=agent, user_id=user_id)
        elif self.codex_bridge is not None:
            session_id = await self.codex_bridge.create_session(user_id=user_id)
        else:
            return "(no session)"
        self._record_session(session_id, agent_name=agent or "", cwd=cwd, preview=preview)
        return session_id

    async def _try_resume_prior_session(
        self, user_id: str, agent_name: str | None, chat_id: str, chat_type: str,
    ) -> None:
        """If the bridge has no session for this user yet, try to load the most
        recent one from SessionStore so the conversation continues seamlessly
        across bridge restarts.

        On success the session is registered in the bridge's ``_user_sessions``,
        so the subsequent ``get_or_create_session`` call will find it and skip
        creating a new one (no reset notification).

        On failure (session expired, agent doesn't support resume) we silently
        fall through — ``get_or_create_session`` will create a fresh session
        and the user will see the reset card.
        """
        if self._session_store is None:
            return

        # Check if bridge already has a session for this user — nothing to do.
        has_existing = False
        agent = agent_name or self._resolve_agent_name(user_id, chat_id, chat_type) or ""
        if self._agent_manager is not None and agent:
            bridge = self._agent_manager._bridges.get(agent)
            has_existing = bridge is not None and user_id in bridge._user_sessions
        elif self.codex_bridge is not None:
            has_existing = user_id in self.codex_bridge._user_sessions
        if has_existing:
            return

        # Look up the most recent session for this cwd + agent
        cwd = self._get_cwd(chat_id, chat_type, user_id)
        prior = self._session_store.list_for_cwd(cwd)
        target_session_id: str | None = None
        for s in prior:
            if agent and s.get("agent_name") == agent:
                target_session_id = s.get("session_id")
                break
        if not target_session_id:
            return

        try:
            if self._agent_manager is not None and agent:
                await self._agent_manager.load_session(
                    agent_name=agent, session_id=target_session_id, user_id=user_id,
                )
            elif self.codex_bridge is not None:
                await self.codex_bridge.load_session(target_session_id, user_id=user_id)
            logger.info("session-resumed", session_id=target_session_id, agent=agent, user_id=user_id)
        except Exception as exc:
            logger.debug("session-resume-failed", session_id=target_session_id, error=str(exc))
            # Fall through — chat() will create a new session and fire on_session_reset

    def _update_session_preview(
        self, user_id: str, agent_name: str | None,
        chat_id: str, chat_type: str, text: str,
    ) -> None:
        """Update the SessionStore preview for the user's current session.

        Called after every agent run so ``/resume`` shows the user's message
        as the session title instead of a bare timestamp.
        """
        if self._session_store is None:
            return
        agent = agent_name or self._resolve_agent_name(user_id, chat_id, chat_type) or ""
        cwd = self._get_cwd(chat_id, chat_type, user_id)
        # Look up the session_id from the bridge
        sid: str | None = None
        if self._agent_manager is not None and agent:
            bridge = self._agent_manager._bridges.get(agent)
            sid = bridge._user_sessions.get(user_id) if bridge else None
        elif self.codex_bridge is not None:
            sid = self.codex_bridge._user_sessions.get(user_id)
        if sid:
            self._record_session(sid, agent_name=agent, cwd=cwd, preview=text)

    # ------------------------------------------------------------------ #
    # /agent command handler
    # ------------------------------------------------------------------ #

    async def _handle_agent_command(
        self, user_id: str, message_id: str, sub: str, rest: str,
        chat_id: str, chat_type: str,
    ) -> None:
        if self._agent_manager is None:
            await self._reply_card(message_id, simple_text_card("⚠️ 多 agent 功能未启用。", "🤖 Agent"))
            return

        sub = sub.lower()
        agents = self._agent_manager.list_agents()
        scope = self._agent_manager.scope_key(user_id, chat_id, chat_type)
        active = self._agent_manager.active_agent_for(user_id, chat_id, chat_type)

        if sub in ("", "list"):
            agent_dicts = [
                {"name": a.name, "description": a.description, "running": a.running}
                for a in agents
            ]
            await self._reply_card(message_id, agent_list_card(agent_dicts, active))

        elif sub == "use":
            name = rest.strip()
            if not name:
                await self._reply_card(message_id, simple_text_card("用法: `/agent use <name>`", "🤖 Agent"))
                return
            if not self._agent_manager.has_agent(name):
                available = ", ".join(self._agent_manager.registered_names) or "（无）"
                await self._reply_card(message_id, simple_text_card(
                    f"❌ 未知 agent: `{name}`\n可用: {available}", "🤖 Agent"
                ))
                return
            try:
                self._agent_manager.set_active_agent(scope, name)
            except ValueError as e:
                await self._reply_card(message_id, simple_text_card(f"❌ {e}", "🤖 Agent"))
                return
            # Reset session for the new agent
            try:
                await self._agent_manager.close_session(agent_name=name, user_id=user_id)
                session_id = await self._agent_manager.create_session(agent_name=name, user_id=user_id)
                # Find description
                desc = next((a.description for a in agents if a.name == name), name)
                await self._reply_card(message_id, agent_switched_card(name, desc, session_id))
            except Exception as e:
                await self._reply_card(message_id, simple_text_card(
                    f"✅ 已切换到 **{name}**（会话重置失败: {e}）", "🤖 Agent"
                ))

        else:
            await self._reply_card(message_id, simple_text_card(
                "用法: `/agent list` 或 `/agent use <name>`", "🤖 Agent"
            ))

    # ------------------------------------------------------------------ #
    # /cd command handler
    # ------------------------------------------------------------------ #

    async def _handle_cd_command(
        self, user_id: str, message_id: str, path_arg: str,
        chat_id: str, chat_type: str,
    ) -> None:
        if not path_arg:
            current = self._get_cwd(chat_id, chat_type, user_id)
            await self._reply_card(message_id, simple_text_card(
                f"当前 cwd: `{current}`\n用法: `/cd <path>`", "📁 工作目录"
            ))
            return

        # Expand ~ and make absolute
        expanded = os.path.expanduser(path_arg)
        target = Path(expanded).resolve()

        if not target.exists():
            await self._reply_card(message_id, simple_text_card(
                f"❌ 路径不存在: `{target}`", "📁 工作目录"
            ))
            return
        if not target.is_dir():
            await self._reply_card(message_id, simple_text_card(
                f"❌ 不是目录: `{target}`", "📁 工作目录"
            ))
            return

        # Block system/dangerous directories
        if self._is_blocked_path(target):
            await self._reply_card(message_id, simple_text_card(
                f"❌ 禁止使用系统目录: `{target}`\n请选择你的项目目录。", "📁 工作目录"
            ))
            return

        key = self._scope_key(chat_id, chat_type, user_id)
        if self._workspace_store is not None:
            self._workspace_store.set_cwd(key, str(target))
        else:
            self._cwd_by_scope[key] = str(target)

        # Reset session for the scope
        agent_name = self._resolve_agent_name(user_id, chat_id, chat_type)
        if self._agent_manager is not None and agent_name:
            await self._agent_manager.close_session(agent_name=agent_name, user_id=user_id)
            session_id = await self._agent_manager.create_session(agent_name=agent_name, user_id=user_id)
        elif self.codex_bridge is not None:
            await self.codex_bridge.close_session(user_id=user_id)
            session_id = await self.codex_bridge.create_session(user_id=user_id)
        else:
            session_id = "(no session)"

        await self._reply_card(message_id, simple_text_card(
            f"✅ cwd 已切换到: `{target}`\n会话 ID: `{session_id}`", "📁 工作目录"
        ))

    # ------------------------------------------------------------------ #
    # /ws command handler
    # ------------------------------------------------------------------ #

    async def _handle_ws_command(
        self, user_id: str, message_id: str, sub: str, rest: str,
        chat_id: str, chat_type: str,
    ) -> None:
        sub = sub.lower()

        if sub in ("", "list"):
            current_dir = self._get_cwd(chat_id, chat_type, user_id)
            named = self._workspace_store.list_named() if self._workspace_store else {}
            workspaces_list = [{"name": k, "path": v} for k, v in named.items()]
            await self._reply_card(message_id, workspaces_card(current_dir, workspaces_list))

        elif sub == "save":
            name = rest.strip()
            if not name:
                await self._reply_card(message_id, simple_text_card("用法: `/ws save <name>`", "📂 工作空间"))
                return
            current_cwd = self._get_cwd(chat_id, chat_type, user_id)
            if self._workspace_store is not None:
                self._workspace_store.save_named(name, current_cwd)
                await self._reply_card(message_id, simple_text_card(
                    f"✅ 工作目录已保存: `{name}` → `{current_cwd}`", "📂 工作空间"
                ))
            else:
                await self._reply_card(message_id, simple_text_card("⚠️ 工作空间存储未配置。", "📂 工作空间"))

        elif sub == "use":
            name = rest.strip()
            if not name:
                await self._reply_card(message_id, simple_text_card("用法: `/ws use <name>`", "📂 工作空间"))
                return
            if self._workspace_store is None:
                await self._reply_card(message_id, simple_text_card("⚠️ 工作空间存储未配置。", "📂 工作空间"))
                return
            path = self._workspace_store.get_named(name)
            if path is None:
                available = ", ".join(self._workspace_store.list_named().keys()) or "（无）"
                await self._reply_card(message_id, simple_text_card(
                    f"❌ 未找到工作空间: `{name}`\n可用: {available}", "📂 工作空间"
                ))
                return
            key = self._scope_key(chat_id, chat_type, user_id)
            self._workspace_store.set_cwd(key, path)
            # Reset session
            agent_name = self._resolve_agent_name(user_id, chat_id, chat_type)
            if self._agent_manager is not None and agent_name:
                await self._agent_manager.close_session(agent_name=agent_name, user_id=user_id)
                session_id = await self._agent_manager.create_session(agent_name=agent_name, user_id=user_id)
            elif self.codex_bridge is not None:
                await self.codex_bridge.close_session(user_id=user_id)
                session_id = await self.codex_bridge.create_session(user_id=user_id)
            else:
                session_id = "(no session)"
            await self._reply_card(message_id, simple_text_card(
                f"✅ 已切换到 `{name}` → `{path}`\n会话 ID: `{session_id}`", "📂 工作空间"
            ))

        elif sub == "remove":
            name = rest.strip()
            if not name:
                await self._reply_card(message_id, simple_text_card("用法: `/ws remove <name>`", "📂 工作空间"))
                return
            if self._workspace_store is None:
                await self._reply_card(message_id, simple_text_card("⚠️ 工作空间存储未配置。", "📂 工作空间"))
                return
            if self._workspace_store.remove_named(name):
                await self._reply_card(message_id, simple_text_card(
                    f"✅ 已删除工作空间: `{name}`", "📂 工作空间"
                ))
            else:
                await self._reply_card(message_id, simple_text_card(
                    f"❌ 未找到工作空间: `{name}`", "📂 工作空间"
                ))

        else:
            await self._reply_card(message_id, simple_text_card(
                "用法:\n- `/ws list` 查看所有\n- `/ws save <name>` 保存\n- `/ws use <name>` 切换\n- `/ws remove <name>` 删除",
                "📂 工作空间"
            ))

    # ------------------------------------------------------------------ #
    # /new chat handler
    # ------------------------------------------------------------------ #

    async def _handle_new_chat(
        self, user_id: str, message_id: str, chat_name: str,
        source_chat_id: str, chat_type: str,
    ) -> None:
        from .group import create_bound_chat

        # Resolve open_id for the requesting user (user_id might be user_id or open_id)
        invite_open_id = user_id

        try:
            info = await create_bound_chat(
                client=self.client,
                name=chat_name,
                invite_open_id=invite_open_id,
            )
        except RuntimeError as e:
            await self._reply_message(message_id, f"❌ {e}")
            return

        # Inherit cwd from source chat
        source_cwd = self._get_cwd(source_chat_id, chat_type, user_id)
        if self._workspace_store is not None:
            self._workspace_store.set_cwd(f"chat:{info.chat_id}", source_cwd)

        # Welcome message in new group
        if source_cwd and source_cwd != str(self._settings.working_dir):
            welcome = f"🎉 群已建好，cwd 继承自原群：`{source_cwd}`\n\n@我 + 任意消息开始对话。"
        else:
            welcome = "🎉 群已建好。\n\n请先发送 `/cd <path>` 设置工作目录，再 @我 开始对话。"

        try:
            await self._send_card(info.chat_id, simple_text_card(welcome))
        except Exception as exc:
            logger.warning("new-chat-welcome-failed", error=str(exc))

        await self._reply_message(
            message_id,
            f"✅ 已创建群 **{info.name}**，去新群里继续。"
        )

    async def _handle_status(self, user_id: str, message_id: str, chat_id: str = "", chat_type: str = "p2p") -> None:
        task = self._active_tasks.get(user_id)
        is_running = task is not None and not task.done()
        current_cwd = self._get_cwd(chat_id, chat_type, user_id)

        if self._agent_manager is not None:
            agent_name = self._resolve_agent_name(user_id, chat_id, chat_type) or "none"
            session_count = self._agent_manager.active_session_count(agent_name) if agent_name else 0
            bridge_running = self._agent_manager.is_running
            agent_type = f"multi-agent (active={agent_name})"
            agent_command = agent_name
        elif self.codex_bridge is not None:
            session_count = self.codex_bridge.active_session_count
            bridge_running = self.codex_bridge.is_running
            agent_type = "custom" if self._settings.agent_command else "codex"
            agent_command = self._agent_name
        else:
            session_count = 0
            bridge_running = False
            agent_type = "none"
            agent_command = "(none)"

        info = {
            "working_dir": current_cwd,
            "session_count": session_count,
            "active_run": is_running,
            "bridge_running": bridge_running,
            "agent_type": agent_type,
            "agent_command": agent_command,
        }
        card = status_card(info)
        if message_id:
            await self._reply_card(message_id, card)
        elif chat_id:
            await self._send_card(chat_id, card)
        else:
            logger.warning("handle-status-no-target", user_id=user_id)

    async def _stop_user_run(self, user_id: str, message_id: str, chat_id: str = "") -> bool:
        """Cancel the active run for a user. Returns True if something was cancelled."""
        task = self._active_tasks.get(user_id)
        if task is None or task.done():
            return False
        if message_id:
            await self._reply_message(message_id, "⏹️ 已发送中断信号，正在停止...")
        elif chat_id:
            await self._send_card(chat_id, simple_text_card("⏹️ 已发送中断信号，正在停止..."))
        if self._agent_manager is not None:
            for name in self._agent_manager.registered_names:
                await self._agent_manager.cancel(agent_name=name, user_id=user_id)
        elif self.codex_bridge is not None:
            await self.codex_bridge.cancel(user_id=user_id)
        task.cancel()
        return True

    # ------------------------------------------------------------------ #
    # Card action handlers (button clicks)
    # ------------------------------------------------------------------ #

    async def _handle_card_action(self, request: web.Request) -> web.Response:
        """Handle card button clicks in webhook mode."""
        try:
            data = await request.json()
            action_value = data.get("action", {}).get("value", {})
            operator = data.get("operator", {})
            user_id = operator.get("open_id") or operator.get("user_id") or "unknown"
            # In Lark's card action callback, open_chat_id and open_message_id live under "context"
            context = data.get("context", {})
            open_chat_id = context.get("open_chat_id") or data.get("open_chat_id", "")
            open_message_id = context.get("open_message_id") or data.get("open_message_id", "")

            await self._dispatch_card_action(action_value, user_id, open_chat_id, open_message_id)
            return web.json_response({"toast": {"type": "info", "content": "正在处理…"}})
        except Exception as exc:
            logger.error("card-action-handler-failed", error=str(exc), exc_info=True)
            return web.json_response({"toast": {"type": "error", "content": "处理失败"}})

    async def _dispatch_card_action(self, action_value: dict[str, Any], user_id: str, chat_id: str, message_id: str = "") -> None:
        """Route card button clicks to the appropriate command handler.

        ``action_value`` is the ``value`` dict from the button, e.g. ``{"cmd": "status"}``.
        ``message_id`` is the source message ID from the card context (may be empty).
        """
        cmd = action_value.get("cmd", "")
        logger.info("card-action", cmd=cmd, user_id=user_id, chat_id=chat_id)

        if cmd == "new":
            agent_name = self._resolve_agent_name(user_id, chat_id, "p2p")
            if self._agent_manager is not None and agent_name:
                await self._agent_manager.close_session(agent_name=agent_name, user_id=user_id)
                session_id = await self._agent_manager.create_session(agent_name=agent_name, user_id=user_id)
                sess_count = self._agent_manager.active_session_count(agent_name)
                running = self._agent_manager.is_running
                a_type = f"multi-agent (active={agent_name})"
                a_cmd = agent_name
            elif self.codex_bridge is not None:
                await self.codex_bridge.close_session(user_id=user_id)
                session_id = await self.codex_bridge.create_session(user_id=user_id)
                sess_count = self.codex_bridge.active_session_count
                running = self.codex_bridge.is_running
                a_type = "custom" if self._settings.agent_command else "codex"
                a_cmd = self._agent_name
            else:
                sess_count = 0
                running = False
                a_type = "none"
                a_cmd = "(none)"
            await self._send_card(chat_id, simple_text_card(
                f"✅ 新会话已创建\n会话 ID: `{session_id}`", "🆕 新会话"
            ))

        elif cmd == "status":
            await self._handle_status(user_id, message_id, chat_id, "group")

        elif cmd == "help":
            await self._send_card(chat_id, help_card())

        elif cmd == "resume":
            current_cwd = self._get_cwd(chat_id, "group", user_id)
            entries: list[dict[str, Any]] = []
            if self._session_store is not None:
                raw = self._session_store.list_for_cwd(current_cwd)
                for s in raw:
                    entries.append({
                        "session_id": s.get("session_id", ""),
                        "preview": s.get("preview", "") or s.get("created_at", "")[:19],
                        "is_current": False,
                    })
            await self._send_card(chat_id, resume_card(entries=entries))

        elif cmd == "resume.use":
            session_id = action_value.get("arg", "")
            if not session_id:
                await self._send_card(chat_id, simple_text_card("❌ 缺少 session_id", "🔁"))
                return
            agent_name = self._resolve_agent_name(user_id, chat_id, "group")
            try:
                if self._agent_manager is not None and agent_name:
                    loaded = await self._agent_manager.load_session(
                        agent_name=agent_name, session_id=session_id, user_id=user_id
                    )
                elif self.codex_bridge is not None:
                    loaded = await self.codex_bridge.load_session(session_id, user_id=user_id)
                else:
                    await self._send_card(chat_id, simple_text_card("⚠️ 未配置任何 agent。", "🔁"))
                    return
                await self._send_card(chat_id, simple_text_card(
                    f"✅ 会话已恢复\n会话 ID: `{loaded}`", "🔁 恢复会话"
                ))
            except Exception as exc:
                logger.warning("resume-session-failed", session_id=session_id, error=str(exc))
                await self._send_card(chat_id, simple_text_card(
                    f"❌ 恢复失败: {exc}\n该会话可能已过期或 agent 不支持恢复。", "🔁"
                ))

        elif cmd == "ws.list":
            current_dir = self._get_cwd(chat_id, "group", user_id)
            named = self._workspace_store.list_named() if self._workspace_store else {}
            ws_list = [{"name": k, "path": v} for k, v in named.items()]
            await self._send_card(chat_id, workspaces_card(current_dir, ws_list))

        elif cmd == "agent.list":
            if self._agent_manager is not None:
                agents = self._agent_manager.list_agents()
                active = self._agent_manager.active_agent_for(user_id, chat_id, "group")
                agent_dicts = [
                    {"name": a.name, "description": a.description, "running": a.running}
                    for a in agents
                ]
                await self._send_card(chat_id, agent_list_card(agent_dicts, active))
            else:
                await self._send_card(chat_id, simple_text_card("⚠️ 多 agent 功能未启用。", "🤖 Agent"))

        elif cmd == "agent.use":
            name = action_value.get("name", "")
            if self._agent_manager is not None and name:
                scope = self._agent_manager.scope_key(user_id, chat_id, "group")
                try:
                    self._agent_manager.set_active_agent(scope, name)
                    await self._agent_manager.close_session(agent_name=name, user_id=user_id)
                    session_id = await self._agent_manager.create_session(agent_name=name, user_id=user_id)
                    agents = self._agent_manager.list_agents()
                    desc = next((a.description for a in agents if a.name == name), name)
                    await self._send_card(chat_id, agent_switched_card(name, desc, session_id))
                except Exception as e:
                    await self._send_card(chat_id, simple_text_card(f"❌ 切换失败: {e}", "🤖 Agent"))

        elif cmd == "stop":
            stopped = await self._stop_user_run(user_id, message_id, chat_id)
            if not stopped:
                await self._send_card(chat_id, simple_text_card("⚠️ 当前没有正在运行的操作。", "⏹️"))

        else:
            logger.warning("unknown-card-action", cmd=cmd)

    # ------------------------------------------------------------------ #
    # Lark API helpers (best-effort; log errors, never raise to caller)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_text(content: str) -> str:
        try:
            return str(json.loads(content).get("text", "")).strip()
        except json.JSONDecodeError:
            return ""

    @staticmethod
    def _strip_bot_mentions(text: str, mentions: list[dict[str, Any]], bot_open_id: str | None) -> str:
        """Remove @_user_N placeholders from text for bot mentions.

        Feishu message text contains ``@_user_1``, ``@_user_2`` etc. placeholders.
        We strip the ones that correspond to the bot so the agent only sees the
        actual user prompt.
        """
        if not bot_open_id or not mentions:
            return text
        import re
        for i, m in enumerate(mentions, 1):
            if m.get("open_id") == bot_open_id:
                # Feishu uses @_user_1, @_user_2... for the Nth mention
                text = re.sub(rf"@_user_{i}\s?", "", text)
        return text.strip()

    async def _reply_message(self, message_id: str, text: str) -> None:
        content = json.dumps({"text": text}, ensure_ascii=False)
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder().msg_type("text").content(content).build()
            )
            .build()
        )
        response = self.client.im.v1.message.reply(request)
        if not response.success():
            logger.error("reply-message-failed", code=response.code, msg=response.msg)
        else:
            logger.info("reply-message-sent", message_id=message_id, text_len=len(text))

    async def _reply_card(self, message_id: str, card: dict[str, Any]) -> str | None:
        content = json.dumps(card, ensure_ascii=False)
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder().msg_type("interactive").content(content).build()
            )
            .build()
        )
        response = self.client.im.v1.message.reply(request)
        if not response.success():
            logger.error("reply-card-failed", code=response.code, msg=response.msg)
            return None
        card_message_id = getattr(response.data, "message_id", None)
        logger.info("reply-card-sent", source_message_id=message_id, card_message_id=card_message_id)
        return card_message_id

    async def _send_card(self, chat_id: str, card: dict[str, Any]) -> str | None:
        """Send a card to a chat (without replying to a specific message)."""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        content = json.dumps(card, ensure_ascii=False)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(content)
                .build()
            )
            .build()
        )
        response = self.client.im.v1.message.create(request)
        if not response.success():
            logger.error("send-card-failed", code=response.code, msg=response.msg, chat_id=chat_id)
            return None
        card_message_id = getattr(response.data, "message_id", None)
        logger.info("send-card-sent", chat_id=chat_id, card_message_id=card_message_id)
        return card_message_id

    async def _update_card(self, message_id: str, card: dict[str, Any]) -> None:
        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder().content(json.dumps(card, ensure_ascii=False)).build()
            )
            .build()
        )
        response = self.client.im.v1.message.patch(request)
        if not response.success():
            logger.error("update-card-failed", code=response.code, msg=response.msg)
        else:
            logger.info("card-updated", message_id=message_id)

    async def _add_reaction(self, message_id: str, reaction_type: str) -> str | None:
        request = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder().emoji_type(reaction_type).build())
                .build()
            )
            .build()
        )
        response = self.client.im.v1.message_reaction.create(request)
        if not response.success():
            logger.warning("add-reaction-failed", code=response.code, msg=response.msg)
            return None
        return getattr(response.data, "reaction_id", None)

    async def _remove_reaction(self, message_id: str, reaction_id: str) -> None:
        request = (
            DeleteMessageReactionRequest.builder()
            .message_id(message_id)
            .reaction_id(reaction_id)
            .build()
        )
        response = self.client.im.v1.message_reaction.delete(request)
        if not response.success():
            logger.warning("remove-reaction-failed", code=response.code, msg=response.msg)

    # ------------------------------------------------------------------ #
    # Graceful shutdown
    # ------------------------------------------------------------------ #

    async def shutdown(self) -> None:
        """Cancel all in-flight agent runs and wait up to 5 seconds for cleanup.

        Called by the CLI layer on SIGTERM/SIGINT so that running cards are
        updated to a terminal state rather than staying stuck on "thinking".
        """
        self._shutdown_event.set()
        tasks = [t for t in self._active_tasks.values() if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=5.0)
        # Cancel any pending debounce tasks
        for dt in list(self._debounce_tasks.values()):
            dt.cancel()
        self._debounce_tasks.clear()
        self._debounce_texts.clear()

    # ------------------------------------------------------------------ #
    # Server lifecycle
    # ------------------------------------------------------------------ #

    async def start_webhook_server(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        logger.info("feishu-webhook-started", host=host, port=port)
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await runner.cleanup()

    async def start_ws_client(self) -> None:
        """Start Feishu long-connection client.

        The lark-oapi WebSocket client is blocking; run it in a worker thread
        and dispatch incoming events back to the current asyncio loop.
        """
        self._loop = asyncio.get_running_loop()
        event_handler = self._build_event_handler()
        ws_client = lark.ws.Client(
            self._settings.feishu_app_id,
            self._settings.feishu_app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )
        logger.info("feishu-ws-starting")
        await asyncio.to_thread(ws_client.start)
