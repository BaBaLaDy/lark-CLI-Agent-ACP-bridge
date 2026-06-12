"""AgentManager: multi-agent registry with per-scope routing and lazy bridge startup."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from ..acp.client import SessionState
from ..acp.codex_bridge import CodexACPBridge
from ..config.settings import AgentConfig, get_agents_json_path

logger = structlog.get_logger()


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #

@dataclass
class AgentInfo:
    """Public snapshot of one registered agent."""

    name: str
    description: str
    command: list[str]
    running: bool


# --------------------------------------------------------------------------- #
# AgentManager
# --------------------------------------------------------------------------- #

class AgentManager:
    """Manages multiple ACP-compatible agents with per-scope active routing.

    Agents are lazily started: the underlying subprocess (CodexACPBridge) is
    created only on the first call to ``get_bridge(name)``.  Subsequent calls
    return the existing bridge.

    Scope rules (Option C from design):
    - DM (chat_type "p2p"): scope key = ``user_id``
    - Group (chat_type "group" or "topic"): scope key = ``chat_id``
    """

    def __init__(
        self,
        working_dir: str,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ):
        self._working_dir = working_dir
        self._api_key = api_key
        self._base_url = base_url
        self._model = model

        # Registered agents: name → AgentConfig
        self._registry: dict[str, AgentConfig] = {}
        # Lazily-started bridges: name → CodexACPBridge
        self._bridges: dict[str, CodexACPBridge] = {}
        # Per-scope active agent: scope_key → agent_name
        self._scope_agent: dict[str, str] = {}
        # Global default: set during load_agents()
        self._default_agent: str = ""

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #

    async def load_agents(self, agents_json_path: Path | None = None) -> None:
        """Load agents from agents.json. Falls back to empty registry if missing."""
        path = agents_json_path or get_agents_json_path()
        if path.exists():
            try:
                from ..config.settings import _parse_agents_json

                agents, active = _parse_agents_json(path)
                self._registry.update(agents)
                if active:
                    self._default_agent = active
                elif not self._default_agent and self._registry:
                    # Use first agent as default if no "active" key
                    self._default_agent = next(iter(self._registry))
                logger.info(
                    "agent-manager-loaded",
                    path=str(path),
                    count=len(agents),
                    default=self._default_agent,
                )
            except Exception as exc:
                logger.warning("agent-manager-load-failed", error=str(exc), exc_info=True)

    def register_agent(self, name: str, config: AgentConfig) -> None:
        """Register an agent config (for programmatic or fallback usage)."""
        self._registry[name] = config
        if not self._default_agent:
            self._default_agent = name

    def has_agent(self, name: str) -> bool:
        """Return True if agent ``name`` is registered."""
        return name in self._registry

    @property
    def registered_names(self) -> list[str]:
        return list(self._registry.keys())

    # ------------------------------------------------------------------ #
    # Scope helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def scope_key(user_id: str, chat_id: str, chat_type: str) -> str:
        """Derive the scope key from event metadata."""
        if chat_type == "p2p":
            return f"user:{user_id}"
        return f"chat:{chat_id}"

    # ------------------------------------------------------------------ #
    # Active agent per scope
    # ------------------------------------------------------------------ #

    def active_agent_for(self, user_id: str, chat_id: str, chat_type: str) -> str:
        """Return the active agent name for the given scope, or the global default."""
        scope = self.scope_key(user_id, chat_id, chat_type)
        return self._scope_agent.get(scope, self._default_agent)

    def set_active_agent(self, scope: str, name: str) -> None:
        """Set the active agent for a scope. Raises ValueError if name not registered."""
        if name not in self._registry:
            raise ValueError(f"未知 agent: {name!r}。可用: {', '.join(self._registry) or '（无）'}")
        self._scope_agent[scope] = name
        logger.info("agent-switched", scope=scope, agent=name)

    # ------------------------------------------------------------------ #
    # Bridge lifecycle (lazy start)
    # ------------------------------------------------------------------ #

    async def get_bridge(self, name: str) -> CodexACPBridge:
        """Return the running bridge for ``name``, starting it lazily if needed.

        Raises ValueError if name is not registered.
        Raises RuntimeError if the bridge fails to start.
        """
        if name not in self._registry:
            raise ValueError(f"未知 agent: {name!r}。可用: {', '.join(self._registry) or '（无）'}")

        existing = self._bridges.get(name)
        if existing is not None:
            if existing.is_running:
                return existing
            # Bridge died — remove and recreate
            logger.warning("bridge-died-recreating", agent=name)
            del self._bridges[name]

        cfg = self._registry[name]
        bridge = CodexACPBridge(
            working_dir=self._working_dir,
            api_key=self._api_key,
            base_url=self._base_url,
            model=self._model,
            agent_command=cfg.full_command,
            agent_env=cfg.env,
            on_death=lambda b, _n=name: logger.warning("agent-died", agent=_n),
        )
        await bridge.start()
        self._bridges[name] = bridge
        logger.info("bridge-started", agent=name, command=cfg.full_command)
        return bridge

    async def stop_all(self) -> None:
        """Stop all running bridges."""
        for name, bridge in list(self._bridges.items()):
            try:
                await bridge.stop()
                logger.info("bridge-stopped", agent=name)
            except Exception as exc:
                logger.warning("bridge-stop-failed", agent=name, error=str(exc))
        self._bridges.clear()

    # ------------------------------------------------------------------ #
    # Info
    # ------------------------------------------------------------------ #

    def list_agents(self) -> list[AgentInfo]:
        """Return info for all registered agents."""
        return [
            AgentInfo(
                name=name,
                description=cfg.description or name,
                command=cfg.full_command,
                running=name in self._bridges and self._bridges[name].is_running,
            )
            for name, cfg in self._registry.items()
        ]

    # ------------------------------------------------------------------ #
    # Chat / cancel / session proxies
    # ------------------------------------------------------------------ #

    async def chat(
        self,
        message: str,
        agent_name: str,
        user_id: str | None = None,
        session_id: str | None = None,
        on_text: Callable[[str], None] | None = None,
        on_state_change: Callable[[SessionState], None] | None = None,
        on_session_reset: Callable[[], None] | None = None,
    ) -> SessionState:
        """Route a chat message to the named agent bridge.

        ``on_session_reset`` is forwarded to the bridge and called when
        a new session had to be created (context lost).
        """
        bridge = await self.get_bridge(agent_name)
        return await bridge.chat(
            message=message,
            user_id=user_id,
            session_id=session_id,
            on_text=on_text,
            on_state_change=on_state_change,
            on_session_reset=on_session_reset,
        )

    async def cancel(
        self,
        agent_name: str,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Cancel the active run for the named agent bridge."""
        bridge = self._bridges.get(agent_name)
        if bridge is not None:
            await bridge.cancel(user_id=user_id, session_id=session_id)

    async def close_session(
        self,
        agent_name: str,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Close the user session for the named agent bridge."""
        bridge = self._bridges.get(agent_name)
        if bridge is not None:
            await bridge.close_session(user_id=user_id, session_id=session_id)

    async def create_session(
        self,
        agent_name: str,
        user_id: str | None = None,
    ) -> str:
        """Create a new session for the named agent bridge."""
        bridge = await self.get_bridge(agent_name)
        return await bridge.create_session(user_id=user_id)

    async def load_session(
        self,
        agent_name: str,
        session_id: str,
        user_id: str | None = None,
    ) -> str:
        """Resume an existing session on the named agent bridge."""
        bridge = await self.get_bridge(agent_name)
        return await bridge.load_session(session_id, user_id=user_id)

    @property
    def is_running(self) -> bool:
        """True if any bridge is currently running."""
        return any(b.is_running for b in self._bridges.values())

    def active_session_count(self, agent_name: str) -> int:
        """Return active session count for a specific agent bridge."""
        bridge = self._bridges.get(agent_name)
        return bridge.active_session_count if bridge else 0
