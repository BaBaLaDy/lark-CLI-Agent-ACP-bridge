 # Lark ACP Bridge

 通过 ACP (Agent Communication Protocol) 协议连接飞书与多个 AI Agent（Claude Code、Codex 等）。

 ## 功能特性

 - ✅ 使用官方 `agent-client-protocol` Python SDK
 - ✅ 通过 ACP 协议与任意兼容 Agent 通信（JSON-RPC over stdio）
 - ✅ **多 Agent 支持**：运行时切换 Claude Code、Codex 等任意 ACP 兼容 agent
 - ✅ **消息前缀路由**：`claude: 帮我写代码` 临时路由到指定 agent
 - ✅ **工作空间管理**：`/cd` 切换目录，`/ws` 命名工作空间
 - ✅ **拉群聊**：`/new chat` 创建飞书群聊，继承当前工作目录
 - ✅ 飞书长连接集成（默认，不需要公网回调）
 - ✅ 飞书 Webhook 集成（可选）
 - ✅ 交互卡片回复：先发送”正在思考”卡片，完成后更新为结果卡片
 - ✅ 流式响应支持

 ## 多 Agent 配置

 在 `~/.lark-acp-bridge/agents.json` 中定义可用 agent：

 ```json
 {
   “active”: “claude”,
   “agent_servers”: {
     “claude”: {
       “command”: “claude-agent-acp”,
       “args”: [],
       “env”: {},
       “description”: “Claude Code”
     },
     “codex”: {
       “command”: “npx”,
       “args”: [“-y”, “@zed-industries/codex-acp”],
       “env”: { “OPENAI_API_KEY”: “sk-...” },
       “description”: “Codex CLI”
     }
   }
 }
 ```

 - `active`：全局默认 agent
 - `agent_servers`：agent 注册表，每个 entry 定义 command、args、env、description

 配置后启动服务即可使用，在飞书内用 `/agent use claude` 或 `/agent use codex` 切换。

 ## 安装

 ```bash
 cd lark-acp-bridge
 pip install -e .
 ```

 ## 配置

 1. 复制示例配置文件：

 ```bash
 cp config.toml.example config.toml
 ```

 2. 编辑 `config.toml`，填入真实值：
    - `feishu.app_id` - 飞书应用 ID
    - `feishu.app_secret` - 飞书应用密钥
   - `openai.api_key` - 可选；留空时使用本机 Codex 登录态
   - `openai.base_url` - 可选；第三方 OpenAI 兼容服务地址
    - `general.working_dir` - 工作目录

 或使用 CLI 初始化：

 ```bash
 lark-acp-bridge init
 ```

 ## 使用

 ### 启动服务

 ```bash
lark-acp-bridge start
```

服务启动后将：
1. 读取 `agents.json`，注册所有配置的 agent（懒启动，首次使用时才启动进程）
2. 通过飞书长连接接收消息
3. 收到消息后先回复运行中卡片
4. 路由到当前活跃 agent（或前缀指定的 agent）
5. Agent 完成后更新同一张卡片为结果

如需使用 Webhook 回调模式：

```bash
lark-acp-bridge start --mode webhook --webhook-port 8080
```

 ### 测试模式

 ```bash
 lark-acp-bridge test “你好，请帮我写一个 Hello World 程序”
 ```

 测试模式会启动 ACP 代理并发送测试消息，不启动飞书服务。

 ### 查看配置

 ```bash
 lark-acp-bridge config
 ```

 ## 飞书配置

 1. 在[飞书开放平台](https://open.feishu.cn/)创建企业自建应用
 2. 添加机器人能力
3. 在事件订阅中启用长连接模式（WebSocket）
4. 订阅事件：`im.message.receive_v1`
5. （可选）添加 `im:chat` 权限，以支持 `/new chat` 拉群聊功能
6. 发布应用并获取 App ID 和 App Secret

 ## 架构

 ```
 用户 (飞书)
    ↓
飞书长连接 (WebSocket)
    ↓
 FeishuBot (消息处理器 + 命令路由)
    ↓
 AgentManager (多 agent 注册表 + per-scope 路由)
    ├── ACPBridge(“claude”)  ← 懒启动
    ├── ACPBridge(“codex”)   ← 懒启动
    └── ...
    ↓
 ACPClient (ACP 协议客户端)
    ↓
 Agent 子进程 (claude-agent-acp / codex-acp / 自定义)
 ```

 ## 飞书命令

 | 命令 | 说明 |
 |------|------|
 | `/new` | 创建新会话 |
 | `/new chat [name]` | 创建群聊，继承当前 cwd |
 | `/stop` 或 `/cancel` | 取消当前操作 |
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

 ## 开发

 ### 运行测试

 ```bash
 pip install -e “.[dev]”
 pytest
 ```

 ### 代码检查

 ```bash
 ruff check src/
 ```

 ## 依赖

 - `agent-client-protocol>=0.10.0` - 官方 ACP Python SDK
 - `pydantic>=2.7` - 数据验证
 - `pydantic-settings>=2.0` - 配置管理
 - `typer>=0.12` - CLI 框架
 - `structlog>=24.1` - 结构化日志
 - `aiohttp>=3.9` - Webhook 服务器
 - `tomli-w>=1.0` - TOML 配置写入

 ## 许可证

 MIT
