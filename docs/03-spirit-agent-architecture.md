# Spirit Agent 架构设计

> 基于 Hermes 架构参考，设计 Spirit Agent 的完整产品架构。

---

## 一、架构总览

### 1.1 分层架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                          前端接入层                                  │
│                                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │
│  │ VSCode   │  │ Web      │  │ CLI      │  │ Message Gateway  │   │
│  │ Extension│  │Dashboard │  │ Terminal │  │ TG/Discord/Slack │   │
│  │          │  │          │  │          │  │ /钉钉/飞书/企微   │   │
│  │ TypeScript│ │ React+TS │  │ Python   │  │ Python           │   │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────────┬─────────┘   │
│       │              │              │                 │             │
│       └──────────────┴──────┬───────┴─────────────────┘             │
│                             │                                       │
├─────────────────────────────┼───────────────────────────────────────┤
│                        API 网关层                                   │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                   Spirit API Server                          │   │
│  │                                                             │   │
│  │  HTTP REST  │  WebSocket  │  SSE  │  JSON-RPC              │   │
│  │  (FastAPI)     (实时通信)    (流式)    (VSCode/TUI)          │   │
│  └─────────────────────────┬───────────────────────────────────┘   │
│                             │                                       │
├─────────────────────────────┼───────────────────────────────────────┤
│                        Agent 核心层                                 │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                   SpiritAgent (状态容器)                     │   │
│  │                                                             │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐    │   │
│  │  │ Conversation │  │ Context     │  │ Memory          │    │   │
│  │  │ Loop         │  │ Engine      │  │ Manager         │    │   │
│  │  │ (对话循环)   │  │ (上下文压缩) │  │ (记忆管理)      │    │   │
│  │  └─────────────┘  └─────────────┘  └─────────────────┘    │   │
│  │                                                             │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐    │   │
│  │  │ System      │  │ Skill       │  │ Iteration       │    │   │
│  │  │ Prompt      │  │ Manager     │  │ Budget          │    │   │
│  │  │ (提示词构建) │  │ (技能管理)  │  │ (迭代预算)      │    │   │
│  │  └─────────────┘  └─────────────┘  └─────────────────┘    │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                             │                                       │
├─────────────────────────────┼───────────────────────────────────────┤
│                        工具调度层                                   │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                   Tool Dispatcher                            │   │
│  │                                                             │   │
│  │  ┌───────────┐  ┌───────────┐  ┌─────────────────────┐    │   │
│  │  │ Tool      │  │ Toolset   │  │ Tool Executor       │    │   │
│  │  │ Registry  │  │ Manager   │  │ (串行/并行/分段)    │    │   │
│  │  │ (工具注册) │  │ (工具集)  │  │                     │    │   │
│  │  └───────────┘  └───────────┘  └─────────────────────┘    │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                             │                                       │
├─────────────────────────────┼───────────────────────────────────────┤
│                        工具实现层（复用 Hermes）                     │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                   tools/ (90+ 工具)                          │   │
│  │                                                             │   │
│  │  terminal │ file │ web │ browser │ vision │ tts │ code_exec │   │
│  │  process  │ patch │ search │ navigate │ analyze │ speech     │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│                        扩展层                                       │
│                                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐          │
│  │ Plugins  │  │ Skills   │  │ MCP      │  │ Cron     │          │
│  │ (插件)   │  │ (技能)   │  │ Servers  │  │ (定时)   │          │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘          │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│                        基础设施层                                   │
│                                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐          │
│  │ Config   │  │ SQLite   │  │ Logging  │  │ i18n     │          │
│  │ (YAML)   │  │ + FTS5   │  │          │  │          │          │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘          │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 二、目录结构设计

```
spirit-agent-main/
│
├── spirit/                      # Python 后端核心
│   ├── __init__.py
│   ├── agent/                   # Agent 核心
│   │   ├── __init__.py
│   │   ├── agent.py             # SpiritAgent 类（状态容器）
│   │   ├── agent_init.py        # 初始化逻辑
│   │   ├── conversation_loop.py # 对话循环
│   │   ├── context_engine.py    # 上下文引擎基类
│   │   ├── context_compressor.py# 默认压缩实现
│   │   ├── memory_manager.py    # 记忆管理
│   │   ├── system_prompt.py     # 系统提示词构建
│   │   ├── skill_manager.py     # 技能管理
│   │   ├── iteration_budget.py  # 迭代预算
│   │   └── error_classifier.py  # 错误分类
│   │
│   ├── tools/                   # 工具系统（复用 Hermes）
│   │   ├── __init__.py
│   │   ├── registry.py          # 工具注册表
│   │   ├── terminal_tool.py     # 终端工具
│   │   ├── file_tool.py         # 文件工具
│   │   ├── web_tool.py          # Web 搜索工具
│   │   ├── browser_tool.py      # 浏览器工具
│   │   ├── vision_tool.py       # 视觉工具
│   │   └── ... (90+ 工具)
│   │
│   ├── toolsets.py              # 工具集定义
│   ├── dispatcher.py            # 工具分发（精简版 model_tools.py）
│   │
│   ├── transports/              # LLM Provider 适配
│   │   ├── __init__.py
│   │   ├── base.py              # 基类
│   │   ├── openai.py            # OpenAI 兼容
│   │   ├── anthropic.py         # Anthropic
│   │   ├── gemini.py            # Google Gemini
│   │   └── bedrock.py           # AWS Bedrock
│   │
│   ├── storage/                 # 数据存储
│   │   ├── __init__.py
│   │   ├── session_db.py        # SQLite 会话存储
│   │   ├── config.py            # YAML 配置管理
│   │   └── credential_pool.py   # 凭证池
│   │
│   ├── gateway/                 # 消息网关
│   │   ├── __init__.py
│   │   ├── runner.py            # 网关运行器
│   │   ├── platforms/           # 平台适配器
│   │   │   ├── telegram.py
│   │   │   ├── discord.py
│   │   │   ├── slack.py
│   │   │   └── ...
│   │   └── session.py           # 网关会话管理
│   │
│   ├── api/                     # API 服务
│   │   ├── __init__.py
│   │   ├── server.py            # FastAPI 服务
│   │   ├── routes/              # API 路由
│   │   │   ├── chat.py          # 聊天 API
│   │   │   ├── sessions.py      # 会话管理 API
│   │   │   ├── config.py        # 配置 API
│   │   │   └── tools.py         # 工具 API
│   │   └── websocket.py         # WebSocket 处理
│   │
│   └── cli/                     # CLI 入口
│       ├── __init__.py
│       ├── main.py              # 主命令
│       ├── chat.py              # spirit chat
│       └── commands.py          # 斜杠命令
│
├── web/                         # Web Dashboard 前端
│   ├── package.json
│   ├── src/
│   │   ├── App.tsx
│   │   ├── pages/
│   │   │   ├── ChatPage/        # 聊天界面
│   │   │   ├── SessionsPage/    # 会话历史
│   │   │   ├── ConfigPage/      # 配置管理
│   │   │   └── ToolsPage/       # 工具管理
│   │   ├── components/
│   │   └── lib/
│   │       └── api.ts           # API 客户端
│   └── vite.config.ts
│
├── vscode-extension/            # VSCode 扩展
│   ├── package.json
│   ├── src/
│   │   ├── extension.ts         # 扩展入口
│   │   ├── client.ts            # Spirit 后端客户端
│   │   ├── chat/                # 聊天侧边栏
│   │   ├── inline/              # 内联操作
│   │   └── completion/          # 代码补全
│   └── media/
│
├── skills/                      # 内置技能
│   ├── research/
│   ├── coding/
│   └── ...
│
├── plugins/                     # 插件
│   ├── memory/
│   ├── providers/
│   └── ...
│
├── tests/                       # 测试
│   ├── test_agent/
│   ├── test_tools/
│   └── ...
│
├── docs/                        # 文档
│   ├── 01-product-overview.md
│   ├── 02-hermes-architecture-analysis.md
│   ├── 03-spirit-agent-architecture.md
│   └── ...
│
├── pyproject.toml               # Python 项目配置
├── package.json                 # Node.js 项目配置（前端）
└── README.md
```

---

## 三、核心模块设计

### 3.1 SpiritAgent 类（精简版 AIAgent）

```python
class SpiritAgent:
    """Spirit Agent 核心类 — 状态容器 + 调度中心"""
    
    # ============ 属性 ============
    
    # LLM 连接
    client: Any              # OpenAI 兼容客户端
    api_key: str
    model: str
    provider: str
    api_mode: str            # "chat_completions" | "anthropic_messages" | ...
    
    # 工具系统
    enabled_toolsets: List[str]
    disabled_toolsets: List[str]
    
    # 会话
    session_id: str
    platform: str            # "cli" | "vscode" | "web" | "telegram" | ...
    
    # 控制
    max_iterations: int = 90
    _interrupt_requested: bool = False
    
    # 上下文引擎
    context_engine: ContextEngine
    
    # 回调（简化为事件总线）
    event_bus: EventBus
    
    # ============ 方法 ============
    
    def __init__(self, config: AgentConfig):
        """初始化 — 委托给 agent_init.py"""
        init_agent(self, config)
    
    def run_conversation(self, user_message: str, **kwargs) -> ConversationResult:
        """主入口 — 委托给 conversation_loop.py"""
        from spirit.agent.conversation_loop import run_conversation
        return run_conversation(self, user_message, **kwargs)
    
    def interrupt(self):
        """请求中断当前操作"""
        self._interrupt_requested = True
    
    def switch_model(self, model: str):
        """运行时切换模型"""
        from spirit.agent.runtime_helpers import switch_model
        switch_model(self, model)
```

### 3.2 对话循环（精简版）

```python
async def run_conversation(agent: SpiritAgent, user_message: str, **kwargs):
    """Spirit Agent 对话循环 — 精简版"""
    
    # 构建消息
    messages = build_messages(agent, user_message)
    tools = get_tool_definitions(agent)
    
    while agent._api_call_count < agent.max_iterations:
        # 检查中断
        if agent._interrupt_requested:
            break
        
        # 调用 LLM
        response = await call_llm(agent, messages, tools)
        agent._api_call_count += 1
        
        # 处理响应
        if response.tool_calls:
            # 执行工具
            results = await execute_tools(agent, response.tool_calls)
            messages.extend(results)
            
            # 检查是否需要压缩
            if agent.context_engine.should_compress():
                messages = agent.context_engine.compress(messages)
        else:
            # 文本回复，结束循环
            return ConversationResult(
                response=response.content,
                messages=messages,
                usage=response.usage,
            )
```

### 3.3 事件总线（替代 Hermes 的 15+ 回调）

```python
class EventBus:
    """简化版事件系统，替代 Hermes 的多个 callback"""
    
    def __init__(self):
        self._handlers: Dict[str, List[Callable]] = {}
    
    def on(self, event: str, handler: Callable):
        """注册事件处理器"""
        self._handlers.setdefault(event, []).append(handler)
    
    def emit(self, event: str, **data):
        """触发事件"""
        for handler in self._handlers.get(event, []):
            try:
                handler(**data)
            except Exception as e:
                logger.debug(f"Event handler error: {e}")

# 使用示例
agent.event_bus.on("tool_start", lambda name, args: print(f"Starting {name}"))
agent.event_bus.on("tool_complete", lambda name, result: print(f"Done {name}"))
agent.event_bus.on("stream_delta", lambda token: sys.stdout.write(token))
```

---

## 四、API 设计

### 4.1 REST API

```
# 聊天
POST /api/v1/chat
  Body: { "message": "帮我写个函数", "session_id": "xxx" }
  Response: { "response": "...", "session_id": "xxx" }

# 流式聊天
POST /api/v1/chat/stream
  Body: { "message": "帮我写个函数" }
  Response: SSE stream

# 会话管理
GET  /api/v1/sessions              # 列表
GET  /api/v1/sessions/{id}         # 详情
DELETE /api/v1/sessions/{id}       # 删除
POST /api/v1/sessions/{id}/resume  # 恢复

# 配置
GET  /api/v1/config                # 获取配置
PUT  /api/v1/config                # 更新配置

# 工具
GET  /api/v1/tools                 # 工具列表
GET  /api/v1/tools/{name}          # 工具详情
GET  /api/v1/toolsets              # 工具集列表

# 模型
GET  /api/v1/models                # 可用模型
POST /api/v1/models/switch         # 切换模型
```

### 4.2 WebSocket API

```
# 连接
ws://localhost:9001/ws/chat?session_id=xxx

# 客户端消息
{ "type": "user_message", "content": "帮我写个函数" }
{ "type": "interrupt" }
{ "type": "tool_approve", "tool_call_id": "xxx" }

# 服务端消息
{ "type": "assistant_text", "content": "好的，我来帮你..." }
{ "type": "tool_start", "name": "write_file", "args": {...} }
{ "type": "tool_complete", "name": "write_file", "result": "..." }
{ "type": "stream_delta", "token": "你" }
{ "type": "stream_end" }
{ "type": "error", "message": "..." }
```

---

## 五、多前端接入设计

### 5.1 统一后端，多前端适配

```
                    ┌─────────────────────────────┐
                    │      Spirit 后端服务         │
                    │                             │
                    │  SpiritAgent (共享核心)      │
                    │  ToolRegistry (共享工具)     │
                    │  SessionDB (共享数据库)      │
                    └──────────┬──────────────────┘
                               │
          ┌────────────────────┼────────────────────┐
          │                    │                    │
    ┌─────┴─────┐       ┌─────┴─────┐       ┌─────┴─────┐
    │ WebSocket │       │ HTTP REST │       │ Gateway   │
    │ Handler   │       │ Handler   │       │ Handler   │
    └─────┬─────┘       └─────┬─────┘       └─────┬─────┘
          │                    │                    │
    ┌─────┴─────┐       ┌─────┴─────┐       ┌─────┴─────┐
    │ VSCode    │       │ Web       │       │ Telegram  │
    │ Extension │       │ Dashboard │       │ Discord   │
    │ CLI       │       │ API       │       │ Slack     │
    └───────────┘       └───────────┘       └───────────┘
```

### 5.2 各前端适配要点

| 前端 | 通信方式 | 特殊适配 |
|------|---------|---------|
| **VSCode** | WebSocket + LSP | 代码补全、内联 diff、文件树感知 |
| **Web Dashboard** | HTTP REST + SSE | 聊天界面、配置管理、会话历史 |
| **CLI** | 直接调用 | Rich 渲染、prompt_toolkit 输入 |
| **Telegram** | Bot API | Markdown 格式、命令菜单、图片处理 |
| **Discord** | discord.py | Embed 格式、Slash 命令、线程管理 |

---

## 六、会话管理设计

### 6.1 会话生命周期

```
创建 → 活跃 → 压缩/分支 → 结束 → 恢复
 │       │        │          │       │
 ↓       ↓        ↓          ↓       ↓
新建     对话循环  上下文满    用户退出  /resume
session  工具调用  自动分裂   超时退出  加载历史
```

### 6.2 会话分裂（压缩触发时）

```
Session A (原始会话)
  ├── messages: [msg1, msg2, ..., msg100]
  ├── end_reason: "compression"
  └── ↓ 压缩后
  
Session B (压缩续接)
  ├── parent_session_id: A
  ├── messages: [summary_of_A, msg101, msg102, ...]
  └── 继续对话...
```

---

## 七、安全设计

### 7.1 工具安全分级

```python
# 工具安全级别
TOOL_SAFETY = {
    # 只读操作 — 可并行
    "read_file": "safe",
    "web_search": "safe",
    "vision_analyze": "safe",
    
    # 写操作 — 需要确认（VSCode/编辑器场景）
    "write_file": "moderate",
    "patch": "moderate",
    "terminal": "moderate",
    
    # 危险操作 — 必须确认
    "execute_code": "dangerous",
    "browser_navigate": "dangerous",
}
```

### 7.2 API 认证

```
# 本地模式（默认）
- 仅监听 127.0.0.1
- 无需认证

# 远程模式
- JWT Token 认证
- API Key 认证
- 可选 OAuth2
```

---

*下一步：[04-technical-design.md](./04-technical-design.md) — 技术设计详情*
