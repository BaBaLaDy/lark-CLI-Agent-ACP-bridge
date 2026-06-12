"""Static card templates for slash commands.

These cards use **Schema 1.x** format (Feishu card JSON without the `schema` field,
with `header`/`elements` at top level). Text uses `lark_md` inside `div` elements.
Buttons carry a `value` dict with `cmd` key for callback routing.

Templates implemented (matching the TypeScript originals):
- ``help_card()`` — /help command
- ``status_card(info)`` — /status command
- ``resume_card(entries)`` — /resume command
- ``workspaces_card(current_dir, workspaces)`` — /ws list command
"""

from __future__ import annotations

from typing import Any


# --------------------------------------------------------------------------- #
# Public card builders
# --------------------------------------------------------------------------- #

def help_card() -> dict[str, Any]:
    """Render the /help card with command list and quick-access buttons."""
    commands_text = (
        "**可用命令：**\n\n"
        "| 命令 | 说明 |\n"
        "|------|------|\n"
        "| `/new` | 创建新 ACP 会话，重置对话上下文 |\n"
        "| `/new chat [name]` | 拉群聊，继承当前工作目录 |\n"
        "| `/stop` | 中断当前正在运行的 Agent |\n"
        "| `/status` | 查看当前运行状态和会话信息 |\n"
        "| `/agent list` | 查看所有可用 Agent |\n"
        "| `/agent use <name>` | 切换到指定 Agent |\n"
        "| `<name>: <msg>` | 临时路由到指定 Agent（不改默认）|\n"
        "| `/cd <path>` | 切换当前工作目录 |\n"
        "| `/ws` | 管理命名工作空间 |\n"
        "| `/resume` | 查看历史会话列表 |\n"
        "| `/help` | 显示本帮助信息 |\n\n"
        "直接发送消息即可与 Agent 对话。"
    )
    return _s1_card(
        title="💡 使用帮助",
        elements=[
            _div_md(commands_text),
            _hr(),
            _action_row([
                _button("📊 状态", {"cmd": "status"}, type="primary"),
                _button("🤖 Agent", {"cmd": "agent.list"}, type="default"),
                _button("🔁 恢复会话", {"cmd": "resume"}, type="default"),
                _button("🆕 新会话", {"cmd": "new"}, type="default"),
            ]),
        ],
    )


def agent_list_card(agents: list[dict[str, Any]], active: str) -> dict[str, Any]:
    """Render the /agent list card.

    Each agent dict: name, description, running, active.
    """
    elements: list[dict[str, Any]] = []
    for a in agents:
        marker = " ◀ **当前**" if a["name"] == active else ""
        status = "🟢" if a.get("running") else "⚪"
        elements.append(_div_md(f"{status} **{a['name']}**: {a['description']}{marker}"))
        if a["name"] != active:
            elements.append(_action_row([
                _button(f"切换到 {a['name']}", {"cmd": "agent.use", "name": a["name"]}, type="primary"),
            ]))
        elements.append(_hr())
    if elements and elements[-1].get("tag") == "hr":
        elements.pop()  # Remove trailing hr
    return _s1_card(title="🤖 可用 Agent", elements=elements)


def agent_switched_card(name: str, description: str, session_id: str) -> dict[str, Any]:
    """Render confirmation card after agent switch."""
    text = (
        f"✅ 已切换到 **{name}**\n\n"
        f"描述：{description}\n"
        f"会话 ID：`{session_id}`\n"
        f"当前会话已重置。"
    )
    return _s1_card(
        title="🤖 Agent 已切换",
        elements=[
            _div_md(text),
            _hr(),
            _action_row([
                _button("📊 状态", {"cmd": "status"}, type="default"),
                _button("💡 帮助", {"cmd": "help"}, type="default"),
            ]),
        ],
    )


def status_card(info: dict[str, Any]) -> dict[str, Any]:
    """Render the /status card with current bridge and session info.

    ``info`` dict fields:
    - ``working_dir``: str — current working directory
    - ``session_count``: int — number of active ACP sessions
    - ``active_run``: bool — whether a run is currently in progress
    - ``bridge_running``: bool — whether the bridge process is online
    - ``agent_type``: str — "codex" or "custom"
    - ``agent_command``: str — display string for agent command
    """
    working_dir = info.get("working_dir", "未知")
    session_count = info.get("session_count", 0)
    active_run = info.get("active_run", False)
    bridge_running = info.get("bridge_running", False)
    agent_type = info.get("agent_type", "codex")
    agent_command = info.get("agent_command", "@zed-industries/codex-acp")

    status_lines = [
        f"🧩 **Bridge 状态：** {'✅ 运行中' if bridge_running else '⛔ 未启动'}",
        f"🤖 **Agent 类型：** {agent_type}",
        f"📁 **工作目录：** `{working_dir}`",
        f"🔗 **活跃会话：** {session_count}",
        f"🏃 **当前运行：** {'⏳ 正在执行…' if active_run else '✅ 空闲'}",
        f"⚙️ **Agent 命令：** `{agent_command}`",
    ]
    return _s1_card(
        title="📊 当前状态",
        elements=[
            _div_md("\n".join(status_lines)),
            _hr(),
            _action_row([
                _button("🆕 新会话", {"cmd": "new"}, type="primary"),
                _button("🔁 恢复会话", {"cmd": "resume"}, type="default"),
                _button("📂 工作目录", {"cmd": "ws.list"}, type="default"),
                _button("💡 帮助", {"cmd": "help"}, type="default"),
            ]),
        ],
    )


def resume_card(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Render the /resume card showing historical sessions.

    Each entry dict should contain:
    - ``session_id``: str
    - ``preview``: str — short preview of last message (≤60 chars)
    - ``is_current``: bool — whether this is the active session
    """
    elements: list[dict[str, Any]] = []

    if not entries:
        elements.append(_div_md("此工作目录下没有历史会话。"))
    else:
        for i, entry in enumerate(entries, 1):
            session_id = entry.get("session_id", "")
            preview = entry.get("preview", "无预览")
            is_current = entry.get("is_current", False)

            current_marker = " ← 当前" if is_current else ""
            entry_text = f"**{i}.** {preview}{current_marker}\n`{session_id[:8]}…`"
            elements.append(_div_md(entry_text))

            if is_current:
                btn = _button("已是当前会话", {"cmd": "resume.use", "arg": session_id}, type="default")
            else:
                btn = _button("▸ 恢复此会话", {"cmd": "resume.use", "arg": session_id}, type="primary")
            elements.append(_action_row([btn]))

            if i < len(entries):
                elements.append(_hr())

    return _s1_card(title="🔁 历史会话", elements=elements)


def workspaces_card(current_dir: str, workspaces: list[dict[str, Any]]) -> dict[str, Any]:
    """Render the /ws list card showing saved workspaces.

    Each workspace dict should contain:
    - ``name``: str — display name
    - ``path``: str — filesystem path
    """
    elements: list[dict[str, Any]] = [
        _div_md(f"当前 cwd: `{current_dir}`"),
        _hr(),
    ]

    if not workspaces:
        elements.append(_div_md("暂无命名工作目录。"))
        elements.append(_div_md("提示：可通过配置 `working_dir` 修改工作目录。"))
    else:
        for i, ws in enumerate(workspaces):
            name = ws.get("name", "")
            path = ws.get("path", "")
            is_current = (path == current_dir)
            current_marker = " ← 当前" if is_current else ""
            elements.append(_div_md(f"**{name}** → `{path}`{current_marker}"))

            if is_current:
                switch_btn = _button("当前目录", {"cmd": "ws.use", "name": name}, type="default")
            else:
                switch_btn = _button("切换到此处", {"cmd": "ws.use", "name": name}, type="primary")
            remove_btn = _button("删除", {"cmd": "ws.remove", "name": name}, type="danger")
            elements.append(_action_row([switch_btn, remove_btn]))

            if i < len(workspaces) - 1:
                elements.append(_hr())

    return _s1_card(title="📂 工作目录", elements=elements)


def session_reset_card() -> dict[str, Any]:
    """Render a warning card when the agent session was rebuilt.

    Notifies the user that their previous conversation context has been
    lost, typically because the agent subprocess restarted.
    """
    text = (
        "⚠️ 与 Agent 的连接已断开并自动重建了新会话。\n\n"
        "**之前的对话上下文已丢失**，Agent 不再记得本次会话前的内容。\n\n"
        "如需手动重置会话，可发送 `/new`。"
    )
    return _s1_card(
        title="⚠️ 会话已重建",
        elements=[
            _div_md(text),
            _hr(),
            _action_row([
                _button("📊 状态", {"cmd": "status"}, type="default"),
                _button("🆕 新会话", {"cmd": "new"}, type="primary"),
            ]),
        ],
    )


# --------------------------------------------------------------------------- #
# Schema 1.x card-building helpers
# --------------------------------------------------------------------------- #

def _s1_card(title: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a Schema 1.x card (header + elements, no `schema` field)."""
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }


def simple_text_card(text: str, title: str = "💬 消息") -> dict[str, Any]:
    """A simple card with just a text message."""
    return _s1_card(title=title, elements=[_div_md(text)])


def _div_md(content: str) -> dict[str, Any]:
    """A div element with lark_md text."""
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _hr() -> dict[str, Any]:
    """Horizontal rule separator."""
    return {"tag": "hr"}


def _action_row(buttons: list[dict[str, Any]]) -> dict[str, Any]:
    """An action row containing buttons."""
    return {"tag": "action", "actions": buttons}


def _button(text: str, value: dict[str, Any], type: str = "default") -> dict[str, Any]:
    """A button element with callback value."""
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": type,
        "value": value,
    }
