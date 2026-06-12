# Lark ACP Bridge

通过 ACP（Agent Communication Protocol）协议，将飞书与多个 AI Agent（Claude Code、Codex、OpenCode 等）桥接。用户在飞书私聊或群聊中 @bot 发送消息，bridge 通过 ACP 转发给本机 agent 子进程，agent 输出实时流式渲染为飞书交互卡片。

---

## 快速开始

### 前置条件

| 依赖 | 版本要求 | 说明 |
|------|----------|------|
| Python | >= 3.11 | 运行 bridge 服务 |
| Node.js | >= 18 | 安装和运行 ACP Agent CLI |
| 飞书企业自建应用 | — | 获取 App ID / App Secret |

### 安装

```bash
# 从 GitHub 安装（推荐）
pip install git+https://github.com/BaBaLaDy/lark-CLI-ACP-bridge.git

# 或使用 pipx（自动隔离环境）
pipx install git+https://github.com/BaBaLaDy/lark-CLI-ACP-bridge.git
```

### 初始化（交互式向导）

```bash
lark-acp-bridge init
```

向导会引导你完成：
1. 环境检查（Node.js 是否安装）
2. 选择并安装 ACP Agent CLI（Claude / OpenCode / Codex）
3. 输入飞书 App ID 和 App Secret
4. 设置工作目录

完成后配置文件写入 `~/.lark-acp-bridge/`。

### 启动

```bash
lark-acp-bridge start
```

### 升级版本

当 GitHub 仓库有更新时，重新运行安装命令即可升级到最新版：

```bash
pip install --upgrade git+https://github.com/BaBaLaDy/lark-CLI-ACP-bridge.git

# 确认版本
lark-acp-bridge version
```

> **注意**：升级只更新 bridge 代码，不会影响 `~/.lark-acp-bridge/` 中的配置文件。
> 如需重新初始化配置，运行 `lark-acp-bridge init --force`。

---

## 功能特性

- ✅ 使用官方 `agent-client-protocol` Python SDK
- ✅ 通过 ACP 协议与任意兼容 Agent 通信（JSON-RPC over stdio）
- ✅ **多 Agent 支持**：运行时切换 Claude Code、Codex 等任意 ACP 兼容 agent
- ✅ **消息前缀路由**：`claude: 帮我写代码` 临时路由到指定 agent
- ✅ **工作空间管理**：`/cd` 切换目录，`/ws` 命名工作空间
- ✅ **拉群聊**：`/new chat` 创建飞书群聊，继承当前工作目录
- ✅ 飞书长连接集成（默认，不需要公网回调）
- ✅ 飞书 Webhook 集成（可选）
- ✅ 交互卡片回复：先发送"正在思考"卡片，完成后更新为结果卡片
- ✅ 流式响应支持
- ✅ 系统服务（daemon 模式）

---

## 配置文件

所有配置文件位于 `~/.lark-acp-bridge/`（Windows: `C:\Users\<用户名>\.lark-acp-bridge\`）：

```
~/.lark-acp-bridge/
├── config.toml      # 飞书凭证、通用设置
├── agents.json      # Agent 注册表（多 agent 配置）
├── workspaces.json  # 命名工作空间（自动生成）
└── sessions.json    # 会话状态（自动生成）
```

### 配置优先级（从低到高）

1. `config.toml` 中的默认值
2. `agents.json` 中的 agent 配置（覆盖 TOML 中同名 agent）
3. 环境变量（`LARK_ACP_*` 前缀，如 `LARK_ACP_FEISHU_APP_ID`）

### config.toml

由 `lark-acp-bridge init` 生成，示例：

```toml
[feishu]
app_id = "cli_xxxxxxxxxx"
app_secret = "your_secret"

[openai]
api_key = ""          # 留空时使用本机 Codex 登录态

[general]
working_dir = "/path/to/project"
log_level = "INFO"
idle_timeout_seconds = 300
card_update_throttle_ms = 400
max_concurrent_runs_per_user = 1

[agent]
show_tool_calls = true
```

### agents.json

定义可用 Agent 列表，`lark-acp-bridge init` 可自动创建。手动编辑示例：

```json
{
  "active": "claude",
  "agent_servers": {
    "claude": {
      "command": "claude-agent-acp",
      "args": [],
      "env": { "ANTHROPIC_API_KEY": "sk-ant-..." },
      "description": "Claude Code"
    },
    "codex": {
      "command": "npx",
      "args": ["-y", "@zed-industries/codex-acp"],
      "env": { "OPENAI_API_KEY": "sk-..." },
      "description": "Codex CLI"
    },
    "opencode": {
      "command": "opencode",
      "args": ["acp"],
      "description": "OpenCode (multi-provider)"
    }
  }
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `command` | ✅ | 可执行命令（需在 PATH 中） |
| `args` | — | 命令参数列表 |
| `env` | — | 传给子进程的环境变量 |
| `description` | — | 显示名称 |

**Agent CLI 安装命令：**

| Agent | 安装命令 |
|-------|----------|
| Claude Code | `npm install -g @agentclientprotocol/claude-agent-acp` |
| OpenCode | `npm install -g opencode-ai` |
| Codex | 无需安装（npx 自动下载） |
| Qoder | `npm install -g @qoder/cli` |
| Kiro | 安装 Kiro CLI |

---

## 飞书应用配置

1. 在 [飞书开放平台](https://open.feishu.cn/) 创建企业自建应用
2. 添加**机器人**能力
3. 在"事件与回调"中启用**长连接模式**（WebSocket）
4. 订阅事件：`im.message.receive_v1`
5. 发布应用，复制 **App ID** 和 **App Secret**

### 所需权限

| 权限 | 用途 |
|------|------|
| `im:message` | 发送/回复/更新消息 |
| `im:message:send_as_bot` | 以 bot 身份发消息 |
| `im:chat` | 创建群聊（`/new chat` 功能） |
| `application:application:readonly` | 获取 bot open_id（群聊 @检测） |

---

## 命令参考

### `lark-acp-bridge init`

交互式初始化向导。检查环境、引导安装 Agent CLI、配置飞书凭证。
若配置文件已存在，默认保留，不会覆盖。

```bash
lark-acp-bridge init [--skip-checks] [--force]
```

| 选项 | 说明 |
|------|------|
| `--skip-checks` | 跳过 Node.js 环境检查（用于 CI 等非交互场景） |
| `--force` / `-f` | 强制重新配置，覆盖已有配置文件 |

### `lark-acp-bridge version`

显示当前安装的版本号。

```bash
lark-acp-bridge version
```

### `lark-acp-bridge start`

启动桥接服务。

```bash
lark-acp-bridge start [选项]
```

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `--mode` | `ws` | `ws`（飞书长连接）或 `webhook`（HTTP 回调） |
| `--webhook-port` | `8080` | Webhook 模式监听端口 |
| `--daemon` / `-d` | `false` | 注册为系统服务并在后台运行 |
| `--agent-type` | `codex` | `codex` 或 `custom`（配合 `--agent-command`） |
| `--agent-command` | — | 自定义 agent 启动命令，如 `"python my_agent.py"` |

### `lark-acp-bridge test <message>`

测试 ACP Agent 连接，不启动飞书服务。实时在终端输出 Agent 返回的文本。

```bash
lark-acp-bridge test "你好，帮我写一个 Hello World"
```

### `lark-acp-bridge config`

显示当前配置（读取 `~/.lark-acp-bridge/`）。

```bash
lark-acp-bridge config
```

### `lark-acp-bridge daemon`

管理系统服务（后台常驻运行）。

```bash
lark-acp-bridge daemon install [--mode ws]   # 注册服务
lark-acp-bridge daemon uninstall             # 卸载服务
lark-acp-bridge daemon status                # 查询状态
```

支持的后台服务：Linux（systemd）、macOS（launchd）、Windows（schtasks）。

---

## 飞书内命令

| 命令 | 说明 |
|------|------|
| `/new` | 创建新会话 |
| `/new chat [name]` | 创建群聊，继承当前 cwd |
| `/stop` 或 `/cancel` | 取消当前运行 |
| `/status` | 查看状态 |
| `/agent list` | 列出所有可用 agent |
| `/agent use <name>` | 切换到指定 agent（持久） |
| `<name>: <msg>` | 临时路由到指定 agent（不改默认） |
| `/cd <path>` | 切换当前工作目录 |
| `/ws list` | 列出命名工作空间 |
| `/ws save <name>` | 保存当前目录为别名 |
| `/ws use <name>` | 切换到命名工作空间 |
| `/ws remove <name>` | 删除命名工作空间 |
| `/help` | 显示帮助信息 |

---

## 架构

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

### Scope 规则

| 场景 | Scope Key | 含义 |
|------|-----------|------|
| 私聊 (p2p) | `user:{user_id}` | 每个用户独立 |
| 群聊 (group) | `chat:{chat_id}` | 整个群共享 |

Scope 影响：active agent 选择、工作目录 cwd。

---

## 环境变量

所有 `Settings` 字段都可以通过 `LARK_ACP_` 前缀的环境变量覆盖：

```bash
LARK_ACP_FEISHU_APP_ID=cli_xxx
LARK_ACP_FEISHU_APP_SECRET=xxx
LARK_ACP_OPENAI_API_KEY=sk-xxx
LARK_ACP_WORKING_DIR=/path/to/project
LARK_ACP_LOG_LEVEL=DEBUG
```

---

## 开发

```bash
# 克隆仓库
git clone https://github.com/BaBaLaDy/lark-CLI-ACP-bridge.git
cd lark-CLI-ACP-bridge

# 安装（含开发依赖）
pip install -e ".[dev]"

# 运行测试
pytest

# 代码检查
ruff check src/
```

---

## 依赖

| 包 | 版本 | 用途 |
|----|------|------|
| `agent-client-protocol` | `>=0.10.0` | 官方 ACP Python SDK |
| `pydantic` | `>=2.7` | 数据验证 |
| `pydantic-settings` | `>=2.0` | 配置管理 |
| `typer` | `>=0.12` | CLI 框架 |
| `structlog` | `>=24.1` | 结构化日志 |
| `lark-oapi` | `>=1.0` | 飞书开放平台 SDK |
| `aiohttp` | `>=3.9` | Webhook 服务器 |
| `tomli-w` | `>=1.0` | TOML 配置写入 |

---

## 许可证

[MIT](LICENSE)
