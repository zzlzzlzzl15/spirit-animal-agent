# Spirit Agent 产品愿景 V2 — 桌面精灵与智能自动化

> **一句话定位**：一个本地部署的 AI 智能体服务，既是 VSCode 中的编程助手，也是桌面上的灵动小伙伴，更是自动化的任务执行者。

---

## 一、产品愿景

### 1.1 为什么需要 Spirit Agent V2？

当前 AI 编程助手市场存在三大割裂：

| 割裂 | 现状 | Spirit V2 解决方案 |
|------|------|-------------------|
| **交互割裂** | 只能在编辑器/终端/网页中使用，离开就断联 | **桌宠常驻** + 多端同步，随时召唤 |
| **能力割裂** | 要么只能写代码，要么只能聊天 | **全栈智能体**：编码 + 部署 + 监控 + 迭代 |
| **自动化割裂** | 需要人工逐步指令，无法自主规划执行 | **ReAct 自动化**：创建任务 → 分解 → 执行 → 检查 → 迭代 |

**Spirit V2 的核心理念**：

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│   "不是工具，是伙伴；不是助手，是队友"                              │
│                                                                 │
│   • 像 QQ 宠物一样陪伴你，点击就能对话                            │
│   • 像项目经理一样规划任务，自主执行                               │
│   • 像全栈工程师一样写代码、部署、调试                            │
│   • 像管家一样监控系统状态，主动汇报                              │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 三大产品形态

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Spirit Agent V2                               │
│                                                                     │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐  │
│  │   VSCode 扩展     │  │   桌面精灵(桌宠)  │  │   Web Dashboard  │  │
│  │                  │  │                  │  │                  │  │
│  │  • 代码补全       │  │  • 语音对话       │  │  • 聊天界面       │  │
│  │  • 内联操作       │  │  • 系统监控       │  │  • 会话历史       │  │
│  │  • 工具审批       │  │  • 知识库查看     │  │  • 配置管理       │  │
│  │  • 终端集成       │  │  • 任务进度       │  │  • 工具管理       │  │
│  │                  │  │  • 语音任务部署   │  │                  │  │
│  └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘  │
│           │                      │                      │           │
│           └──────────────────────┼──────────────────────┘           │
│                                  │                                   │
│                           ┌──────┴──────┐                           │
│                           │  Spirit 后端 │                           │
│                           │  (本地服务)  │                           │
│                           └──────┬──────┘                           │
│                                  │                                   │
│              ┌───────────────────┼───────────────────┐              │
│              ↓                   ↓                   ↓              │
│         LLM API            工具系统             数据库              │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 二、桌面精灵（桌宠）— 核心创新

### 2.1 产品定位

**桌宠不是附属品，是 Spirit Agent 的灵魂入口。**

就像 QQ 宠物是你 QQ 旅程的陪伴，Spirit 桌宠是你的编程旅程的伙伴。

### 2.2 核心功能

#### 2.2.1 语音对话

```
┌─────────────────────────────────────────────────────────┐
│                                                         │
│   用户点击桌宠 → 弹出对话框 → 按住说话 → Spirit 回应     │
│                                                         │
│   技术栈：                                              │
│   • 语音识别：Whisper (本地) / Azure STT (云端)          │
│   • 语音合成：NeuTTS (本地) / Azure TTS (云端)           │
│   • 唤醒词："Hey Spirit" / "小灵"                       │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

**语音交互场景**：

| 场景 | 用户说 | Spirit 做 |
|------|--------|----------|
| **任务部署** | "帮我部署一下项目" | 调用 terminal → 执行部署脚本 → 汇报结果 |
| **状态查询** | "现在项目什么状态？" | 检查 Git 状态、运行测试、汇报进度 |
| **代码解释** | "这段代码什么意思？" | 读取当前文件 → 解释选中代码 |
| **知识检索** | "之前那个 bug 怎么修的？" | 搜索会话历史 → 找到相关对话 |

#### 2.2.2 系统监控面板

点击桌宠 → 展开监控面板：

```
┌─────────────────────────────────────────────────────────┐
│  Spirit Agent 监控面板                          [×]      │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  📊 系统状态                                            │
│  ┌─────────────────────────────────────────────────┐   │
│  │ CPU: 45%  ████████░░░░░░░░  Memory: 6.2/16 GB  │   │
│  │ GPU: 23%  ████░░░░░░░░░░░░  Disk: 234/512 GB   │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
│  🤖 Agent 状态                                          │
│  ┌─────────────────────────────────────────────────┐   │
│  │ 模型: gpt-4o  │  会话: 3 个活跃  │  工具: 47 个  │   │
│  │ Token: 12.5K  │  费用: $0.23     │  迭代: 15/90  │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
│  📋 当前任务                                            │
│  ┌─────────────────────────────────────────────────┐   │
│  │ ✅ 代码审查 (完成)                               │   │
│  │ 🔄 部署到测试环境 (进行中 60%)                   │   │
│  │ ⏳ 运行集成测试 (等待中)                         │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
│  📚 知识库                                              │
│  ┌─────────────────────────────────────────────────┐   │
│  │ 最近会话: 156 条  │  记忆条目: 42  │  技能: 12   │   │
│  │ [查看会话历史]  [查看记忆]  [查看技能]            │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

#### 2.2.3 任务执行进度

桌宠实时展示当前任务的执行进度：

```
┌─────────────────────────────────────────────────────────┐
│  🎯 任务：实现用户认证模块                    [暂停] [取消]│
├─────────────────────────────────────────────────────────┤
│                                                         │
│  进度: ████████████░░░░░░░░ 60% (3/5 子任务完成)        │
│                                                         │
│  ✅ 1. 创建 User 模型                      00:12        │
│  ✅ 2. 实现注册 API                        00:34        │
│  ✅ 3. 实现登录 API                        00:28        │
│  🔄 4. 添加 JWT 认证中间件                 01:15...     │
│  ⏳ 5. 编写单元测试                        --:--        │
│                                                         │
│  📝 当前步骤:                                           │
│  "正在创建 auth/middleware.py，添加 JWT 验证逻辑..."     │
│                                                         │
│  💬 [查看详情]  [发送消息]  [中断当前步骤]               │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### 2.3 桌宠技术实现

```python
# spirit/desktop/pet.py
class SpiritPet:
    """桌面精灵 — 系统托盘 + 悬浮窗 + 语音交互"""
    
    def __init__(self, agent: SpiritAgent):
        self.agent = agent
        self.tray_icon = SystemTrayIcon()
        self.pet_window = PetWindow()  # 悬浮窗
        self.voice_engine = VoiceEngine()  # 语音引擎
        self.monitor = SystemMonitor()  # 系统监控
        
    def start(self):
        """启动桌宠服务"""
        self.tray_icon.show()
        self.pet_window.show()
        self.voice_engine.start_listening()
        self._start_monitor_loop()
        
    def on_click(self):
        """点击桌宠 → 弹出交互面板"""
        self.pet_window.toggle_chat_panel()
        
    def on_voice_command(self, text: str):
        """语音指令处理"""
        response = self.agent.run_conversation(text)
        self.voice_engine.speak(response)
        self.pet_window.show_response(response)
```

---

## 三、自动化任务系统 — 核心能力

### 3.1 设计理念

**借鉴 Hermes 的 ReAct 模式，实现自主任务规划与执行。**

```
用户指令: "实现用户认证模块"
          ↓
    ┌─────────────┐
    │  LLM 规划    │ ← 系统提示词引导 + 上下文感知
    │  任务分解    │
    └──────┬──────┘
           ↓
    ┌─────────────────────────────────────────┐
    │  TODO List (任务清单)                    │
    │                                         │
    │  1. 创建 User 模型          [pending]   │
    │  2. 实现注册 API            [pending]   │
    │  3. 实现登录 API            [pending]   │
    │  4. 添加 JWT 中间件         [pending]   │
    │  5. 编写单元测试            [pending]   │
    └──────────────┬──────────────────────────┘
                   ↓
    ┌─────────────────────────────────────────┐
    │  ReAct 循环                              │
    │                                         │
    │  while 有未完成任务:                     │
    │    1. 选择下一个任务                     │
    │    2. 执行任务 (调用工具)                │
    │    3. 检查结果                           │
    │    4. 标记完成 / 重试 / 跳过             │
    │    5. 更新进度                           │
    └─────────────────────────────────────────┘
```

### 3.2 任务生命周期

```
创建 → 规划 → 执行 → 检查 → 完成/失败
 │       │       │       │       │
 ↓       ↓       ↓       ↓       ↓
用户    LLM    工具    验证    标记
指令    分解    调用    测试    状态
```

**任务状态机**：

```python
class TaskState(Enum):
    PENDING = "pending"        # 等待执行
    IN_PROGRESS = "in_progress"  # 执行中
    WAITING_REVIEW = "waiting_review"  # 等待审查
    COMPLETED = "completed"    # 已完成
    FAILED = "failed"          # 失败
    SKIPPED = "skipped"        # 跳过
    CANCELLED = "cancelled"    # 取消
```

### 3.3 子任务委托（Fork）

**借鉴 Hermes 的 `delegate_task` 工具，实现子代理并行执行。**

```python
# 主 Agent 创建子任务
todo_tool.create_task("实现用户认证模块")

# 主 Agent 分解为子任务
todo_tool.add_subtask("创建 User 模型")
todo_tool.add_subtask("实现注册 API")
todo_tool.add_subtask("实现登录 API")

# 主 Agent 委托子代理执行
delegate_tool.delegate(
    task="实现注册 API",
    subagent_type="coding",
    context={"model": "User", "framework": "FastAPI"}
)

# 子代理独立执行，完成后汇报
```

### 3.4 任务进度实时推送

```python
# 任务进度事件
class TaskProgressEvent:
    task_id: str
    task_name: str
    state: TaskState
    progress: float  # 0.0 - 1.0
    current_step: str
    elapsed_time: float
    subtasks: List[SubTaskProgress]

# 推送到各前端
event_bus.emit("task_progress", TaskProgressEvent(...))

# VSCode 扩展 → 侧边栏更新
# 桌宠 → 悬浮窗更新
# Web Dashboard → WebSocket 推送
# Telegram → 消息推送
```

---

## 四、钩子系统 — 核心机制

### 4.1 设计理念

**借鉴 Hermes 的防御性编程和生命周期钩子，构建可扩展的事件系统。**

```
┌─────────────────────────────────────────────────────────┐
│                    钩子系统架构                           │
│                                                         │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐ │
│  │ 生命周期钩子 │    │ 工具钩子     │    │ 任务钩子     │ │
│  │             │    │             │    │             │ │
│  │ on_init     │    │ before_tool │    │ on_task_    │ │
│  │ on_session  │    │ after_tool  │    │   create    │ │
│  │ on_destroy  │    │ on_tool_    │    │ on_task_    │ │
│  │             │    │   error     │    │   complete  │ │
│  └──────┬──────┘    └──────┬──────┘    └──────┬──────┘ │
│         │                   │                   │       │
│         └───────────────────┼───────────────────┘       │
│                             │                           │
│                      ┌──────┴──────┐                    │
│                      │  EventBus   │                    │
│                      │  (事件总线)  │                    │
│                      └──────┬──────┘                    │
│                             │                           │
│              ┌──────────────┼──────────────┐            │
│              ↓              ↓              ↓            │
│         日志记录        状态通知        插件扩展         │
└─────────────────────────────────────────────────────────┘
```

### 4.2 钩子分类

#### 4.2.1 生命周期钩子

| 钩子 | 触发时机 | 用途 |
|------|---------|------|
| `on_agent_init` | Agent 初始化完成 | 加载插件、注册工具 |
| `on_session_start` | 新会话开始 | 加载历史、初始化上下文 |
| `on_session_end` | 会话结束 | 保存状态、清理资源 |
| `on_session_reset` | `/new` 或 `/reset` | 重置引擎状态 |
| `on_agent_destroy` | Agent 销毁 | 持久化记忆、关闭连接 |

#### 4.2.2 工具钩子

| 钩子 | 触发时机 | 用途 |
|------|---------|------|
| `before_tool_execute` | 工具执行前 | 参数校验、审批检查 |
| `after_tool_execute` | 工具执行后 | 结果处理、日志记录 |
| `on_tool_error` | 工具执行失败 | 错误处理、重试逻辑 |
| `on_tool_approved` | 用户审批通过 | 继续执行 |
| `on_tool_rejected` | 用户审批拒绝 | 跳过或取消 |

#### 4.2.3 任务钩子

| 钩子 | 触发时机 | 用途 |
|------|---------|------|
| `on_task_create` | 任务创建 | 通知前端、记录日志 |
| `on_task_start` | 任务开始执行 | 更新进度、启动计时 |
| `on_task_complete` | 任务完成 | 标记状态、触发下一个 |
| `on_task_fail` | 任务失败 | 重试或跳过、通知用户 |
| `on_task_progress` | 任务进度更新 | 实时推送到前端 |

#### 4.2.4 对话钩子

| 钩子 | 触发时机 | 用途 |
|------|---------|------|
| `before_llm_call` | 调用 LLM 前 | 消息清洗、上下文注入 |
| `after_llm_call` | LLM 响应后 | 响应标准化、缓存 |
| `on_stream_delta` | 流式 token | 实时推送到前端 |
| `on_context_compress` | 上下文压缩触发 | 压缩、分裂会话 |

### 4.3 钩子实现

```python
# spirit/agent/hooks.py
class HookManager:
    """钩子管理器 — 统一管理所有事件钩子"""
    
    def __init__(self):
        self._hooks: Dict[str, List[Callable]] = {}
        
    def register(self, event: str, handler: Callable):
        """注册钩子处理器"""
        self._hooks.setdefault(event, []).append(handler)
        
    def emit(self, event: str, **kwargs):
        """触发钩子事件"""
        for handler in self._hooks.get(event, []):
            try:
                handler(**kwargs)
            except Exception as e:
                logger.debug(f"Hook {event}.{handler.__name__} failed: {e}")
                # 防御性编程：钩子失败不影响主流程

# 使用示例
hook_manager = HookManager()

# 注册生命周期钩子
hook_manager.register("on_session_start", lambda session_id: print(f"Session {session_id} started"))

# 注册工具钩子
hook_manager.register("before_tool_execute", lambda name, args: check_approval(name, args))

# 注册任务钩子
hook_manager.register("on_task_complete", lambda task: notify_dashboard(task))
```

### 4.4 钩子应用场景

#### 场景 1：工具审批

```python
# 危险工具需要用户审批
def check_tool_approval(name: str, args: dict):
    if name in DANGEROUS_TOOLS:
        # 发送审批请求到前端
        event_bus.emit("tool_approval_required", {
            "tool": name,
            "args": args,
            "callback": wait_for_approval
        })
        # 阻塞等待用户审批
        approved = wait_for_approval()
        if not approved:
            raise ToolRejectedError("User rejected the tool execution")

hook_manager.register("before_tool_execute", check_tool_approval)
```

#### 场景 2：任务进度推送

```python
# 任务进度实时更新
def push_task_progress(task_id: str, progress: float, step: str):
    event = {
        "type": "task_progress",
        "task_id": task_id,
        "progress": progress,
        "current_step": step
    }
    # 推送到所有连接的前端
    websocket_manager.broadcast(event)
    # 桌宠更新
    pet_window.update_task_progress(event)

hook_manager.register("on_task_progress", push_task_progress)
```

#### 场景 3：上下文压缩

```python
# 上下文超长时自动压缩
def check_context_compression(response):
    usage = response.usage
    if context_engine.should_compress(usage.prompt_tokens):
        messages = context_engine.compress(messages)
        # 触发会话分裂
        event_bus.emit("on_context_compress", {
            "old_session": agent.session_id,
            "new_session": new_session_id
        })

hook_manager.register("after_llm_call", check_context_compression)
```

---

## 五、多端协同 — 统一体验

### 5.1 会话同步

```
┌─────────────────────────────────────────────────────────┐
│                                                         │
│  VSCode 扩展 ──┐                                       │
│                │                                       │
│  桌宠 ─────────┼──→ Spirit 后端 ──→ SessionDB (SQLite) │
│                │         │                             │
│  Web UI ───────┘         │                             │
│                          ↓                             │
│                    所有前端实时同步                      │
│                                                         │
│  场景：                                                 │
│  1. 在 VSCode 中开始对话                                │
│  2. 离开电脑，在手机上通过 Telegram 继续                 │
│  3. 回家后在 Web UI 查看完整历史                        │
│  4. 所有平台共享同一个会话上下文                        │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### 5.2 消息格式适配

```python
# 同一个 Agent 响应，不同前端的格式化
class ResponseFormatter:
    def format(self, response: str, platform: str) -> str:
        formatters = {
            "vscode": self._format_vscode,      # Markdown + diff
            "desktop_pet": self._format_pet,    # 简洁文本 + 语音
            "web": self._format_web,            # 完整 Markdown
            "telegram": self._format_telegram,  # HTML/Markdown
            "discord": self._format_discord,    # Discord Markdown
            "cli": self._format_cli,            # Rich 渲染
        }
        return formatters.get(platform, lambda x: x)(response)
```

---

## 六、产品差异化

### 6.1 vs 现有 AI 编程助手

| 特性 | Copilot | Cursor | Cline | Hermes | **Spirit V2** |
|------|---------|--------|-------|--------|--------------|
| **代码补全** | ✅ | ✅ | ❌ | ❌ | ✅ |
| **聊天对话** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **文件读写** | ❌ | ✅ | ✅ | ✅ | ✅ |
| **终端执行** | ❌ | ✅ | ✅ | ✅ | ✅ |
| **桌面精灵** | ❌ | ❌ | ❌ | ❌ | ✅ |
| **语音交互** | ❌ | ❌ | ❌ | ❌ | ✅ |
| **自动化任务** | ❌ | ❌ | ❌ | ✅ | ✅ |
| **子代理委托** | ❌ | ❌ | ❌ | ✅ | ✅ |
| **多平台同步** | ❌ | ❌ | ❌ | ✅ | ✅ |
| **自托管** | ❌ | ❌ | ✅ | ✅ | ✅ |

### 6.2 核心竞争优势

1. **桌面精灵**：唯一提供桌宠体验的 AI 编程助手，像 QQ 宠物一样陪伴
2. **语音交互**：支持语音对话、语音任务部署
3. **自动化任务**：ReAct 模式自主规划执行，支持子任务委托
4. **钩子系统**：可扩展的事件机制，支持插件化
5. **多端协同**：VSCode + 桌宠 + Web + Telegram + Discord
6. **全 Provider 覆盖**：10+ LLM 提供商即插即用
7. **完整工具生态**：90+ 工具覆盖文件/终端/浏览器/搜索/媒体
8. **安全审批**：危险命令检测 + 文件安全 + 密钥管理

---

## 七、多 Provider LLM 支持

### 7.1 支持的 LLM 提供商

借鉴 Hermes 的 Transport 层设计，Spirit V2 通过适配器模式支持所有主流 LLM：

| Provider | 适配器 | API 模式 | 特殊能力 |
|----------|--------|---------|----------|
| **OpenAI** | Chat Completions | `chat_completions` | GPT-4o/GPT-5.x, 视觉, 工具调用 |
| **OpenAI** | Responses API | `responses` | Codex 推理模式, 加密推理内容 |
| **Anthropic** | Messages API | `anthropic_messages` | Prompt Cache, 扩展思考 |
| **Google** | Gemini Native | `gemini` | 原生 Gemini 格式 |
| **Google** | Vertex AI | `vertex` | 企业级 Vertex 端点 |
| **AWS** | Bedrock | `bedrock` | Converse API |
| **Azure** | Azure OpenAI | `azure` | Azure Identity 认证 |
| **GitHub** | Copilot ACP | `copilot` | Copilot 代理访问 |
| **OpenRouter** | 兼容 OpenAI | `openrouter` | 多模型路由 |
| **Ollama** | 兼容 OpenAI | `ollama` | 本地模型 |
| **LM Studio** | 兼容 OpenAI | `lmstudio` | 本地模型 + 推理模式 |
| **Moonshot** | 原生 API | `moonshot` | 月之暗面 |
| **Nous Research** | 兼容 OpenAI | `nous` | 速率限制守卫 |

### 7.2 凭据池与轮换

```yaml
# 多 API Key 轮换配置
credentials:
  - api_key: "sk-aaa"
    provider: "openai"
    rate_limit: 500/min
  - api_key: "sk-bbb"
    provider: "openai"
    rate_limit: 500/min
  - api_key: "ant-ccc"
    provider: "anthropic"
    rate_limit: 100/min
```

- **自动轮换**：当一个 Key 达到速率限制时自动切换到下一个
- **故障切换**：API 失败时自动尝试备用 Key
- **限速追踪**：每个 Key 独立追踪调用频率
- **凭据持久化**：安全存储到本地密钥管理器

### 7.3 错误智能分类

借鉴 Hermes 的 `error_classifier.py`，对 API 错误进行智能分类：

| 错误类型 | HTTP 码 | 处理策略 |
|---------|---------|----------|
| 速率限制 | 429 | 等待后重试（指数退避） |
| 上下文超长 | 400 | 触发上下文压缩后重试 |
| 认证失败 | 401/403 | 切换凭据池中的 Key |
| 服务端错误 | 500/502/503 | 短暂等待后重试 |
| 网络错误 | - | 自动重试 3 次 |
| 余额耗尽 | 402 | 切换到备用 Provider |
| 模型不可用 | 404 | 降级到备用模型 |

---

## 八、浏览器自动化系统

### 8.1 三种浏览器后端

| 后端 | 说明 | 适用场景 |
|------|------|----------|
| **Playwright** | 标准浏览器自动化 | 网页操作、截图、表单填写 |
| **CDP (Chrome DevTools Protocol)** | 原生 CDP 连接 | 高级浏览器控制、网络监控 |
| **Camofox** | 反指纹检测浏览器 | 需要绕过检测的场景 |

### 8.2 CDP Supervisor

```python
# 持久 CDP 连接监控
class CDPSupervisor:
    """持续监控浏览器状态"""
    - 对话框自动处理（alert/confirm/prompt）
    - Frame 树追踪（iframe 感知）
    - Console 事件监听
    - 网络请求拦截
    - 下载管理
```

### 8.3 浏览器工具集

| 工具 | 功能 |
|------|------|
| `browser_navigate` | 导航到 URL |
| `browser_click` | 点击元素 |
| `browser_type` | 输入文本 |
| `browser_screenshot` | 截图 |
| `browser_extract` | 提取页面内容 |
| `browser_scroll` | 滚动页面 |
| `browser_select` | 选择下拉选项 |
| `browser_wait` | 等待元素/条件 |
| `browser_console` | 执行 JavaScript |
| `browser_network` | 监控网络请求 |

---

## 九、多媒体生成系统

### 9.1 Provider 注册表架构

所有多媒体能力采用统一的 Provider + Registry 模式：

```
Provider 接口 (抽象)
    ↓
Registry (注册表)
    ↓
具体实现 (DALL-E / Stable Diffusion / FAL.ai / ...)
```

### 9.2 图像生成

| Provider | 模型 | 说明 |
|----------|------|------|
| OpenAI | DALL-E 3 / GPT-Image | 通过 OpenAI API |
| Anthropic | - | 不支持原生图像 |
| FAL.ai | Flux / SDXL | 通过 FAL 网关 |
| xAI | Aurora | Imagine 图像生成 |
| 本地 | Stable Diffusion | 通过 ComfyUI/A1111 |

### 9.3 视频生成

| Provider | 模型 | 说明 |
|----------|------|------|
| xAI | Imagine Video | 视频编辑和扩展 |
| FAL.ai | Kling / Runway | 通过 FAL 网关 |

### 9.4 语音合成 (TTS)

| Provider | 引擎 | 说明 |
|----------|------|------|
| OpenAI | tts-1-hd | 高质量语音 |
| Azure | Neural TTS | 多语言多音色 |
| NeuTTS | 本地 | 离线语音合成 |
| Edge TTS | 免费 | 微软 Edge 引擎 |

### 9.5 语音识别 (STT)

| Provider | 引擎 | 说明 |
|----------|------|------|
| OpenAI | Whisper API | 云端识别 |
| Whisper | 本地 | 离线识别（whisper.cpp） |
| Azure | Speech Service | 企业级识别 |

---

## 十、技能与插件生态

### 10.1 三层扩展体系

```
┌─────────────────────────────────────────────────────┐
│                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐ │
│  │ Skills   │  │ Plugins  │  │ MCP Servers      │ │
│  │ (技能)   │  │ (插件)   │  │ (MCP 服务器)     │ │
│  │          │  │          │  │                  │ │
│  │ Markdown │  │ Python   │  │ 标准 MCP 协议    │ │
│  │ 提示词包 │  │ 代码模块 │  │ 外部工具服务     │ │
│  │          │  │          │  │                  │ │
│  │ /skill   │  │ 自动加载 │  │ npx @modelcontext│ │
│  │ 命令调用 │  │ 热插拔   │  │ 协议连接         │ │
│  └──────────┘  └──────────┘  └──────────────────┘ │
│                                                     │
└─────────────────────────────────────────────────────┘
```

### 10.2 技能系统

- **技能格式**：Markdown 文件，包含提示词 + 工具约束 + 触发条件
- **技能管理**：后台自动下载/更新（Curator 机制）
- **技能 bundles**：技能打包分发
- **技能命令**：`/skill list`、`/skill use <name>`、`/skill install <url>`
- **内置技能**：coding、research、review、debug 等

### 10.3 插件系统

- **插件接口**：Python 模块，可注册新的 Provider、工具、钩子
- **插件类型**：
  - LLM Provider 插件（自定义模型后端）
  - Context Engine 插件（自定义压缩算法）
  - Memory Provider 插件（自定义记忆存储）
  - Tool 插件（自定义工具）
- **热加载**：插件可在运行时加载/卸载

### 10.4 MCP 协议支持

- **MCP Client**：连接外部 MCP 服务器，获取工具
- **MCP Server**：将 Spirit 的工具暴露为 MCP 服务
- **双向桥接**：Spirit 工具 ↔ MCP 工具无缝集成

---

## 十一、消息网关系统

### 11.1 支持的消息平台

| 平台 | 适配器 | 特殊能力 |
|------|--------|----------|
| **Telegram** | Bot API | Markdown、命令菜单、图片、文件 |
| **Discord** | discord.py | Embed、Slash 命令、线程 |
| **Slack** | Slack API | Block Kit、Workflow |
| **钉钉** | DingTalk API | 卡片消息、审批流 |
| **飞书** | Feishu/Lark API | 文档读写、Drive 操作 |
| **企业微信** | WeCom API | 应用消息、群聊 |
| **QQ** | QQ Bot API v2 | 扫码配置、分块上传 |
| **WhatsApp** | WhatsApp API | 身份验证 |
| **BlueBubbles** | iMessage | iMessage 桥接 |
| **Webhook** | 通用 | Microsoft Graph 等 |
| **API Server** | OpenAI 兼容 | HTTP API 接入 |

### 11.2 网关核心能力

- **会话镜像**：跨平台消息同步到同一会话
- **DM 配对**：私聊绑定到特定 Agent 实例
- **斜杠命令**：`/new`、`/reset`、`/model`、`/tools` 等
- **消息路由**：根据来源平台格式化响应
- **频道目录**：缓存可达频道/联系人列表
- **死信检测**：检测不可达的投递目标
- **排空控制**：Dashboard → Gateway 的排空信号

---

## 十二、执行环境系统

### 12.1 多种执行环境

| 环境 | 说明 | 适用场景 |
|------|------|----------|
| **Local** | 本地执行（默认） | 日常开发 |
| **Docker** | 容器沙箱 | 隔离执行、CI/CD |
| **SSH** | 远程服务器 | 远程部署、服务器管理 |
| **Modal** | Modal 云执行 | 云端计算 |
| **Daytona** | Daytona 云环境 | 开发环境即服务 |
| **Singularity** | HPC 容器 | 高性能计算集群 |
| **Managed Modal** | 托管网关 | 团队共享执行 |

### 12.2 环境特性

- **文件同步**：远程环境自动同步文件
- **会话快照**：本地环境支持会话快照恢复
- **连接复用**：SSH ControlMaster 连接持久化
- **安全隔离**：Docker/Singularity 沙箱隔离

---

## 十三、安全体系

### 13.1 工具审批系统

```python
# 三级安全分级
SAFETY_LEVELS = {
    "safe":       ["read_file", "web_search", "vision_analyze"],
    "moderate":   ["write_file", "patch", "terminal"],
    "dangerous":  ["execute_code", "browser_navigate", "delete_file"],
}

# 危险命令检测
HARDLINE_PATTERNS = [  # 无条件阻止
    "rm -rf /", "mkfs", "dd if=", ":(){:|:&};:",  # fork bomb
    "shutdown", "reboot", "format",
]
DANGEROUS_PATTERNS = [  # 需要审批
    "rm -rf *", "chmod 777", "DROP TABLE",
    "curl | sh", "git push --force", "pip uninstall",
]
```

### 13.2 文件安全

- **路径遍历检测**：阻止 `../` 等路径逃逸
- **危险路径阻止**：阻止修改系统文件（/etc, /usr, C:\Windows）
- **Git 忽略尊重**：不操作 .gitignore 中的文件
- **二进制文件检测**：跳过二进制文件的文本操作

### 13.3 密钥管理

| 来源 | 集成方式 | 说明 |
|------|---------|------|
| 环境变量 | 直接读取 | 默认方式 |
| 配置文件 | ~/.spirit/credentials | 加密存储 |
| 1Password | `op` CLI | 通过 1Password CLI |
| Bitwarden | `bws` CLI | 通过 Bitwarden Secrets |
| 系统密钥链 | macOS Keychain / Windows Credential | 原生密钥管理 |

### 13.4 SSL 安全

- **SSL 证书验证**：检测自签名证书和中间人攻击
- **代理环境支持**：正确处理企业代理的 SSL 拦截
- **敏感信息脱敏**：从日志和输出中移除 API Key

---

## 十四、记忆与学习系统

### 14.1 跨会话记忆

```python
# 记忆管理器核心功能
class MemoryManager:
    def remember(self, key: str, value: str)      # 记住信息
    def recall(self, query: str) -> List[str]      # 搜索记忆
    def update(self, key: str, value: str)         # 更新记忆
    def forget(self, key: str)                     # 删除记忆
    def list_all(self) -> List[Dict]               # 列出所有记忆
```

- **持久化存储**：SQLite FTS5 全文搜索
- **自动提取**：从对话中自动提取关键信息作为记忆
- **记忆注入**：每轮对话自动注入相关记忆到上下文
- **记忆回顾**：会话结束时触发记忆回顾（辅助任务）

### 14.2 学习图谱

- **知识追踪**：记录 Agent 在每个领域的学习进度
- **学习变异**：从交互中学习新的模式和偏好
- **图谱渲染**：将学习进度可视化为文本/图表
- **提示词学习**：从成功的交互中学习新的提示词模式

### 14.3 对话洞察

- **模式提取**：从对话历史中提取常见模式
- **偏好学习**：学习用户的编码风格、命名习惯
- **项目理解**：逐步深入理解项目架构和约定

---

## 十五、计费与用量追踪

### 15.1 用量追踪

| 指标 | 说明 |
|------|------|
| Token 用量 | 每个 Provider 的 prompt/completion tokens |
| API 调用次数 | 按模型、按工具分类统计 |
| 费用估算 | 根据模型定价实时计算费用 |
| 辅助任务消耗 | 压缩/记忆/标题等辅助任务的 token 消耗 |

### 15.2 计费视图

- **实时费用**：当前会话的累计费用
- **历史统计**：按天/周/月的费用趋势
- **预算控制**：设置月度预算上限
- **分 Provider 统计**：每个 LLM Provider 的独立统计

---

## 十六、定时任务系统 (Cron)

### 16.1 定时任务功能

- **Cron 表达式**：支持标准 cron 表达式调度
- **Blueprint 蓝图**：可共享的自动化模板
- **任务生命周期**：创建 → 调度 → 执行 → 完成/失败
- **建议目录**：Agent 可建议定时任务
- **执行日志**：每次执行的详细日志

### 16.2 典型用例

```
# 每天早上 9 点检查 Git 仓库更新
spirit cron add "0 9 * * *" "检查项目更新并汇报"

# 每小时运行一次代码质量检查
spirit cron add "0 * * * *" "运行 lint 和测试，有问题通知我"

# 每周五下午生成周报
spirit cron add "0 17 * * 5" "总结本周的代码提交和任务完成情况"
```

---

## 十七、看板系统 (Kanban)

### 17.1 看板功能

- **任务分解**：大任务自动分解为看板卡片
- **状态追踪**：Todo → In Progress → Review → Done
- **看板监控**：Agent 自动监控看板变化
- **诊断工具**：看板状态诊断和异常检测

---

## 十八、LSP 代码智能集成

### 18.1 LSP 子系统

借鉴 Hermes 的 `agent/lsp/` 子包，Spirit V2 内置 LSP 集成：

| 模块 | 功能 |
|------|------|
| `manager.py` | LSP 服务编排，管理多个语言服务器 |
| `client.py` | 异步 LSP 客户端（stdin/stdout） |
| `servers.py` | 语言服务器注册表（每种语言的 LSP 定义） |
| `install.py` | LSP 服务器自动安装 |
| `workspace.py` | 工作区和项目根目录解析 |
| `reporter.py` | LSP 诊断信息格式化 |
| `protocol.py` | LSP JSON-RPC 2.0 协议帧 |
| `range_shift.py` | Diff 感知行号偏移映射 |

### 18.2 LSP 提供的能力

- **跳转定义** (Go to Definition)
- **查找引用** (Find References)
- **悬停信息** (Hover)
- **诊断信息** (Diagnostics)
- **工作区符号** (Workspace Symbol)
- **代码操作** (Code Action)

---

## 十九、桌面宠物系统 (Petdex)

### 19.1 宠物引擎

借鉴 Hermes 的 `agent/pet/` 子包，Spirit V2 的桌宠不只是悬浮窗，而是一个完整的宠物系统：

| 模块 | 功能 |
|------|------|
| `constants.py` | 宠物精灵图几何 + 动画状态分类 |
| `atlas.py` | 精灵图组装 — 生成行条 → 完整图集 |
| `imagegen.py` | 宠物精灵图的图像生成层 |
| `orchestrate.py` | 宠物生成编排 — 基础草稿 → 孵化流程 |
| `prompts.py` | 宠物生成的提示词构建 |
| `render.py` | 解码精灵图并编码为终端帧 |
| `state.py` | Agent 活动 → 宠物状态映射 |
| `store.py` | 磁盘宠物商店 — 安装/列表/解析宠物 |
| `manifest.py` | 获取公共宠物清单 |

### 19.2 宠物状态

宠物根据 Agent 的活动状态变化：

| Agent 状态 | 宠物表现 |
|-----------|----------|
| 空闲 | 闲逛、打盹 |
| 执行工具 | 忙碌工作动画 |
| 等待用户 | 好奇张望 |
| 出错 | 困惑表情 |
| 任务完成 | 开心庆祝动画 |

---

## 二十、Mixture of Agents (MoA) — 多模型协作

### 20.1 设计理念

借鉴 Hermes 的 `moa_loop.py` + `moa_trace.py`，Spirit V2 支持多个 LLM 协作解决复杂问题：

```
用户问题
    ↓
┌─────────────────────────────────────────┐
│  MoA 循环                                │
│                                         │
│  Round 1:                               │
│    • LLM-A (GPT-4o) → 回答 A            │
│    • LLM-B (Claude) → 回答 B            │
│    • LLM-C (Gemini) → 回答 C            │
│                                         │
│  Round 2 (聚合):                         │
│    • LLM-D (聚合器) → 综合 A+B+C → 最终答案│
└─────────────────────────────────────────┘
```

### 20.2 MoA 配置

```yaml
moa:
  enabled: true
  rounds: 2
  primary: "gpt-4o"
  slots:
    - provider: "anthropic"
      model: "claude-sonnet-4-20250514"
      role: "reasoner"
    - provider: "google"
      model: "gemini-2.5-pro"
      role: "reasoner"
  aggregator:
    provider: "openai"
    model: "gpt-4o"
```

### 20.3 应用场景

| 场景 | 说明 |
|------|------|
| **复杂推理** | 多模型各自推理，聚合器选最优 |
| **代码审查** | 不同模型关注不同维度（安全/性能/可读性） |
| **翻译校对** | 一个翻译，一个校对，一个润色 |
| **创意生成** | 多模型头脑风暴，聚合器综合创意 |

---

## 二十一、Profile 多实例系统

### 21.1 设计理念

借鉴 Hermes 的 `profiles.py` + `profile_distribution.py`，Spirit V2 支持多个隔离的 Agent 实例：

```
~/.spirit/
├── profiles/
│   ├── default/          # 默认配置
│   │   ├── config.yaml
│   │   ├── sessions.db
│   │   ├── memory.db
│   │   └── skills/
│   ├── work/             # 工作配置
│   │   ├── config.yaml   # 不同的模型/工具/提示词
│   │   └── ...
│   └── personal/         # 个人配置
│       └── ...
```

### 21.2 Profile 能力

| 功能 | 说明 |
|------|------|
| **隔离存储** | 每个 Profile 独立的会话/记忆/技能 |
| **独立配置** | 不同模型、工具集、安全策略 |
| **Profile 分发** | 通过 Git 共享 Profile 配置 |
| **自动描述** | AI 自动生成 Profile 描述 |
| **快速切换** | `spirit profile switch work` |

---

## 二十二、目标系统 (Goals / Ralph Loop)

### 22.1 设计理念

借鉴 Hermes 的 `goals.py`（Ralph Loop），Spirit V2 支持持久化的会话目标：

```
用户: "我的目标是今天完成用户认证模块"
          ↓
    ┌─────────────┐
    │  目标引擎    │ ← 持续追踪目标进度
    │  Ralph Loop  │ ← 每轮对话检查目标进展
    └──────┬──────┘
           ↓
    Agent 每轮对话后：
    1. 检查目标是否推进
    2. 如果偏离，提醒用户
    3. 如果完成，标记并建议新目标
```

### 22.2 目标类型

| 类型 | 说明 | 示例 |
|------|------|------|
| **编码目标** | 完成特定代码任务 | "实现 JWT 认证" |
| **学习目标** | 学习特定技术 | "理解 React Hooks" |
| **质量目标** | 达到质量标准 | "测试覆盖率 > 80%" |
| **探索目标** | 调研特定问题 | "对比 Redis vs Memcached" |

---

## 二十三、TUI 终端界面网关

### 23.1 设计理念

借鉴 Hermes 的 `tui_gateway/` + `ui-tui/`，Spirit V2 提供完整的终端 UI：

```
┌─────────────────────────────────────────────────────────┐
│  Spirit TUI                                    [?] [×]  │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌─── 项目树 ───┐  ┌────── 聊天区域 ──────────────────┐ │
│  │ 📁 src/       │  │                                  │ │
│  │  📁 agent/    │  │  User: 帮我重构这个模块          │ │
│  │   📄 agent.py │  │                                  │ │
│  │  📄 main.py   │  │  Spirit: 我来分析代码结构...     │ │
│  │ 📁 tests/     │  │  📝 write_file(agent.py)         │ │
│  │ 📄 README.md  │  │  ✅ 文件已更新                   │ │
│  └───────────────┘  │                                  │ │
│                     │  User: 运行测试                   │ │
│  ┌─── 工具面板 ───┐  │  📝 terminal(pytest)            │ │
│  │ 📊 系统监控    │  │  ✅ 15 passed, 0 failed         │ │
│  │ 📋 任务列表    │  │                                  │ │
│  │ 🧠 记忆条目    │  └──────────────────────────────────┘ │
│  └───────────────┘  ┌────── 输入区域 ──────────────────┐ │
│                     │ > _                               │ │
│                     └──────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

### 23.2 TUI 功能

| 功能 | 说明 |
|------|------|
| **项目树** | 浏览项目文件结构 |
| **多会话** | 同时管理多个聊天会话 |
| **斜杠命令** | TUI 内执行命令 |
| **Git 集成** | 查看 Git 状态、diff |
| **实时渲染** | Markdown 实时渲染 |
| **主题支持** | 可自定义皮肤/主题 |

---

## 二十四、Computer Use 桌面控制

### 24.1 设计理念

借鉴 Hermes 的 `tools/computer_use/` 子包，Spirit V2 支持通用的桌面控制：

| 后端 | 平台 | 说明 |
|------|------|------|
| **CUA-Driver** | macOS/Windows/Linux | 通用桌面控制后端 |
| **Playwright** | 跨平台 | 浏览器自动化控制 |
| **系统原生** | 各平台 | 原生辅助功能 API |

### 24.2 Computer Use 工具集

| 工具 | 功能 |
|------|------|
| `computer_screenshot` | 截取屏幕截图 |
| `computer_click` | 点击屏幕元素 |
| `computer_type` | 输入文本 |
| `computer_scroll` | 滚动页面 |
| `computer_drag` | 拖拽元素 |
| `computer_key` | 按键操作 |
| `computer_wait` | 等待条件 |
| `computer_run` | 执行脚本 |

### 24.3 视觉路由

```
截屏 → 视觉模型分析 → 定位元素坐标 → 执行操作 → 验证结果
```

---

## 二十五、Web Dashboard 完整方案

### 25.1 设计理念

借鉴 Hermes 的 `hermes_cli/web_server.py`（18,137 行），Spirit V2 提供完整的 Web UI：

| 页面 | 功能 |
|------|------|
| **Chat** | 完整聊天界面，支持流式响应 |
| **Sessions** | 会话列表、搜索、导出 |
| **Tasks** | 任务看板、进度追踪 |
| **Knowledge** | 记忆管理、知识库浏览 |
| **Config** | 模型/工具/插件配置 |
| **Billing** | 用量统计、费用图表 |
| **Pets** | 桌宠管理、宠物商店 |
| **Cron** | 定时任务管理 |
| **Logs** | 执行日志查看 |

### 25.2 Dashboard 认证

| 模式 | 说明 |
|------|------|
| **本地模式** | 无需认证（localhost 访问） |
| **Bearer Token** | 静态令牌认证 |
| **OAuth 2.0** | 支持 GitHub/Google OAuth |
| **Basic Auth** | 用户名/密码认证 |

---

## 二十六、会话管理系统

### 26.1 会话导出

| 格式 | 说明 |
|------|------|
| **HTML** | 带样式的完整会话导出 |
| **Markdown** | 纯文本格式导出 |
| **JSON** | 结构化数据导出 |

### 26.2 会话搜索

```python
# 跨会话长期回忆搜索
class SessionSearch:
    def search(self, query: str) -> List[SessionMatch]:
        """在所有历史会话中搜索"""
        # FTS5 全文搜索 + 语义搜索
        ...
    
    def recall(self, topic: str, limit: int = 10) -> List[Message]:
        """回忆特定主题的历史对话"""
        ...
```

### 26.3 会话回顾

- **会话摘要**：自动生成会话摘要
- **会话 Recap**：总结当前会话已发生的内容
- **会话过滤**：按时间/关键词/模型过滤会话
- **会话归档**：自动归档过期会话

---

## 二十七、安全扫描与审计

### 27.1 安全子系统

借鉴 Hermes 的安全相关模块，Spirit V2 构建多层安全防线：

| 模块 | 功能 | 对应 Hermes |
|------|------|-------------|
| **Tirith 安全扫描** | 工具执行前安全扫描 | `tools/tirith_security.py` |
| **威胁模式库** | 上下文窗口安全扫描 | `tools/threat_patterns.py` |
| **技能安全守卫** | 外部技能安全扫描 | `tools/skills_guard.py` |
| **URL 安全检查** | 阻止访问内网地址 | `tools/url_safety.py` |
| **OSV 恶意检测** | MCP 包恶意检测 | `tools/osv_check.py` |
| **供应链审计** | 安装供应链审计 | `hermes_cli/security_audit.py` |
| **安全公告** | 安全公告检查 | `hermes_cli/security_advisories.py` |

### 27.2 Tirith 扫描流程

```
工具调用请求
    ↓
┌─────────────────────────┐
│  Tirith 安全扫描          │
│                         │
│  1. 模式匹配（威胁模式库）│
│  2. 路径安全检查          │
│  3. 权限检查             │
│  4. 风险评估             │
└──────────┬──────────────┘
           ↓
    安全 → 执行    危险 → 阻止/审批
```

---

## 二十八、技能中心 (Skills Hub)

### 28.1 设计理念

借鉴 Hermes 的 `tools/skills_hub.py`（4,227 行）+ `agent/curator.py`（2,016 行），Spirit V2 构建完整的技能生态：

```
┌─────────────────────────────────────────────────┐
│                Spirit Skills Hub                  │
│                                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐ │
│  │ 内置技能  │  │ 社区技能  │  │ 自定义技能   │ │
│  │ (Bundled)│  │ (Community)│ │ (Custom)     │ │
│  └──────────┘  └──────────┘  └──────────────┘ │
│                                                 │
│  Curator 后台管理:                              │
│  • 自动发现新技能                                │
│  • 自动更新已安装技能                            │
│  • 技能安全扫描                                  │
│  • 技能使用统计                                  │
└─────────────────────────────────────────────────┘
```

### 28.2 技能管理命令

| 命令 | 功能 |
|------|------|
| `spirit skills list` | 列出已安装技能 |
| `spirit skills search <query>` | 搜索技能中心 |
| `spirit skills install <name>` | 安装技能 |
| `spirit skills update [name]` | 更新技能 |
| `spirit skills create <name>` | 创建自定义技能 |
| `spirit skills bundle <names>` | 打包技能集 |

---

## 二十九、进程注册表与后台任务

### 29.1 进程注册表

借鉴 Hermes 的 `tools/process_registry.py`（2,348 行），Spirit V2 管理后台进程：

```python
class ProcessRegistry:
    """后台进程注册表 — 管理所有长期运行的进程"""
    
    def start(self, name: str, cmd: List[str], **opts) -> ProcessHandle:
        """启动并注册后台进程"""
        ...
    
    def list_running(self) -> List[ProcessHandle]:
        """列出所有运行中的进程"""
        ...
    
    def stop(self, pid: str) -> bool:
        """停止指定进程"""
        ...
    
    def get_output(self, pid: str) -> str:
        """获取进程输出"""
        ...
```

### 29.2 异步委托

```python
# 后台异步执行长任务
class AsyncDelegation:
    def delegate(self, task: str, callback: Callable) -> str:
        """异步委托任务，返回 task_id"""
        ...
    
    def check_status(self, task_id: str) -> TaskStatus:
        """检查异步任务状态"""
        ...
```

---

## 三十、本地代理服务器

### 30.1 设计理念

借鉴 Hermes 的 `hermes_cli/proxy/`，Spirit V2 提供 OpenAI 兼容的本地代理：

```
其他应用 (IDE/脚本/工具)
    ↓ HTTP (localhost:9001/v1/chat/completions)
┌─────────────────────────────────┐
│  Spirit Proxy Server             │
│                                 │
│  • OpenAI 兼容 API              │
│  • 自动路由到配置的 Provider     │
│  • OAuth 认证上游               │
│  • 请求日志/统计                │
└─────────────────────────────────┘
    ↓
  OpenAI / Anthropic / Gemini / ...
```

### 30.2 上游适配器

| 上游 | 说明 |
|------|------|
| **Nous Portal** | Nous Portal OAuth 上游 |
| **xAI** | xAI Grok OAuth 上游 |
| **自定义** | 任意 OpenAI 兼容端点 |

---

## 三十一、检查点系统

### 31.1 设计理念

借鉴 Hermes 的 `tools/checkpoint_manager.py`（1,675 行），Spirit V2 提供文件系统快照：

```python
class CheckpointManager:
    """检查点管理器 — 透明的文件系统快照"""
    
    def create(self, label: str = "") -> Checkpoint:
        """创建当前文件系统的快照"""
        ...
    
    def restore(self, checkpoint_id: str) -> bool:
        """恢复到指定检查点"""
        ...
    
    def list(self) -> List[Checkpoint]:
        """列出所有检查点"""
        ...
    
    def diff(self, cp1: str, cp2: str) -> str:
        """比较两个检查点的差异"""
        ...
```

### 31.2 使用场景

| 场景 | 说明 |
|------|------|
| **任务回滚** | 任务执行失败，回滚到之前状态 |
| **分支实验** | 创建检查点 → 实验 → 不满意则回滚 |
| **版本对比** | 对比不同时间点的文件状态 |

---

## 三十二、语音模式 (Voice Mode)

### 32.1 _push-to-talk 模式_

借鉴 Hermes 的 `tools/voice_mode.py`（1,218 行），Spirit V2 的 CLI 支持语音模式：

| 模式 | 说明 |
|------|------|
| **Push-to-talk** | 按住键说话，松开后发送 |
| **Voice Activity Detection** | 自动检测语音起止 |
| **实时转录** | 实时显示转录文本 |
| **语音回复** | TTS 语音回复 |

### 32.2 语音管线

```
麦克风 → VAD 检测 → STT 转录 → 文本 → Agent 处理 → LLM 回复 → TTS → 扬声器
```

---

## 三十三、蓝图自动化 (Blueprints)

### 33.1 设计理念

借鉴 Hermes 的 `cron/blueprint_catalog.py`（713 行）+ `tools/blueprints.py`（325 行），Spirit V2 支持可共享的自动化蓝图：

```yaml
# 蓝图示例：每日代码审查
name: "daily-code-review"
description: "每天自动审查代码并提交报告"
slots:
  - name: "repo_url"
    type: "string"
    required: true
  - name: "review_focus"
    type: "string"
    default: "security,performance"
schedule: "0 9 * * *"
steps:
  - tool: "terminal"
    args: { command: "git pull {{repo_url}}" }
  - tool: "terminal"
    args: { command: "git diff HEAD~1" }
  - action: "agent_analyze"
    prompt: "审查以下代码变更，关注 {{review_focus}}"
  - action: "notify"
    channel: "telegram"
```

---

## 三十四、外部集成

### 34.1 智能家居 (Home Assistant)

借鉴 Hermes 的 `tools/homeassistant_tool.py`，Spirit V2 可控制智能家居：

| 功能 | 说明 |
|------|------|
| **设备控制** | 开关灯、调温度、播放音乐 |
| **场景执行** | 执行预设场景（回家/离家/睡眠） |
| **状态查询** | 查询设备状态 |
| **自动化触发** | 触发 Home Assistant 自动化 |

### 34.2 音乐服务 (Spotify)

| 功能 | 说明 |
|------|------|
| **播放控制** | 播放/暂停/跳过/上一首 |
| **搜索** | 搜索歌曲/专辑/播放列表 |
| **播放列表管理** | 创建/修改播放列表 |
| **当前播放** | 查看当前播放信息 |

### 34.3 会议集成 (Google Meet / Teams)

| 功能 | 说明 |
|------|------|
| **加入会议** | Agent 自动加入会议 |
| **实时转录** | 会议内容实时转录 |
| **会议纪要** | 自动生成会议纪要 |
| **跟进事项** | 提取会议行动项 |

---

## 三十五、成就系统

### 35.1 设计理念

借鉴 Hermes 的 `plugins/achievements/`，Spirit V2 通过成就激励用户：

| 成就类别 | 示例 |
|---------|------|
| **编码成就** | "第一个 100 行代码"、"Bug 终结者" |
| **效率成就** | "10 个任务连续完成"、"零失败部署" |
| **探索成就** | "使用过 10 种工具"、"尝试 5 个 Provider" |
| **社交成就** | "帮助 3 个团队成员"、"分享 10 个技能" |

---

## 三十六、工具搜索与渐进式发现

### 36.1 工具搜索

借鉴 Hermes 的 `tools/tool_search.py`（735 行），Spirit V2 支持渐进式工具发现：

```
用户: "我需要处理 PDF 文件"
Spirit: 搜索可用工具...
  → 找到: read_file (支持 PDF 提取)
  → 找到: execute_code (可运行 PDF 处理脚本)
  → 建议: 安装 pdf-tools 技能获取更多 PDF 工具
```

### 36.2 工具输出管理

| 功能 | 说明 |
|------|------|
| **输出截断** | 大输出自动截断，保留关键信息 |
| **输出持久化** | 大输出保存到磁盘，返回引用 |
| **预算配置** | 可配置每种工具的输出上限 |

---

## 三十七、模糊匹配与补丁解析

### 37.1 模糊文件匹配

借鉴 Hermes 的 `tools/fuzzy_match.py`（950 行），Spirit V2 支持模糊文件定位：

```python
class FuzzyMatcher:
    def find_file(self, query: str) -> List[FileMatch]:
        """模糊搜索文件 — 支持部分名称、路径片段"""
        # "agen ag py" → "spirit/agent/agent.py"
        ...
    
    def find_symbol(self, query: str) -> List[SymbolMatch]:
        """模糊搜索符号 — 类名、函数名"""
        # "TaskMgr" → "TaskManager (spirit/task/task_manager.py)"
        ...
```

### 37.2 补丁解析器

借鉴 Hermes 的 `tools/patch_parser.py`（637 行），支持标准补丁格式：

```
Agent 生成补丁 → 解析 → 验证 → 应用到文件
```

---

## 三十八、完整功能清单总览

| 功能域 | 模块数 | 关键能力 |
|--------|--------|----------|
| **Agent 核心** | 15+ | 对话循环、上下文压缩、记忆管理、错误分类、提示词构建 |
| **LLM 适配** | 13+ | OpenAI/Anthropic/Gemini/Bedrock/Azure/Ollama 等全覆盖 |
| **工具系统** | 90+ | 终端/文件/搜索/浏览器/代码执行/图像/视频/TTS/STT |
| **任务自动化** | 5+ | TODO/委托/看板/定时任务/蓝图 |
| **钩子系统** | 4类 | 生命周期/工具/任务/对话 钩子 |
| **消息网关** | 11+ | Telegram/Discord/Slack/钉钉/飞书/QQ/WhatsApp |
| **执行环境** | 7+ | Local/Docker/SSH/Modal/Daytona/Singularity |
| **安全体系** | 7+ | 审批/文件安全/SSL/密钥管理/脱敏/Tirith扫描/威胁模式 |
| **桌面精灵** | 9+ | 宠物引擎/语音/监控/任务进度/知识库 |
| **技能插件** | 3层 | Skills + Plugins + MCP Servers |
| **计费用量** | 5+ | Token 追踪/费用估算/预算控制 |
| **LSP 集成** | 8+ | 跳转定义/查找引用/诊断/符号搜索 |
| **多媒体** | 15+ | 图像/视频/TTS/STT Provider 注册表 |
| **MoA 多模型** | 3+ | 多模型协作/聚合器/追踪 |
| **Profile 系统** | 3+ | 多实例隔离/配置分发/快速切换 |
| **目标系统** | 2+ | Ralph Loop/持久目标/进度追踪 |
| **TUI 终端** | 5+ | 项目树/多会话/斜杠命令/Git 集成/主题 |
| **Computer Use** | 5+ | 桌面控制/截屏/点击/输入/视觉路由 |
| **Web Dashboard** | 9+ | 聊天/会话/任务/知识/配置/计费/宠物/定时/日志 |
| **会话管理** | 5+ | 导出(HTML/MD/JSON)/搜索/回顾/归档 |
| **安全审计** | 5+ | 技能守卫/URL 检查/OSV 检测/供应链审计 |
| **技能中心** | 5+ | 搜索/安装/更新/安全扫描/Curator 后台 |
| **进程注册表** | 3+ | 后台进程/异步委托/孤儿恢复 |
| **本地代理** | 3+ | OpenAI 兼容 API/OAuth 上游/请求日志 |
| **检查点系统** | 2+ | 文件系统快照/回滚/差异对比 |
| **语音模式** | 3+ | Push-to-talk/VAD/实时转录 |
| **蓝图自动化** | 3+ | 参数化模板/共享蓝图/调度执行 |
| **外部集成** | 5+ | Home Assistant/Spotify/Google Meet/Teams |
| **成就系统** | 2+ | 编码/效率/探索/社交成就 |
| **工具搜索** | 3+ | 渐进式发现/输出管理/预算配置 |
| **模糊匹配** | 2+ | 文件模糊搜索/符号搜索/补丁解析 |

---

*下一步：[00-architecture-v2.md](./00-architecture-v2.md) — 完整架构设计*
