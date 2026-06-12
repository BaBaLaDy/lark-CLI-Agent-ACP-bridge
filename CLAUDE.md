# CLAUDE.md — Lark ACP Bridge

## Project Overview

飞书消息与本地 ACP Agent（Claude Code / Codex CLI 等）的桥接服务。用户通过飞书私聊或群聊 @bot 发送消息，bridge 通过 ACP 协议转发给本机 agent 子进程，agent 输出实时流式渲染为飞书交互卡片。

## Core Principles

**一切围绕 ACP。** 所有 agent 交互都通过 Agent Communication Protocol（JSON-RPC over stdio）实现。不直接调用任何 agent 的私有 API，不绕过 ACP SDK。新增 agent 类型只需在 `agents.json` 注册一个兼容 ACP 的命令即可。

**一切回复用飞书卡片。** 不使用纯文本消息回复命令结果。命令响应用 Schema 1.x 卡片（buttons, action rows），agent 运行状态用 Schema 2.0 卡片（streaming mode, collapsible panels）。所有用户可见的输出都应该是结构化的交互卡片。

**模块化、可扩展。** 每个模块有明确职责，通过接口组合而非继承。新增功能应添加新模块或扩展现有接口，不修改核心消息管道。

## Architecture

```
用户 (飞书) ←→ FeishuBot (消息管道 + 命令路由)
                    ↓
              AgentManager (多 agent 注册表 + per-scope 路由)
                ├── CodexACPBridge("claude")  ← 懒启动
                ├── CodexACPBridge("codex")   ← 懒启动
                └── ... 任意 ACP 兼容 agent
                    ↓
              ACPClient (ACP SDK wrapper)
                    ↓
              Agent 子进程 (stdio JSON-RPC)
```

## Module Responsibilities

| 模块 | 职责 | 不应做 |
|------|------|--------|
| `acp/client.py` | ACP 协议回调、SessionState 累积、observer 通知 | 不处理飞书 API |
| `acp/codex_bridge.py` | 单个 agent 进程生命周期、per-user session | 不处理多 agent 路由 |
| `acp/agent_manager.py` | 多 agent 注册表、懒启动、per-scope 路由 | 不处理飞书消息 |
| `bot/feishu_bot.py` | 消息管道、命令路由、卡片更新调度 | 不直接调用 ACP SDK |
| `bot/group.py` | 飞书 im:chat 群聊创建 | 不处理消息路由 |
| `card/renderer.py` | Schema 2.0 卡片（agent 运行态） | 不含按钮/交互 |
| `card/templates.py` | Schema 1.x 卡片（命令响应） | 不含 streaming 逻辑 |
| `config/settings.py` | TOML + JSON 配置加载 | 不持有运行时状态 |
| `config/workspace_store.py` | 持久化 cwd 和命名工作空间 | 不处理 agent 会话 |

## Card System — Two Schemas

### Schema 2.0 (`card/renderer.py`) — Agent 运行态
用于 `render_running_card`、`render_streaming_card`、`render_result_card`、`render_error_card`。

```python
{
    "schema": "2.0",
    "config": {"streaming_mode": True, "summary": {"content": "..."}},
    "body": {"elements": [...]}
}
```

- `streaming_mode: true` → 卡片保持可 patch 状态，支持增量更新
- `collapsible_panel` → 可折叠面板（思考过程、工具调用）
- 通过 `_ThrottledCardUpdater` 节流（默认 400ms），避免 API 限流

### Schema 1.x (`card/templates.py`) — 命令响应
用于 `help_card`、`status_card`、`agent_list_card`、`workspaces_card` 等。

```python
{
    "config": {"wide_screen_mode": True, "update_multi": True},
    "header": {"title": {"tag": "plain_text", "content": "..."}},
    "elements": [
        {"tag": "div", "text": {"tag": "lark_md", "content": "..."}},
        {"tag": "action", "actions": [{"tag": "button", "value": {"cmd": "..."}}]}
    ]
}
```

- 按钮的 `value` 字典里用 `cmd` 字段路由到 `_dispatch_card_action`
- `simple_text_card(text, title)` 用于简单文本消息的卡片包装

**添加新命令时必须返回卡片，不返回纯文本。** 用 `_reply_card(message_id, card)` 或 `simple_text_card(text, title)` 包装。

## ACP Protocol Integration

### 关键 SDK 类型（来自 `agent-client-protocol`）

```python
from acp import Client, spawn_agent_process, text_block, PROTOCOL_VERSION
from acp.schema import (
    AgentMessageChunk, AgentThoughtChunk,
    ToolCallStart, ToolCallProgress, ToolCallUpdate,
    UsageUpdate, TextContentBlock, ...
)
```

### Session 生命周期

```python
# 1. 启动 agent 进程
ctx = spawn_agent_process(client, *command, env=..., cwd=...)
conn, process = await ctx.__aenter__()

# 2. 初始化协议
await conn.initialize(protocol_version=PROTOCOL_VERSION, ...)

# 3. 创建 session
result = await conn.new_session(mcp_servers=[], cwd=...)

# 4. 发送 prompt（触发 session_update 回调流）
await conn.prompt(session_id=..., prompt=[text_block(message)])

# 5. 取消/关闭
await conn.cancel(session_id=...)
await conn.close_session(session_id=...)
```

### 回调模型

`BridgeClient.session_update()` 接收以下类型的更新：
- `AgentMessageChunk` → 文本/图片/音频输出 → `state.emit_text()`
- `AgentThoughtChunk` → 思考过程 → `state.thinking_chunks`
- `ToolCallStart/Progress/Update` → 工具调用状态 → `state.tool_calls`
- `UsageUpdate` → token 用量 → `state.input_tokens/output_tokens`

### 添加新 Agent 类型

只需在 `~/.lark-acp-bridge/agents.json` 中注册：

```json
{
  "agent_servers": {
    "my-agent": {
      "command": "python",
      "args": ["my_acp_agent.py"],
      "env": {"MY_API_KEY": "..."},
      "description": "My Custom Agent"
    }
  }
}
```

不需要修改任何代码。`AgentManager` 会自动用 `CodexACPBridge` 包装它（因为所有 ACP agent 共享相同的 JSON-RPC over stdio 协议）。

## Scope Rules

| 场景 | Scope Key | 含义 |
|------|-----------|------|
| 私聊 (p2p) | `user:{user_id}` | 每个用户独立 |
| 群聊 (group) | `chat:{chat_id}` | 整个群共享 |

Scope 影响：active agent 选择、工作目录 cwd。

## Configuration Layers (优先级从低到高)

1. **TOML** (`~/.lark-acp-bridge/config.toml`) — 基础配置
2. **agents.json** (`~/.lark-acp-bridge/agents.json`) — agent 注册表，覆盖 TOML 中的 agents
3. **环境变量** (`LARK_ACP_*`) — 覆盖所有 Settings 字段

## Key Patterns

- **懒启动**: `AgentManager.get_bridge(name)` 首次调用时才 start 子进程
- **节流更新**: `_ThrottledCardUpdater` 合并高频 card patch（400ms 间隔）
- **消息去重**: 60 秒 TTL 的 message_id 追踪，防 WebSocket 重连重放
- **前缀路由**: `"claude: 帮我写代码"` → 临时路由到 claude，不改持久 active agent
- **群聊 @检测**: 通过 `application/v6` API 获取 bot open_id，非 DM 必须 @bot 才回复
- **优雅降级**: AgentManager/WorkspaceStore 用 try/except 导入，不可用时回退单 agent

## Coding Conventions

- **Python >= 3.11**, 使用 `from __future__ import annotations`
- **类型标注**: 所有公开方法参数和返回值需有类型标注
- **日志**: 使用 `structlog`，事件名用 kebab-case（如 `agent-switched`）
- **错误处理**: 飞书 API 调用不向调用者抛异常，只 log error
- **异步**: 使用 `asyncio`，不引入其他异步框架
- **Windows 兼容**: subprocess 解析 `.cmd`/`.ps1`/`.bat` 包装器；`_run_async` 使用 `ProactorEventLoop`

## Testing

```bash
pip install -e ".[dev]"
pytest
```

- pytest + pytest-asyncio (auto mode)
- 所有 ACP 和飞书 API 调用使用 `unittest.mock` mock
- 测试文件: `tests/test_codex_bridge.py`, `tests/test_feishu_bot.py`
- 新增功能应附带对应单元测试

## Feishu Permissions Required

| 权限 | 用途 |
|------|------|
| `im:message` | 发送/回复/更新消息 |
| `im:message:send_as_bot` | 以 bot 身份发消息 |
| `im:chat` | 创建群聊（`/new chat`） |
| `application:application:readonly` | 获取 bot open_id（群聊 @检测） |
