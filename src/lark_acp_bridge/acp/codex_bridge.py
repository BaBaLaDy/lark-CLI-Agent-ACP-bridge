"""Codex ACP bridge backed by @zed-industries/codex-acp (or any ACP-compatible agent)."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from shutil import which
from typing import Any

import structlog

from .client import ACPClient, SessionState

logger = structlog.get_logger()


def _resolve_command(cmd: str) -> str:
    """Resolve a command name to an executable path, handling Windows .cmd wrappers."""
    resolved = which(cmd)
    if resolved:
        return resolved
    if sys.platform == "win32":
        # asyncio.create_subprocess_exec cannot find .cmd files by bare name;
        # try common Windows npm wrapper extensions explicitly.
        for ext in (".cmd", ".ps1", ".bat"):
            resolved = which(cmd + ext)
            if resolved:
                return resolved
    return cmd


class CodexACPBridge:
    """Manages an ACP agent process and per-user ACP sessions.

    By default launches @zed-industries/codex-acp via npx. Pass a custom
    ``agent_command`` to use any ACP-compatible CLI agent instead — e.g.
    ``["python", "my_agent.py"]`` or ``["claude-acp"]``.

    ``on_death`` is called (synchronously) when the agent subprocess
    exits unexpectedly, allowing the caller to react (e.g. notify
    users or trigger a restart).
    """

    def __init__(
        self,
        working_dir: str,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        codex_path: str = "codex",
        agent_command: list[str] | None = None,
        agent_env: dict[str, str] | None = None,
        on_death: Callable[["CodexACPBridge"], None] | None = None,
    ):
        self.working_dir = working_dir
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.codex_path = codex_path
        # Custom agent command: when provided, codex_path / api_key / base_url / model
        # are ignored for process construction (caller is responsible for env vars).
        self.agent_command = agent_command
        self.agent_env = agent_env or {}
        self._client: ACPClient | None = None
        self._user_sessions: dict[str, str] = {}
        self._watchdog_task: asyncio.Task | None = None
        self._on_death = on_death

    @property
    def is_running(self) -> bool:
        return self._client is not None

    @property
    def is_connected(self) -> bool:
        """Return True if the bridge is running and the ACP connection is alive."""
        return self._client is not None and self._client.is_connected

    @property
    def active_session_count(self) -> int:
        """Return the number of currently tracked user sessions."""
        return len(self._user_sessions)

    async def start(self) -> None:
        if self._client is not None:
            return

        if self.agent_command:
            command = [_resolve_command(self.agent_command[0])] + list(self.agent_command[1:])
            env: dict[str, str] = dict(self.agent_env)
            logger.info("starting-custom-acp-agent", command=command, working_dir=self.working_dir)
        else:
            command = [self._resolve_npx(), "-y", "@zed-industries/codex-acp"]
            env = {"CODEX_BIN": self.codex_path}
            if self.api_key:
                env["OPENAI_API_KEY"] = self.api_key
            if self.base_url:
                env["OPENAI_BASE_URL"] = self.base_url
            if self.model:
                env["CODEX_MODEL"] = self.model
            logger.info("starting-codex-acp", working_dir=self.working_dir)

        self._client = ACPClient(command=command, cwd=self.working_dir, env=env)
        await self._client.start()
        # Start background watchdog — detects unexpected process exit.
        self._watchdog_task = asyncio.create_task(self._watchdog())

    async def _watchdog(self) -> None:
        """Background task that detects agent subprocess death.

        Waits for the underlying process to exit, then marks the bridge
        as not running and invokes the ``on_death`` callback if provided.

        Uses a polling fallback to handle edge cases (e.g. Windows pipe
        hangs where proc.wait() blocks despite the process having exited).
        """
        proc = self._client.process if self._client else None
        if proc is None:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            # proc.wait() didn't return in 10s — poll returncode instead
            while True:
                if proc.returncode is not None:
                    break
                await asyncio.sleep(2)
        except Exception:
            pass
        # Process exited — mark bridge as dead so get_bridge() will recreate it.
        self._client = None
        logger.warning("agent-process-died", working_dir=self.working_dir)
        if self._on_death is not None:
            try:
                self._on_death(self)
            except Exception as exc:
                logger.warning("on-death-callback-failed", error=str(exc), exc_info=True)

    async def stop(self) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog_task = None
        if self._client is None:
            return
        for session_id in list(self._user_sessions.values()):
            try:
                await self._client.close_session(session_id)
            except Exception as exc:
                logger.warning("close-session-failed", error=str(exc))
        self._user_sessions.clear()
        await self._client.stop()
        self._client = None

    async def create_session(self, user_id: str | None = None) -> str:
        if self._client is None:
            raise RuntimeError("ACP bridge is not started")
        session_id = await self._client.new_session()
        if user_id:
            old_session_id = self._user_sessions.get(user_id)
            if old_session_id:
                try:
                    await self._client.close_session(old_session_id)
                except Exception as exc:
                    logger.warning("close-old-session-failed", error=str(exc))
            self._user_sessions[user_id] = session_id
        return session_id

    async def get_or_create_session(self, user_id: str) -> tuple[str, bool]:
        """Return (session_id, is_new).

        ``is_new`` is True when a fresh session was just created because
        no prior session existed for this user (e.g. after a bridge
        restart). The caller can use this to notify the user that their
        previous conversation context has been lost.
        """
        session_id = self._user_sessions.get(user_id)
        if session_id:
            return session_id, False
        new_id = await self.create_session(user_id)
        return new_id, True

    async def load_session(self, session_id: str, user_id: str | None = None) -> str:
        """Resume an existing session by ID.

        The agent subprocess reconnects to the session using its own
        persisted conversation history. If ``user_id`` is provided, the
        session is associated with that user for future lookups.

        If the agent subprocess has exited (``is_connected`` is False),
        the bridge is restarted automatically before attempting to load.
        """
        if self._client is None:
            raise RuntimeError("ACP bridge is not started")
        if not self._client.is_connected:
            # Agent subprocess exited — restart it before loading session.
            logger.info("load-session-reconnecting", session_id=session_id)
            await self._client.stop()
            self._client = None
            await self.start()
        loaded_id = await self._client.load_session(session_id)
        if user_id:
            old_session_id = self._user_sessions.get(user_id)
            if old_session_id and old_session_id != loaded_id:
                try:
                    await self._client.close_session(old_session_id)
                except Exception as exc:
                    logger.warning("close-old-session-failed", error=str(exc))
            self._user_sessions[user_id] = loaded_id
        return loaded_id

    async def close_session(self, session_id: str | None = None, user_id: str | None = None) -> None:
        if self._client is None:
            return
        active_session_id = session_id
        if user_id:
            active_session_id = self._user_sessions.pop(user_id, None)
        if active_session_id:
            await self._client.close_session(active_session_id)

    async def chat(
        self,
        message: str,
        user_id: str | None = None,
        session_id: str | None = None,
        on_text: Callable[[str], None] | None = None,
        on_state_change: Callable[[SessionState], None] | None = None,
        on_session_reset: Callable[[], None] | None = None,
        extra_blocks: list[Any] | None = None,
    ) -> SessionState:
        """Send a chat message to the ACP agent.

        ``on_session_reset`` is called (synchronously) when a new session
        had to be created because no prior one existed — useful for
        notifying the user that their context has been lost.

        ``extra_blocks`` is a list of additional ACP content blocks (e.g.
        ``image_block``) appended to the prompt after the text block.
        """
        if self._client is None:
            raise RuntimeError("ACP bridge is not started")
        if not self._client.is_connected:
            # Agent subprocess exited — restart it before sending message.
            logger.info("chat-reconnecting")
            await self._client.stop()
            self._client = None
            await self.start()
        active_session_id = session_id
        if not active_session_id:
            if not user_id:
                raise ValueError("Either user_id or session_id is required")
            active_session_id, is_new = await self.get_or_create_session(user_id)
            if is_new and on_session_reset is not None:
                on_session_reset()
        return await self._client.prompt(
            message=message,
            session_id=active_session_id,
            on_text=on_text,
            on_state_change=on_state_change,
            extra_blocks=extra_blocks,
        )

    async def cancel(self, user_id: str | None = None, session_id: str | None = None) -> None:
        if self._client is None:
            return
        active_session_id = session_id
        if not active_session_id and user_id:
            active_session_id = self._user_sessions.get(user_id)
        if active_session_id:
            await self._client.cancel(active_session_id)

    async def __aenter__(self) -> "CodexACPBridge":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()

    @staticmethod
    def _resolve_npx() -> str:
        npx = which("npx") or which("npx.cmd")
        if not npx:
            raise RuntimeError(
                "找不到 npx。请先安装 Node.js/npm，并确认 npx 或 npx.cmd 在 PATH 中。"
            )
        return npx
