"""Main CLI entry point."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from shutil import which

import structlog
import typer

from ..acp.codex_bridge import CodexACPBridge
from ..acp.agent_manager import AgentManager
from ..config.workspace_store import WorkspaceStore
from ..config.session_store import SessionStore
from ..bot.feishu_bot import FeishuBot
from ..config.settings import Settings, get_agents_json_path, load_settings, save_settings

logger = structlog.get_logger()

app = typer.Typer(name="lark-acp-bridge", help="飞书与 ACP Agent 的桥接工具", add_completion=False)
daemon_app = typer.Typer(name="daemon", help="管理系统服务（可选 daemon 模式）")
app.add_typer(daemon_app)

# ---------------------------------------------------------------------------
# PID file — ensures only one instance runs at a time
# ---------------------------------------------------------------------------

_PID_FILE = Path.home() / ".lark-acp-bridge" / "bridge.pid"


def _kill_previous_instance() -> None:
    """Terminate any previously running bridge instance before starting this one.

    Reads the PID from the PID file, sends SIGTERM (or taskkill on Windows),
    waits up to 5 seconds for graceful exit, then force-kills if still alive.
    Cleans up stale PID files pointing to non-existent processes.
    """
    if not _PID_FILE.exists():
        return

    try:
        old_pid = int(_PID_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        _PID_FILE.unlink(missing_ok=True)
        return

    # Don't kill ourselves
    if old_pid == os.getpid():
        return

    # Check if the process is still running
    try:
        os.kill(old_pid, 0)  # signal 0 = check existence, no actual signal
    except OSError:
        # Process doesn't exist — stale PID file
        _PID_FILE.unlink(missing_ok=True)
        return

    logger.info("killing-previous-instance", pid=old_pid)
    typer.echo(f"[start] 正在关闭上一个实例 (PID {old_pid})...")

    # Send termination signal
    try:
        if sys.platform == "win32":
            # On Windows, os.kill(pid, SIGTERM) calls TerminateProcess — immediate kill.
            # This is the most reliable way to stop a console Python process.
            os.kill(old_pid, signal.SIGTERM)
        else:
            os.kill(old_pid, signal.SIGTERM)
            # On Unix, SIGTERM is asynchronous — wait for graceful exit.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    os.kill(old_pid, 0)
                    time.sleep(0.2)
                except OSError:
                    break  # Process is gone
            else:
                logger.warning("force-killing-previous-instance", pid=old_pid)
                try:
                    os.kill(old_pid, signal.SIGKILL)
                except OSError:
                    pass
    except OSError:
        pass

    _PID_FILE.unlink(missing_ok=True)
    typer.echo("[start] 上一个实例已关闭")


def _write_pid_file() -> None:
    """Write the current process PID to the PID file."""
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def _remove_pid_file() -> None:
    """Remove the PID file on clean shutdown."""
    _PID_FILE.unlink(missing_ok=True)



# ---------------------------------------------------------------------------
# Agent presets for the interactive init wizard
# ---------------------------------------------------------------------------

_AGENT_PRESETS: list[dict] = [
    {
        "label": "Claude Code     （需要 Anthropic API Key）",
        "name": "claude",
        "command": "claude-agent-acp",
        "npm_pkg": "@agentclientprotocol/claude-agent-acp",
        "env_key": "ANTHROPIC_API_KEY",
        "env_prompt": "ANTHROPIC_API Key（sk-ant-...）",
        "args": [],
        "description": "Claude Code",
    },
    {
        "label": "OpenCode        （multi-provider，npm 全局安装）",
        "name": "opencode",
        "command": "opencode",
        "npm_pkg": "opencode-ai",
        "env_key": None,
        "env_prompt": "",
        "args": ["acp"],
        "description": "OpenCode (multi-provider)",
    },
    {
        "label": "Codex (npx)     （需要 OpenAI API Key，npx 自动下载无需手动安装）",
        "name": "codex",
        "command": "npx",
        "npm_pkg": None,
        "env_key": "OPENAI_API_KEY",
        "env_prompt": "OPENAI API Key（sk-...）",
        "args": ["-y", "@zed-industries/codex-acp"],
        "description": "Codex CLI",
    },
]


def _run_check(cmd: list[str], timeout: int = 5) -> str | None:
    """Run a command and return stripped stdout, or None on failure."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _check_node() -> bool:
    """Check Node.js availability and print result. Returns True if node is available."""
    node_ver = _run_check(["node", "--version"])
    npm_ver = _run_check(["npm", "--version"])
    if node_ver and npm_ver:
        typer.echo(f"  ✅ Node.js {node_ver}（npm {npm_ver}）")
        return True
    typer.echo("  ❌ Node.js 未找到")
    typer.echo("     大部分 ACP Agent CLI 需要 Node.js 才能安装和运行。")
    typer.echo("     下载地址：https://nodejs.org/")
    return False


def _resolve_command(cmd: str) -> str | None:
    """Return the resolved path of a command, or None if not found."""
    resolved = which(cmd)
    if resolved:
        return resolved
    if sys.platform == "win32":
        for ext in (".cmd", ".ps1", ".bat"):
            resolved = which(cmd + ext)
            if resolved:
                return resolved
    return None


@app.command()
def init(
    skip_checks: bool = typer.Option(False, "--skip-checks", help="跳过环境检查（非交互场景）"),
    force: bool = typer.Option(False, "--force", "-f", help="强制重新配置，忽略已有配置文件"),
) -> None:
    """初始化配置文件（交互式向导）。

    引导你完成环境检查、Agent 选择和飞书凭证配置，
    将结果写入 ~/.lark-acp-bridge/config.toml 和 agents.json。
    若配置文件已存在，默认跳过向导；使用 --force 强制重新配置。
    """
    from ..config.settings import get_config_path

    config_path = get_config_path()
    agents_json_path = get_agents_json_path(config_path)

    typer.echo("\n🚀  Lark ACP Bridge 初始化向导")
    typer.echo("═" * 36)

    # ── 0. 检测已有配置 ──────────────────────────────────────────────────────
    if config_path.exists() and not force:
        typer.echo(f"\n📁  检测到已有配置：{config_path}")
        existing = load_settings(config_path)
        typer.echo(f"   飞书 App ID : {existing.feishu_app_id or '（未设置）'}")
        typer.echo(f"   工作目录    : {existing.working_dir}")
        if existing.agents:
            active_marker = f" ◀ active" if existing.active_agent else ""
            typer.echo(f"   已配置 Agent: {', '.join(existing.agents.keys())}{active_marker}")
        if agents_json_path.exists():
            typer.echo(f"   agents.json : {agents_json_path}")

        reconfigure = typer.confirm("\n   是否重新配置？", default=False)
        if not reconfigure:
            typer.echo("\n✅  保留现有配置，跳过初始化向导。")
            typer.echo("   如需重新配置，运行：lark-acp-bridge init --force")
            typer.echo()
            return

    # ── 1. 环境检查 ──────────────────────────────────────────────────────────
    if not skip_checks:
        typer.echo("\n🔍  检查运行环境...")
        v = sys.version_info
        typer.echo(f"  ✅ Python {v.major}.{v.minor}.{v.micro}")
        has_node = _check_node()
    else:
        has_node = _run_check(["node", "--version"]) is not None

    # ── 2. 选择 Agent ────────────────────────────────────────────────────────
    typer.echo("\n─────────────────────────────────────")
    typer.echo("🤖  选择 ACP Agent")
    typer.echo("─────────────────────────────────────")
    for i, p in enumerate(_AGENT_PRESETS, 1):
        typer.echo(f"  {i}. {p['label']}")
    typer.echo(f"  {len(_AGENT_PRESETS) + 1}. 跳过（稍后手动编辑 agents.json）")

    choice = typer.prompt("选择", default="1", show_default=False)
    try:
        idx = int(choice) - 1
    except ValueError:
        idx = -1

    selected_preset: dict | None = None
    if 0 <= idx < len(_AGENT_PRESETS):
        selected_preset = _AGENT_PRESETS[idx]
    else:
        typer.echo("  ⏭  跳过 Agent 配置，请稍后手动编辑 ~/.lark-acp-bridge/agents.json")

    agent_name: str = ""
    agent_env: dict[str, str] = {}
    agent_installed = False

    if selected_preset is not None:
        name = selected_preset["name"]
        command = selected_preset["command"]
        agent_name = name

        # Check if the CLI is already installed
        typer.echo(f"\n  📦  检查 {command} ...")
        resolved = _resolve_command(command)

        if resolved:
            typer.echo(f"  ✅ 已找到：{resolved}")
            agent_installed = True
        elif selected_preset["npm_pkg"] and has_node:
            typer.echo(f"  ❌ 未检测到 {command}")
            install = typer.confirm(f"  是否现在安装？（npm install -g {selected_preset['npm_pkg']}）", default=True)
            if install:
                typer.echo(f"  ▶ npm install -g {selected_preset['npm_pkg']}")
                try:
                    subprocess.run(
                        ["npm", "install", "-g", selected_preset["npm_pkg"]],
                        check=True,
                        timeout=120,
                    )
                    # Verify after install
                    if _resolve_command(command):
                        typer.echo("  ✅ 安装完成")
                        agent_installed = True
                    else:
                        typer.echo(f"  ⚠️  npm 安装完成，但未找到 {command} 命令，请检查 npm 全局 bin 目录是否在 PATH 中")
                except subprocess.CalledProcessError as exc:
                    typer.echo(f"  ❌ 安装失败（exit {exc.returncode}），请稍后手动安装")
                except subprocess.TimeoutExpired:
                    typer.echo("  ❌ 安装超时（120秒），请稍后手动安装")
        elif selected_preset["command"] == "npx":
            # npx is always available if node is installed
            if has_node:
                typer.echo("  ✅ npx 可用（首次使用时自动下载 agent 包）")
                agent_installed = True
            else:
                typer.echo("  ❌ npx 不可用，请先安装 Node.js")

        # Prompt for env key (API key)
        if selected_preset["env_key"]:
            api_key = typer.prompt(
                f"\n  {selected_preset['env_prompt']}",
                default="",
                hide_input=True,
            )
            if api_key:
                agent_env[selected_preset["env_key"]] = api_key

    # ── 3. 飞书配置 ──────────────────────────────────────────────────────────
    typer.echo("\n─────────────────────────────────────")
    typer.echo("📱  飞书应用配置")
    typer.echo("─────────────────────────────────────")
    feishu_app_id = typer.prompt("  App ID（cli_xxxx）")
    feishu_app_secret = typer.prompt("  App Secret", hide_input=True)

    # ── 4. 工作目录 ──────────────────────────────────────────────────────────
    typer.echo("\n─────────────────────────────────────")
    working_dir_str = typer.prompt(
        "📁  工作目录（留空使用当前目录）",
        default=str(Path.cwd()),
        show_default=False,
    )
    working_dir = Path(working_dir_str) if working_dir_str else Path.cwd()

    # ── 5. 保存 config.toml ──────────────────────────────────────────────────
    settings = Settings(
        feishu_app_id=feishu_app_id,
        feishu_app_secret=feishu_app_secret,
        working_dir=working_dir,
    )
    config_path = save_settings(settings)

    # ── 6. 写入 agents.json ─────────────────────────────────────────────────
    agents_json_path = get_agents_json_path(config_path)
    agents_json_written = False

    if selected_preset is not None:
        if agents_json_path.exists():
            overwrite = typer.confirm(f"\n  {agents_json_path} 已存在，是否覆盖？", default=False)
            if not overwrite:
                typer.echo("  ⏭  跳过 agents.json，保留现有配置")
                selected_preset = None  # skip writing

        if selected_preset is not None:
            agents_data: dict = {
                "active": agent_name,
                "agent_servers": {
                    agent_name: {
                        "command": selected_preset["command"],
                        "args": selected_preset["args"],
                        "description": selected_preset["description"],
                    }
                },
            }
            if agent_env:
                agents_data["agent_servers"][agent_name]["env"] = agent_env

            agents_json_path.parent.mkdir(parents=True, exist_ok=True)
            with open(agents_json_path, "w", encoding="utf-8") as f:
                json.dump(agents_data, f, indent=2, ensure_ascii=False)
            agents_json_written = True

    # ── 7. 输出结果 ──────────────────────────────────────────────────────────
    typer.echo("\n─────────────────────────────────────")
    typer.echo("✅  配置已写入：")
    typer.echo(f"   {config_path}")
    if agents_json_written:
        typer.echo(f"   {agents_json_path}")

    if selected_preset and not agent_installed:
        typer.echo(f"\n⚠️  注意：{selected_preset['command']} 尚未安装，启动服务前请先确保已安装。")

    typer.echo("\n🚀  下一步：运行  lark-acp-bridge start")
    typer.echo()


@app.command()
def start(
    mode: str = typer.Option("ws", help="接入模式：ws 长连接，webhook 回调"),
    webhook_port: int = typer.Option(8080, help="Webhook 服务器端口，仅 webhook 模式使用"),
    agent_type: str = typer.Option(
        "codex",
        help="ACP Agent 类型：codex 使用内置 Codex ACP；custom 使用自定义命令（需配合 --agent-command）",
    ),
    agent_command: str = typer.Option(
        "",
        help="自定义 ACP Agent 启动命令（仅 agent_type=custom 时使用），例如：'python my_agent.py'",
    ),
    daemon: bool = typer.Option(False, "--daemon", "-d", help="注册为系统服务并在后台运行"),
) -> None:
    """启动桥接服务。"""
    settings = load_settings()

    # Resolve agent command, env, and display name.
    # Priority: new [agents.*]+active config > legacy CLI --agent-type > default codex.
    resolved_agent_command: list[str] | None = None
    resolved_agent_env: dict[str, str] = {}
    resolved_agent_name: str = "codex-acp"

    agent_cfg = settings.resolve_active_agent()
    if agent_cfg:
        resolved_agent_command = agent_cfg.full_command
        resolved_agent_env = agent_cfg.env
        resolved_agent_name = agent_cfg.description or settings.active_agent
    elif agent_type == "custom":
        if agent_command:
            resolved_agent_command = agent_command.split()
        elif settings.agent_command:
            resolved_agent_command = settings.agent_command
        else:
            typer.echo("❌ agent_type=custom 需要提供 --agent-command 或配置文件中的 agent.command", err=True)
            raise typer.Exit(1)
        resolved_agent_name = resolved_agent_command[0] if resolved_agent_command else "custom"
    elif agent_type != "codex":
        typer.echo(f"❌ 未知 agent_type: {agent_type}。支持: codex, custom", err=True)
        raise typer.Exit(1)

    # -- Daemon mode: register service and exit --------------------------------
    if daemon:
        _start_daemon(settings, mode, webhook_port, resolved_agent_command, resolved_agent_env)
        return

    # -- Kill any previous bridge instance before starting this one -----------
    _kill_previous_instance()
    _write_pid_file()

    async def run_bridge() -> None:
        # Build AgentManager if agents are configured in agents.json
        agents_json_path = get_agents_json_path()
        agent_manager: AgentManager | None = None
        workspace_store = WorkspaceStore()
        session_store = SessionStore()

        if agents_json_path.exists() or settings.agents:
            agent_manager = AgentManager(
                working_dir=str(settings.working_dir),
                api_key=settings.openai_api_key or None,
                base_url=settings.openai_base_url,
                model=settings.model,
            )
            # Register agents from settings.agents (TOML + agents.json merged)
            for name, cfg in settings.agents.items():
                agent_manager.register_agent(name, cfg)
            # Set the default active agent from settings
            if settings.active_agent and settings.active_agent in settings.agents:
                agent_manager._default_agent = settings.active_agent
            await agent_manager.load_agents(agents_json_path)
            agent_count = len(agent_manager.registered_names)
            typer.echo(f"[agents] Multi-agent mode: {agent_count} agent(s) registered, default: {agent_manager._default_agent}")

        # Legacy single-bridge fallback
        bridge: CodexACPBridge | None = None
        if agent_manager is None:
            bridge = CodexACPBridge(
                working_dir=str(settings.working_dir),
                api_key=settings.openai_api_key or None,
                base_url=settings.openai_base_url,
                model=settings.model,
                agent_command=resolved_agent_command,
                agent_env=resolved_agent_env,
            )
            await bridge.start()
            typer.echo(f"[start] Bridge started (mode={mode}, agent={resolved_agent_name})")

        bot = FeishuBot(
            settings=settings,
            codex_bridge=bridge,
            agent_name=resolved_agent_name,
            agent_manager=agent_manager,
            workspace_store=workspace_store,
            session_store=session_store,
        )

        # Register signal handlers for graceful shutdown
        shutdown_event = asyncio.Event()

        def _on_signal() -> None:
            logger.info("signal-received-shutting-down")
            shutdown_event.set()

        loop = asyncio.get_running_loop()
        if sys.platform != "win32":
            loop.add_signal_handler(signal.SIGTERM, _on_signal)
            loop.add_signal_handler(signal.SIGINT, _on_signal)
        else:
            signal.signal(signal.SIGTERM, lambda *_: loop.call_soon_threadsafe(_on_signal))

        # Start bot in a task so we can wait for shutdown_event
        bot_task = asyncio.create_task(
            _run_bot(bot, mode, webhook_port), name="bot-main"
        )

        # Wait for shutdown signal, then clean up
        await shutdown_event.wait()
        logger.info("shutting-down-gracefully")
        await bot.shutdown()
        bot_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bot_task
        if bridge is not None:
            await bridge.stop()
        if agent_manager is not None:
            await agent_manager.stop_all()
        _remove_pid_file()
        logger.info("shutdown-complete")

    _run_async(run_bridge())
    typer.echo("\n[exit] 服务已退出。")


async def _run_bot(bot: FeishuBot, mode: str, webhook_port: int) -> None:
    """Run the bot (ws or webhook) until cancelled."""
    try:
        if mode == "ws":
            await bot.start_ws_client()
        elif mode == "webhook":
            await bot.start_webhook_server(port=webhook_port)
        else:
            raise ValueError("mode 必须是 ws 或 webhook")
    except asyncio.CancelledError:
        pass


def _start_daemon(
    settings: Settings,
    mode: str,
    webhook_port: int,
    agent_command: list[str] | None,
    agent_env: dict[str, str],
) -> None:
    """Install and start the daemon service."""
    from ..daemon import get_daemon_adapter

    try:
        adapter = get_daemon_adapter()
    except RuntimeError as exc:
        typer.echo(f"❌ {exc}", err=True)
        raise typer.Exit(1) from exc

    # Build the foreground command that the daemon will run.
    exe = sys.executable
    args = ["-m", "lark_acp_bridge.cli.main", "start", "--mode", mode]
    if mode == "webhook":
        args.extend(["--webhook-port", str(webhook_port)])

    try:
        adapter.install(exe, args, str(settings.working_dir))
        adapter.start()
        status = adapter.status()
        typer.echo(f"✅ 服务已安装并启动 (status: {status})")
    except Exception as exc:
        typer.echo(f"❌ daemon 启动失败: {exc}", err=True)
        raise typer.Exit(1) from exc


@daemon_app.command("install")
def daemon_install(
    mode: str = typer.Option("ws", help="接入模式：ws 长连接，webhook 回调"),
    webhook_port: int = typer.Option(8080, help="Webhook 服务器端口"),
) -> None:
    """仅注册系统服务（不启动）。"""
    from ..daemon import get_daemon_adapter

    settings = load_settings()
    try:
        adapter = get_daemon_adapter()
    except RuntimeError as exc:
        typer.echo(f"❌ {exc}", err=True)
        raise typer.Exit(1) from exc

    exe = sys.executable
    args = ["-m", "lark_acp_bridge.cli.main", "start", "--mode", mode]
    if mode == "webhook":
        args.extend(["--webhook-port", str(webhook_port)])

    try:
        adapter.install(exe, args, str(settings.working_dir))
        typer.echo(f"✅ 服务已注册: {adapter.SERVICE_NAME}")
    except Exception as exc:
        typer.echo(f"❌ 注册失败: {exc}", err=True)
        raise typer.Exit(1) from exc


@daemon_app.command("uninstall")
def daemon_uninstall() -> None:
    """停止并卸载系统服务。"""
    from ..daemon import get_daemon_adapter

    try:
        adapter = get_daemon_adapter()
    except RuntimeError as exc:
        typer.echo(f"❌ {exc}", err=True)
        raise typer.Exit(1) from exc

    try:
        adapter.uninstall()
        typer.echo(f"✅ 服务已卸载: {adapter.SERVICE_NAME}")
    except Exception as exc:
        typer.echo(f"❌ 卸载失败: {exc}", err=True)
        raise typer.Exit(1) from exc


@daemon_app.command("status")
def daemon_status() -> None:
    """查询系统服务状态。"""
    from ..daemon import get_daemon_adapter

    try:
        adapter = get_daemon_adapter()
    except RuntimeError as exc:
        typer.echo(f"❌ {exc}", err=True)
        raise typer.Exit(1) from exc

    status = adapter.status()
    typer.echo(f"服务: {adapter.SERVICE_NAME}")
    typer.echo(f"状态: {status}")


@app.command()
def test(message: str = typer.Argument(..., help="测试消息")) -> None:
    """测试 ACP Agent 连接，不启动飞书服务。

    实时在控制台输出 Agent 返回的文本片段，便于调试 Agent 侧问题。
    """
    settings = load_settings()

    async def run_test() -> None:
        agent_cfg = settings.resolve_active_agent()
        test_command = agent_cfg.full_command if agent_cfg else (settings.agent_command or None)
        test_env = agent_cfg.env if agent_cfg else {}
        bridge = CodexACPBridge(
            working_dir=str(settings.working_dir),
            api_key=settings.openai_api_key or None,
            base_url=settings.openai_base_url,
            model=settings.model,
            agent_command=test_command,
            agent_env=test_env,
        )

        # Real-time streaming output to stdout
        def on_text(delta: str) -> None:
            sys.stdout.write(delta)
            sys.stdout.flush()

        try:
            await bridge.start()
            typer.echo("--- ACP Agent 连接成功，发送消息 ---")
            state = await asyncio.wait_for(
                bridge.chat(message=message, user_id="test-user", on_text=on_text),
                timeout=float(settings.idle_timeout_seconds),
            )
            typer.echo("\n--- 完成 ---")
            if state.input_tokens or state.output_tokens:
                typer.echo(f"tokens: input={state.input_tokens}  output={state.output_tokens}")
        except asyncio.TimeoutError:
            typer.echo(f"\n❌ 超时（{settings.idle_timeout_seconds}秒无响应）", err=True)
            raise typer.Exit(1)
        except Exception as exc:
            typer.echo(f"\n❌ 失败: {exc}", err=True)
            raise typer.Exit(1)
        finally:
            await bridge.stop()

    asyncio.run(run_test())


@app.command()
def config() -> None:
    """显示当前配置。"""
    settings = load_settings()
    typer.echo(f"飞书应用 ID      : {settings.feishu_app_id}")
    typer.echo("飞书应用密钥     : ********")
    typer.echo(f"OpenAI API 密钥  : {'已配置' if settings.openai_api_key else '未配置，使用本机 Codex 登录态'}")
    typer.echo(f"OpenAI Base URL  : {settings.openai_base_url or '默认'}")
    typer.echo(f"工作目录         : {settings.working_dir}")
    typer.echo(f"模型             : {settings.model or '默认'}")
    typer.echo(f"超时（秒）       : {settings.idle_timeout_seconds}")
    typer.echo(f"卡片节流（ms）   : {settings.card_update_throttle_ms}")
    typer.echo(f"消息防抖（ms）   : {settings.debounce_ms}")
    typer.echo(f"每用户最大并发   : {settings.max_concurrent_runs_per_user}")
    typer.echo(f"展示工具调用     : {'是' if settings.show_tool_calls else '否'}")
    typer.echo(f"自定义Agent命令  : {settings.agent_command or '未配置（使用 codex-acp）'}")
    json_path = get_agents_json_path()
    typer.echo(f"Agents JSON      : {json_path} {'✅' if json_path.exists() else '（未创建）'}")
    if settings.agents:
        typer.echo(f"Active Agent     : {settings.active_agent or '未设置'}")
        typer.echo("已配置 Agents    :")
        for name, cfg in settings.agents.items():
            marker = " ◀ active" if name == settings.active_agent else ""
            typer.echo(f"  [{name}]{marker}  {' '.join(cfg.full_command)}  {cfg.description}")


@app.command()
def version() -> None:
    """显示当前版本。"""
    from .. import __version__
    typer.echo(f"lark-acp-bridge {__version__}")


def _run_async(coro) -> None:
    """asyncio.run() replacement that avoids Windows asyncgen cleanup crashes.

    On Windows, asyncio.run()'s _on_sigint can raise KeyboardInterrupt while an
    async generator's finally block is already executing, leaving it in an
    "already running" state that triggers RuntimeError during loop shutdown.
    Manually managing the loop lets us cancel tasks first, then drain async
    generators cleanly before closing.
    """
    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
    else:
        loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(coro)
    except KeyboardInterrupt:
        pass
    finally:
        with contextlib.suppress(Exception):
            pending = {t for t in asyncio.all_tasks(loop) if not t.done()}
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        with contextlib.suppress(Exception):
            loop.run_until_complete(loop.shutdown_asyncgens())
        with contextlib.suppress(Exception):
            loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()
        asyncio.set_event_loop(None)


def main() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    app()


if __name__ == "__main__":
    main()
