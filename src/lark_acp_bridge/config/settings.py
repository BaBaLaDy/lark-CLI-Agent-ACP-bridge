"""TOML configuration loading and saving."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog
import tomli_w
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

logger = structlog.get_logger()


class AgentConfig(BaseModel):
    """Configuration for a single ACP-compatible coding agent."""

    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    description: str = ""

    @property
    def full_command(self) -> list[str]:
        return [self.command] + self.args


class Settings(BaseSettings):
    """Application settings with env-var override support."""

    model_config = SettingsConfigDict(env_prefix="LARK_ACP_", env_file=".env", extra="ignore")

    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    openai_api_key: str = ""
    openai_base_url: str | None = None
    model: str | None = None
    working_dir: Path = Field(default_factory=Path.cwd)
    log_level: str = "INFO"
    # --- New fields ---
    idle_timeout_seconds: int = 300          # Agent 无响应超时（秒）
    card_update_throttle_ms: int = 400       # 卡片流式更新节流间隔（毫秒）
    max_concurrent_runs_per_user: int = 1    # 每用户最大并发 Agent 运行数
    agent_command: list[str] = Field(default_factory=list)  # 自定义 ACP Agent 启动命令（空则用 codex-acp）
    show_tool_calls: bool = True             # 是否在卡片展示工具调用过程
    debounce_ms: int = 600                   # 消息防抖窗口（毫秒），0 表示禁用
    # Multi-agent support
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    active_agent: str = ""

    def resolve_active_agent(self) -> AgentConfig | None:
        """Return the active AgentConfig, or None to fall back to legacy agent_command."""
        if self.active_agent and self.active_agent in self.agents:
            return self.agents[self.active_agent]
        return None


def get_config_path() -> Path:
    return Path.home() / ".lark-acp-bridge" / "config.toml"


def get_agents_json_path(config_path: Path | None = None) -> Path:
    """Return the agents.json path co-located with the TOML config file."""
    base = (config_path or get_config_path()).parent
    return base / "agents.json"


def _parse_agents_json(json_path: Path) -> tuple[dict[str, AgentConfig], str]:
    """Parse an agents.json file using the agent_servers schema.

    Returns (agents_dict, active_agent_name).  active_agent_name is empty
    when the "active" key is absent.
    """
    with open(json_path, encoding="utf-8") as fh:
        data = json.load(fh)

    servers: dict[str, Any] = data.get("agent_servers", {})
    active: str = str(data.get("active", ""))

    agents: dict[str, AgentConfig] = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        agents[name] = AgentConfig(
            command=str(cfg.get("command", "")),
            args=list(cfg.get("args", [])),
            env={str(k): str(v) for k, v in cfg.get("env", {}).items()},
            description=str(cfg.get("description", "")),
        )
    return agents, active


def load_settings(config_path: Path | None = None) -> Settings:
    path = config_path or get_config_path()
    if not path.exists():
        return Settings()

    with open(path, "rb") as file:
        raw = tomllib.load(file)

    # Explicitly map TOML sections to Settings field names.
    # This fixes a prior bug where general.working_dir was flattened to
    # "general_working_dir" which Settings did not recognise.
    feishu = raw.get("feishu", {}) if isinstance(raw.get("feishu"), dict) else {}
    openai = raw.get("openai", {}) if isinstance(raw.get("openai"), dict) else {}
    general = raw.get("general", {}) if isinstance(raw.get("general"), dict) else {}
    agent = raw.get("agent", {}) if isinstance(raw.get("agent"), dict) else {}
    agents_raw = raw.get("agents", {}) if isinstance(raw.get("agents"), dict) else {}

    kwargs: dict[str, Any] = {
        "feishu_app_id": feishu.get("app_id", ""),
        "feishu_app_secret": feishu.get("app_secret", ""),
        "openai_api_key": openai.get("api_key", ""),
    }
    # Only set optional fields when present so Settings defaults are respected.
    if openai.get("base_url") is not None:
        kwargs["openai_base_url"] = openai["base_url"]
    if openai.get("model") is not None:
        kwargs["model"] = openai["model"]
    if general.get("working_dir") is not None:
        kwargs["working_dir"] = general["working_dir"]
    if general.get("log_level") is not None:
        kwargs["log_level"] = general["log_level"]
    if general.get("idle_timeout_seconds") is not None:
        kwargs["idle_timeout_seconds"] = int(general["idle_timeout_seconds"])
    if general.get("card_update_throttle_ms") is not None:
        kwargs["card_update_throttle_ms"] = int(general["card_update_throttle_ms"])
    if general.get("max_concurrent_runs_per_user") is not None:
        kwargs["max_concurrent_runs_per_user"] = int(general["max_concurrent_runs_per_user"])
    if general.get("debounce_ms") is not None:
        kwargs["debounce_ms"] = int(general["debounce_ms"])
    if agent.get("command") is not None:
        cmd = agent["command"]
        kwargs["agent_command"] = cmd if isinstance(cmd, list) else [str(cmd)]
    if agent.get("show_tool_calls") is not None:
        kwargs["show_tool_calls"] = bool(agent["show_tool_calls"])
    # Multi-agent: parse [agents.NAME] sections
    if agents_raw:
        parsed_agents: dict[str, AgentConfig] = {}
        for name, cfg in agents_raw.items():
            if not isinstance(cfg, dict):
                continue
            parsed_agents[name] = AgentConfig(
                command=str(cfg.get("command", "")),
                args=list(cfg.get("args", [])),
                env={str(k): str(v) for k, v in cfg.get("env", {}).items()},
                description=str(cfg.get("description", "")),
            )
        kwargs["agents"] = parsed_agents
    if agent.get("active") is not None:
        kwargs["active_agent"] = str(agent["active"])

    # agents.json (agent_servers format) — loaded after TOML so it takes precedence.
    json_path = get_agents_json_path(path)
    if json_path.exists():
        try:
            json_agents, json_active = _parse_agents_json(json_path)
            merged = dict(kwargs.get("agents", {}))
            merged.update(json_agents)          # JSON entries win over TOML entries
            kwargs["agents"] = merged
            if json_active:                     # JSON active wins over TOML active
                kwargs["active_agent"] = json_active
            logger.debug("agents-json-loaded", path=str(json_path), count=len(json_agents))
        except Exception as exc:
            logger.warning("agents-json-parse-error", path=str(json_path), error=str(exc))

    return Settings(**kwargs)


def save_settings(settings: Settings, config_path: Path | None = None) -> Path:
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "feishu": {"app_id": settings.feishu_app_id, "app_secret": settings.feishu_app_secret},
        "openai": {"api_key": settings.openai_api_key},
        "general": {
            "working_dir": str(settings.working_dir),
            "log_level": settings.log_level,
            "idle_timeout_seconds": settings.idle_timeout_seconds,
            "card_update_throttle_ms": settings.card_update_throttle_ms,
            "max_concurrent_runs_per_user": settings.max_concurrent_runs_per_user,
            "debounce_ms": settings.debounce_ms,
        },
        "agent": {
            "show_tool_calls": settings.show_tool_calls,
        },
    }
    if settings.openai_base_url:
        data["openai"]["base_url"] = settings.openai_base_url
    if settings.model:
        data["openai"]["model"] = settings.model
    if settings.agent_command:
        data["agent"]["command"] = settings.agent_command
    if settings.active_agent:
        data["agent"]["active"] = settings.active_agent
    if settings.agents:
        agents_data: dict[str, Any] = {}
        for name, cfg in settings.agents.items():
            entry: dict[str, Any] = {"command": cfg.command}
            if cfg.args:
                entry["args"] = cfg.args
            if cfg.env:
                entry["env"] = cfg.env
            if cfg.description:
                entry["description"] = cfg.description
            agents_data[name] = entry
        data["agents"] = agents_data

    with open(path, "wb") as file:
        tomli_w.dump(data, file)
    logger.info("config-saved", path=str(path))
    return path
