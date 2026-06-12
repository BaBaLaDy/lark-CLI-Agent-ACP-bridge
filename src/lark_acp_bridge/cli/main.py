"""Main CLI entry point."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from pathlib import Path

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


@app.command()
def init(
    feishu_app_id: str = typer.Option(..., prompt=True, help="飞书应用 ID"),
    feishu_app_secret: str = typer.Option(..., prompt=True, hide_input=True, help="飞书应用密钥"),
    openai_api_key: str = typer.Option(
        "",
        prompt="OpenAI API 密钥（可留空，留空时使用本机 Codex 登录态）",
        hide_input=True,
        help="OpenAI API 密钥，可留空以复用本机 Codex 配置",
    ),
    openai_base_url: str = typer.Option("", help="OpenAI 兼容服务的 Base URL，可选"),
    working_dir: Path = typer.Option(Path.cwd(), help="工作目录"),
    idle_timeout_seconds: int = typer.Option(300, help="Agent 无响应超时时间（秒）"),
) -> None:
    """初始化配置文件。"""
    path = save_settings(
        Settings(
            feishu_app_id=feishu_app_id,
            feishu_app_secret=feishu_app_secret,
            openai_api_key=openai_api_key,
            openai_base_url=openai_base_url or None,
            working_dir=working_dir,
            idle_timeout_seconds=idle_timeout_seconds,
        )
    )
    typer.echo(f"配置已保存到 {path}")


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
            typer.echo(f"🤖 Multi-agent mode: {agent_count} agent(s) registered, default: {agent_manager._default_agent}")

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
            typer.echo(f"✅ 桥接服务已启动 (mode={mode}, agent={resolved_agent_name})")

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
        logger.info("shutdown-complete")

    _run_async(run_bridge())
    typer.echo("\n⛔ 服务已退出。")


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
