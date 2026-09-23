# Spirit Agent 系统架构总览（As-Built v1.0）

> **文档定位**：本文是 Spirit Agent 的**现状架构（as-built）**权威入口，反映截至 2026-09 全部已落地代码
> （Phase 1–4 开发 + 后续全部生产修复）。早期设计稿见 `00-architecture-v2.md` / `03-spirit-agent-architecture.md`，
> 与本文冲突时**以本文为准**。
>
> 参考基线：Hermes Agent v0.18.2 对话循环架构（ReAct 无独立规划器 + 模块化子系统）。

---

## 1. 系统总览

Spirit Agent 是一个**本地部署的多端 AI Agent 平台**：Python 后端承载 Agent 核心与工具执行，
Electron 桌宠、VSCode 扩展、Web 面板、TUI/CLI 四种形态通过同一条 WebSocket 协议接入。

```
┌─────────────────────────────── 接入层 ───────────────────────────────┐
│                                                                      │
│  ┌──────────────── ┌──────────────┐ ┌───────────┐ ┌──────────────┐ │
│  │ Electron 桌宠   │ │ VSCode       │ │ Web 面板  │ │ TUI / CLI    │ │
│  │ Vue3 + xterm.js │ │ Extension    │ │ (web/)    │ │ (spirit/tui) │ │
│  │ 桌宠/气泡/状态/  │ │ 双向 WS      │ │ HTTP/WS   │ │ Rich 终端    │ │
│  │ CLI 终端 四窗口  │ │ 代码智能+编辑 │ │           │ │              │ │
│  └───────┬────────┘ └─────────────┘ └─────┬─────┘ └──────┬───────┘ │
└──────────┼────────────────┼───────────────┼──────────────┼─────────┘
           └────────────────┴───────┬───────┴──────────────┘
                                    ▼  WebSocket (ws://127.0.0.1:9877)
┌─────────────────────────── 服务层 spirit/desktop ─────────────────────────┐
│  ws_server.py：32 个命令 + 7 类服务端事件（流式 delta / 工具事件 / 状态广播）│
│  pet_engine（桌宠状态机） voice_engine  system_status  _launcher（入口）   │
└───────────────────────────────────┬───────────────────────────────────────┘
                                    ▼
┌─────────────────────────── Agent 核心层 spirit/agent ─────────────────────┐
│  SpiritAgent（状态容器） → conversation_loop（ReAct 主循环）               │
│  TurnContext / IterationBudget / ErrorClassifier / ContextCompressor      │
│  PromptCaching / ToolExecutor+Guardrails / UsageTracker / 流式门控        │
└───────┬──────────────┬───────────────┬───────────────┬───────────────────┘
        ▼              ▼               ▼               ▼
┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌──────────────────────┐
│ tools/      │ │ lsp/        │ │ goals/ moa/ │ │ storage/ sessions/   │
│ 38 文件     │ │ LSP 客户端   │ │ Ralph 目标  │ │ session_db（SQLite） │
│ 55 个工具   │ │ pyright 等   │ │ MoA 聚合    │ │ hooks/ process/      │
│ 定义        │ │ 文本流转     │ │ 循环        │ │ gateway/ task/       │
└─────────────┘ └─────────────┘ └─────────────┘ └──────────────────────┘
        ▼                              ▼
┌────────────────────────── 外部依赖 ──────────────────────────┐
│ LLM Provider（MiniMax-M3，OpenAI 兼容协议，思考模型）          │
│ Memora 个人知识库（Docker Compose，可选）                      │
│ 系统 shell / 文件系统 / 浏览器 / 语音后端                      │
└──────────────────────────────────────────────────────────────┘
```

### 1.1 关键运行时事实

| 项 | 值 |
|---|---|
| Python 后端入口 | `spirit/desktop/_launcher.py`（由 Electron 主进程 spawn，`--ws-port 9877`） |
| WebSocket 端口 | 9877（环境变量 `SPIRIT_WS_PORT` 可改） |
| 默认模型 | MiniMax-M3（provider=minimax，OpenAI 兼容端点，**思考模型**：推理 in-band 计入输出配额） |
| 工具定义数 | 运行时加载 55 个（`spirit/tools/` 38 个文件聚合） |
| 迭代预算 | 单轮 max_total=90（`agent.max_iterations`） |
| chat 超时 | 1800s（`timeouts.chat_request`），前端 `wsSend` 同值对齐 |
| 输出上限 | `llm.max_tokens=0` 时按 provider 默认：minimax=32768 / 通用=16384 |
| 会话存储 | SQLite `state.db`（First Reason Wins、Resume/Branch 恢复） |
| 桌面栈 | Electron 29.1.4 + Vue 3.4 + xterm 5.3 + Vite 5（vite-plugin-electron） |
| 测试规模 | 78 个 test 文件；当前全量 **1973 用例通过**（含钩子接线与解析器回归） |

---

## 2. 仓库布局

| 目录 | 职责 |
|---|---|
| `spirit/` | Python 后端全部代码（18 个子包，见下） |
| `desktop-app/` | Electron 桌宠应用：`electron/`（主进程 TS）+ `src/`（Vue 渲染层） |
| `vscode-extension/` | VSCode 扩展：双向 WS 协议、代码智能、编辑能力 |
| `web/` | Web 面板（静态资源由 Electron/网关加载） |
| `docs/` | 设计稿与本文（文档地图见 §14） |
| `tests/` |  pytest 测试集（76 文件） |
| `scripts/` | 运维/迁移脚本 |

### 2.1 spirit/ 子包职责

| 子包 | 职责 |
|---|---|
| `agent/` | Agent 核心：对话循环、压缩、预算、错误恢复、工具执行、提示词、流式（16 模块，见 §3） |
| `desktop/` | 桌面后端服务：`ws_server`（WS 协议）、`pet_engine/state/store`（桌宠状态机）、`voice_engine`、`system_status`、`_launcher` |
| `tools/` | 工具实现（38 文件 / 55 定义）：terminal、file、patch、web、browser、vision、tts、code_exec、process、todo、search 等 |
| `lsp/` | 独立 LSP 代码智能：client/manager/servers/protocol/range_shift/install（pyright 等，装于 `~/.spirit/lsp/bin`） |
| `goals/` | Ralph Loop 目标系统：manager/loop/judge/store/goal_state/commands（自主续传 + judge 裁决） |
| `moa/` | Mixture-of-Agents：moa_loop（参考 fan-out + 聚合）、moa_trace、transports 能力映射 |
| `storage/` | `session_db.py`：SQLite 会话库（三层终止、First Reason Wins、Resume/Branch） |
| `sessions/` | 会话导出 / 回顾 / 搜索（session_export/recap/search） |
| `gateway/` | 消息网关：platform_registry/delivery/runner/session（TG/Discord 等接入） |
| `process/` | 后台进程注册表与完成通知回灌（registry/notifications/session） |
| `task/` | 任务系统：planner/executor/checker/manager/models（含 Hermes 式任务检查） |
| `hooks/` | 钩子系统：conversation/lifecycle/tool/task hooks + memora_auto_save；装配于 `SpiritAgent.__init__`（所有构造路径统一生效），emit 点在主循环/工具执行器/任务系统/会话生命周期 |
| `skills/` `skills_hub/` | 技能加载与技能市场 |
| `computer_use/` | 桌面计算机使用能力 |
| `cli/` `tui/` | 终端入口（Rich TUI；`_clean_think_tags` 等输出清洗在此） |
| `api/` | HTTP API 服务 |
| `config.py`（模块） | 全局配置默认值 + 环境变量映射（见 §9） |

### 2.2 desktop-app 结构

| 路径 | 职责 |
|---|---|
| `electron/main.ts` | 主进程入口：spawn Python 后端、托盘、IPC 注册、看门狗启动、GPU 退出自愈 |
| `electron/window.ts` | 窗口工厂（pet/bubble/status/cli 四类）+ 宠物看门狗 + 坐标钳制 |
| `electron/preload.ts` | contextBridge：IPC 白名单 + `clipboardRead/Write` + `petHeartbeat` |
| `src/App.vue` | 桌宠主视图路由（按 hash 区分 pet/bubble/status/cli 四模式）+ 合成心跳 |
| `src/components/CLITerminalPage.vue` | xterm.js 交互式 CLI 终端（流式渲染、门控兜底、剪贴板快捷键） |
| `src/components/PetSprite.vue` | canvas 双缓冲精灵渲染（spritesheet + 占位狐狸） |
| `src/components/`（其余） | RadialMenu / StatusPopup(Page) / SpeechBubble(Page) / SystemPanel / VoiceChat / KnowledgePanel / TaskCLI |
| `src/composables/` | useWebSocket（WS 客户端）/ usePetAnimation（帧调度）/ useDrag |
| `src/stores/` | petStore / statusStore（Pinia） |

---

## 3. Agent 核心层（spirit/agent）

### 3.1 模块清单

| 模块 | 职责 |
|---|---|
| `agent.py` | SpiritAgent 状态容器与入口（`chat()`/`interrupt()`/`clear_interrupt()`/三层终止/Resume/Branch） |
| `conversation_loop.py` | ReAct 主循环（本文核心，见 3.2） |
| `turn_context.py` | per-turn 初始化：用户消息清洗、系统提示词、迭代预算 reset |
| `iteration_budget.py` | 线程安全迭代预算（consume/refund/reset） |
| `context_compressor.py` | 上下文压缩（protect_first_n=3 / protect_last_n=6 / 目标 30%） |
| `prompt_builder.py` / `prompt_caching.py` | 系统提示词构建（一轮缓存；stable 层含 MEMORA_GUIDANCE，向模型声明 Memora 为默认长期记忆/知识库及 knowledge_* 工具使用规则）/ cache_control 断点注入 |
| `error_handler.py` | ErrorClassifier + ErrorRecoveryPlan（分类重试/等待/压缩/换凭证） |
| `credential_pool.py` | 多凭证池与轮换 |
| `tool_executor.py` | 工具串行/并发执行（`execute_tool_calls_sequential/concurrent`） |
| `tool_guardrails.py` | 工具循环护栏（重复调用/无进展检测 → halt） |
| `text_tool_parser.py` | 文本 XML 工具调用回退解析（MiniMax 等无原生 tool_calls 时） |
| `streaming.py` | 流式辅助 |
| `memory_manager.py` | 记忆管理（含 Memora 载体） |
| `usage_tracker.py` | Usage Tracker / Billing Monitor 后台统计（失败不阻断主流程） |

### 3.2 conversation_loop 主流程

```
用户消息 → TurnContext 构建（消息清洗/系统提示词/预算 reset=90）
   │
   ▼
┌─ 主循环（budget.remaining > 0）──────────────────────────────┐
│ 1. 中断标志检查（_interrupt_requested → 返回 [已中断]）        │
│ 2. prepare_api_messages + prompt caching 断点                │
│ 3. _call_llm（stream=True + 门控，显式 max_tokens）           │
│    ├─ 实时推送文本增量（gate_open 时）                        │
│    ├─ 检测到 <think|tool_call|...> 开头 → 关闸暂扣            │
│    └─ 累积原生 tool_calls 增量；记录 finish_reason/usage      │
│ 4. 失败 → ErrorClassifier → 重试/等待/压缩/refund             │
│ 5. 解析工具调用：原生 tool_calls 优先，回退文本 XML 解析        │
│ 6. 有工具调用：                                                │
│    ├─ add_message(assistant, text+tool_calls)                │
│    ├─ 串行(1 个)/并发(>1 个) 执行 + guardrails 检查            │
│    └─ continue（让模型看工具结果再推理）                       │
│ 7. 无工具调用（最终回答分支）：                                │
│    ├─ 读 finish_reason：                                      │
│    │   └─ =length 且续写<2 且预算有余 → 存干净文本 +           │
│    │      注入续写提示 → continue（截断自愈）                 │
│    ├─ add_message(assistant, content)                        │
│    ├─ 后台进程通知回灌（有则注入 user 消息再推一轮）           │
│    ├─ 门控回补：只推「暂扣后缀」（不重发已流式前缀）           │
│    └─ 返回 ConversationResult                                │
└──────────────────────────────────────────────────────────────┘
```

**关键设计决策**（均含生产修复沉淀）：

- **ReAct 无独立规划器**：规划能力内化于模型 + `task/` 检查器 + `goals/` Ralph 自主续传，对齐 Hermes。
- **流式门控**：防止 `<think>`/工具 JSON 泄露到终端；回补只推后缀（`_stream_pushed_text`），杜绝重复文本。
- **截断自愈**：思考模型推理吃输出配额 → `finish_reason=length` 不得当轮次结束，自动续写 ≤2 次。
- **中断标志纪律**：`cmd_chat` 入口必调 `clear_interrupt()`，防止单次超时永久毒化后续请求。
- **三层终止**：Soft End（刷数据）/ Medium End（关 provider）/ Hard End（全清理）；session_db First Reason Wins 防覆盖。

---

## 4. 工具系统

- **规模**：`spirit/tools/` 38 个实现文件，运行时注册 **55 个工具定义**（日志 `工具定义已加载: 55 个工具`）。
- **来源**：Hermes 92+ 工具全量映射移植（含 dual-mode 执行：平台检测选 PowerShell/Bash 实现）。
- **执行**：单工具串行、多工具并发（`tool_executor`）；每个工具结果回灌为 tool message。
- **护栏**：`tool_guardrails` 检测重复/无进展循环 → halt 并返回停止原因。
- **分类**：terminal / 文件读写 / patch / 搜索 / web / browser / vision / tts / code_exec / 后台 process / todo / LSP 文本流转等。
- **后台进程**：`process/` 注册表跟踪长任务，完成事件经 `_inject_process_notifications` 回灌主循环。

---

## 5. 桌面后端服务（spirit/desktop）

### 5.1 WebSocket 协议（ws_server.py，32 命令）

| 分组 | 命令 |
|---|---|
| 对话 | `chat`（流式）、`get_history`、`new_session`、`compress_context`、`idle`（/stop） |
| 桌宠 | `get_status`、`get_pet_info`、`list_pets`、`switch_pet`、`set_scale`、`set_position`、`set_state` |
| 系统 | `get_system_status`、`get_cpu`、`get_memory`、`get_version`、`ping` |
| 配置 | `get_config`、`set_config`、`get_config_value` |
| 语音 | `transcribe_audio`、`synthesize_speech`、`voice_backends`、`voice_chat` |
| 高级 | `goal`、`subgoal`、`goal_run`、`moa`、`skill`、`search_knowledge`、`execute_command`、`list_tools` |

**服务端事件**（→ 渲染层）：`init` / `stream_delta` / `tool_start` / `tool_complete` / `chat_complete` / `state_change` / `pet_switch`。

**顺序保证**：delta 推送采用阻塞等待（`.result(timeout=N)`），杜绝 `chat_complete` 先于 delta 到达的竞态丢文本。

**超时与中断**：`cmd_chat` 用 `asyncio.wait_for(future, timeouts.chat_request=1800s)`；超时 interrupt 当前轮；
新 chat 入口先 `clear_interrupt()`（防标志锁死假死）。

### 5.2 其余桌面模块

- `pet_engine/pet_state/pet_store`：桌宠状态机（idle/thinking/... ），状态变更经 `state_change` 广播。
- `voice_engine`：STT/TTS 后端适配。
- `system_status`：CPU/内存采样供状态弹窗。
- `_launcher.py`：统一入口——初始化 PetEngine、SpiritAgent、ws_server；打印 `READY ws://...` 供 Electron 判定。

---

## 6. 桌面应用（desktop-app，Electron + Vue）

### 6.1 进程与窗口拓扑

- **主进程**（main.ts）：spawn Python 后端（**不得手动另启**，否则 9877 端口冲突）→ 托盘 → IPC → 看门狗。
- **四类窗口**（window.ts 工厂，均 transparent+frame:false，pet/bubble/status 置顶）：

| 窗口 | 特征 |
|---|---|
| pet | 桌宠主视图；位置/缩放持久化（electron-store）；离屏校验 |
| bubble | 周期/事件气泡；`focusable:false` + showInactive，不抢输入焦点；4s 自动关；坐标钳制 ≥0 |
| status | 右键状态弹窗；blur 自动关 |
| cli | xterm 交互终端（700×500，非透明）；renderer console 管道到主进程日志 |

### 6.2 宠物窗口分层自愈（看门狗，15s 巡检）

| 失效类别 | 检测 | 自愈 |
|---|---|---|
| 窗口被意外销毁 | 巡检发现 null/destroyed | 按持久化位置重建 |
| 被 hide 且非用户意图 | `isVisible()===false` 且 `!petUserHidden` | showInactive + moveTop |
| 坐标完全离屏 | getBounds 与所有 workArea 无 ≥40px 交集 | 钳回最近屏幕边缘 |
| 合成层停绘 | 渲染层 rAF 计帧每 10s 心跳上报，连续 2 周期不涨 | hide+showInactive 重建合成层 |
| z-order 掉层 | 周期性 | moveTop 夺回置顶 |
| 渲染进程崩溃 | `render-process-gone` | webContents.reload() |
| GPU 进程退出 | app `child-process-gone`(type=GPU) | 强制重绘 |

配套：所有移动走 `movePetWindow` 统一入口（拖拽 IPC 同样钳制，源头防离屏）；托盘显隐走
`showPetWindow/hidePetWindow` 同步 `petUserHidden`，看门狗不覆盖用户意图；`before-quit` 先停看门狗；
每 4 周期打诊断快照 `pos/visible/frames/stalled`。

### 6.3 剪贴板方案（CLI 终端）

Electron 无边框窗口无 Edit 菜单角色 → Ctrl+V 不产生 paste 事件。方案：preload 桥接
`clipboard.readText/writeText`（同步、无权限门控）+ xterm `attachCustomKeyEventHandler`
接管 Ctrl+V/C/Shift+Insert（preventDefault + 返回 false 防双插）；键入与粘贴共用 `insertText()`。

### 6.4 构建与启动链

```
npm run build:vite   # vite-plugin-electron 同编 main.ts+preload.ts → dist-electron/；渲染层 → dist/
npx electron .       # 生产构建运行（无 HMR：改任何文件都必须重 build + 完全重启）
```

主进程/preload 改动必须 `Stop-Process electron` 全量重启（Electron 会连带重 spawn Python 后端）。

---

## 7. 扩展子系统

| 子系统 | 位置 | 要点 |
|---|---|---|
| Ralph 目标循环 | `goals/` | manager/loop/judge/store；max_turns=20 自主续传；judge 解析失败 3 次自动暂停 |
| MoA | `moa/` | 参考模型 fan-out + 聚合器；transports 能力映射；trace 刷写 |
| LSP 代码智能 | `lsp/` | 独立于 IDE 的 langserver 管理（pyright 等）；range_shift 文本流转；事件日志 |
| 消息网关 | `gateway/` | 平台注册/delivery/runner；Agent 实例 LRU 缓存 32 |
| 会话工具 | `sessions/` | export / recap / search（session_search 兜底） |
| 钩子 | `hooks/` | conversation/lifecycle/tool/task 六类 + memora_auto_save |
| 任务系统 | `task/` | planner/executor/checker（Hermes 式任务检查用例来源） |
| 技能 | `skills/` `skills_hub/` | 技能包加载与市场 |
| 计算机使用 | `computer_use/` | 桌面 UI 操作能力 |
| TUI/CLI | `tui/` `cli/` | Rich 终端；think 标签清洗 `_clean_think_tags` |
| VSCode 扩展 | `vscode-extension/` | 双向 WS：代码智能、编辑审批（见 docs/05） |
| Web 面板 | `web/` | 静态面板接入同一网关 |

---

## 8. 存储与记忆

| 载体 | 内容 |
|---|---|
| `state.db`（SQLite，`storage/session_db.py`） | 会话/消息/压缩链；First Reason Wins；Resume/Branch 双模恢复；懒连接 |
| electron-store（JSON） | 桌宠位置 windowX/Y、缩放 petScale、activePetSlug |
| Memora（Docker Compose） | 个人知识库 / 文件记忆载体，**系统默认长期记忆工具**（系统提示词 MEMORA_GUIDANCE 已向模型声明）；**宿主机端口 8000**（compose 映射 8000→容器 8080）；`search_knowledge` 命令 + after_tool_execute 钩子自动保存（已接线）；Docker 未运行时 health_check 失败降级跳过（非致命） |
| `memory_manager.py` | Agent 记忆管理（会话内 + 跨会话摘要） |

会话重置策略：idle 60min / max_messages 500 / max_tokens 100k（`session_reset.*`）。

---

## 9. 配置系统（spirit/config.py）

单一默认值字典 + `get_config_value(path, default)` + 环境变量映射（`MAX_TOKENS→llm.max_tokens` 等）。
关键运行时参数（含生产修复后的值）：

| 键 | 默认 | 说明 |
|---|---|---|
| `llm.max_tokens` | 0 | 0=按 provider 默认表（minimax 32768 / 通用 16384）；思考模型必须留足 |
| `llm.context_length` | 128000 | 上下文窗口 |
| `agent.max_iterations` | 90 | 单轮迭代预算 |
| `agent.protect_first_n / protect_last_n` | 3 / 6 | 压缩保护带 |
| `timeouts.chat_request` | 1800 | chat 超时（秒），前端 wsSend 同值 |
| `ralph.max_turns` | 20 | 目标自主续传轮数 |
| `gateway.agent_cache_size` | 32 | 网关 Agent LRU |

---

## 10. 可靠性与自愈机制总表

| 故障模式 | 机制 | 位置 |
|---|---|---|
| 超时后所有对话秒回 [已中断] | chat 入口 clear_interrupt + 超时 300→1800s | ws_server / agent |
| 长任务被 5 分钟误杀 | timeouts.chat_request=1800 + 前端对齐 | ws_server / CLITerminalPage |
| 宣布步骤后中断（输出截断） | 显式 max_tokens + finish_reason=length 自动续写 ≤2 | conversation_loop |
| 终端文本重复 | 门控回补只推暂扣后缀 | conversation_loop |
| 流式文本丢失 | 阻塞发送保序 + chat_complete 全文兜底 | ws_server / CLITerminalPage |
| 宠物消失（合成层停绘） | rAF 心跳 + hide/showInactive 重建 | window.ts / App.vue |
| 宠物消失（坐标离屏） | 移动钳制 + 看门狗离屏检测钳回 | window.ts |
| 宠物消失（渲染崩溃/GPU 退出） | render-process-gone reload / child-process-gone 重绘 | window.ts / main.ts |
| 休眠/分辨率变化后不绘制 | powerMonitor.resume + display-metrics-changed 强制重绘 | main.ts |
| CLI 无法 Ctrl+V 粘贴 | preload clipboard 桥 + customKeyEventHandler | preload / CLITerminalPage |
| 中文输入法丢焦点 | xterm 焦点修复（textarea 保活） | CLITerminalPage |
| API 抖动/限流 | ErrorClassifier 分类重试 + 凭证池轮换 | error_handler / credential_pool |
| 上下文膨胀 | 压缩引擎 + protect 带 + 会话重置策略 | context_compressor |
| 工具死循环 | guardrails halt + 迭代预算 | tool_guardrails / iteration_budget |
| 工具调用被静默丢弃（in-band 解析失败） | 门控暂扣零解析时 WARNING 记录原始内容；解析器支持 wrapper 标签/零参数 invoke | conversation_loop / text_tool_parser |
| 会话消息从未落库 | on_session_end/on_agent_destroy 钩子触发 save_messages（会话轮换与关闭时持久化） | agent.py / lifecycle_hooks |
| 钩子只注册不触发 | SpiritAgent.__init__ 统一装配 + 主循环/执行器/会话生命周期全部 emit 点接线 | agent.py / conversation_loop / tool_executor |

---

## 11. 测试与质量

- **pytest 集**：`tests/` 78 个 test 文件；全量 **1973 用例通过**（含钩子接线 test_hook_wiring、解析器 test_text_tool_parser 回归）。
- **Hermes 式任务检查**：`task/checker.py` 对每阶段交付做结构化检查（对齐用户要求「参考 hermes 的任务规划与检查用例」）。
- **分层**：单元（budget/compressor/session_db）→ 集成（conversation_loop）→ E2E（WS 客户端直连后端验证事件序）。
- **回归沉淀**：每个生产修复均补对应记忆与日志指纹（见 §10 表），便于日志驱动回归。

---

## 12. 构建、部署与运维

```powershell
# 后端+前端一体启动（推荐，Electron 自动 spawn Python）
cd desktop-app; npm run build:vite; npx electron .

# 进程拓扑校验
electron=4 进程；python(_launcher)=1；9877 Listen=1
```

- **日志**：主进程 stdout（Electron 终端）含 `[Python]` 前缀后端日志 + `[pet watchdog]` 快照 + `[CLI renderer]` 管道。
- **重启约束**：改 `spirit/**` 或 `electron/**`/`src/**` 均需重 build + 全量重启（无热更）。
- **端口**：仅 9877（WS）；Memora 另占 Docker 端口（可选）。

---

## 13. 已知问题与限制

| 问题 | 状态 |
|---|---|
| Memora 需 Docker Desktop 运行，否则启动时降级跳过 | 设计如此（非致命）；注意宿主机端口是 8000 非 8080 |
| Windows 下 pyright-langserver 经 `.bin` shim 启动报 WinError 193 | 待修：应直调 `.cmd`/node 入口 |
| GBK 控制台中文日志乱码 | 显示层问题，不影响功能；验证时用计数而非文本匹配 |
| 单实例后端（9877 固定） | 多实例需改 SPIRIT_WS_PORT 并隔离 store |

---

## 14. 文档地图

| 文档 | 性质 | 说明 |
|---|---|---|
| **12-system-architecture-asbuilt.md（本文）** | **as-built 权威** | 当前系统唯一架构入口 |
| 00-architecture-v2.md | 设计稿 | V2 总体设计（实施前），分层图仍具参考价值 |
| 00-product-vision-v2.md | 愿景 | 产品愿景与场景 |
| 01-product-overview.md | 概述 | 早期产品概述 |
| 02-hermes-architecture-analysis.md | 参考分析 | Hermes 架构拆解（移植依据） |
| 03-spirit-agent-architecture.md | 设计稿 | 早期 Spirit 架构（已被本文取代） |
| 04-technical-design.md | 设计稿 | 技术设计细节 |
| 05-vscode-extension-design.md | 设计稿 | VSCode 扩展协议设计 |
| 06-implementation-roadmap.md | 计划 | 路线图（Phase 1–4 已全部完成） |
| 07-development-checklist.md | 计划 | 开发清单 |
| 08-conversation-loop-refactor.md | 专项 | 对话循环重构方案（已落地，见本文 §3.2） |
| 09/10/11-*.md | 报告 | 阶段进度/完成报告（历史快照） |
| LSP_INTEGRATION.md | 专项 | LSP 集成说明（已落地，见本文 §7） |
