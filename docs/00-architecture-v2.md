# Spirit Agent 架构设计 V2 — 完整技术方案

> 基于 Hermes 架构参考，设计 Spirit Agent V2 的完整技术架构，涵盖桌宠、语音、自动化任务、钩子系统等核心模块。

---

## 一、架构总览

### 1.1 分层架构图

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            前端接入层                                    │
│                                                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐ │
│  │ VSCode   │  │ 桌面精灵  │  │ Web      │  │ CLI      │  │ Message  │ │
│  │ Extension│  │ (桌宠)   │  │ Dashboard│  │ Terminal │  │ Gateway  │ │
│  │          │  │          │  │          │  │          │  │ TG/Disc  │ │
│  │ TypeScript│ │ Python   │  │ React+TS │  │ Python   │  │ Python   │ │
│  │          │  │ (Electron│  │          │  │ (Rich)   │  │          │ │
│  │          │  │  /PyQt)  │  │          │  │          │  │          │ │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘ │
│       └──────────────┴──────────────┴──────────────┴──────────────┘     │
│                                  │                                       │
├──────────────────────────────────┼───────────────────────────────────────┤
│                             API 网关层                                    │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │                      Spirit API Server                            │   │
│  │                                                                  │   │
│  │  HTTP REST    │  WebSocket   │  SSE (流式)  │  JSON-RPC          │   │
│  │  (FastAPI)       (实时通信)     (chat/stream)   (VSCode/TUI)     │   │
│  └────────────────────────────┬─────────────────────────────────────┘   │
│                               │                                          │
├───────────────────────────────┼──────────────────────────────────────────┤
│                          Agent 核心层                                     │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │                    SpiritAgent (状态容器 + 调度中心)               │   │
│  │                                                                  │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐   │   │
│  │  │ Conversation │  │ Context      │  │ Memory               │   │   │
│  │  │ Loop         │  │ Engine       │  │ Manager              │   │   │
│  │  │ (对话循环)   │  │ (上下文压缩) │  │ (记忆管理)           │   │   │
│  │  └──────────────┘  └──────────────┘  └──────────────────────┘   │   │
│  │                                                                  │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐   │   │
│  │  │ System       │  │ Task         │  │ Hook                 │   │   │
│  │  │ Prompt       │  │ Manager      │  │ Manager              │   │   │
│  │  │ (提示词构建) │  │ (任务管理)   │  │ (钩子管理)           │   │   │
│  │  └──────────────┘  └──────────────┘  └──────────────────────┘   │   │
│  │                                                                  │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐   │   │
│  │  │ Skill        │  │ Iteration    │  │ Event                │   │   │
│  │  │ Manager      │  │ Budget       │  │ Bus                  │   │   │
│  │  │ (技能管理)   │  │ (迭代预算)   │  │ (事件总线)           │   │   │
│  │  └──────────────┘  └──────────────┘  └──────────────────────┘   │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                               │                                          │
├───────────────────────────────┼──────────────────────────────────────────┤
│                          工具调度层                                       │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │                    Tool Dispatcher                                │   │
│  │                                                                  │   │
│  │  ┌────────────┐  ┌────────────┐  ┌──────────────────────────┐   │   │
│  │  │ Tool       │  │ Toolset    │  │ Tool Executor            │   │   │
│  │  │ Registry   │  │ Manager    │  │ (串行/并行/审批/重试)    │   │   │
│  │  │ (工具注册) │  │ (工具集)   │  │                          │   │   │
│  │  └────────────┘  └────────────┘  └──────────────────────────┘   │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                               │                                          │
├───────────────────────────────┼──────────────────────────────────────────┤
│                     工具实现层（复用 Hermes）                              │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │                    tools/ (90+ 工具)                              │   │
│  │                                                                  │   │
│  │  terminal │ file │ web │ browser │ vision │ tts │ code_exec       │   │
│  │  process  │ patch │ search │ navigate │ analyze │ speech │ todo   │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                          │
├──────────────────────────────────────────────────────────────────────────┤
│                          扩展层                                          │
│                                                                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐ │
│  │ Plugins  │  │ Skills   │  │ MCP      │  │ Cron     │  │ Desktop  │ │
│  │ (插件)   │  │ (技能)   │  │ Servers  │  │ (定时)   │  │ Pet      │ │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘  └──────────┘ │
│                                                                          │
├──────────────────────────────────────────────────────────────────────────┤
│                          基础设施层                                      │
│                                                                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐ │
│  │ Config   │  │ SQLite   │  │ Logging  │  │ i18n     │  │ Voice    │ │
│  │ (YAML)   │  │ + FTS5   │  │          │  │          │  │ Engine   │ │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘  └──────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 二、目录结构设计

```
spirit-agent-main/
│
├── spirit/                          # Python 后端核心
│   ├── __init__.py
│   │
│   ├── agent/                       # Agent 核心
│   │   ├── __init__.py
│   │   ├── agent.py                 # SpiritAgent 类（状态容器）
│   │   ├── agent_init.py            # 初始化逻辑
│   │   ├── conversation_loop.py     # 对话循环（ReAct 模式）
│   │   ├── context_compressor.py    # 上下文压缩引擎
│   │   ├── memory_manager.py        # 记忆管理
│   │   ├── prompt_builder.py        # 系统提示词构建
│   │   ├── error_handler.py         # 错误分类与处理
│   │   ├── streaming.py             # 流式响应处理
│   │   ├── tool_executor.py         # 工具执行引擎
│   │   └── tool_guardrails.py       # 工具安全护栏
│   │
│   ├── task/                        # 任务自动化系统 (新增)
│   │   ├── __init__.py
│   │   ├── task_manager.py          # 任务管理器
│   │   ├── task_models.py           # 任务数据模型
│   │   ├── task_executor.py         # 任务执行器
│   │   ├── task_planner.py          # 任务规划器（LLM 分解）
│   │   ├── todo_tool.py             # TODO 工具
│   │   └── delegate_tool.py         # 子代理委托工具
│   │
│   ├── hooks/                       # 钩子系统 (新增)
│   │   ├── __init__.py
│   │   ├── hook_manager.py          # 钩子管理器
│   │   ├── lifecycle_hooks.py       # 生命周期钩子
│   │   ├── tool_hooks.py            # 工具钩子
│   │   ├── task_hooks.py            # 任务钩子
│   │   └── conversation_hooks.py    # 对话钩子
│   │
│   ├── desktop/                     # 桌面精灵 (新增)
│   │   ├── __init__.py
│   │   ├── pet.py                   # 桌宠主类
│   │   ├── pet_window.py            # 悬浮窗 UI
│   │   ├── tray_icon.py             # 系统托盘
│   │   ├── monitor_panel.py         # 系统监控面板
│   │   ├── task_panel.py            # 任务进度面板
│   │   └── knowledge_panel.py       # 知识库面板
│   │
│   ├── voice/                       # 语音引擎 (新增)
│   │   ├── __init__.py
│   │   ├── voice_engine.py          # 语音引擎主类
│   │   ├── stt.py                   # 语音识别 (STT)
│   │   ├── tts.py                   # 语音合成 (TTS)
│   │   ├── wake_word.py             # 唤醒词检测
│   │   └── voice_mode.py            # Push-to-talk 语音模式
│   │
│   ├── moa/                         # Mixture of Agents (新增)
│   │   ├── __init__.py
│   │   ├── moa_loop.py              # 多模型协作循环
│   │   ├── moa_trace.py             # MoA 追踪记录
│   │   └── moa_config.py            # MoA 配置管理
│   │
│   ├── profile/                     # Profile 多实例系统 (新增)
│   │   ├── __init__.py
│   │   ├── profile_manager.py       # Profile 管理器
│   │   ├── profile_distribution.py  # Profile 分发（Git）
│   │   └── profile_describer.py     # Profile 自动描述
│   │
│   ├── goals/                       # 目标系统 (新增)
│   │   ├── __init__.py
│   │   ├── goal_engine.py           # 目标引擎（Ralph Loop）
│   │   └── goal_tracker.py          # 目标进度追踪
│   │
│   ├── tui/                         # TUI 终端界面 (新增)
│   │   ├── __init__.py
│   │   ├── tui_server.py            # TUI WebSocket 服务
│   │   ├── project_tree.py          # 项目树构建器
│   │   ├── slash_worker.py          # 斜杠命令处理器
│   │   └── render.py                # TUI 渲染桥接
│   │
│   ├── computer_use/                # 桌面控制 (新增)
│   │   ├── __init__.py
│   │   ├── cua_backend.py           # CUA 桌面控制后端
│   │   ├── vision_routing.py        # 视觉路由
│   │   ├── permissions.py           # 平台权限管理
│   │   └── schema.py                # 工具 Schema 定义
│   │
│   ├── dashboard/                   # Web Dashboard (新增)
│   │   ├── __init__.py
│   │   ├── web_server.py            # Web UI 服务器
│   │   ├── auth_provider.py         # 认证提供者
│   │   ├── session_export.py        # 会话导出 (HTML/MD/JSON)
│   │   └── skin_engine.py           # 主题/皮肤引擎
│   │
│   ├── security/                    # 安全扫描与审计 (新增)
│   │   ├── __init__.py
│   │   ├── tirith_scanner.py        # Tirith 安全扫描
│   │   ├── threat_patterns.py       # 威胁模式库
│   │   ├── skills_guard.py          # 技能安全守卫
│   │   ├── url_safety.py            # URL 安全检查
│   │   ├── supply_chain_audit.py    # 供应链审计
│   │   └── security_advisories.py   # 安全公告检查
│   │
│   ├── sessions/                    # 会话管理 (新增)
│   │   ├── __init__.py
│   │   ├── session_search.py        # 跨会话搜索
│   │   ├── session_recap.py         # 会话回顾
│   │   ├── session_export_html.py   # HTML 导出
│   │   ├── session_export_md.py     # Markdown 导出
│   │   └── session_filters.py       # 会话过滤/归档
│   │
│   ├── skills_hub/                  # 技能中心 (新增)
│   │   ├── __init__.py
│   │   ├── hub_manager.py           # 技能中心管理器
│   │   ├── curator.py               # 后台技能维护
│   │   ├── skills_sync.py           # 技能同步
│   │   └── skills_index.py          # 技能索引
│   │
│   ├── process/                     # 进程注册表 (新增)
│   │   ├── __init__.py
│   │   ├── process_registry.py      # 后台进程注册表
│   │   └── async_delegation.py      # 异步委托
│   │
│   ├── proxy/                       # 本地代理服务器 (新增)
│   │   ├── __init__.py
│   │   ├── proxy_server.py          # OpenAI 兼容代理
│   │   └── upstream.py              # 上游适配器
│   │
│   ├── checkpoint/                  # 检查点系统 (新增)
│   │   ├── __init__.py
│   │   └── checkpoint_manager.py    # 文件系统快照
│   │
│   ├── integrations/                # 外部集成 (新增)
│   │   ├── __init__.py
│   │   ├── home_assistant.py        # Home Assistant
│   │   ├── spotify.py               # Spotify
│   │   ├── google_meet.py           # Google Meet
│   │   └── teams.py                 # Microsoft Teams
│   │
│   ├── achievements/                # 成就系统 (新增)
│   │   ├── __init__.py
│   │   └── achievement_engine.py    # 成就引擎
│   │
│   ├── tools/                       # 工具系统（复用 Hermes）
│   │   ├── __init__.py
│   │   ├── registry.py              # 工具注册表
│   │   ├── approval.py              # 工具审批系统
│   │   ├── async_delegation.py      # 异步委托
│   │   ├── browser.py               # 浏览器自动化
│   │   ├── checkpoint.py            # 检查点
│   │   ├── code_intelligence.py     # 代码智能
│   │   ├── delegate_tool.py         # 子代理委托
│   │   ├── edit_proposal.py         # 编辑提案
│   │   ├── execute_code.py          # 代码执行
│   │   ├── file_operations.py       # 文件操作
│   │   ├── file_tools.py            # 文件工具
│   │   ├── gateway_primitives.py    # 网关原语
│   │   ├── memory_tool.py           # 记忆工具
│   │   ├── platforms.py             # 平台集成
│   │   ├── search_tools.py          # 搜索工具
│   │   ├── terminal_tool.py         # 终端工具
│   │   ├── todo_tool.py             # TODO 工具
│   │   ├── web_tools.py             # Web 工具
│   │   └── ...                      # 更多工具
│   │
│   ├── api/                         # API 服务
│   │   ├── __init__.py
│   │   ├── server.py                # FastAPI 服务
│   │   ├── context.py               # 上下文管理
│   │   ├── events.py                # 事件定义
│   │   └── websocket.py             # WebSocket 处理
│   │
│   ├── storage/                     # 数据存储
│   │   ├── __init__.py
│   │   ├── session_db.py            # SQLite 会话存储
│   │   └── config.py                # YAML 配置管理
│   │
│   ├── gateway/                     # 消息网关
│   │   ├── __init__.py
│   │   └── platforms/               # 平台适配器
│   │       ├── telegram.py
│   │       ├── discord.py
│   │       └── ...
│   │
│   └── cli/                         # CLI 入口
│       ├── __init__.py
│       └── main.py
│
├── vscode-extension/                # VSCode 扩展
│   ├── src/
│   │   ├── extension.ts
│   │   ├── ChatViewProvider.ts
│   │   ├── SpiritClient.ts
│   │   ├── CodeIntelligence.ts
│   │   ├── CodeEditor.ts
│   │   ├── TerminalManager.ts
│   │   └── WorkspaceContext.ts
│   └── media/
│
├── desktop-app/                     # 桌面精灵应用 (新增)
│   ├── main.py                      # 入口
│   ├── ui/                          # UI 组件
│   │   ├── pet_window.py
│   │   ├── monitor_panel.py
│   │   └── ...
│   └── assets/                      # 资源文件
│       ├── icons/
│       └── sounds/
│
├── web/                             # Web Dashboard
│   ├── src/
│   │   ├── pages/
│   │   │   ├── ChatPage/
│   │   │   ├── SessionsPage/
│   │   │   ├── TasksPage/           # 任务管理 (新增)
│   │   │   └── ConfigPage/
│   │   └── components/
│   └── ...
│
├── docs/                            # 文档
│   ├── 00-product-vision-v2.md
│   ├── 00-architecture-v2.md
│   └── ...
│
├── pyproject.toml
└── README.md
```

---

## 三、核心模块设计

### 3.1 SpiritAgent 类（转发器架构）

```python
# spirit/agent/agent.py
class SpiritAgent:
    """Spirit Agent 核心类 — 状态容器 + 调度中心
    
    借鉴 Hermes 的转发器架构（Forwarder Pattern）：
    - SpiritAgent 持有所有状态，但不实现具体逻辑
    - 所有逻辑委托给外部模块
    - 通过钩子系统实现扩展
    """
    
    # ============ 状态属性 ============
    
    # LLM 连接
    client: Any              # OpenAI 兼容客户端
    model: str
    provider: str
    
    # 工具系统
    tool_registry: ToolRegistry
    enabled_toolsets: List[str]
    
    # 会话
    session_id: str
    platform: str            # "cli" | "vscode" | "desktop_pet" | "web" | "telegram"
    
    # 控制
    max_iterations: int = 90
    _interrupt_requested: bool = False
    
    # 核心引擎
    context_compressor: ContextCompressor
    memory_manager: MemoryManager
    task_manager: TaskManager
    hook_manager: HookManager
    event_bus: EventBus
    
    # ============ 转发器方法 ============
    
    def __init__(self, config: AgentConfig):
        """初始化 — 委托给 agent_init.py"""
        from spirit.agent.agent_init import init_agent
        init_agent(self, config)
    
    def run_conversation(self, user_message: str, **kwargs) -> ConversationResult:
        """主入口 — 委托给 conversation_loop.py"""
        from spirit.agent.conversation_loop import run_conversation
        # 触发钩子
        self.hook_manager.emit("before_conversation", message=user_message)
        result = run_conversation(self, user_message, **kwargs)
        self.hook_manager.emit("after_conversation", result=result)
        return result
    
    def interrupt(self):
        """请求中断"""
        self._interrupt_requested = True
    
    # ============ 任务管理 ============
    
    def create_task(self, description: str) -> Task:
        """创建任务 — 委托给 task_manager"""
        return self.task_manager.create_task(description)
    
    def delegate_task(self, task: Task, subagent_type: str = "coding") -> SubAgent:
        """委托子代理 — 创建新的 SpiritAgent 实例"""
        from spirit.task.delegate_tool import create_subagent
        return create_subagent(self, task, subagent_type)
```

### 3.2 对话循环（ReAct 模式）

```python
# spirit/agent/conversation_loop.py
async def run_conversation(agent: SpiritAgent, user_message: str, **kwargs):
    """Spirit Agent 对话循环 — 借鉴 Hermes 的 ReAct 模式
    
    7 个执行阶段：
    1. 安检：中断检查、预算检查、消息修复
    2. 构建请求：清洗消息、注入记忆、拼系统提示词
    3. 调用 LLM：带重试机制的 API 调用
    4. 响应标准化：统一不同 Provider 的格式
    5. 分支判断：工具调用 → 执行 → continue；文本 → break
    6. 异常处理：分类错误，决定重试或停止
    7. 收尾：保存会话、触发记忆回顾
    """
    
    messages = build_messages(agent, user_message)
    tools = get_tool_definitions(agent)
    
    while agent._api_call_count < agent.max_iterations:
        # === 阶段 1: 安检 ===
        if agent._interrupt_requested:
            break
        
        # === 阶段 2: 构建请求 ===
        # 触发钩子
        agent.hook_manager.emit("before_llm_call", messages=messages)
        
        # 消息清洗
        messages = sanitize_messages(messages)
        # 注入外部记忆
        messages = inject_memory(messages, agent.memory_manager)
        # 拼系统提示词
        system_prompt = build_system_prompt(agent)
        
        # === 阶段 3: 调用 LLM ===
        try:
            response = await call_llm(agent, messages, tools)
            agent._api_call_count += 1
        except Exception as e:
            # 触发错误钩子
            agent.hook_manager.emit("on_llm_error", error=e)
            # 错误分类与重试
            classification = classify_error(e)
            if classification.should_retry:
                await asyncio.sleep(classification.retry_delay)
                continue
            else:
                raise
        
        # === 阶段 4: 响应标准化 ===
        response = normalize_response(response, agent.provider)
        
        # 触发钩子
        agent.hook_manager.emit("after_llm_call", response=response)
        
        # === 阶段 5: 分支判断 ===
        if response.tool_calls:
            # 有工具调用 → 执行工具
            for tool_call in response.tool_calls:
                # 触发工具钩子
                agent.hook_manager.emit("before_tool_execute", 
                    name=tool_call.name, args=tool_call.args)
                
                try:
                    result = agent.tool_registry.dispatch(
                        tool_call.name, 
                        json.loads(tool_call.arguments)
                    )
                    agent.hook_manager.emit("after_tool_execute",
                        name=tool_call.name, result=result)
                except ToolRejectedError:
                    result = "Tool execution rejected by user"
                except Exception as e:
                    agent.hook_manager.emit("on_tool_error",
                        name=tool_call.name, error=e)
                    result = f"Tool error: {e}"
                
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result
                })
            
            # 检查上下文是否需要压缩
            if agent.context_compressor.should_compress(response.usage):
                messages = agent.context_compressor.compress(messages)
                agent.hook_manager.emit("on_context_compress",
                    old_count=len(messages))
            
            continue  # 回到循环顶部
        
        else:
            # 文本回复 → 结束循环
            agent.hook_manager.emit("on_conversation_complete",
                response=response.content)
            return ConversationResult(
                response=response.content,
                messages=messages,
                usage=response.usage,
            )
    
    return ConversationResult(
        response="达到最大迭代次数",
        messages=messages,
    )
```

### 3.3 任务管理器

```python
# spirit/task/task_manager.py
class TaskManager:
    """任务管理器 — 管理 TODO 列表和任务执行"""
    
    def __init__(self, agent: SpiritAgent):
        self.agent = agent
        self.tasks: Dict[str, Task] = {}
        self.hook_manager = agent.hook_manager
    
    def create_task(self, description: str) -> Task:
        """创建新任务"""
        task = Task(
            id=str(uuid.uuid4()),
            description=description,
            state=TaskState.PENDING,
            created_at=datetime.now()
        )
        self.tasks[task.id] = task
        
        # 触发钩子
        self.hook_manager.emit("on_task_create", task=task)
        
        return task
    
    def plan_task(self, task: Task) -> List[SubTask]:
        """使用 LLM 分解任务为子任务"""
        from spirit.task.task_planner import plan_with_llm
        subtasks = plan_with_llm(self.agent, task.description)
        task.subtasks = subtasks
        
        for subtask in subtasks:
            self.hook_manager.emit("on_subtask_create", 
                task=task, subtask=subtask)
        
        return subtasks
    
    def execute_task(self, task: Task):
        """执行任务 — ReAct 循环"""
        task.state = TaskState.IN_PROGRESS
        self.hook_manager.emit("on_task_start", task=task)
        
        # 如果没有子任务，先规划
        if not task.subtasks:
            self.plan_task(task)
        
        # 逐个执行子任务
        for i, subtask in enumerate(task.subtasks):
            if subtask.state == TaskState.COMPLETED:
                continue
            
            subtask.state = TaskState.IN_PROGRESS
            progress = i / len(task.subtasks)
            
            self.hook_manager.emit("on_task_progress",
                task=task, progress=progress, 
                current_step=subtask.description)
            
            try:
                # 使用 Agent 执行子任务
                result = self.agent.run_conversation(
                    f"Execute: {subtask.description}"
                )
                
                # 验证结果
                if self._verify_result(subtask, result):
                    subtask.state = TaskState.COMPLETED
                    self.hook_manager.emit("on_subtask_complete",
                        task=task, subtask=subtask, result=result)
                else:
                    subtask.state = TaskState.FAILED
                    self.hook_manager.emit("on_subtask_fail",
                        task=task, subtask=subtask, reason="Verification failed")
                    
            except Exception as e:
                subtask.state = TaskState.FAILED
                self.hook_manager.emit("on_subtask_fail",
                    task=task, subtask=subtask, error=e)
        
        # 检查所有子任务是否完成
        if all(s.state == TaskState.COMPLETED for s in task.subtasks):
            task.state = TaskState.COMPLETED
            self.hook_manager.emit("on_task_complete", task=task)
        else:
            task.state = TaskState.FAILED
            self.hook_manager.emit("on_task_fail", task=task)
    
    def _verify_result(self, subtask: SubTask, result: str) -> bool:
        """验证子任务结果"""
        # 使用 LLM 验证，或运行测试
        verification_prompt = f"""
        Task: {subtask.description}
        Result: {result}
        
        Does the result correctly implement the task? Answer YES or NO.
        """
        response = self.agent.client.chat.completions.create(
            model=self.agent.model,
            messages=[{"role": "user", "content": verification_prompt}]
        )
        return "YES" in response.choices[0].message.content.upper()
```

### 3.4 钩子管理器

```python
# spirit/hooks/hook_manager.py
class HookManager:
    """钩子管理器 — 统一管理所有事件钩子
    
    设计原则（借鉴 Hermes）：
    1. 防御性编程：钩子失败不影响主流程
    2. 异常隔离：每个钩子独立 try-except
    3. 细粒度控制：支持按事件类型注册多个处理器
    """
    
    def __init__(self):
        self._hooks: Dict[str, List[Callable]] = {}
        self._global_handlers: List[Callable] = []  # 全局监听器
    
    def register(self, event: str, handler: Callable, priority: int = 0):
        """注册钩子处理器
        
        Args:
            event: 事件名称（如 "on_task_create"）
            handler: 处理函数
            priority: 优先级（数字越小越先执行）
        """
        self._hooks.setdefault(event, [])
        self._hooks[event].append((priority, handler))
        self._hooks[event].sort(key=lambda x: x[0])
    
    def emit(self, event: str, **kwargs):
        """触发钩子事件
        
        防御性设计：
        - 每个 handler 独立 try-except
        - 一个 handler 失败不影响其他
        - 记录 debug 日志但不中断
        """
        # 触发特定事件处理器
        for priority, handler in self._hooks.get(event, []):
            try:
                handler(**kwargs)
            except Exception as e:
                logger.debug(f"Hook {event}.{handler.__name__} failed: {e}")
        
        # 触发全局监听器
        for handler in self._global_handlers:
            try:
                handler(event, **kwargs)
            except Exception as e:
                logger.debug(f"Global hook handler failed: {e}")
    
    def on_all(self, handler: Callable):
        """注册全局监听器（监听所有事件）"""
        self._global_handlers.append(handler)


# ============ 预定义钩子实现 ============

def setup_default_hooks(agent: SpiritAgent):
    """设置默认钩子处理器"""
    hm = agent.hook_manager
    
    # --- 生命周期钩子 ---
    hm.register("on_session_start", lambda session_id: 
        logger.info(f"Session {session_id} started"))
    
    hm.register("on_session_end", lambda session_id:
        agent.session_db.save_session_state(session_id))
    
    # --- 工具钩子 ---
    hm.register("before_tool_execute", lambda name, args:
        check_dangerous_tool(name, args, agent))
    
    hm.register("after_tool_execute", lambda name, result:
        logger.info(f"Tool {name} executed successfully"))
    
    # --- 任务钩子 ---
    hm.register("on_task_progress", lambda task, progress, current_step:
        agent.event_bus.emit("task_progress", {
            "task_id": task.id,
            "progress": progress,
            "step": current_step
        }))
    
    hm.register("on_task_complete", lambda task:
        agent.event_bus.emit("task_complete", {"task_id": task.id}))
    
    # --- 对话钩子 ---
    hm.register("on_context_compress", lambda old_count:
        logger.info(f"Context compressed: {old_count} messages"))
```

---

## 四、桌面精灵架构

### 4.1 技术选型

| 方案 | 优点 | 缺点 | 选择 |
|------|------|------|------|
| **Electron** | 跨平台、Web 技术栈 | 内存占用大 | ❌ |
| **PyQt/PySide** | Python 原生、轻量 | UI 不够现代 | ⚠️ |
| **Tauri** | 轻量、现代、Rust 后端 | 需要 Rust | ⚠️ |
| **CustomTkinter** | 纯 Python、简单 | 功能有限 | ⚠️ |
| **Web + 系统托盘** | 最轻量、技术栈灵活 | 需要浏览器 | ✅ 推荐 |

**推荐方案：Web UI + 系统托盘**

```python
# spirit/desktop/pet.py
class SpiritPet:
    """桌面精灵 — 系统托盘 + Web UI"""
    
    def __init__(self, agent: SpiritAgent):
        self.agent = agent
        self.tray = SystemTray()  # 系统托盘图标
        self.server = PetWebServer()  # 本地 Web 服务
        self.voice = VoiceEngine()  # 语音引擎
        
    def start(self):
        """启动桌宠"""
        # 1. 显示系统托盘图标
        self.tray.show(on_click=self._on_tray_click)
        
        # 2. 启动本地 Web 服务（桌宠 UI）
        self.server.start(port=9002)
        
        # 3. 启动语音引擎
        self.voice.start()
        
        # 4. 注册钩子
        self._register_hooks()
    
    def _on_tray_click(self):
        """点击托盘图标 → 打开浏览器"""
        webbrowser.open("http://localhost:9002/pet")
    
    def _register_hooks(self):
        """注册桌宠相关钩子"""
        hm = self.agent.hook_manager
        
        # 任务进度更新 → 托盘图标动画
        hm.register("on_task_progress", self._update_tray_animation)
        
        # 任务完成 → 系统通知
        hm.register("on_task_complete", self._show_notification)
        
        # 语音指令 → Agent 对话
        self.voice.on_command(self._handle_voice_command)
    
    def _handle_voice_command(self, text: str):
        """处理语音指令"""
        response = self.agent.run_conversation(text)
        self.voice.speak(response)
```

### 4.2 桌宠 UI 面板

```
┌─────────────────────────────────────────────────────────┐
│  🐾 Spirit 桌宠                              [—] [×]   │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌─────┐  你好！我是 Spirit，你的编程小伙伴~            │
│  │ ◕‿◕ │  当前有 3 个任务正在执行。                    │
│  └─────┘  需要我帮你做什么吗？                         │
│                                                         │
│  ┌─────────────────────────────────────────────────┐   │
│  │ 🎤 按住说话...                                   │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐  │
│  │ 📊 监控  │ │ 📋 任务  │ │ 📚 知识  │ │ ⚙️ 设置  │  │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘  │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

---

## 4½、新增核心模块设计

### 4½.1 Mixture of Agents (MoA)

```python
# spirit/moa/moa_loop.py
class MoALoop:
    """多模型协作循环 — 借鉴 Hermes moa_loop.py"""
    
    def __init__(self, config: MoAConfig):
        self.slots: List[MoASlot] = config.slots      # 推理槽位
        self.aggregator: MoAAggregator = MoAAggregator(config.aggregator)
        self.rounds: int = config.rounds
    
    async def run(self, question: str, agent: SpiritAgent) -> str:
        """执行 MoA 循环"""
        responses = []
        for round_num in range(self.rounds):
            if round_num == 0:
                # 第一轮：每个槽位独立回答
                tasks = [slot.chat(question) for slot in self.slots]
                round_results = await asyncio.gather(*tasks)
                responses.extend(round_results)
            else:
                # 后续轮次：可以看到前面轮次的结果
                context = "\n".join(r.text for r in responses)
                tasks = [slot.chat(f"{question}\n\nPrevious answers:\n{context}") 
                         for slot in self.slots]
                round_results = await asyncio.gather(*tasks)
                responses.extend(round_results)
        
        # 聚合器综合所有结果
        return await self.aggregator.synthesize(question, responses)

# spirit/moa/moa_trace.py
class MoATrace:
    """记录 MoA 执行追踪"""
    rounds: List[MoARound]
    total_tokens: int
    total_cost: float
```

### 4½.2 Profile 多实例系统

```python
# spirit/profile/profile_manager.py
class ProfileManager:
    """Profile 管理器 — 多实例隔离"""
    
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir / "profiles"
    
    def list_profiles(self) -> List[Profile]:
        """列出所有 Profile"""
        return [Profile(d.name, d) for d in self.base_dir.iterdir() if d.is_dir()]
    
    def create_profile(self, name: str, template: str = "default") -> Profile:
        """创建新 Profile"""
        profile_dir = self.base_dir / name
        profile_dir.mkdir(parents=True)
        # 复制模板配置
        shutil.copytree(self.base_dir / template / "skills", profile_dir / "skills")
        return Profile(name, profile_dir)
    
    def switch_profile(self, name: str) -> AgentConfig:
        """切换 Profile — 返回新配置"""
        profile = self.get_profile(name)
        return AgentConfig.from_profile(profile)
    
    def get_active_profile(self) -> Profile:
        """获取当前活动的 Profile"""
        ...
```

### 4½.3 目标系统 (Ralph Loop)

```python
# spirit/goals/goal_engine.py
class GoalEngine:
    """目标引擎 — 持久化会话目标 (Ralph Loop)"""
    
    def __init__(self, agent: SpiritAgent):
        self.agent = agent
        self.goals: List[Goal] = []
    
    def set_goal(self, description: str, goal_type: str = "coding") -> Goal:
        """设置新目标"""
        goal = Goal(id=str(uuid.uuid4()), description=description, 
                    type=goal_type, state=GoalState.ACTIVE)
        self.goals.append(goal)
        self.agent.hook_manager.emit("on_goal_set", goal=goal)
        return goal
    
    def check_progress(self, conversation_context: str) -> GoalStatus:
        """检查目标进度 — 每轮对话后调用"""
        for goal in self.active_goals():
            prompt = f"Goal: {goal.description}\nContext: {conversation_context}\nIs this goal progressed?"
            result = self.agent.quick_llm_call(prompt)
            if "YES" in result.upper():
                goal.state = GoalState.COMPLETED
                self.agent.hook_manager.emit("on_goal_complete", goal=goal)
        return self._aggregate_status()
    
    def get_nudge(self) -> Optional[str]:
        """获取目标偏离提醒"""
        if self._is_off_track():
            return f"Reminder: Your current goal is '{self.current_goal.description}'"
        return None
```

### 4½.4 Computer Use 桌面控制

```python
# spirit/computer_use/cua_backend.py
class CUABackend:
    """CUA 桌面控制后端 — 借鉴 Hermes computer_use"""
    
    def screenshot(self) -> Image:
        """截取屏幕"""
        ...
    
    def click(self, x: int, y: int, button: str = "left"):
        """点击屏幕坐标"""
        ...
    
    def type_text(self, text: str):
        """输入文本"""
        ...
    
    def key_press(self, keys: List[str]):
        """按键组合"""
        ...
    
    def scroll(self, x: int, y: int, direction: str = "down"):
        """滚动"""
        ...

# spirit/computer_use/vision_routing.py
class VisionRouter:
    """视觉路由 — 截屏 → 视觉模型 → 定位元素"""
    
    def locate_element(self, screenshot: Image, description: str) -> Tuple[int, int]:
        """用视觉模型定位元素坐标"""
        response = self.vision_model.chat([
            {"role": "user", "content": [
                {"type": "image", "image": screenshot},
                {"type": "text", "text": f"Find: {description}. Return coordinates."}
            ]}
        ])
        return parse_coordinates(response)
```

### 4½.5 安全扫描系统

```python
# spirit/security/tirith_scanner.py
class TirithScanner:
    """Tirith 安全扫描 — 借鉴 Hermes tirith_security.py"""
    
    def __init__(self):
        self.patterns = ThreatPatternLibrary()
    
    def scan(self, tool_name: str, args: dict) -> ScanResult:
        """扫描工具调用是否安全"""
        threats = self.patterns.match(tool_name, args)
        if threats:
            return ScanResult(safe=False, threats=threats, 
                            action="block" if threats.severity == "critical" else "warn")
        return ScanResult(safe=True)

# spirit/security/skills_guard.py
class SkillsGuard:
    """技能安全守卫 — 扫描外部技能的安全性"""
    
    def scan_skill(self, skill_path: Path) -> SkillScanResult:
        """扫描技能文件的安全性"""
        # 检查可疑模式：eval/exec/ subprocess/os.system
        # 检查网络访问：requests/urllib/socket
        # 检查文件系统访问：open/write/unlink
        ...

# spirit/security/supply_chain_audit.py
class SupplyChainAuditor:
    """供应链审计"""
    
    def audit_install(self) -> AuditReport:
        """审计安装供应链安全性"""
        # 检查包来源、哈希校验、依赖树分析
        ...
```

### 4½.6 会话管理系统

```python
# spirit/sessions/session_search.py
class SessionSearch:
    """跨会话搜索 — 借鉴 Hermes session_search_tool.py"""
    
    def __init__(self, session_db: SessionDB):
        self.db = session_db
    
    def search(self, query: str, limit: int = 20) -> List[SessionMatch]:
        """FTS5 全文搜索 + 语义搜索"""
        # 1. FTS5 全文搜索
        fts_results = self.db.fts_search(query, limit)
        # 2. 语义搜索（嵌入向量）
        semantic_results = self.db.semantic_search(query, limit)
        # 3. 融合排序
        return self._merge_rank(fts_results, semantic_results)

# spirit/sessions/session_export_html.py
class SessionExportHTML:
    """HTML 会话导出"""
    
    def export(self, session_id: str) -> str:
        """导出为带样式的 HTML"""
        messages = self.db.get_messages(session_id)
        return self._render_html_template(messages)
```

### 4½.7 技能中心 (Skills Hub)

```python
# spirit/skills_hub/hub_manager.py
class SkillsHubManager:
    """技能中心管理器 — 借鉴 Hermes skills_hub.py (4227 行)"""
    
    def __init__(self, config: Config):
        self.config = config
        self.adapters = [GitHubAdapter(), LocalAdapter(), RegistryAdapter()]
    
    def search(self, query: str) -> List[SkillInfo]:
        """搜索技能中心"""
        results = []
        for adapter in self.adapters:
            results.extend(adapter.search(query))
        return results
    
    def install(self, skill_name: str) -> bool:
        """安装技能"""
        skill = self.search(skill_name)[0]
        # 下载 → 安全扫描 → 安装到技能目录
        SkillsGuard().scan_skill(skill.path)
        shutil.copytree(skill.path, self.config.skills_dir / skill_name)
        return True

# spirit/skills_hub/curator.py
class Curator:
    """后台技能维护 — 借鉴 Hermes curator.py (2016 行)"""
    
    def run_maintenance(self):
        """后台维护循环"""
        # 1. 检查技能更新
        updates = self._check_updates()
        # 2. 自动更新
        for skill in updates:
            self._update_skill(skill)
        # 3. 清理过期缓存
        self._cleanup_cache()
        # 4. 生成维护报告
        self._write_report()
```

### 4½.8 进程注册表与检查点

```python
# spirit/process/process_registry.py
class ProcessRegistry:
    """后台进程注册表 — 借鉴 Hermes process_registry.py (2348 行)"""
    
    def __init__(self):
        self._processes: Dict[str, ProcessHandle] = {}
        self._lock = threading.Lock()
    
    def start(self, name: str, cmd: List[str], **opts) -> ProcessHandle:
        """启动并注册后台进程"""
        proc = subprocess.Popen(cmd, **opts)
        handle = ProcessHandle(name=name, pid=proc.pid, process=proc)
        with self._lock:
            self._processes[handle.id] = handle
        return handle
    
    def recover_orphans(self) -> List[ProcessHandle]:
        """恢复孤儿进程"""
        ...

# spirit/checkpoint/checkpoint_manager.py
class CheckpointManager:
    """检查点管理器 — 借鉴 Hermes checkpoint_manager.py (1675 行)"""
    
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.checkpoint_dir = workspace / ".spirit_checkpoints"
    
    def create(self, label: str = "") -> Checkpoint:
        """创建文件系统快照"""
        cp = Checkpoint(id=str(uuid.uuid4()), label=label, 
                       timestamp=datetime.now(), files={})
        for file in self.workspace.rglob("*"):
            if not self._is_ignored(file):
                cp.files[str(file)] = file.read_bytes()
        self._save_checkpoint(cp)
        return cp
    
    def restore(self, checkpoint_id: str) -> bool:
        """恢复到指定检查点"""
        cp = self._load_checkpoint(checkpoint_id)
        for path, content in cp.files.items():
            Path(path).write_bytes(content)
        return True
```

### 4½.9 本地代理服务器

```python
# spirit/proxy/proxy_server.py
class SpiritProxyServer:
    """本地 OpenAI 兼容代理 — 借鉴 Hermes proxy/"""
    
    def __init__(self, agent: SpiritAgent):
        self.agent = agent
        self.app = FastAPI()
        self.upstreams = {}
    
    def start(self, port: int = 9001):
        """启动代理服务器"""
        @self.app.post("/v1/chat/completions")
        async def chat_completions(request: ChatCompletionRequest):
            # 路由到配置的 Provider
            upstream = self._resolve_upstream(request.model)
            return await upstream.forward(request)
        
        uvicorn.run(self.app, host="127.0.0.1", port=port)

# spirit/proxy/upstream.py
class UpstreamAdapter(ABC):
    """上游适配器基类"""
    @abstractmethod
    async def forward(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        ...

class NousPortalUpstream(UpstreamAdapter): ...
class XAIGrokUpstream(UpstreamAdapter): ...
class CustomUpstream(UpstreamAdapter): ...
```

### 4½.10 外部集成

```python
# spirit/integrations/home_assistant.py
class HomeAssistantIntegration:
    """Home Assistant 智能家居集成"""
    
    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
    
    async def call_service(self, domain: str, service: str, data: dict):
        """调用 HA 服务"""
        ...
    
    async def get_states(self) -> List[EntityState]:
        """获取所有实体状态"""
        ...

# spirit/integrations/spotify.py
class SpotifyIntegration:
    """Spotify 音乐服务集成"""
    
    async def play(self, uri: str = None):
        """播放音乐"""
        ...
    
    async def search(self, query: str, type: str = "track") -> List[dict]:
        """搜索音乐"""
        ...

# spirit/achievements/achievement_engine.py
class AchievementEngine:
    """成就引擎 — 借鉴 Hermes achievements plugin"""
    
    ACHIEVEMENTS = {
        "first_code": {"name": "第一行代码", "desc": "完成第一个编码任务"},
        "bug_slayer": {"name": "Bug 终结者", "desc": "连续修复 10 个 Bug"},
        "tool_master": {"name": "工具大师", "desc": "使用过 20 种不同工具"},
        "polyglot": {"name": "多语言者", "desc": "使用过 5 个不同 Provider"},
        "marathon": {"name": "马拉松", "desc": "单日完成 50 个任务"},
    }
    
    def check(self, event: str, context: dict) -> List[Achievement]:
        """检查是否解锁新成就"""
        newly_unlocked = []
        for ach_id, ach in self.ACHIEVEMENTS.items():
            if not self._is_unlocked(ach_id) and self._check_condition(ach_id, context):
                self._unlock(ach_id)
                newly_unlocked.append(ach)
        return newly_unlocked
```

---

## 五、数据流全景

### 5.1 用户指令 → 任务执行 → 结果反馈

```
用户（语音/文本）: "帮我实现用户认证模块"
     │
     ↓
┌─────────────────────────────────────────────────────────┐
│  1. 前端接收指令                                         │
│     • 桌宠: 语音识别 → 文本                              │
│     • VSCode: 聊天输入                                   │
│     • Telegram: 消息接收                                 │
└─────────────────────────┬───────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│  2. API 网关路由                                         │
│     • WebSocket → Agent 调度                             │
│     • 创建/获取 SpiritAgent 实例                         │
└─────────────────────────┬───────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│  3. Agent 对话循环（ReAct）                              │
│     • 构建消息 + 系统提示词                              │
│     • 调用 LLM                                          │
│     • LLM 决定：创建 TODO → 分解子任务                   │
│     • 触发钩子: on_task_create, on_subtask_create        │
└─────────────────────────┬───────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│  4. 任务执行循环                                         │
│     for subtask in subtasks:                             │
│       • 触发钩子: on_task_start                          │
│       • 调用工具: write_file, terminal, etc.             │
│       • 触发钩子: before/after_tool_execute              │
│       • 验证结果                                         │
│       • 触发钩子: on_task_progress                       │
│       • 推送到前端: WebSocket broadcast                  │
└─────────────────────────┬───────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│  5. 结果反馈                                             │
│     • 触发钩子: on_task_complete                         │
│     • 推送到桌宠: 系统通知 + 语音播报                    │
│     • 推送到 VSCode: 侧边栏更新                          │
│     • 推送到 Web: 页面刷新                               │
│     • 推送到 Telegram: 消息推送                          │
└─────────────────────────────────────────────────────────┘
```

---

## 六、技术栈汇总

### 6.1 后端

| 组件 | 技术 | 版本 |
|------|------|------|
| 语言 | Python | 3.11+ |
| LLM SDK | openai | 1.x |
| HTTP | httpx | 0.25+ |
| Web 框架 | FastAPI | 0.100+ |
| 数据库 | SQLite + FTS5 | 内置 |
| CLI | click + rich | 最新 |
| 配置 | pyyaml | 6.x |
| 语音 | whisper + NeuTTS | 最新 |
| 桌宠 | pystray + webbrowser | 最新 |
| 桌面控制 | pyautogui + pynput | 最新 |
| TUI | textual / curses | 最新 |
| 代理服务器 | uvicorn + FastAPI | 最新 |
| 安全扫描 | 自研 (Tirith + 模式匹配) | - |
| 模糊匹配 | thefuzz / rapidfuzz | 最新 |

### 6.2 前端

| 组件 | 技术 | 版本 |
|------|------|------|
| Web UI | React + TypeScript | 19 + 5.x |
| 构建 | Vite | 5.x |
| 样式 | Tailwind CSS | 4.x |
| VSCode | VSCode Extension API | 1.85+ |
| 桌宠 UI | Web (localhost:9002) | - |
| TUI | Terminal WebSocket + React | - |

---

## 七、从 Hermes 复用的清单

| 模块 | 复用方式 | 工作量 |
|------|---------|--------|
| `tools/registry.py` | 直接复用 | 低 |
| `tools/*_tool.py` (90+) | 功能域合并为 30 个模块 | 低 |
| `conversation_loop.py` 设计模式 | 参考精简 | 高 |
| `context_compressor.py` | 参考精简 | 中 |
| `error_classifier.py` | 直接复用 | 低 |
| `agent_init.py` 转发器模式 | 参考设计 | 中 |
| 钩子系统设计 | 参考 Hermes callback 模式 | 中 |
| 任务系统（TODO + delegate） | 参考 Hermes 的 todo_tool + delegate_tool | 高 |
| `moa_loop.py` + `moa_trace.py` | 参考多模型协作设计 | 中 |
| `profiles.py` + `profile_distribution.py` | 参考多实例隔离设计 | 中 |
| `goals.py` (Ralph Loop) | 参考持久目标设计 | 中 |
| `tui_gateway/` | 参考 TUI WebSocket 架构 | 高 |
| `computer_use/` | 直接复用桌面控制后端 | 中 |
| `web_server.py` (18K 行) | 参考精简 Web Dashboard | 高 |
| `session_search_tool.py` | 参考跨会话搜索 | 低 |
| `skills_hub.py` (4K 行) + `curator.py` (2K 行) | 参考技能中心设计 | 高 |
| `process_registry.py` (2K 行) | 参考进程管理设计 | 中 |
| `proxy/` | 参考本地代理设计 | 低 |
| `checkpoint_manager.py` (1.6K 行) | 参考检查点设计 | 中 |
| `tirith_security.py` + `threat_patterns.py` | 参考安全扫描设计 | 中 |
| `skills_guard.py` (1.1K 行) | 参考技能安全扫描 | 低 |
| `fuzzy_match.py` (950 行) | 参考模糊匹配设计 | 低 |
| `patch_parser.py` (637 行) | 参考补丁解析设计 | 低 |
| `voice_mode.py` (1.2K 行) | 参考 Push-to-talk 设计 | 低 |
| `blueprint_catalog.py` (713 行) | 参考蓝图自动化设计 | 中 |
| `homeassistant_tool.py` / Spotify / Meet | 参考外部集成设计 | 低 |
| `achievements/plugin_api.py` | 参考成就引擎设计 | 低 |
| `tool_search.py` (735 行) | 参考工具渐进式发现 | 低 |

---

## 八、模块总数统计

| 功能域 | 目录数 | 关键模块 |
|--------|--------|----------|
| **Agent 核心** | `spirit/agent/` | conversation_loop, context_compressor, memory_manager, prompt_builder, error_classifier, tool_executor |
| **任务自动化** | `spirit/task/` | task_manager, task_planner, todo_tool, delegate_tool |
| **钩子系统** | `spirit/hooks/` | hook_manager, lifecycle/tool/task/conversation_hooks |
| **桌面精灵** | `spirit/desktop/` | pet, pet_window, tray_icon, monitor/task/knowledge_panel |
| **语音引擎** | `spirit/voice/` | voice_engine, stt, tts, wake_word, voice_mode |
| **MoA 多模型** | `spirit/moa/` | moa_loop, moa_trace, moa_config |
| **Profile 系统** | `spirit/profile/` | profile_manager, profile_distribution, profile_describer |
| **目标系统** | `spirit/goals/` | goal_engine, goal_tracker |
| **TUI 终端** | `spirit/tui/` | tui_server, project_tree, slash_worker, render |
| **桌面控制** | `spirit/computer_use/` | cua_backend, vision_routing, permissions, schema |
| **Web Dashboard** | `spirit/dashboard/` | web_server, auth_provider, session_export, skin_engine |
| **安全系统** | `spirit/security/` | tirith_scanner, threat_patterns, skills_guard, url_safety, supply_chain_audit |
| **会话管理** | `spirit/sessions/` | session_search, session_recap, session_export_html/md, session_filters |
| **技能中心** | `spirit/skills_hub/` | hub_manager, curator, skills_sync, skills_index |
| **进程管理** | `spirit/process/` | process_registry, async_delegation |
| **本地代理** | `spirit/proxy/` | proxy_server, upstream |
| **检查点** | `spirit/checkpoint/` | checkpoint_manager |
| **外部集成** | `spirit/integrations/` | home_assistant, spotify, google_meet, teams |
| **成就系统** | `spirit/achievements/` | achievement_engine |
| **工具系统** | `spirit/tools/` | 30 个模块，59 个注册工具 |
| **消息网关** | `spirit/gateway/` | platforms (Telegram/Discord/Slack/钉钉/飞书/QQ/...) |
| **API 服务** | `spirit/api/` | server, websocket, events, context |
| **数据存储** | `spirit/storage/` | session_db, config |

---

*下一步：开始编码实现！* 🚀
