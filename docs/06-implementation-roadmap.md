# Spirit Agent 实施路线图

> 分阶段实施计划，从最小可用产品到完整生态。

---

## 一、开发阶段总览

```
Phase 1: 核心骨架 (4-6 周)
  └── Agent 核心 + 工具系统 + CLI + SQLite

Phase 2: API 服务 (2-3 周)
  └── FastAPI + WebSocket + HTTP REST

Phase 3: Web Dashboard (3-4 周)
  └── React 前端 + 聊天界面 + 配置管理

Phase 4: VSCode 扩展 (3-4 周)
  └── 扩展骨架 + 聊天侧边栏 + 代码补全

Phase 5: 消息网关 (2-3 周)
  └── Telegram + Discord 适配器

Phase 6: 打磨优化 (持续)
  └── 性能优化 + 插件系统 + 文档完善
```

---

## 二、Phase 1: 核心骨架 (4-6 周)

### 2.1 目标

构建最小可用的 Agent 核心，能通过 CLI 对话、调用工具、持久化会话。

### 2.2 任务清单

#### Week 1-2: 项目初始化 + Agent 核心

```bash
# 项目结构搭建
mkdir -p spirit/{agent,tools,storage,cli}
touch spirit/__init__.py
# ...

# 依赖配置
cat > pyproject.toml << 'EOF'
[project]
name = "spirit-agent"
version = "0.1.0"
dependencies = [
    "openai>=1.0.0",
    "httpx>=0.25.0",
    "pyyaml>=6.0",
    "rich>=13.0",
    "prompt-toolkit>=3.0",
]
EOF
```

**核心文件**：

| 文件 | 职责 | 参考 Hermes |
|------|------|------------|
| `spirit/agent/agent.py` | SpiritAgent 类 | run_agent.py |
| `spirit/agent/agent_init.py` | 初始化逻辑 | agent_init.py |
| `spirit/agent/conversation_loop.py` | 对话循环 | conversation_loop.py |
| `spirit/agent/system_prompt.py` | 系统提示词 | system_prompt.py + prompt_builder.py |

**关键代码**：

```python
# spirit/agent/agent.py — 最小实现
class SpiritAgent:
    def __init__(self, config: dict):
        self.model = config.get("model", "gpt-4o")
        self.api_key = config.get("api_key")
        self.base_url = config.get("base_url", "https://api.openai.com/v1")
        self.max_iterations = config.get("max_iterations", 90)
        self.session_id = str(uuid.uuid4())
        self._interrupt_requested = False
        self._api_call_count = 0
        
        # 初始化 OpenAI 客户端
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        
        # 初始化工具注册表
        from spirit.tools.registry import ToolRegistry
        self.registry = ToolRegistry()
        self._discover_tools()
    
    def run_conversation(self, user_message: str) -> str:
        messages = [{"role": "user", "content": user_message}]
        tools = self.registry.get_definitions()
        
        while self._api_call_count < self.max_iterations:
            if self._interrupt_requested:
                break
            
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools
            )
            self._api_call_count += 1
            
            choice = response.choices[0]
            
            if choice.message.tool_calls:
                for tool_call in choice.message.tool_calls:
                    result = self.registry.dispatch(
                        tool_call.function.name,
                        json.loads(tool_call.function.arguments)
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result
                    })
            else:
                return choice.message.content
        
        return "达到最大迭代次数"
```

#### Week 3: 工具系统

**复用 Hermes 工具**：

```bash
# 方式 1：符号链接（推荐开发期）
ln -s /path/to/hermes-agent-main/tools/registry.py spirit/tools/
ln -s /path/to/hermes-agent-main/tools/terminal_tool.py spirit/tools/
# ...

# 方式 2：复制（推荐发布期）
cp -r hermes-agent-main/tools/* spirit/tools/
```

**最小工具集**：

| 工具 | 优先级 | 说明 |
|------|--------|------|
| `terminal` | P0 | 终端执行 |
| `read_file` | P0 | 读取文件 |
| `write_file` | P0 | 写入文件 |
| `patch` | P0 | 补丁文件 |
| `web_search` | P1 | 网络搜索 |
| `web_extract` | P1 | 网页提取 |

#### Week 4: 数据库存储

**SQLite 实现**：

```python
# spirit/storage/session_db.py
class SessionDB:
    def __init__(self, db_path: Path = None):
        self.db_path = db_path or Path.home() / ".spirit" / "state.db"
        self._init_db()
    
    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        
        # 创建表
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (...);
            CREATE TABLE IF NOT EXISTS messages (...);
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(...);
        """)
```

#### Week 5-6: CLI 界面

```python
# spirit/cli/main.py
import click
from rich.console import Console
from rich.markdown import Markdown
from prompt_toolkit import PromptSession

@click.group()
def cli():
    """Spirit Agent - AI Agent for everyone"""
    pass

@cli.command()
def chat():
    """Start interactive chat"""
    console = Console()
    agent = create_agent()
    session = PromptSession()
    
    console.print("[bold green]Spirit Agent[/] ready! Type /help for commands.")
    
    while True:
        try:
            user_input = session.prompt("You> ")
            
            if user_input.startswith("/"):
                handle_command(user_input)
                continue
            
            console.print("[bold blue]Spirit>[/] ", end="")
            response = agent.run_conversation(user_input)
            console.print(Markdown(response))
            
        except KeyboardInterrupt:
            continue
        except EOFError:
            break

@cli.command()
def start():
    """Start Spirit backend service"""
    # TODO: Phase 2
    pass
```

### 2.3 Phase 1 交付物

- [ ] `spirit chat` 可交互对话
- [ ] 5 个核心工具可用（terminal, read_file, write_file, patch, web_search）
- [ ] 会话持久化到 SQLite
- [ ] 基础配置管理（~/.spirit/config.yaml）

---

## 三、Phase 2: API 服务 (2-3 周)

### 3.1 目标

构建 HTTP/WebSocket API，支持多前端接入。

### 3.2 任务清单

```python
# spirit/api/server.py
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Spirit Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# REST API
@app.post("/api/v1/chat")
async def chat(request: ChatRequest):
    agent = get_or_create_agent(request.session_id)
    response = agent.run_conversation(request.message)
    return {"response": response, "session_id": agent.session_id}

# WebSocket
@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()
    agent = SpiritAgent(config)
    
    while True:
        data = await websocket.receive_json()
        
        if data["type"] == "user_message":
            # 流式响应
            async for token in agent.stream_conversation(data["content"]):
                await websocket.send_json({
                    "type": "stream_delta",
                    "token": token
                })
            await websocket.send_json({"type": "done"})
```

### 3.3 Phase 2 交付物

- [ ] `spirit start` 启动 API 服务
- [ ] POST /api/v1/chat 可用
- [ ] WebSocket /ws/chat 流式响应
- [ ] 基础会话管理 API

---

## 四、Phase 3: Web Dashboard (3-4 周)

### 4.1 目标

构建 Web 管理面板，提供聊天、配置、会话历史功能。

### 4.2 技术栈

```
React 19 + TypeScript + Tailwind CSS + Vite
```

### 4.3 页面规划

| 页面 | 功能 |
|------|------|
| Chat | 聊天界面，支持流式响应 |
| Sessions | 会话历史列表，全文搜索 |
| Config | 配置编辑器 |
| Tools | 工具列表和状态 |

### 4.4 Phase 3 交付物

- [ ] `spirit dashboard` 启动 Web UI
- [ ] 聊天界面（流式响应）
- [ ] 会话历史（全文搜索）
- [ ] 配置编辑

---

## 五、Phase 4: VSCode 扩展 (3-4 周)

### 5.1 目标

构建 VSCode 扩展，提供编辑器内 AI 辅助体验。

### 5.2 任务清单

| 周 | 任务 |
|----|------|
| Week 1 | 扩展骨架 + SpiritClient 通信 |
| Week 2 | 聊天侧边栏 Webview |
| Week 3 | 代码补全 + 内联操作 |
| Week 4 | 工具审批 UI + 打磨 |

### 5.3 Phase 4 交付物

- [ ] VSCode 扩展可安装
- [ ] 聊天侧边栏可用
- [ ] 代码补全可用
- [ ] 右键菜单操作可用

---

## 六、Phase 5: 消息网关 (2-3 周)

### 6.1 目标

支持 Telegram/Discord 消息平台接入。

### 6.2 任务清单

```python
# spirit/gateway/platforms/telegram.py
from telegram import Update
from telegram.ext import Application, MessageHandler, filters

class TelegramAdapter:
    def __init__(self, bot_token: str):
        self.app = Application.builder().token(bot_token).build()
        self.app.add_handler(MessageHandler(filters.TEXT, self.handle_message))
    
    async def handle_message(self, update: Update, context):
        agent = get_or_create_agent(update.effective_chat.id)
        response = agent.run_conversation(update.message.text)
        await update.message.reply_text(response)
    
    async def start(self):
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
```

### 6.3 Phase 5 交付物

- [ ] `spirit gateway` 启动消息网关
- [ ] Telegram 适配器可用
- [ ] Discord 适配器可用

---

## 七、Phase 6: 打磨优化 (持续)

### 7.1 性能优化

- [ ] 上下文压缩引擎
- [ ] 凭证池轮换
- [ ] 工具执行并行化

### 7.2 插件系统

- [ ] 插件加载框架
- [ ] Memory 插件接口
- [ ] Context Engine 插件接口

### 7.3 文档完善

- [ ] 用户文档
- [ ] 开发者文档
- [ ] API 文档

---

## 八、技术栈汇总

### 8.1 后端

| 组件 | 技术 | 版本 |
|------|------|------|
| 语言 | Python | 3.11+ |
| LLM SDK | openai | 1.x |
| HTTP | httpx | 0.25+ |
| Web 框架 | FastAPI | 0.100+ |
| 数据库 | SQLite + FTS5 | 内置 |
| CLI | click + rich | 最新 |
| 配置 | pyyaml | 6.x |

### 8.2 前端

| 组件 | 技术 | 版本 |
|------|------|------|
| Web UI | React + TypeScript | 19 + 5.x |
| 构建 | Vite | 5.x |
| 样式 | Tailwind CSS | 4.x |
| VSCode | VSCode Extension API | 1.85+ |

---

## 九、从 Hermes 复用的清单

| 模块 | 复用方式 | 工作量 |
|------|---------|--------|
| `tools/registry.py` | 直接复用 | 低 |
| `tools/*_tool.py` (90+) | 直接复用 | 低 |
| `toolsets.py` | 参考精简 | 中 |
| `hermes_state.py` | 参考重写 | 高 |
| `context_engine.py` | 参考精简 | 中 |
| `error_classifier.py` | 直接复用 | 低 |
| `agent_init.py` | 大幅精简 | 高 |
| `conversation_loop.py` | 大幅精简 | 高 |

---

## 十、风险与应对

| 风险 | 影响 | 应对 |
|------|------|------|
| Hermes 工具依赖复杂 | 复用困难 | 逐步剥离，先复制再精简 |
| 多平台会话同步 | 状态一致性 | 统一 SessionDB，单写多读 |
| VSCode 扩展审核 | 上架延迟 | 提前了解审核规则 |
| LLM API 成本 | 用户成本 | 支持本地模型，上下文压缩 |

---

## 十一、文档索引

| 文档 | 内容 |
|------|------|
| [01-product-overview.md](./01-product-overview.md) | 产品愿景与定位 |
| [02-hermes-architecture-analysis.md](./02-hermes-architecture-analysis.md) | Hermes 架构深度分析 |
| [03-spirit-agent-architecture.md](./03-spirit-agent-architecture.md) | Spirit Agent 架构设计 |
| [04-technical-design.md](./04-technical-design.md) | 技术设计（工具/通信/存储） |
| [05-vscode-extension-design.md](./05-vscode-extension-design.md) | VSCode 扩展设计 |
| [06-implementation-roadmap.md](./06-implementation-roadmap.md) | 实施路线图（本文档） |

---

*文档完成！开始动手写代码吧~* 🚀
