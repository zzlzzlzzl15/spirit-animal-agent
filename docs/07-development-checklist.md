# Spirit Agent V2 — 完整开发清单

> 基于 `hermes文件功能手册.md`（3105 文件）对标，结合 `00-product-vision-v2.md` 和 `00-architecture-v2.md` 设计
> 
> 生成时间：2026-09-08 · **最后更新：2026-09-28（Phase 7 常驻自主探索 Autonomy 落地并完成**完全自动化接线** —— `spirit/autonomy/` 4 文件，沙箱护栏 + AutonomyLoop 常驻循环 + 桌宠 WS 桥接 + 部署集成（持久记忆 + DRS 三角色推理 + 真实 execute：SpiritAgent 在沙箱工作目录内动手）；**验证收敛修复**（execute 跑完快照沙箱产物→丰富 Evidence.files，Verifier 有据可裁；停止策略缺省 curriculum_review + max_rounds=5）+ **架构型自提议**（make_proposer：做完一个→基于记忆自提下一个架构/设计型问题→继续探索）+ **任务级自动迭代**（loop 重试注入上一轮校验反馈→Agent 针对性改进直至真收敛，可配 verifier_pass 省 token），新增 42 测试全通（累计 2698 passed / 0 failed）；解决“任务做完就停”，实现无人值守自我探索 + 该问就问/无应答自决，见下 Phase 7）**
> 
> 上次更新：2026-09-18（Phase 4 高级功能推进：进程注册表 / MoA / 技能中心 / Computer Use / TUI 五大子系统落地）

---

## 项目总览

| 指标 | 数值 |
|------|------|
| **Python 文件（spirit/）** | 204 个 |
| **Python 行数（spirit/）** | ~58,900 行 |
| **前端文件（desktop-app TS/Vue）** | 23 个（4,894 行） |
| **VSCode 扩展 TS 文件** | 8 个 |
| **测试文件（tests/）** | 98 个 test_*.py（2440 项通过 + 54 跳过；新增 418 项 Phase 5 测试） |
| **规划模块总数** | 24 个目录 |
| **已实现后端模块** | 23 个（agent/tools/api/storage/cli/desktop/gateway/hooks/lsp/sessions/skills/task/goals + moa/process/skills_hub/computer_use/tui/profile + proxy/checkpoint/integrations/achievements） |
| **已实现前端界面** | 3 个（desktop-app 桌宠端 / vscode-extension / web 面板） |
| **待实现模块** | ~1 个（🟢 低优先级：acp_adapter 等生态适配） |
| **整体完成度** | 核心闭环 ~100% · 含全部规划 ~88% |

---

## 一、已完成模块 ✅

### 1.1 Agent 核心引擎 — `spirit/agent/` ✅

| 文件 | 行数 | 状态 | 说明 |
|------|------|------|------|
| `agent.py` | 454 | ✅ 完成 | SpiritAgent 状态容器 + 转发器架构 + AgentConfig.from_dict |
| `agent_init.py` | 247 | ✅ 完成 | 完整初始化逻辑（委托集中式配置系统） |
| `conversation_loop.py` | 292 | ✅ 完成 | ReAct 对话循环（7 阶段） |
| `context_compressor.py` | 407 | ✅ 完成 | 上下文压缩引擎 |
| `error_handler.py` | 628 | ✅ 完成 | 错误分类与智能重试 |
| `prompt_builder.py` | 393 | ✅ 完成 | 系统提示词构建（三层） |
| `streaming.py` | 328 | ✅ 完成 | 流式响应处理 |
| `tool_executor.py` | 441 | ✅ 完成 | 工具执行引擎 |
| `tool_guardrails.py` | 501 | ✅ 完成 | 工具安全护栏 |
| `memory_manager.py` | 473 | ✅ 完成 | 跨会话记忆管理 |
| `credential_pool.py` | 413 | ✅ 完成 | 多凭据轮换 |
| `iteration_budget.py` | 167 | ✅ 完成 | 迭代预算管理（防无限循环）· Phase 1 |
| `turn_context.py` | 328 | ✅ 完成 | Per-turn 初始化（9 步流程）· Phase 1 |
| `usage_tracker.py` | 215 | ✅ 完成 | 用量追踪器 · Phase 3 |
| `text_tool_parser.py` | 98 | ✅ 完成 | 文本 XML/JSON 工具调用回退解析 |
| `prompt_caching.py` | — | ✅ 完成 | Prompt 缓存断点注入 |
| `transports/` (7 文件) | ~1,500 | ✅ 完成 | 多 Provider 适配（anthropic/bedrock/gemini/openai + factory/base） |
| `lsp/` (1 文件) | — | ✅ 完成 | Agent 侧 LSP 桥接 |
| `__init__.py` | 21 | ✅ 完成 | 模块初始化 |
| **小计** | **~7,000+** | | **17 顶层文件 + transports/ + lsp/** |

**还缺少的 Agent 子模块**（参考 Hermes 156 个文件）：

| 待实现功能 | 对应 Hermes | 优先级 | 说明 |
|-----------|-------------|--------|------|
| `memory_manager.py` | `agent/memory_manager.py` (1231行) | ✅ 已完成 | 跨会话记忆管理 (473行) |
| `agent_init.py` | `agent/agent_init.py` (2200行) | ✅ 已完成 | 完整初始化逻辑 (247行, 委托集中式配置) |
| `credential_pool.py` | `agent/credential_pool.py` (2601行) | ✅ 已完成 | 多凭据轮换 (413行) |
| ~~`transports/`~~ | `agent/transports/` (7 files) | ✅ 已完成 | 多 Provider 适配层（anthropic/bedrock/gemini/openai + factory/base） |
| `model_metadata.py` | `agent/model_metadata.py` (2714行) | 🟡 中 | 模型元数据 |
| `system_prompt.py` | `agent/system_prompt.py` (592行) | 🟡 中 | 系统提示词三层构建 |
| `title_generator.py` | `agent/title_generator.py` (376行) | 🟢 低 | 会话标题自动生成 |
| `insights.py` | `agent/insights.py` (1092行) | 🟢 低 | 对话洞察提取 |
| `learning_graph.py` | `agent/learning_graph.py` (328行) | 🟢 低 | 学习图谱 |
| `i18n.py` | `agent/i18n.py` (302行) | 🟢 低 | 国际化 |
| `redact.py` | `agent/redact.py` (811行) | 🟡 中 | 敏感信息脱敏 |
| `shell_hooks.py` | `agent/shell_hooks.py` (928行) | 🟢 低 | Shell 脚本钩子 |

### 1.2 工具系统 — `spirit/tools/` ✅

| 统计 | 数值 |
|------|------|
| 文件数 | 37 个 |
| 总行数 | ~12,500 行 |
| 注册工具数 | 59 个 |
| Hermes 覆盖率 | 93/93 (100%) |

**核心工具模块**：

| 模块 | 行数 | 状态 | 覆盖工具 |
|------|------|------|----------|
| `registry.py` | 320 | ✅ | 工具注册表 |
| `file_tools.py` | 503 | ✅ | read_file, write_file, list_dir... |
| `file_operations.py` | 337 | ✅ | move, delete, create_dir, file_info... |
| `terminal_tool.py` | 375 | ✅ | terminal_execute |
| `web_tools.py` | 305 | ✅ | web_search, web_fetch |
| `search_tools.py` | 289 | ✅ | search_files, find_files |
| `execute_code.py` | 140 | ✅ | execute_python |
| `project_tools.py` | 374 | ✅ | git_status, git_diff, git_log, project_info |
| `todo_tool.py` | 167 | ✅ | todo_create, todo_update, todo_list |
| `memory_tool.py` | 213 | ✅ | memory_remember, memory_recall |
| `read_extract.py` | 233 | ✅ | extract_document (ipynb/docx/xlsx) |
| `approval.py` | 973 | ✅ | 危险命令检测与审批 · **已接入 `terminal` 执行前门禁**（`request_command_approval` + 交互式回调） |
| `async_delegation.py` | 554 | ✅ | 后台任务委托 |
| `browser.py` | 1,037 | ✅ | 浏览器自动化 + CDP + Camofox |
| `gateway_primitives.py` | 745 | ✅ | 网关原语整合 |
| `platforms.py` | 523 | ✅ | discord, feishu, homeassistant... |
| `skills.py` | 385 | ✅ | 技能系统 |
| `mcp_client.py` | 403 | ✅ | MCP 协议支持 |
| `extra_tools.py` | 574 | ✅ | kanban, cronjob, blueprints... |
| `media_gen.py` | 258 | ✅ | 图像/视频生成 |
| `voice.py` | 231 | ✅ | TTS/STT |
| `checkpoint.py` | 244 | ✅ | 会话检查点 |
| `code_intelligence.py` | 511 | ✅ | 代码智能 |
| `edit_proposal.py` | 170 | ✅ | 编辑提案 |
| `image_analysis.py` | 152 | ✅ | 图像分析 |
| 其他基础设施 | ~1,900 | ✅ | infra_utils, tool_infra, terminal_ext... |

### 1.3 API 服务 — `spirit/api/` ✅

| 文件 | 行数 | 状态 | 说明 |
|------|------|------|------|
| `server.py` | 605 | ✅ | FastAPI 服务 |
| `websocket.py` | — | ✅ | WebSocket 处理 |
| `events.py` | 180 | ✅ | 事件定义 |
| `context.py` | 164 | ✅ | 上下文管理 |
| **小计** | **951** | | |

### 1.4 数据存储 — `spirit/storage/` ✅

| 文件 | 行数 | 状态 | 说明 |
|------|------|------|------|
| `session_db.py` | 214 | ✅ | SQLite 会话存储 |
| `config.py` | — | ✅ | YAML 配置管理（已迁移至集中式配置） |
| **小计** | **216** | | |

### 1.5 CLI 入口 — `spirit/cli/` ✅

| 文件 | 行数 | 状态 | 说明 |
|------|------|------|------|
| `main.py` | 260 | ✅ | CLI 入口 |
| `__init__.py` | 2 | ✅ | 模块初始化 |
| **小计** | **262** | | |

### 1.6 VSCode 扩展 — `vscode-extension/` ✅

| 文件 | 行数 | 状态 | 说明 |
|------|------|------|------|
| `extension.ts` | 233 | ✅ | 扩展入口 |
| `ChatViewProvider.ts` | 210 | ✅ | 聊天侧边栏 |
| `SpiritClient.ts` | 233 | ✅ | WebSocket 客户端 |
| `CodeIntelligence.ts` | 382 | ✅ | 代码智能（跳转/引用/悬停） |
| `CodeEditor.ts` | 223 | ✅ | 代码编辑集成 |
| `CompletionProvider.ts` | 167 | ✅ | 代码补全 |
| `TerminalManager.ts` | 194 | ✅ | 终端集成 |
| `WorkspaceContext.ts` | 222 | ✅ | 工作区上下文 |
| `chat.css` | 371 | ✅ | 聊天界面样式 |
| `chat.js` | 352 | ✅ | 聊天前端逻辑 |
| `package.json` | 187 | ✅ | 扩展配置 |

### 1.7 独立 LSP 代码智能 — `spirit/lsp/` ✅（本次核对新增）

> 双模式设计：VSCode 回调桥优先 + 独立 LSP 回退，任何环境（CLI / Gateway / VSCode）都能代码导航。共 10 个文件，`tests/test_lsp.py` 覆盖。

| 文件 | 状态 | 说明 |
|------|------|------|
| `protocol.py` | ✅ | JSON-RPC 2.0 帧编解码 |
| `workspace.py` | ✅ | Git 工作区检测 + URI 路径转换（Windows 兼容） |
| `servers.py` | ✅ | 9 种语言服务器注册表（pyright/tsserver/gopls/rust-analyzer…） |
| `client.py` | ✅ | 异步 LSP 客户端（子进程生命周期管理） |
| `install.py` | ✅ | 自动安装（npm 策略 + 优雅降级） |
| `manager.py` | ✅ | 服务编排（同步/异步桥接 + 会话绑定） |
| `reporter.py` | ✅ | 诊断格式化 + delta baseline |
| `eventlog.py` / `range_shift.py` | ✅ | 事件日志 / 位置偏移修正 |
| `lsp_integration.py`（顶层） | ✅ | 写文件前后快照 + 新增错误检查 |

> 支持 Python/TypeScript/Go/Rust/YAML/JSON/HTML/CSS/Lua 共 9 种语言。

### 1.8 集中式配置系统 — `spirit/config.py` ✅

| 文件 | 行数 | 状态 | 说明 |
|------|------|------|------|
| `spirit/config.py` | 356 | ✅ | 集中式配置：DEFAULT_CONFIG + load_config + Provider 自动解析 |
| `config.yaml.example` | 112 | ✅ | 示例配置文件（llm/agent/compression/streaming/guardrails） |
| `tests/test_config.py` | 366 | ✅ | 34 个配置系统测试 |
| **小计** | **834** | | |

**核心特性**：
- 优先级链：**参数 > 环境变量 (SPIRIT_ 前缀) > YAML 文件 > DEFAULT_CONFIG**
- Provider 自动解析：11 个 Provider 的 base_url + API key 环境变量映射
- AgentConfig.from_dict()：支持嵌套格式和扁平格式
- 自动发现 `~/.spirit/config.yaml`，无需显式传路径

### 1.9 文档 — `docs/` ✅

| 文件 | 行数 | 状态 |
|------|------|------|
| `00-product-vision-v2.md` | 1,619 | ✅ 产品愿景 V2 |
| `00-architecture-v2.md` | 1,352 | ✅ 架构设计 V2 |
| `01-product-overview.md` | 186 | ✅ 产品概述 |
| `02-hermes-architecture-analysis.md` | 478 | ✅ Hermes 架构分析 |
| `03-spirit-agent-architecture.md` | 506 | ✅ Spirit 架构 v1 |
| `04-technical-design.md` | 513 | ✅ 技术设计 |
| `05-vscode-extension-design.md` | 686 | ✅ VSCode 扩展设计 |
| `06-implementation-roadmap.md` | 467 | ✅ 实施路线图 |
| `07-development-checklist.md` | 480 | ✅ 开发清单 |

---

## 二、模块实现状态（原“待实现”清单已核对）

> 以下 20 项为原规划清单。核对后：**2.1/2.2/2.3/2.4（部分）/2.12/2.19/2.20 已实现**；2.5–2.11 / 2.13–2.18 仍为 🟢 低优先级待开发。

### 2.1 任务自动化系统 — `spirit/task/` ✅ 🔴 高优先级

| 文件 | 说明 | 状态 |
|------|------|------|
| `models.py` | Task/TaskStatus/TaskType 数据模型 (148行) | ✅ |
| `manager.py` | TaskManager — SQLite 持久化 + CRUD + 钩子 (375行) | ✅ |
| `executor.py` | TaskExecutor — 线程池执行 + 预检 + 进度钩子 (341行) | ✅ |
| `checker.py` | TaskChecker — 依赖/环境/资源/冲突预检 (347行) | ✅ |
| `planner.py` | TaskPlanner — 模板/metadata/自定义分解器 (493行) | ✅ |
| `tools/task_tool.py` | task_manage 工具 — 连接持久化 TaskManager (360行) | ✅ |
| `tools/todo_tool.py` | todo 工具 — 内存快速追踪 (167行) | ✅ |
| 测试 | test_task + test_checker + test_planner + test_executor_enhanced | ✅ 78个测试 |

### 2.2 钩子系统 — `spirit/hooks/` ✅ 🔴 高优先级

| 文件 | 说明 | 状态 |
|------|------|------|
| `hook_manager.py` | HookManager + HookEvent (264行, 含 10 类任务事件) | ✅ |
| `lifecycle_hooks.py` | Agent 生命周期钩子 | ✅ |
| `conversation_hooks.py` | 对话钩子 | ✅ |
| `tool_hooks.py` | 工具钩子 | ✅ |
| `task_hooks.py` | 增强任务钩子 — 状态广播/预检/进度/自动启动 (249行) | ✅ |
| `memora_auto_save.py` | Memora 自动保存钩子 | ✅ |
| 测试 | test_hook_manager + test_task_hooks_enhanced | ✅ 31个测试 |

### 2.3 桌面精灵 — `spirit/desktop/` ✅ 已完成 🟡 中优先级

> 实际采用 **Electron + Python 后端** 架构（非原规划的纯 Python UI）。后端 9 个文件：

| 文件 | 状态 | 说明 |
|------|------|------|
| `pet_engine.py` | ✅ | PetEngine 状态机 + 宠物管理 + 偏好持久化 |
| `pet_state.py` | ✅ | PetState 状态推导（Agent 信号 → 动画状态） |
| `pet_store.py` | ✅ | `~/.spirit/pets` 宠物仓库 + `pet_prefs.json` |
| `pet_constants.py` | ✅ | 宠物常量定义 |
| `ws_server.py` | ✅ | WebSocket 服务（9877）+ 命令/事件协议 |
| `voice_engine.py` | ✅ | 语音引擎（STT/TTS 接口） |
| `system_status.py` | ✅ | 系统状态采集（监控面板数据源） |
| `_launcher.py` | ✅ | 后端启动器（spawn + 引导安装内置宠物） |

> 前端桌宠窗口/托盘/悬浮窗/面板 → 见 `desktop-app/`（Electron），已在 2.20 标记完成。

### 2.4 语音引擎 — `desktop/voice_engine.py` + `tools/voice.py` ✅ 已完成 🟡 中优先级

| 组件 | 状态 | 说明 |
|------|------|------|
| `desktop/voice_engine.py` | ✅ | 语音引擎（462 行）：STT(faster-whisper) + TTS(edge-tts/pyttsx3) + 超时保护 |
| `listen_and_respond` | ✅ | **完整语音问答管线 STT → Agent → TTS**（可注入 `agent_chat` 回调，支持同步/异步/dict 返回） |
| `tools/voice.py` | ✅ | 云端 TTS/STT 工具（231 行，ElevenLabs/OpenAI） |
| `ws_server.py` 语音命令 | ✅ | `transcribe_audio`（STT）+ `synthesize_speech`（TTS，返回 base64 音频）+ `voice_backends`（能力探测）+ `voice_chat`（STT→Agent→TTS 一站式，已注入 Agent 回调） |
| 前端 `VoiceChat.vue` | ✅ | 语音聊天界面（径向菜单入口）+ **语音播报开关与音频播放**（data URL + HTMLAudioElement） |
| 唤醒词 | ✅ | `VoiceConfig.wake_word` + `check_wake_word()`（未配置时始终通过） |
| 独立 STT/TTS Provider 目录 | ⚠️ | 后端按需延迟加载，未拆成独立 Provider 模块（功能等价） |

**测试**：`tests/desktop/test_voice_engine.py`（STT/TTS 降级、唤醒词门控、listen_and_respond 11 个管线场景）+ `tests/desktop/test_ws_voice.py`（四条 WS 语音命令、Agent 注入全管线、mime 映射、base64 ASCII 安全、超时保护），共 **50 项全通过**，语音后端全部打桩，不加载真实模型。

> ⚠️ 同期修复：`tools/registry.py` 的 handler 签名适配器。工具 handler 存在两种历史约定（`handler(args: Dict, **kwargs)` 与 `handler(**kwargs)`），`dispatch` 统一用 `handler(**args)` 曾导致 browser / platforms / skills / terminal_ext / **voice** 等约 21 个工具全部 TypeError；现注册时用 `inspect.signature` 探测并分支调用，回归测试见 `tests/tools/test_registry_dispatch.py`（19 项）。

### 2.5 MoA 多模型协作 — `spirit/moa/` ✅ 已完成（Phase 4·2026-09-18）

> **Mixture-of-Agents**：`provider == "moa"` 时 `SpiritAgent.client` 返回 `MoAClient`，其
> `.chat.completions.create` 透明拦截——并行跑多个「参考模型」生成候选 + guidance，再调「聚合模型」
> 综合成最终答案。对标 Hermes `agent/moa_loop.py`(1182行)+`moa_trace.py`(167行)+`hermes_cli/moa_config.py`；
> LLM 调用经 `transports` 抽象注入（可 mock），全流程离线单测。

| 文件 | 行数 | 状态 | 说明 |
|------|------|------|------|
| `moa_loop.py` | 1,078 | ✅ | 参考模型 fan-out + 聚合器 + `MoAClient` facade（透明拦截 completions） |
| `config.py` | 402 | ✅ | 预设归一化 + 校验 + slot→运行时解析 |
| `commands.py` | 347 | ✅ | `/moa` 斜杠命令分发（传输无关） |
| `moa_trace.py` | 155 | ✅ | opt-in 完整轮次追踪持久化（参考/聚合可回放） |
| `__init__.py` | 55 | ✅ | 公共 API 导出 |
| **小计** | **~2,037** | | **5 文件** |

**测试**：`tests/moa/` 6 个测试文件 + conftest，**111 项全通过**（moa_loop / config / commands / trace / streaming / reference_view）。参考模型与聚合器全部打桩，无需真实 Provider。

### 2.6 Profile 多实例 — `spirit/profile/` ✅ 已完成（Phase 4·2026-09-27，精简子集）

> **多实例隔离 HOME**：一个 profile 是一个完全独立的 HOME 目录（自带 config/.env/
> memories/sessions/skills/logs/plans），让用户在同一台机器上并行运行多个互不干扰的
> Spirit 身份（如工作/个人/金融研究）。默认 profile ``default`` 就是 ``SPIRIT_HOME`` 本身（零迁移）。

| 已创建文件 | 对应 Hermes | 行数 | 说明 |
|-----------|-------------|------|------|
| `paths.py` | `profiles.py` 路径层 | 109 | 按调用解析 default_root/profiles_root/profile_dir/resolve_env/apply |
| `manager.py` | `profiles.py` (2225行) 核心子集 | 533 | ProfileInfo + 名字校验 + CRUD + profile.yaml meta + active + 技能计数 + clone |
| `describer.py` | `profile_describer.py` (288行) | 267 | 注入式 LLM 自动描述（优雅降级，绝不抛） |
| `commands.py` | `profiles.py` CLI 子命令 | 333 | `/profile` 传输无关分发（CLI / ws_server 共用） |
| `__init__.py` | — | 95 | 公共 API 导出 |
| **小计** | | **~1,337 行 + 93 测试** | 已接线 `/profile` 到 `cli/main_enhanced.py` + `desktop/ws_server.py` |

> **缓建**（记录于此，独立关注点）：wrapper 别名脚本（对 Windows 不友好）、gateway service
> 注册（Hermes 专属 s6/systemd/launchd）、Git 分发（`profile_distribution.py` 726 行）、export/import 归档。

### 2.7 目标系统 — `spirit/goals/` ✅ 已完成（Phase 4·2026-09-16）

> **Ralph Loop 持久目标系统**：一个 goal 是跨轮次保持活跃的自由用户目标。每轮结束后，
> 一个轻量 judge 调用问辅助模型“助手最后的响应满足这个目标了吗”（done/continue/wait 三态）；
> 若否，Spirit 把一条续传 prompt 喂回同一会话继续干，直到目标完成 / turn 预算耗尽 /
> 用户暂停或清除。状态存 SessionDB 的 `state_meta` 表，`/resume` 可重新捡起未完成的目标。
>
> 对标 Hermes `hermes_cli/goals.py` (1749行)，适配 Spirit 架构：无 auxiliary_client → judge
> 改为注入 `llm_caller`（纯函数、可 mock）；session_db 补 `state_meta` → `GoalStore` 抽象；
> 无 process_registry/kanban → wait 屏障降级 + `run_goal_loop` 通用化（依赖注入 `run_turn`）。

| 文件 | 行数 | 状态 | 说明 |
|------|------|------|------|
| `goal_state.py` | 266 | ✅ 完成 | GoalContract/GoalState 数据模型 + parse_contract（inline 契约解析） |
| `prompts.py` | 205 | ✅ 完成 | 续传/judge/契约起草 prompt 模板（逐字对齐 Hermes） |
| `judge.py` | 490 | ✅ 完成 | judge_goal 裁决 + _parse_judge_response + draft_contract + build_agent_llm_caller |
| `store.py` | 182 | ✅ 完成 | GoalStore 抽象 + InMemory/SessionDB 后端 + migrate_goal_to_session |
| `manager.py` | 550 | ✅ 完成 | GoalManager：生命周期 + evaluate_after_turn 决策 + wait 屏障 + 子目标 |
| `loop.py` | 143 | ✅ 完成 | run_goal_loop 外层驱动（通用化自 Hermes run_kanban_goal_loop） |
| `commands.py` | 397 | ✅ 完成 | 传输无关的 /goal·/subgoal 命令分发 + run_goal_turn_loop |
| `__init__.py` | 70 | ✅ 完成 | 公共 API 导出 |
| **小计** | **~2,300** | | **8 文件** |

**集成点**（已落地）：`config.py`（goals 配置节）· `agent/agent.py`（SpiritAgent.goal_manager property + reset_session 目标迁移）· `desktop/ws_server.py`（goal/subgoal/goal_run RPC）· `cli/main_enhanced.py`（/goal·/subgoal slash 命令）· `storage/session_db.py`（state_meta 表 + get/set/delete_meta）

**测试**：`tests/goals/` 6 个测试文件 + conftest，**231 项全通过**——移植 Hermes 同款“任务检查”用例（test_goal_state 20 / test_judge 37 / test_manager 79 / test_loop 14 / test_store 33 / test_commands 48）。Hermes 用 `patch(judge_goal)` mock 裁决，Spirit 用注入 `llm_caller`/`run_turn` 实现同等可测试性。

### 2.8 TUI 终端界面 — `spirit/tui/` ✅ 已完成（Phase 4·2026-09-18，精简子集）

> Hermes `ui-tui/` 是 TypeScript/Ink(React) 前端（**不移植**）；本子集只移植 Hermes **Python**
> `tui_gateway/` 的可测试核心。`tui_server.py` 对标 Hermes `server.py`(15619行) 的**精简子集**
> （协议信封 + 方法路由 + 会话注册 + 三模块 RPC 接线），是**传输无关**纯同步层
> （`handle_message(dict) -> 信封 dict`），部署时把 websockets send/recv 适配上去即可（信封与
> `desktop/ws_server.py` 的 `type: response/event` 约定一致）。`project_tree.py` 是纯 stdlib，
> near-verbatim 移植以保证 Hermes 结构契约测试逐字通过。

| 文件 | 行数 | 状态 | 说明 |
|------|------|------|------|
| `project_tree.py` | 627 | ✅ | 权威项目树（worktree 折叠 / kanban 塌缩 / 泳道 id / Windows 路径身份 / 标签消歧）·纯 stdlib |
| `tui_server.py` | 225 | ✅ | 最小网关：8 个 RPC（ping/session.*/projects.tree/render.*/slash.exec）+ 信封成形 + 会话注册 |
| `slash_worker.py` | 158 | ✅ | 持久斜杠 worker：`serve_stream` 解耦协议循环 + 父进程死亡看门狗（`getppid` seam） |
| `render.py` | 83 | ✅ | 渲染桥接（`renderer=` seam；Spirit 暂无渲染器 → None 回退前端渲染） |
| `__init__.py` | 52 | ✅ | 包 docstring + 惰性 `build_tree` 导出 |
| **小计** | **~1,145** | | **5 文件** |

**测试**：`tests/tui/` 4 个测试文件 + conftest，**92 项全通过**（project_tree 26 / render 17 / slash_worker 24 / tui_server 25）。git 解析、渲染器、slash runner、时钟、传输全部 seam 注入，离线断言。

### 2.9 Computer Use 桌面控制 — `spirit/computer_use/` ✅ 已完成（Phase 4·2026-09-18，可测试抽象层）

> 对标 Hermes `tools/computer_use/`，但落地为**可测试抽象层**：把「桌面控制的形状」(`backend`)、
> 「安全/审批/派发策略」(`safety`/`tool`)、「就绪度探测」(`permissions`)、「视觉路由决策」
> (`vision_routing`) 与「具体平台驱动」彻底解耦。真实驱动（cua-driver 之类）不在包内实现——经
> `tool.set_backend_factory` 注册即启用；缺省以 `NoopBackend` 安全降级（动作只记录、不触碰真实
> 桌面），故整套逻辑可在无图形会话的 CI 里离线单测。工具入口 `spirit/tools/computer_use_tool.py`（shim）。

| 文件 | 行数 | 状态 | 说明 |
|------|------|------|------|
| `tool.py` | 510 | ✅ | `computer_use` 工具（capture/动作派发）+ 多模态截图信封 + mime 嗅探 + set_backend_factory |
| `permissions.py` | 201 | ✅ | 就绪度探测（driver_cmd / 子进程环境脱敏 / 平台门控 / 权限授予）·全 seam 化 |
| `backend.py` | 185 | ✅ | 抽象 `ComputerUseBackend` + UIElement/CaptureResult/ActionResult 数据模型 |
| `vision_routing.py` | 158 | ✅ | 截图是否路由到 aux vision 的决策链（3 个可注入 lookup seam） |
| `schema.py` | 151 | ✅ | 工具 Schema（动作枚举 / 参数） |
| `noop_backend.py` | 132 | ✅ | 一等内存后端（记录调用，测试 / CI seam） |
| `safety.py` | 98 | ✅ | 审批 / 危险动作门禁 |
| `__init__.py` | 74 | ✅ | 公共 API 导出 + 模块地图 |
| **小计** | **~1,509** | | **8 文件** |

**测试**：`tests/computer_use/` 9 个测试文件 + conftest，**277 项全通过**（backend / schema / safety / registration / approval / dispatch / capture_response / permissions / vision_routing）。驱动、`shutil.which`、子进程、审批全部打桩，无需图形会话。

### 2.10 Web Dashboard — `spirit/dashboard/` ❌ 🟡 中优先级

| 待创建文件 | 对应 Hermes | 说明 |
|-----------|-------------|------|
| `web_server.py` | `hermes_cli/web_server.py` (18137行) | Web UI 服务 |
| `auth_provider.py` | `hermes_cli/dashboard_auth/` | 认证 |
| `session_export.py` | `hermes_cli/session_export.py` (317行) | 会话导出 |
| `skin_engine.py` | `hermes_cli/skin_engine.py` (926行) | 主题引擎 |

### 2.11 安全系统 — 命令审批 + 工具护栏 ✅ 已落地（`spirit/security/` 独立扫描模块 ❌）🟡 中优先级

**已完成（Phase 3.6，2026-09-16）**：

| 组件 | 行数 | 状态 | 说明 |
|------|------|------|------|
| `tools/approval.py` | 973 | ✅ | HARDLINE 无条件阻止 / DANGEROUS 需审批 / 反混淆规范化 / sudo stdin 防护 / YOLO / 会话+永久白名单 |
| `approval.request_command_approval` | — | ✅ | **统一审批门禁**：静态裁决 + 交互式回调（deny/session/always）；无回调时 **fail-safe 阻止**（危险命令绝不静默执行） |
| `approval.set/get/clear_approval_callback` | — | ✅ | 混合存储：全局回调（跳线程可见，适配 `execute_tool_calls_concurrent` 不传播上下文）+ 线程局部优先（并发会话隔离预留） |
| `tools/terminal_tool.py` | 420 | ✅ | **执行前调用门禁**；未批准返回 `blocked/status/pattern_key` JSON（exit_code=-1）；批准后输出前置 `[审批]` 说明；`force=true` 可跳过 |
| `cli/main.py` + `cli/main_enhanced.py` | — | ✅ | 两个 CLI 入口均注册交互式审批面板（rich Panel + y/a/n）+ `set_interactive_context(True)` |
| `agent/tool_guardrails.py` | 502 | ✅ | 循环检测护栏（重复失败/幂等无进展），警告默认开、硬停止 opt-in；已接入 tool_executor / conversation_loop |

**测试**：`tests/tools/test_approval.py`（静态裁决 8 + 门禁 11 + 回调注册 5 + 决策规范化 18 + terminal 集成 6，共 47 项）+ `tests/agent/test_tool_guardrails.py`（36 项）+ `tests/cli/test_cli_approval.py`（18 项），共 **101 项全通过**。移植 Hermes 同款检查思路（对标 `tools/approval.py::_run_approval_gate` 与 `terminal_tool.py::_check_all_guards`）。

> 同期修复潜伏 bug：`approval.py` 中 `add_session_approval` 使用 `time.time()` 但文件从未 `import time`——因原为死代码而未暴露，接线后补齐。

**未建（🟢 低优先级，需独立 `spirit/security/` 模块）**：

| 待创建文件 | 对应 Hermes | 说明 |
|-----------|-------------|------|
| `tirith_scanner.py` | `tools/tirith_security.py` (871行) | 安全扫描 |
| `threat_patterns.py` | `tools/threat_patterns.py` (284行) | 威胁模式库 |
| `skills_guard.py` | `tools/skills_guard.py` (1153行) | 技能安全守卫 |
| `url_safety.py` | `tools/url_safety.py` (503行) | URL 安全 |
| `supply_chain_audit.py` | `hermes_cli/security_audit.py` (576行) | 供应链审计 |
| `security_advisories.py` | `hermes_cli/security_advisories.py` (453行) | 安全公告 |

### 2.12 会话管理 — `spirit/sessions/` ✅ 已完成（核心）🟡 中优先级

| 文件 | 状态 | 说明 |
|------|------|------|
| `session_search.py` | ✅ | 跨会话搜索 |
| `session_recap.py` | ✅ | 会话回顾 |
| `session_export.py` | ✅ | 会话导出（HTML/MD 合并实现） |
| `storage/session_db.py` | ✅ | 三层终止 + 双模恢复（Resume/Branch） |

### 2.13 技能中心 — `spirit/skills_hub/` ✅ 已完成（Phase 4·2026-09-18）

> 对标 Hermes `agent/skill_commands.py`+`skill_bundles.py`+`skill_preprocessing.py`+`skill_utils.py`
> +`skill_provenance.py`+`skill_usage.py`+`tools/skills_hub.py`，按 Spirit「功能域合并 + 自注册」
> 约定收拢为一个内聚包。技能发现是单一事实源（frontmatter 解析 + 平铺/环境/禁用三层门）。

| 文件 | 行数 | 状态 | 说明 |
|------|------|------|------|
| `hub.py` | 694 | ✅ | 技能中心管理器（安装 / 列表 / 启停 / 索引聚合） |
| `commands.py` | 565 | ✅ | `/skill*` 斜杠命令分发（传输无关） |
| `discovery.py` | 481 | ✅ | 技能发现单一事实源（frontmatter / 三层门 / 外部目录 / SkillMeta） |
| `dispatch.py` | 439 | ✅ | 技能调用派发（参数绑定 + 执行） |
| `bundles.py` | 346 | ✅ | 技能包（bundle 打包 / 展开） |
| `__init__.py` | 273 | ✅ | 公共 API 导出 + 模块地图 |
| `preprocessing.py` | 159 | ✅ | 模板变量替换 + 内联 shell 展开 |
| `provenance.py` | 147 | ✅ | 写入来源 ContextVar + 安装来源记录 |
| `paths.py` | 130 | ✅ | 按调用解析的目录约定（SPIRIT_HOME / env 覆盖 / 测试重定向） |
| `usage.py` | 124 | ✅ | 使用 / 查看计数与活跃度聚合 |
| **小计** | **~3,358** | | **10 文件** |

**测试**：`tests/skills_hub/` 9 个测试文件 + conftest，**319 项全通过**（paths / discovery / preprocessing / usage / provenance / commands / bundles / hub / dispatch）。目录约定经 `paths` seam 重定向到临时目录，无需真实 SPIRIT_HOME。

### 2.14 进程注册表 — `spirit/process/` ✅ 已完成（Phase 4·2026-09-18）

> 对标 Hermes `tools/process_registry.py`(2349行)。后台进程注册表：派生、reader 线程、
> poll/read_log/wait、进程树终止、stdin 交互、watch 模式限流、完成队列与 `drain_notifications`、
> checkpoint 崩溃恢复。已接线 `terminal` 后台执行 + `goals` wait 屏障 + 完成通知回灌对话。
> 工具入口 `spirit/tools/process_tool.py`（`process` 工具）。

| 文件 | 行数 | 状态 | 说明 |
|------|------|------|------|
| `registry.py` | 1,720 | ✅ | `ProcessRegistry`：派生 / reader 线程 / poll / read_log / wait / 进程树终止 / stdin / watch 限流 / 完成队列 / checkpoint 恢复·模块级单例 `process_registry` |
| `notifications.py` | 240 | ✅ | `format_process_notification`：队列事件 → 可注入对话的文本 |
| `session.py` | 192 | ✅ | `ProcessSession` 数据模型 + 限额常量 + `format_uptime_short` |
| `__init__.py` | 66 | ✅ | 公共 API 导出 |
| **小计** | **~2,218** | | **4 文件** |

**测试**：`tests/process/` 5 个测试文件 + conftest，**149 项全通过**（registry / notifications / process_tool / watch / integration）。子进程派生经 seam 打桩，离线断言生命周期与通知回灌。

### 2.15 本地代理 — `spirit/proxy/` ❌ 🟢 低优先级

| 待创建文件 | 对应 Hermes | 说明 |
|-----------|-------------|------|
| `proxy_server.py` | `hermes_cli/proxy/` | OpenAI 兼容代理 |
| `upstream.py` | `hermes_cli/proxy/` | 上游适配器 |

### 2.16 检查点系统 — `spirit/checkpoint/` ❌ 🟢 低优先级

| 待创建文件 | 对应 Hermes | 说明 |
|-----------|-------------|------|
| `checkpoint_manager.py` | `tools/checkpoint_manager.py` (1675行) | 文件系统快照 |

### 2.17 外部集成 — `spirit/integrations/` ❌ 🟢 低优先级

| 待创建文件 | 对应 Hermes | 说明 |
|-----------|-------------|------|
| `home_assistant.py` | `tools/homeassistant_tool.py` (513行) | 智能家居 |
| `spotify.py` | `plugins/spotify/` | 音乐服务 |
| `google_meet.py` | `plugins/google_meet/` | 会议集成 |
| `teams.py` | `plugins/teams_meeting/` | Teams 集成 |

### 2.18 成就系统 — `spirit/achievements/` ❌ 🟢 低优先级

| 待创建文件 | 对应 Hermes | 说明 |
|-----------|-------------|------|
| `achievement_engine.py` | `plugins/achievements/plugin_api.py` (1061行) | 成就引擎 |

### 2.19 消息网关 — `spirit/gateway/` ✅ 🟡 中优先级

| 文件 | 状态 | 说明 |
|------|------|------|
| `platforms/telegram.py` | ✅ 已创建 | Telegram |
| `platforms/discord.py` | ✅ 已创建 | Discord |
| `platforms/slack.py` | ✅ 已创建 | Slack (293行) |
| `platforms/dingtalk.py` | ✅ 已创建 | 钉钉 (362行) |
| `platforms/feishu.py` | ✅ 已创建 | 飞书 (281行) |
| `platforms/wecom.py` | ✅ 已创建 | 企业微信 (345行) |
| `platforms/qq.py` | ✅ 已创建 | QQ (378行) |
| `gateway_runner.py` | ✅ 已创建 | 网关运行器 |

### 2.20 前端应用 ✅ 已完成

| 项目 | 状态 | 说明 |
|------|------|------|
| `desktop-app/` (桌宠桌面应用) | ✅ **旗舰界面** | Electron + Vue3 + xterm.js（23 个 TS/Vue，4,839 行）；12 个组件：PetSprite / CLITerminalPage / RadialMenu / SpeechBubble(Page) / StatusPopup(Page) / SystemPanel / KnowledgePanel / TaskCLI / VoiceChat / SpiritFoxAnimator |
| `vscode-extension/` (VSCode 扩展) | ✅ | 8 个 TS（聊天侧边栏 + 代码智能 + 补全 + 终端） |
| `web/` (Web Dashboard 前端) | ✅ 基础版 | index.html + css + js/4（app/chat/knowledge/settings） |

---

## 三、开发优先级规划

### Phase 1 — 核心闭环 🔴 (预计 2 周)

> 目标：Agent 可以完整运行对话循环 + 工具调用

| 序号 | 任务 | 模块 | 预计行数 |
|------|------|------|----------|
| 1.1 | ~~实现记忆管理器~~ | ✅ 已完成 | 473 行 |
| 1.2 | ~~实现 Agent 初始化~~ | ✅ 已完成 | 247 行（委托集中式配置） |
| 1.3 | ~~实现 Transport 层~~ | ✅ 已完成 `spirit/agent/transports/`（7 文件） | ~1,500 |
| 1.4 | ~~实现任务系统~~ | ✅ 已完成 | 1,664 行 + 78 测试 |
| 1.5 | ~~实现钩子系统~~ | ✅ 已完成 | ~800 行 + 31 测试 |
| 1.6 | ~~实现凭据池~~ | ✅ 已完成 | 413 行 |
| 1.7 | ~~集中式配置系统~~ | ✅ 已完成 | 834 行 + 34 测试 |
| **已完成** | | | **7 项（全部）** |
| **待完成** | | | **0 项** ✅ |

### Phase 2 — 多端接入 ✅ 已完成

> 目标：VSCode + Web + 消息平台 都能连接 Agent

| 序号 | 任务 | 模块 | 状态 |
|------|------|------|------|
| 2.1 | 完善 API 服务 | `spirit/api/`（server/events/context/memora_proxy） | ✅ |
| 2.2 | 消息网关框架 | `spirit/gateway/`（runner/registry/delivery/session/config） | ✅ |
| 2.3 | 平台适配器 ×7 | `gateway/platforms/`（telegram/discord/slack/dingtalk/feishu/wecom/qq） | ✅ |
| 2.4 | Web Dashboard 前端 | `web/` | ✅ 基础版 |
| 2.5 | 会话管理系统 | `spirit/sessions/`（search/recap/export） | ✅ |
| 2.6 | VSCode 扩展接入 | `vscode-extension/`（8 TS） | ✅ |

### Phase 3 — 桌宠与语音 ✅ 已完成（2026-09-16）

> 目标：桌面精灵 + 语音交互 可用（架构调整为 Electron 桌宠端 + Python 后端）

| 序号 | 任务 | 模块 | 状态 |
|------|------|------|------|
| 3.1 | 桌宠引擎 + 状态机 + 托盘 | `spirit/desktop/pet_engine.py` 等 + `desktop-app/electron/` | ✅ |
| 3.2 | 桌宠窗口 UI | `desktop-app/src/components/PetSprite.vue` | ✅ |
| 3.3 | 监控/任务/知识面板 | `SystemPanel / TaskCLI / KnowledgePanel.vue` | ✅ |
| 3.4 | 语音引擎 | `desktop/voice_engine.py` + `tools/voice.py` + `ws_server.py` + `VoiceChat.vue` | ✅ **STT→Agent→TTS 全链路打通** |
| 3.5 | 桌面应用打包 | `desktop-app/`（Electron + Vite） | ✅ |
| 3.6 | 安全系统 | `tools/approval.py` + `tools/terminal_tool.py` + `agent/tool_guardrails.py` | ✅ **审批门禁已接线** |
| **已完成** | | | **6 项（全部）** |

**3.4 / 3.6 本轮补完要点**（详见 2.4 / 2.11）：
- 3.4：`listen_and_respond` 从桩代码（仅回显“收到: xxx”）改为真管线，可注入 `agent_chat`；ws_server 新增 `synthesize_speech` / `voice_backends`；`VoiceChat.vue` 新增语音播报开关与音频播放（以前是“哑巴对话”）。
- 3.6：`check_command_approval` 从**死代码**变为真正生效的门禁——`terminal` 执行前调 `request_command_approval`，两个 CLI 注册交互式回调，无人确认时 fail-safe 阻止。
- 附带：修复 `registry.dispatch` 的 handler 签名适配器（救活约 21 个工具）+ `approval.py` 缺失的 `import time`。

**本轮新增测试**：170 项（dispatch 19 / approval+terminal 47 / voice 50 / guardrails 36 / cli 18），全量套件 **998 passed**，`npm run type-check` 零错误。

### Phase 4 — 高级功能 ✅ 完成（7/7 子系统）🟢 (预计 3 周)

> 目标：MoA、Profile、Goals、TUI、Computer Use 等。**七大子系统全部完成**：Goals(4.3) /
> 进程注册表(4.7) / MoA(4.1) / 技能中心(4.6) / Computer Use(4.5) / TUI(4.4)（2026-09-16 ~ 09-18）
> + Profile(4.2)（2026-09-27）。

| 序号 | 任务 | 模块 | 状态 |
|------|------|------|------|
| 4.1 | MoA 多模型协作 | `spirit/moa/`（5 文件 ~2,037 行 + 111 测试） | ✅ 已完成 |
| 4.2 | Profile 多实例 | `spirit/profile/`（5 文件 ~1,337 行 + 93 测试，精简子集）| ✅ 已完成 |
| 4.3 | 目标系统 | `spirit/goals/`（8 文件 ~2,300 行 + 231 测试）| ✅ 已完成 |
| 4.4 | TUI 终端界面 | `spirit/tui/`（5 文件 ~1,145 行 + 92 测试，精简子集）| ✅ 已完成 |
| 4.5 | Computer Use | `spirit/computer_use/`（8 文件 ~1,509 行 + 277 测试）| ✅ 已完成 |
| 4.6 | 技能中心 | `spirit/skills_hub/`（10 文件 ~3,358 行 + 319 测试）| ✅ 已完成 |
| 4.7 | 进程注册表 | `spirit/process/`（4 文件 ~2,218 行 + 149 测试）| ✅ 已完成 |
| **小计** | **7/7 完成** | **~13,904 行 + 1,272 测试** | |

### Phase 5 — 生态完善 ✅ 全部完成 🟢 (2026-09-27)

> 目标：代理服务器、检查点、集成、成就。5.1–5.5 五大任务全部落地——proxy/achievements 从零独立成模块；checkpoint/integrations 由 `tools/checkpoint.py`、`tools/platforms.py` 的薄能力升级为自包含子系统（保留旧工具注册不动以避免回归）；agent 辅助补齐 redact/i18n/insights。全部遵循「精简子集 + 注入式可测试 seam + 传输无关命令分发 + fail-soft 降级」范式，配套离线测试。

| 序号 | 任务 | 模块 | 状态 |
|------|------|------|------|
| 5.1 | 本地代理服务器 | `spirit/proxy/`（8 文件 ~925 行 + 66 测试，OpenAI 兼容 stdlib HTTP）| ✅ 已完成 |
| 5.2 | 检查点系统 | `spirit/checkpoint/`（4 文件 ~727 行 + 60 测试，内容寻址 blob 去重）| ✅ 已完成 |
| 5.3 | 外部集成 | `spirit/integrations/`（9 文件 ~1,355 行 + 113 测试，声明式框架 + HA 安全护栏）| ✅ 已完成 |
| 5.4 | 成就系统 | `spirit/achievements/`（5 文件 ~838 行 + 84 测试）| ✅ 已完成 |
| 5.5 | Agent 辅助模块 | `agent/` 补充 redact 549 + i18n 231 + insights 432 + locales（+ 95 测试）| ✅ 已完成 |
| **小计** | **5/5 完成** | **~5,057 行 + 418 测试** | |

### Phase 6 — 自进化 RSI ✅ 全部完成（6.A–6.F）🟡 中优先级

> 目标：让 Spirit 具备**领域内自主进化**能力——自主探索、自我构建任务、经验沉淀复用、可选人在环（无反馈自跑 / 需拍板才问）。
> 范式参考 **RSIAgent**（已 clone 到仓库根目录，training-free、**无需 GPU**），完整设计与论证见 [13-self-evolution-rsi.md](./13-self-evolution-rsi.md)。
> **本 Phase 的 6.A–6.F 子阶段即 doc 13 的 Phase A–F，一一对应。**

| 序号 | 子阶段 | 交付 | 状态 |
|------|--------|------|------|
| 6.A | 地基与角色协议 | `spirit/evolution/`（4 文件 ~950 行 + 77 测试：protocol/memory/roles/__init__ + README）| ✅ 已完成 |
| 6.B | Verifier 独立校验闭环 | `spirit/evolution/verifier.py`（Evidence 客观证据 + DomainVerifier 接口 + 回滚保护，+ 30 测试）| ✅ 已完成 |
| 6.C | Curriculum 自派任务 + DRS 单阶段进化 | `curriculum.py`+`loop.py`（CurriculumPlanner 只读复盘 + EvolutionLoop DRS 状态机：试目标→校验→蒸馏→练习→重试，STALLED/CONVERGED 可区分，可序列化续跑，+ 31 测试）| ✅ 已完成 |
| 6.D | 广度探索 BRS + 记忆冻结复用 | `memory_hash.py`+`phase1_wave.py`（记忆哈希+冻结快照+校验+FrozenMemory 只读复用；Phase1Wave 广度并行 + wave memory barrier，+ 23 测试）| ✅ 已完成 |
| 6.E | 可选人在环 HITL | `hitl.py`（HITLMode auto/always/never + 低置信度/分叉触发 + 超时 fail-open + 建议回灌记忆 source=user，+ 17 测试）| ✅ 已完成 |
| 6.F | 领域化 + 每日定时自主运行 | `domains/`（Domain 接口 + FinanceDomain 金融示例，注入式数据源/回测）+ `scheduler.py`（interval/daily 调度引擎 + tick + 日报，补全 cron 空壳，+ 36 测试）| ✅ 已完成 |

**小计**：`spirit/evolution/` 14 文件（protocol/memory/roles/verifier/curriculum/loop/memory_hash/phase1_wave/hitl/scheduler/__init__ + domains/{__init__,base,finance} + README）+ **215 测试全通**（6.A 77 · 6.B 30 · 6.C 32 · 6.D 23 · 6.E 17 · 6.F 36 内含 domains/scheduler）。

**cron 前置依赖已解除**：6.F 的 `scheduler.py` 自带 interval/daily 调度引擎（tick + 日报），不再依赖 `cron/` 空壳；进化记忆与续跑复用了 `goals/`（Ralph Loop）范式，技能库可对接 `skills_hub/`。

**复用而非重写**：Actor=`spirit/agent/`、续跑/恢复=`spirit/goals/`、技能库=`spirit/skills_hub/`、模型调用=`spirit/providers/`+`agent/transports/`、HITL 推送=`spirit/gateway/`。**仅新建 `spirit/evolution/` 一个包**。

**预计工作量**：8-11 周（A/B 各 1-2 周 · C/D 各 2 周 · E/F 各 1-2 周），每子阶段可独立验收。

### Phase 7 — 常驻自主探索 Autonomy ✅ 全部完成（7.A–7.D）🟡 中优先级

> 目标：解决“任务做完就停”——把 Phase 6 能力单元串成**永不停机的自主外层循环**：空闲/定时触发 → 提议探索目标 → 深挖执行 → 遇分叉/停滞**该问就问**（桌宠气泡）、**无应答 fail-open 自决** → 沙箱写笔记 → 沉淀记忆 → 提议下一个……
> 用户决策形态：**桌宠内置常驻** + **限定沙箱目录可写**（绝不触碰主代码库）。

| 序号 | 子阶段 | 交付 | 状态 |
|------|--------|------|------|
| 7.A | 沙箱写入护栏 | `spirit/autonomy/sandbox.py`（路径 containment：越界/`..`/绝对路径抛 SandboxViolation；读写列三接口；根=SPIRIT_HOME/autonomy 按调用解析，+ 5 测试）| ✅ 已完成 |
| 7.B | 常驻自主循环 | `spirit/autonomy/explorer.py`（AutonomyLoop：propose→EvolutionLoop 深挖→HITL 决策→沙箱笔记→沉淀；Scheduler interval 探索 + daily 日报；run_forever/start 守护线程；step/tick 可单步离线，+ 6 测试）| ✅ 已完成 |
| 7.C | 桌宠 WS 桥接 | `spirit/autonomy/bridge.py`（DesktopBridge：HITL ask→广播 autonomy_decision 气泡 + 线程安全队列等答复；autonomy_answer/status/start/stop 命令；超时/无通道 fail-open，+ 7 测试）| ✅ 已完成 |
| 7.D | 部署集成（完全自动化） | `spirit/autonomy/integration.py`：`make_llm_caller`（Transport 工厂纯补全 seam）+ `make_memory`（跨轮持久 EvolutionMemory）+ `make_execute`（驱动完整 SpiritAgent 在**沙箱工作目录**内真实执行，注入 workspace 系统提示 + chdir 锁定 + cwd 还原 + **跑完快照沙箱产物→丰富 Evidence.files/env_state**（修复 STALLED：Verifier 有据可裁）+ 全 fail-soft）+ `make_proposer`（**架构型自提议**：每轮基于 `progress_summary(memory)` 提出下一个架构/设计型问题，做完一个→自提新问题→继续探索）+ `resolve_interval_seconds`（探索间隔缺省 **5 小时** 对齐 API token 刷新，可经 `autonomy.interval_hours`/`.interval_seconds` 配置覆盖）；`build_autonomy` 把持久记忆 + DRS 三角色（Actor/Verifier/Curriculum 用同一 caller）+ execute + propose + 停止策略（缺省 `curriculum_review` + `max_rounds=5`，配合 `loop` 重试注入上一轮校验反馈→对任务自动迭代打磨直至真收敛，可经 `autonomy.stop_policy`/`.max_rounds` 配置覆盖）全部接上；`start_autonomy` 挂 WSServer + 守护线程；`_launcher.py` 挂钩（`--no-autonomy` 可关），+ 24 测试）| ✅ 已完成 |

**小计**：`spirit/autonomy/` 4 文件（sandbox/explorer/bridge/integration + __init__）+ **42 测试全通**（7.A 5 · 7.B 6 · 7.C 7 · 7.D 24）。复用 Phase 6：Curriculum/EvolutionLoop/HITL/Scheduler/Memory 全部复用，**仅新建 `spirit/autonomy/` 一个包**；HITL 询问经 `bridge.ask` 桥接桌宠气泡，写操作一律走 `Sandbox` 护栏。**完全自动化链路已闭合**：`build_autonomy` 缺省即接上持久记忆 + DRS 三角色推理 + 真实 execute（SpiritAgent 沙箱内动手）+ 架构型自提议，无 api_key 时 fail-soft 退化为“提议+写笔记”空转，绝不崩溃。**验证收敛已修复**：execute 跑完快照沙箱产物填入 Evidence，Verifier 有据判 PASS。**任务级自动迭代**：停止策略缺省 `curriculum_review` + `max_rounds=5`，且 `EvolutionLoop.run` 重试时把上一轮 Verifier 结论 + Curriculum 建议注入任务串（`_with_feedback`/`_feedback_from`），让 Agent 针对性改进而非盲目重做，直至真收敛（可配 `verifier_pass` 省 token）。

---

## 四、总量统计

| 类别 | 已完成 | 说明 |
|------|--------|------|
| **Python 代码（spirit/）** | ~58,900 行 / 204 文件 | 23 个后端模块（新增 proxy/checkpoint/integrations/achievements）|
| **前端代码（desktop-app）** | 4,894 行 / 23 文件 | Electron 桌宠端（12 组件） |
| **VSCode 扩展** | 8 个 TS | 聊天 + 代码智能 + 补全 |
| **Web 面板** | index.html + js/4 | 基础版 Dashboard |
| **测试** | 109 个 test_*.py | **2698 项通过 + 54 跳过 + 0 失败**（新增 418 项 Phase 5 + 215 项 Phase 6 evolution + 42 项 Phase 7 autonomy 测试全通）；覆盖 agent/api/cli/desktop/gateway/goals/hooks/sessions/skills/storage/task/tools/lsp + moa/process/skills_hub/computer_use/tui/profile/proxy/checkpoint/integrations/achievements/evolution/autonomy…（`test_env_config` 环境隔离缺陷已修复）|
| **后端模块** | 23/24 已建 | 未建的均为 🟢 低优先级 |
| **核心闭环完成度** | **~100%** | 对话循环 + 工具 + 桌宠 + 语音 + 安全审批 + CLI + 网关 + LSP 全通 |
| **含全部规划完成度** | **~92%** | 剩 Web Dashboard/独立安全扫描/acp_adapter 等（Phase 4 + Phase 5 + Phase 6 自进化 RSI + Phase 7 常驻自主探索 全部完成）|

---

## 五、快速参考

### 已实现的 Hermes 功能覆盖

| Hermes 模块 | Spirit 对应 | 覆盖度 |
|-------------|------------|--------|
| `run_agent.py` (AIAgent) | `spirit/agent/agent.py` | ✅ 90% |
| `agent/conversation_loop.py` | `spirit/agent/conversation_loop.py` | ✅ 90% |
| `agent/context_compressor.py` | `spirit/agent/context_compressor.py` | ✅ 85% |
| `agent/error_classifier.py` | `spirit/agent/error_handler.py` | ✅ 90% |
| `agent/prompt_builder.py` | `spirit/agent/prompt_builder.py` | ✅ 85% |
| `agent/tool_executor.py` | `spirit/agent/tool_executor.py` | ✅ 85% |
| `agent/tool_guardrails.py` | `spirit/agent/tool_guardrails.py` | ✅ 90% |
| `tools/approval.py` (危险命令审批) | `spirit/tools/approval.py` + `terminal_tool` 门禁 | ✅ 85% |
| `agent/transcription_*` / `tts_*` Provider | `spirit/desktop/voice_engine.py`（STT/TTS 多后端） | ✅ 80% |
| `hermes_cli/config.py` | `spirit/config.py` | ✅ 90% |
| `tools/` (93 files) | `spirit/tools/` (37 files) | ✅ 100% |
| `agent/transports/` | `spirit/agent/transports/` (7 files) | ✅ 已实现 |
| `agent/lsp/` (11 files) | `spirit/lsp/` (10 files) | ✅ 独立 LSP 系统 |
| `agent/memory_manager.py` | `spirit/agent/memory_manager.py` | ✅ 80% |
| `gateway/` | `spirit/gateway/` (7 平台) | ✅ 90% |
| `hermes_cli/` | `spirit/cli/` + `desktop-app` CLI 终端 | ⚠️ 40% |
| `hermes_cli/goals.py` (Ralph Loop) | `spirit/goals/`（8 文件 + 231 测试）| ✅ 100% |
| `hermes_cli/profiles.py` + `profile_describer.py` (多实例) | `spirit/profile/`（5 文件 + 93 测试，精简子集）| ✅ 70% |
| `agent/moa_loop.py` + `moa_trace.py` (MoA) | `spirit/moa/`（5 文件 + 111 测试）| ✅ 90% |
| `tools/process_registry.py` (后台进程) | `spirit/process/`（4 文件 + 149 测试）| ✅ 90% |
| `agent/skill_*.py` + `tools/skills_hub.py` | `spirit/skills_hub/`（10 文件 + 319 测试）| ✅ 85% |
| `tools/computer_use/` (桌面控制) | `spirit/computer_use/`（8 文件 + 277 测试）| ✅ 80% |
| `tui_gateway/` (Python 核心) | `spirit/tui/`（5 文件 + 92 测试，精简子集）| ✅ 70% |
| OpenAI 兼容 API server（`.plans/openai-api-server.md`） | `spirit/proxy/`（8 文件 + 66 测试，stdlib HTTP/SSE）| ✅ 80% |
| `tools/checkpoint` 能力 | `spirit/checkpoint/`（4 文件 + 60 测试，内容寻址 blob 去重）| ✅ 85% |
| `tools/platforms.py` + `gateway/`（外部集成） | `spirit/integrations/`（9 文件 + 113 测试，声明式框架）| ✅ 80% |
| `agent/redact.py`（敏感信息脱敏） | `spirit/agent/redact.py`（549 行）| ✅ 85% |
| `agent/i18n.py`（国际化） | `spirit/agent/i18n.py`（231 行 + locales）| ✅ 80% |
| `agent/insights.py`（使用洞察） | `spirit/agent/insights.py`（432 行）| ✅ 75% |
| `cron/` | `spirit/tools/extra_tools.py` + `spirit/evolution/scheduler.py`（interval/daily 调度引擎）| ✅ 调度引擎已补全 |
| `acp_adapter/` | ❌ 未实现 | ❌ 0% |

### 关键路径

```
Phase 1 (核心) → Phase 2 (多端) → Phase 3 (桌宠) → Phase 4 (高级) → Phase 5 (生态) → Phase 6 (自进化) → Phase 7 (自主)
   ✅ 完成         ✅ 完成         ✅ 完成        ✅ 7/7 子系统完成   ✅ 5/5 子系统完成   ✅ 6/6 子阶段完成    ✅ 4/4 子阶段完成
     2 周            2 周             2 周            3 周            2 周           8-11 周 (A-F)        1-2 周 (A-D)
                                                                    
当前阶段：Phase 1-7 ✅ 全部完成（Phase 7 常驻自主探索：`spirit/autonomy/` 沙箱护栏 + AutonomyLoop 常驻循环 + 桌宠 WS 桥接 + 部署集成（完全自动化：持久记忆 + DRS 三角色 + 真实 execute）），全量 2687 项测试通过 / 0 失败
剩余工作：🟢 低优先级生态功能（Web Dashboard/独立安全扫描/acp_adapter 等）；**Phase 6 自进化 RSI + Phase 7 常驻自主探索 已全部完成**
```

---

*文档位置：`spirit-agent-main/docs/07-development-checklist.md`*
