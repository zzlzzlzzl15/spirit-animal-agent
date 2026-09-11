# Spirit Agent 技术设计 — 工具 / 通信 / 存储

> 深入技术细节：工具系统如何复用、通信协议如何设计、数据库如何存储。

---

## 一、工具系统设计

### 1.1 复用 Hermes 工具库的策略

```
方案：符号链接 + 适配层

spirit-agent-main/
├── tools/                    # 符号链接 → hermes-agent-main/tools/
│   ├── registry.py           # 直接复用
│   ├── terminal_tool.py      # 直接复用
│   ├── file_tool.py          # 直接复用
│   └── ... (90+ 工具)
│
├── spirit/
│   └── tools_adapter.py      # 适配层（处理路径、日志等差异）
```

**适配层职责**：
```python
# spirit/tools_adapter.py

# 1. 路径适配：Hermes 用 ~/.hermes，Spirit 用 ~/.spirit
HERMES_HOME_OVERRIDE = Path.home() / ".spirit"

# 2. 日志适配：统一日志格式
def setup_spirit_logging():
    """Spirit 风格的日志初始化"""
    ...

# 3. 工具注册钩：添加 Spirit 特有的工具
def register_spirit_tools(registry):
    """注册 Spirit 特有的工具（如 vscode_notify）"""
    registry.register(
        name="vscode_notify",
        toolset="vscode",
        schema={...},
        handler=handle_vscode_notify,
        check_fn=lambda: is_vscode_connected(),
    )
```

### 1.2 工具注册流程（详细）

```
启动时：
  1. 创建 ToolRegistry 全局单例
  2. 扫描 tools/ 目录，导入所有 *_tool.py 文件
  3. 每个文件在模块级别调用 registry.register()
  4. 注册 Spirit 特有工具（vscode_notify 等）
  5. 工具就绪，等待调用

运行时：
  1. get_tool_definitions(agent) → 根据 toolset 过滤
  2. 附加 check_fn 结果缓存（30s TTL）
  3. 返回 OpenAI function calling 格式的 schema 列表
  4. 随 API 请求发送给 LLM

调用时：
  1. LLM 返回 tool_calls
  2. dispatcher.handle_function_call(name, args)
  3. coerce 参数类型
  4. registry.dispatch(name, args) → 调用 handler
  5. 返回结果给 LLM
```

### 1.3 工具 Schema 格式

```python
# OpenAI function calling 标准格式
{
    "type": "function",
    "function": {
        "name": "terminal",
        "description": "Execute a shell command in the terminal.\n\nReturns the command output.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 120)",
                    "default": 120
                }
            },
            "required": ["command"]
        }
    }
}
```

### 1.4 Spirit 特有工具（Hermes 没有的）

```python
# VSCode 集成工具
registry.register(
    name="vscode_open_file",
    toolset="vscode",
    schema={
        "type": "function",
        "function": {
            "name": "vscode_open_file",
            "description": "Open a file in the VSCode editor",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "integer", "description": "Line number to focus on"}
                },
                "required": ["path"]
            }
        }
    },
    handler=handle_vscode_open_file,
    check_fn=lambda: is_vscode_connected(),
)

# Web Dashboard 工具
registry.register(
    name="notify_dashboard",
    toolset="web",
    schema={...},
    handler=handle_notify_dashboard,
    check_fn=lambda: is_dashboard_connected(),
)
```

---

## 二、通信系统设计

### 2.1 LLM API 通信（Transport 层）

```
                    ┌───────────────────────────────────┐
                    │        Transport 层               │
                    │                                   │
  SpiritAgent ─────→│  get_transport(api_mode)          │
                    │       │                           │
                    │       ↓                           │
                    │  ┌─────────────────────────────┐  │
                    │  │  NormalizedResponse         │  │
                    │  │  {                          │  │
                    │  │    content: str,            │  │
                    │  │    tool_calls: List,        │  │
                    │  │    usage: Dict,             │  │
                    │  │    finish_reason: str       │  │
                    │  │  }                          │  │
                    │  └─────────────────────────────┘  │
                    │                                   │
                    └───────────┬───────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        │                       │                       │
  ┌─────┴─────┐          ┌─────┴─────┐          ┌─────┴─────┐
  │ OpenAI    │          │ Anthropic │          │ Gemini    │
  │ Transport │          │ Transport │          │ Transport │
  │           │          │           │          │           │
  │ SDK 调用  │          │ SDK 调用  │          │ SDK 调用  │
  │ → 标准化  │          │ → 标准化  │          │ → 标准化  │
  └───────────┘          └───────────┘          └───────────┘
```

### 2.2 前端通信协议

#### WebSocket 协议（VSCode / Web 聊天）

```typescript
// 连接建立
const ws = new WebSocket('ws://localhost:9001/ws/chat');

// 客户端 → 服务端
ws.send(JSON.stringify({
  type: 'user_message',
  content: '帮我写一个快速排序',
  session_id: 'abc-123',  // 可选，不传则创建新会话
  metadata: {              // 可选，前端特有信息
    cursor_position: { line: 42, column: 10 },
    open_files: ['main.py', 'utils.py'],
    selected_text: 'def sort(arr):'
  }
}));

// 服务端 → 客户端
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  
  switch (msg.type) {
    case 'session_created':
      // { session_id: 'xxx' }
      break;
    case 'assistant_text':
      // { content: '好的，我来帮你写...' }
      break;
    case 'stream_delta':
      // { token: '快速' }
      break;
    case 'tool_start':
      // { name: 'write_file', args: { path: '...' } }
      break;
    case 'tool_complete':
      // { name: 'write_file', result: '文件已写入' }
      break;
    case 'error':
      // { message: '...', code: 'TOOL_FAILED' }
      break;
    case 'done':
      // { usage: { prompt_tokens: 1234, ... } }
      break;
  }
};
```

#### HTTP REST + SSE（流式响应）

```
POST /api/v1/chat/stream
Content-Type: application/json

{
  "message": "帮我写一个快速排序",
  "session_id": "abc-123"
}

Response:
  Content-Type: text/event-stream
  
  data: {"type": "session_created", "session_id": "abc-123"}
  
  data: {"type": "assistant_text", "content": "好的，我来帮你写一个快速排序算法。"}
  
  data: {"type": "stream_delta", "token": "首先"}
  
  data: {"type": "stream_delta", "token": "，我们"}
  
  data: {"type": "tool_start", "name": "write_file", "args": {"path": "sort.py"}}
  
  data: {"type": "tool_complete", "name": "write_file", "result": "文件已写入"}
  
  data: {"type": "done", "usage": {"prompt_tokens": 1234, "completion_tokens": 567}}
```

### 2.3 多前端消息适配

```python
# 同一个 Agent 响应，不同前端的格式化

class ResponseFormatter:
    """根据平台格式化 Agent 响应"""
    
    def format(self, response: str, platform: str) -> str:
        if platform == "vscode":
            return self._format_vscode(response)  # Markdown + diff
        elif platform == "web":
            return self._format_web(response)     # 完整 Markdown
        elif platform == "telegram":
            return self._format_telegram(response)  # HTML/Markdown
        elif platform == "discord":
            return self._format_discord(response)  # Discord Markdown
        elif platform == "cli":
            return self._format_cli(response)      # Rich 渲染
        else:
            return response  # 原始 Markdown
```

---

## 三、数据库存储设计

### 3.1 SQLite Schema

```sql
-- 会话表
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,                    -- UUID
    source TEXT NOT NULL,                   -- "cli", "vscode", "web", "telegram"...
    model TEXT NOT NULL,                    -- 使用的模型
    model_config TEXT,                      -- JSON: 模型参数快照
    system_prompt TEXT,                     -- 系统提示词
    cwd TEXT,                               -- 工作目录
    git_repo_root TEXT,                     -- Git 仓库根目录
    parent_session_id TEXT REFERENCES sessions(id),  -- 父会话（压缩/分支）
    profile_name TEXT,                      -- 配置档案
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP,
    end_reason TEXT,                        -- "user_exit", "compression", "branched"
    metadata TEXT                           -- JSON: 扩展信息
);

-- 消息表
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,                     -- "user", "assistant", "tool", "system"
    content TEXT,                           -- 文本内容
    tool_calls TEXT,                        -- JSON: 工具调用列表
    tool_call_id TEXT,                      -- 工具调用 ID
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    metadata TEXT                           -- JSON: 扩展信息（如 token 计数）
);

-- 全文搜索索引
CREATE VIRTUAL TABLE messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='id'
);

-- 触发器：自动同步 FTS 索引
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

-- 索引
CREATE INDEX idx_messages_session ON messages(session_id);
CREATE INDEX idx_sessions_source ON sessions(source);
CREATE INDEX idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
```

### 3.2 数据库操作封装

```python
class SessionDB:
    """会话数据库操作封装"""
    
    def __init__(self, db_path: Path = None):
        self.db_path = db_path or Path.home() / ".spirit" / "state.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")  # 并发读写
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
    
    def create_session(self, session_id: str, source: str, model: str, **kwargs):
        """创建新会话"""
        self.conn.execute(
            """INSERT INTO sessions (id, source, model, cwd, profile_name, metadata)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, source, model, kwargs.get("cwd"), 
             kwargs.get("profile_name"), json.dumps(kwargs.get("metadata", {})))
        )
        self.conn.commit()
    
    def save_message(self, session_id: str, role: str, content: str, **kwargs):
        """保存消息"""
        self.conn.execute(
            """INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, role, content, 
             json.dumps(kwargs.get("tool_calls")),
             kwargs.get("tool_call_id"))
        )
        self.conn.commit()
    
    def search_messages(self, query: str, limit: int = 20) -> List[Dict]:
        """全文搜索消息"""
        cursor = self.conn.execute(
            """SELECT m.*, s.source, s.model
               FROM messages m
               JOIN sessions s ON m.session_id = s.id
               WHERE messages_fts MATCH ?
               ORDER BY m.created_at DESC
               LIMIT ?""",
            (query, limit)
        )
        return [dict(row) for row in cursor.fetchall()]
    
    def get_session_history(self, session_id: str) -> List[Dict]:
        """获取会话历史"""
        cursor = self.conn.execute(
            """SELECT * FROM messages 
               WHERE session_id = ? 
               ORDER BY created_at""",
            (session_id,)
        )
        return [dict(row) for row in cursor.fetchall()]
```

### 3.3 配置文件结构

```yaml
# ~/.spirit/config.yaml

# LLM 配置
llm:
  provider: "openai"              # openai, anthropic, google, azure, ollama
  model: "gpt-4o"                 # 默认模型
  api_key: ""                     # 或从 .env 读取
  base_url: ""                    # 自定义 API 地址（本地模型等）
  
  # 凭证池（多 key 轮换）
  credentials:
    - api_key: "sk-xxx"
      provider: "openai"
    - api_key: "sk-yyy"
      provider: "anthropic"

# 工具配置
tools:
  enabled_toolsets:
    - terminal
    - file
    - web
    - browser
  disabled_toolsets:
    - tts
    - image_gen
  
  # 工具特定配置
  terminal:
    default_timeout: 120
    allowed_commands: []          # 空 = 全部允许
  
  browser:
    headless: true

# 上下文压缩
compression:
  engine: "compressor"            # 或插件名
  threshold_percent: 0.75         # 达到 75% 时触发
  protect_first_n: 3              # 保护前 3 条消息
  protect_last_n: 6               # 保护最后 6 条消息

# 服务配置
server:
  host: "127.0.0.1"
  port: 9001
  enable_websocket: true
  enable_sse: true

# 消息网关
gateway:
  telegram:
    enabled: false
    bot_token: ""
  discord:
    enabled: false
    bot_token: ""

# 日志
logging:
  level: "INFO"
  file: "~/.spirit/logs/spirit.log"
```

---

## 四、并发模型设计

### 4.1 线程架构

```
主进程
│
├── 主线程（Main Thread）
│   ├── FastAPI 事件循环（asyncio）
│   ├── WebSocket 连接管理
│   └── Agent 调度
│
├── Agent 线程池（每个会话一个线程）
│   ├── Agent-1: 会话 A 的对话循环
│   ├── Agent-2: 会话 B 的对话循环
│   └── ...
│
└── 工具执行线程池（DaemonThreadPoolExecutor）
    ├── Worker-1: tool_1
    ├── Worker-2: tool_2
    └── ...
```

### 4.2 异步架构

```python
# FastAPI + asyncio 为主
# Agent 对话循环为 async
# 工具执行通过 run_in_executor 桥接

async def handle_chat(message: str, session_id: str):
    agent = get_or_create_agent(session_id)
    
    # 在 Agent 线程池中执行对话循环
    result = await asyncio.get_event_loop().run_in_executor(
        agent_executor,
        lambda: agent.run_conversation(message)
    )
    
    return result
```

---

*下一步：[05-vscode-extension-design.md](./05-vscode-extension-design.md) — VSCode 扩展设计*
