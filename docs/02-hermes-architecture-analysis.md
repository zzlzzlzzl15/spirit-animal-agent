# Hermes 架构深度分析 — Spirit Agent 参考蓝本

> 本文档是对 Hermes Agent 核心架构的深度剖析，作为 Spirit Agent 设计的直接参考。

---

## 一、整体架构模式

### 1.1 窄腰宽边（Narrow Waist, Broad Edges）

Hermes 的核心设计哲学：

```
┌─────────────────────────────────────────────────────────────────┐
│  宽边（自由扩展）：前端界面层                                      │
│  CLI │ TUI │ Electron │ VSCode/ACP │ Web Dashboard              │
├─────────────────────────────────────────────────────────────────┤
│  宽边（自由扩展）：消息网关层                                      │
│  Telegram │ Discord │ Slack │ WhatsApp │ 钉钉 │ 飞书 │ 企微      │
├─────────────────────────────────────────────────────────────────┤
│  ★ 窄腰（保持精简）：Agent 核心层                                 │
│  AIAgent ← 对话循环 ← 系统提示词 ← 上下文引擎 ← 记忆 ← 技能      │
├─────────────────────────────────────────────────────────────────┤
│  宽边（自由扩展）：工具调度层                                      │
│  ToolRegistry │ Toolsets │ model_tools.py │ Tool Dispatch        │
├─────────────────────────────────────────────────────────────────┤
│  宽边（自由扩展）：工具实现层                                      │
│  tools/*.py (90+ 工具)                                           │
├─────────────────────────────────────────────────────────────────┤
│  宽边（自由扩展）：扩展层                                          │
│  Plugins │ Skills │ MCP Servers │ Cron Jobs │ LSP               │
├─────────────────────────────────────────────────────────────────┤
│  基础设施层                                                      │
│  配置管理 │ 日志 │ SQLite 存储 │ 凭证池 │ 国际化                   │
└─────────────────────────────────────────────────────────────────┘
```

**核心原则**：
- 新能力优先通过**边缘扩展**（工具/插件/技能/MCP）实现
- 核心工具数量严格控制（每个工具都随 API 请求发送给模型）
- 边缘随便改，核心稳如磐石

### 1.2 转发器架构（Forwarder Pattern）

Hermes 的 `AIAgent` 类采用"状态容器 + 调度中心"模式：

```python
class AIAgent:
    """CEO 角色：拥有所有状态，但不亲自干活"""
    
    def __init__(self, ...70+ 参数...):
        # 委托给 agent_init.init_agent() 做初始化
        init_agent(self, ...)
    
    def run_conversation(self, ...):
        """转发给 conversation_loop.py"""
        from agent.conversation_loop import run_conversation
        return run_conversation(self, ...)  # self 当参数传入
    
    def _execute_tool_calls(self, ...):
        """转发给 tool_executor.py"""
        from agent.tool_executor import execute_tool_calls_concurrent
        ...
```

**设计优势**：
- 单文件不会膨胀到不可维护
- 各模块可独立测试
- 延迟导入（`from xxx import` 在方法内）加快启动速度

---

## 二、Agent 核心设计

### 2.1 AIAgent 类结构

```
AIAgent (run_agent.py, 6476 行)
│
├── 属性（100+ 个，按功能分组）
│   ├── LLM 连接层：client, api_key, model, provider, api_mode
│   ├── 工具系统：enabled_toolsets, disabled_toolsets, _tool_guardrails
│   ├── 会话身份：session_id, platform, _user_id, _delegate_depth
│   ├── 迭代预算：max_iterations, iteration_budget
│   ├── 中断控制：_interrupt_requested, _execution_thread_id
│   ├── 回调函数：15+ 个 callback（progress/start/complete/thinking...）
│   ├── 流式处理：_stream_callback, _stream_writer_token
│   ├── 活动追踪：_last_activity_ts, _api_call_count, _current_tool
│   └── 配置：max_tokens, reasoning_config, request_overrides
│
├── 方法（全是转发器）
│   ├── run_conversation()     → conversation_loop.py
│   ├── _execute_tool_calls()  → tool_executor.py
│   ├── _invoke_tool()         → model_tools.py
│   ├── switch_model()         → agent_runtime_helpers.py
│   └── _compress_context()    → conversation_compression.py
│
└── 设计哲学
    └── "CEO 不干活，只持有状态 + 分发任务"
```

### 2.2 对话循环（Tool-Calling Loop）

Hermes 的核心循环，**不使用 LangChain 等框架**，直接实现：

```
┌──────────────────────────────────────────────────────────────┐
│                    对话循环核心                                │
│                                                              │
│  while (api_call_count < max_iterations):                    │
│    │                                                         │
│    ├── 1. 构建请求                                            │
│    │   messages + tool_schemas (OpenAI function calling 格式) │
│    │                                                         │
│    ├── 2. 调用 LLM API                                       │
│    │   response = client.chat.completions.create(...)         │
│    │                                                         │
│    ├── 3. 检查响应                                            │
│    │   ├── 有 tool_calls?                                    │
│    │   │   ├── 是 → 执行工具                                  │
│    │   │   │   ├── 决策：串行 / 并行 / 分段                   │
│    │   │   │   ├── registry.dispatch(name, args)             │
│    │   │   │   ├── 结果追加到 messages                        │
│    │   │   │   └── 继续循环                                   │
│    │   │   │                                                 │
│    │   │   └── 否 → 返回 response.content（文本回复）          │
│    │   │                                                     │
│    ├── 4. 检查是否需要压缩                                     │
│    │   └── engine.should_compress() → engine.compress()       │
│    │                                                         │
│    └── 5. 检查中断/预算                                       │
│        └── interrupt_requested? budget exhausted?             │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

### 2.3 工具执行模式（三种）

```
模式 1：串行（Sequential）
  tool_1 → 完成 → tool_2 → 完成 → tool_3 → 完成
  适用：单个工具调用

模式 2：并行（Concurrent）
  ┌─ tool_1 ─┐
  ├─ tool_2 ─┤ 同时执行
  └─ tool_3 ─┘
  适用：全部是安全工具（读操作）
  超时：420 秒（7 分钟）

模式 3：分段（Segmented）
  [安全工具 并行] → [危险工具 串行] → [安全工具 并行]
  适用：混合场景

决策逻辑：
  ≤1 个工具 → 串行
  全部安全   → 并行（DaemonThreadPoolExecutor）
  混合      → 分段
```

### 2.4 异步桥接（sync ↔ async）

Hermes 的工具可能是同步或异步的，需要在不同线程环境中调用：

```python
def _run_async(coro):
    """三种场景，三种处理方式"""
    
    # 场景 1：已有事件循环在跑 → 开临时线程
    if loop and loop.is_running():
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(_run_in_worker)
        return future.result(timeout=300)  # 5 分钟
    
    # 场景 2：在工作线程中 → 用线程级持久化循环
    if threading.current_thread() is not threading.main_thread():
        worker_loop = _get_worker_loop()  # threading.local()
        return worker_loop.run_until_complete(coro)
    
    # 场景 3：在主线程中 → 用全局持久化循环
    tool_loop = _get_tool_loop()
    return tool_loop.run_until_complete(coro)
```

---

## 三、工具系统设计

### 3.1 自注册 Registry 模式

```
依赖链（从底向上）：

tools/registry.py     ← 全局单例 ToolRegistry，无依赖
       ↑
tools/*.py            ← 每个工具文件导入时自注册
       ↑
model_tools.py        ← 导入 registry + 触发工具发现 + 分发调用
       ↑
run_agent.py          ← 调用 model_tools.handle_function_call()
```

### 3.2 工具注册流程

```python
# tools/registry.py — 全局单例
registry = ToolRegistry()

# tools/terminal_tool.py — 自注册
registry.register(
    name="terminal",
    toolset="terminal",
    schema={                    # OpenAI function calling 格式
        "type": "function",
        "function": {
            "name": "terminal",
            "description": "Execute a shell command",
            "parameters": {...}
        }
    },
    handler=handle_terminal,    # 实际处理函数
    check_fn=check_terminal,    # 可用性检查（如 ptyprocess 是否安装）
    is_async=False,
)
```

### 3.3 Toolset 工具集系统

```python
# toolsets.py — 按功能分组
TOOLSETS = {
    "web": {
        "description": "Web research tools",
        "tools": ["web_search", "web_extract"],
    },
    "terminal": {
        "description": "Terminal & process tools",
        "tools": ["terminal", "process"],
    },
    "file": {
        "description": "File manipulation tools",
        "tools": ["read_file", "write_file", "patch", "search_files"],
    },
    "coding": {
        "description": "Full coding toolset",
        "includes": ["web", "terminal", "file", "browser", "vision"],
    },
    # ... 更多工具集
}

# 按平台分配不同工具集
# CLI → 全量工具
# Webhook → 安全子集（web_search, web_extract, vision_analyze, clarify）
# ACP（编辑器）→ 编码聚焦（无消息/音频/UI 工具）
```

### 3.4 工具调用分发（handle_function_call）

```
handle_function_call(name, args)
│
├── 1. 参数修正（coerce）
│   "42" → 42, "true" → True
│
├── 2. 异步桥接（_run_async）
│   sync ↔ async 转换
│
├── 3. 插件中间件
│   pre_tool_call → 执行 → post_tool_call
│
├── 4. 工具钩子
│   before_execute → execute → after_execute → transform_result
│
├── 5. ACP 审批（编辑器场景）
│   危险操作需要用户确认
│
├── 6. registry.dispatch()
│   调用工具的 handler 函数
│
└── 7. 异常处理
    捕获异常 → 分类 → 返回错误信息给 LLM
```

---

## 四、通信方式

### 4.1 LLM API 通信

```
消息格式（OpenAI Chat Completions 标准）：

messages = [
    {"role": "system", "content": "You are Spirit Agent..."},
    {"role": "user", "content": "帮我写个函数"},
    {"role": "assistant", "content": null, "tool_calls": [
        {"id": "call_1", "function": {"name": "write_file", "arguments": "..."}}
    ]},
    {"role": "tool", "tool_call_id": "call_1", "content": "文件已写入"}
]

tools = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["path", "content"]
            }
        }
    }
]
```

### 4.2 多 Provider 适配器

```
                    ┌── anthropic_adapter.py ──→ Anthropic SDK
                    ├── bedrock_adapter.py ────→ AWS Bedrock SDK
client.chat. ──────├── vertex_adapter.py ─────→ Google Vertex SDK
completions.        ├── azure_identity_adapter → Azure OpenAI SDK
create()            ├── gemini_native_adapter  → Google Gemini SDK
                    ├── codex_responses_adapter→ Codex Responses API
                    └── (默认) ────────────────→ OpenAI SDK（直连或兼容）
```

### 4.3 前端通信协议

| 前端 | 协议 | 说明 |
|------|------|------|
| CLI | 直接调用 | `AIAgent.run_conversation()` |
| TUI | JSON-RPC over stdio | Node (Ink/React) ↔ Python (tui_gateway) |
| Electron | PTY 桥接 | 内嵌真实 `hermes --tui` |
| Gateway | 平台 SDK | 各平台 Bot API |
| Web Dashboard | HTTP REST + SSE | FastAPI + 静态 SPA |
| ACP（编辑器）| Agent Client Protocol | 标准化编辑器通信 |

---

## 五、数据库存储

### 5.1 SQLite + FTS5 全文搜索

```
state.db (SQLite 数据库)
│
├── sessions 表
│   ├── id (UUID)
│   ├── source ("cli", "telegram", "discord"...)
│   ├── model ("claude-sonnet-4-20250514")
│   ├── model_config (JSON: 模型参数快照)
│   ├── system_prompt (TEXT)
│   ├── cwd (启动目录)
│   ├── parent_session_id (压缩/分支链)
│   ├── started_at / ended_at
│   └── end_reason ("compression", "branched", "user_exit")
│
├── messages 表
│   ├── id (自增)
│   ├── session_id (FK → sessions.id)
│   ├── role ("user", "assistant", "tool", "system")
│   ├── content (TEXT)
│   ├── tool_calls (JSON)
│   ├── tool_call_id (TEXT)
│   └── created_at (timestamp)
│
├── messages_fts (FTS5 虚拟表)
│   ├── 全文搜索索引
│   └── 支持：MATCH '关键词' 快速检索
│
└── 设计特点
    ├── WAL 模式（并发读 + 单写）
    ├── 压缩触发会话分裂（parent_session_id 链）
    ├── 会话来源标记（按平台过滤）
    └── Schema 版本管理（SCHEMA_VERSION = 22）
```

### 5.2 其他存储

| 存储 | 技术 | 用途 |
|------|------|------|
| 用户配置 | `~/.hermes/config.yaml` | 行为设置 |
| 密钥 | `~/.hermes/.env` | API Keys |
| 记忆 | 文件 + 插件 | 跨会话持久记忆 |
| 技能 | Markdown 文件 | 可学习可改进的操作指南 |
| 日志 | 文件 | agent.log / errors.log |

---

## 六、上下文压缩引擎

### 6.1 插件化架构

```
ContextEngine (抽象基类)
    ↑
    └── ContextCompressor (默认实现：LLM 摘要压缩)
```

### 6.2 压缩算法

```
1. 剪枝旧工具输出（无 LLM 调用，纯文本截断）
2. 保护头部消息（系统提示 + 第一轮对话）
3. 保护尾部消息（最近 ~20K tokens）
4. LLM 摘要中间轮次（结构化提示）
5. 迭代更新摘要（保留信息跨多次压缩）
```

### 6.3 生命周期钩子

```python
# 会话切换时的四步仪式
engine.on_session_end(old_id, messages)    # 旧会话告别
engine.on_session_reset()                  # 引擎状态归零
engine.on_session_start(new_id, context)   # 新会话开张
engine.carry_over_new_session_context()    # 携带上下文（可选）
```

---

## 七、并发模型

### 7.1 线程架构

```
主线程（Main Thread）
│
├── 对话循环（run_conversation）
│   ├── LLM API 调用（阻塞等待流式响应）
│   └── 工具分发
│       ├── 串行模式：主线程直接执行
│       └── 并行模式：DaemonThreadPoolExecutor
│           ├── Worker Thread 1 → tool_1
│           ├── Worker Thread 2 → tool_2
│           └── Worker Thread 3 → tool_3
│
└── 守护线程特性
    ├── daemon=True → 主线程崩溃时 OS 自动回收
    ├── 不注册 atexit 钩子 → 不阻塞进程退出
    └── 超时保护 → 420s 批量超时 + 300s 单工具超时
```

### 7.2 三层超时防护

```
第 1 层：_run_async() → 单工具 300 秒
第 2 层：execute_tool_calls_concurrent() → 批量 420 秒
第 3 层：DaemonThreadPoolExecutor → daemon 线程 OS 回收
```

---

## 八、Spirit Agent 可复用清单

| 模块 | Hermes 文件 | Spirit 复用策略 |
|------|-------------|----------------|
| **工具注册表** | `tools/registry.py` | 直接复用 |
| **工具实现** | `tools/*.py` (90+) | 直接复用 |
| **工具集定义** | `toolsets.py` | 参考并精简 |
| **工具分发** | `model_tools.py` | 精简复刻 |
| **数据库存储** | `hermes_state.py` | 参考设计，重新实现 |
| **上下文引擎** | `context_engine.py` + `context_compressor.py` | 参考设计 |
| **Transport 层** | `agent/transports/` | 参考适配器模式 |
| **错误分类** | `agent/error_classifier.py` | 直接复用 |
| **凭证管理** | `agent/credential_pool.py` | 参考设计 |
| **系统提示词** | `agent/system_prompt.py` + `agent/prompt_builder.py` | 参考并中文化 |

---

*下一步：[03-spirit-agent-architecture.md](./03-spirit-agent-architecture.md) — Spirit Agent 架构设计*
