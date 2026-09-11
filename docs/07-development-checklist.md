# Spirit Agent V2 — 完整开发清单

> 基于 `hermes文件功能手册.md`（3105 文件）对标，结合 `00-product-vision-v2.md` 和 `00-architecture-v2.md` 设计
> 
> 生成时间：2026-09-08

---

## 项目总览

| 指标 | 数值 |
|------|------|
| **已完成 Python 文件** | 98 个 |
| **已完成 Python 行数** | 29,012 行 |
| **已完成 TypeScript 文件** | 8 个 |
| **规划模块总数** | 24 个目录 |
| **已实现模块** | 6 个（agent/tools/api/storage/cli/config） |
| **待实现模块** | 18 个 |

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
| `__init__.py` | 21 | ✅ 完成 | 模块初始化 |
| **小计** | **4,598** | | |

**还缺少的 Agent 子模块**（参考 Hermes 156 个文件）：

| 待实现功能 | 对应 Hermes | 优先级 | 说明 |
|-----------|-------------|--------|------|
| `memory_manager.py` | `agent/memory_manager.py` (1231行) | ✅ 已完成 | 跨会话记忆管理 (473行) |
| `agent_init.py` | `agent/agent_init.py` (2200行) | ✅ 已完成 | 完整初始化逻辑 (247行, 委托集中式配置) |
| `credential_pool.py` | `agent/credential_pool.py` (2601行) | ✅ 已完成 | 多凭据轮换 (413行) |
| `transports/` | `agent/transports/` (11 files) | 🔴 高 | 多 Provider 适配层 |
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
| 文件数 | 34 个 |
| 总行数 | 11,979 行 |
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
| `approval.py` | 790 | ✅ | 危险命令检测与审批 |
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

## 二、待实现模块 ❌

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

### 2.3 桌面精灵 — `spirit/desktop/` ❌ 🟡 中优先级

| 待创建文件 | 说明 |
|-----------|------|
| `pet.py` | 桌宠主类 |
| `pet_window.py` | 悬浮窗 UI |
| `tray_icon.py` | 系统托盘 |
| `monitor_panel.py` | 系统监控面板 |
| `task_panel.py` | 任务进度面板 |
| `knowledge_panel.py` | 知识库面板 |

### 2.4 语音引擎 — `spirit/voice/` ❌ 🟡 中优先级

| 待创建文件 | 对应 Hermes | 说明 |
|-----------|-------------|------|
| `voice_engine.py` | — | 语音引擎主类 |
| `stt.py` | `agent/transcription_provider.py` | 语音识别 |
| `tts.py` | `agent/tts_provider.py` | 语音合成 |
| `wake_word.py` | — | 唤醒词检测 |
| `voice_mode.py` | `tools/voice_mode.py` (1218行) | Push-to-talk |

### 2.5 MoA 多模型协作 — `spirit/moa/` ❌ 🟢 低优先级

| 待创建文件 | 对应 Hermes | 说明 |
|-----------|-------------|------|
| `moa_loop.py` | `agent/moa_loop.py` (1182行) | 多模型协作循环 |
| `moa_trace.py` | `agent/moa_trace.py` (167行) | 追踪记录 |
| `moa_config.py` | — | 配置管理 |

### 2.6 Profile 多实例 — `spirit/profile/` ❌ 🟢 低优先级

| 待创建文件 | 对应 Hermes | 说明 |
|-----------|-------------|------|
| `profile_manager.py` | `hermes_cli/profiles.py` (2225行) | Profile 管理 |
| `profile_distribution.py` | `hermes_cli/profile_distribution.py` (726行) | Git 分发 |
| `profile_describer.py` | `hermes_cli/profile_describer.py` (288行) | 自动描述 |

### 2.7 目标系统 — `spirit/goals/` ❌ 🟢 低优先级

| 待创建文件 | 对应 Hermes | 说明 |
|-----------|-------------|------|
| `goal_engine.py` | `hermes_cli/goals.py` (1749行) | Ralph Loop |
| `goal_tracker.py` | — | 进度追踪 |

### 2.8 TUI 终端界面 — `spirit/tui/` ❌ 🟢 低优先级

| 待创建文件 | 对应 Hermes | 说明 |
|-----------|-------------|------|
| `tui_server.py` | `tui_gateway/server.py` (15619行) | WebSocket 服务 |
| `project_tree.py` | `tui_gateway/project_tree.py` (640行) | 项目树 |
| `slash_worker.py` | `tui_gateway/slash_worker.py` (164行) | 斜杠命令 |
| `render.py` | `tui_gateway/render.py` (49行) | 渲染桥接 |

### 2.9 Computer Use 桌面控制 — `spirit/computer_use/` ❌ 🟢 低优先级

| 待创建文件 | 对应 Hermes | 说明 |
|-----------|-------------|------|
| `cua_backend.py` | `tools/computer_use/cua_backend.py` (2335行) | 桌面控制 |
| `vision_routing.py` | `tools/computer_use/vision_routing.py` (204行) | 视觉路由 |
| `permissions.py` | `tools/computer_use/permissions.py` (200行) | 权限管理 |
| `schema.py` | `tools/computer_use/schema.py` (238行) | 工具 Schema |

### 2.10 Web Dashboard — `spirit/dashboard/` ❌ 🟡 中优先级

| 待创建文件 | 对应 Hermes | 说明 |
|-----------|-------------|------|
| `web_server.py` | `hermes_cli/web_server.py` (18137行) | Web UI 服务 |
| `auth_provider.py` | `hermes_cli/dashboard_auth/` | 认证 |
| `session_export.py` | `hermes_cli/session_export.py` (317行) | 会话导出 |
| `skin_engine.py` | `hermes_cli/skin_engine.py` (926行) | 主题引擎 |

### 2.11 安全系统 — `spirit/security/` ❌ 🟡 中优先级

| 待创建文件 | 对应 Hermes | 说明 |
|-----------|-------------|------|
| `tirith_scanner.py` | `tools/tirith_security.py` (871行) | 安全扫描 |
| `threat_patterns.py` | `tools/threat_patterns.py` (284行) | 威胁模式库 |
| `skills_guard.py` | `tools/skills_guard.py` (1153行) | 技能安全守卫 |
| `url_safety.py` | `tools/url_safety.py` (503行) | URL 安全 |
| `supply_chain_audit.py` | `hermes_cli/security_audit.py` (576行) | 供应链审计 |
| `security_advisories.py` | `hermes_cli/security_advisories.py` (453行) | 安全公告 |

### 2.12 会话管理 — `spirit/sessions/` ❌ 🟡 中优先级

| 待创建文件 | 对应 Hermes | 说明 |
|-----------|-------------|------|
| `session_search.py` | `tools/session_search_tool.py` (921行) | 跨会话搜索 |
| `session_recap.py` | `hermes_cli/session_recap.py` (322行) | 会话回顾 |
| `session_export_html.py` | `hermes_cli/session_export_html.py` (871行) | HTML 导出 |
| `session_export_md.py` | `hermes_cli/session_export_md.py` (279行) | MD 导出 |
| `session_filters.py` | `hermes_cli/session_filters.py` (208行) | 过滤/归档 |

### 2.13 技能中心 — `spirit/skills_hub/` ❌ 🟢 低优先级

| 待创建文件 | 对应 Hermes | 说明 |
|-----------|-------------|------|
| `hub_manager.py` | `tools/skills_hub.py` (4227行) | 技能中心 |
| `curator.py` | `agent/curator.py` (2016行) | 后台维护 |
| `skills_sync.py` | `tools/skills_sync.py` (1182行) | 技能同步 |
| `skills_index.py` | — | 技能索引 |

### 2.14 进程注册表 — `spirit/process/` ❌ 🟢 低优先级

| 待创建文件 | 对应 Hermes | 说明 |
|-----------|-------------|------|
| `process_registry.py` | `tools/process_registry.py` (2348行) | 进程管理 |
| `async_delegation.py` | `tools/async_delegation.py` (935行) | 异步委托 |

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

### 2.20 前端应用 ❌

| 项目 | 状态 | 说明 |
|------|------|------|
| `web/` (Web Dashboard 前端) | ❌ 未创建 | React + TypeScript |
| `desktop-app/` (桌宠桌面应用) | ❌ 未创建 | Python + Web UI |

---

## 三、开发优先级规划

### Phase 1 — 核心闭环 🔴 (预计 2 周)

> 目标：Agent 可以完整运行对话循环 + 工具调用

| 序号 | 任务 | 模块 | 预计行数 |
|------|------|------|----------|
| 1.1 | ~~实现记忆管理器~~ | ✅ 已完成 | 473 行 |
| 1.2 | ~~实现 Agent 初始化~~ | ✅ 已完成 | 247 行（委托集中式配置） |
| 1.3 | 实现 Transport 层 | `spirit/agent/transports/` | ~2000 |
| 1.4 | ~~实现任务系统~~ | ✅ 已完成 | 1,664 行 + 78 测试 |
| 1.5 | ~~实现钩子系统~~ | ✅ 已完成 | ~800 行 + 31 测试 |
| 1.6 | ~~实现凭据池~~ | ✅ 已完成 | 413 行 |
| 1.7 | ~~集中式配置系统~~ | ✅ 已完成 | 834 行 + 34 测试 |
| **已完成** | | | **4 项** |
| **待完成** | | | **1 项 (Transport 层)** |

### Phase 2 — 多端接入 🟡 (预计 2 周)

> 目标：VSCode + Web + 消息平台 都能连接 Agent

| 序号 | 任务 | 模块 | 预计行数 |
|------|------|------|----------|
| 2.1 | 完善 API 服务 | `spirit/api/` 扩展 | ~1000 |
| 2.2 | 实现消息网关框架 | `spirit/gateway/` | ~3000 |
| 2.3 | Telegram 适配器 | `spirit/gateway/platforms/telegram.py` | ~2000 |
| 2.4 | Discord 适配器 | `spirit/gateway/platforms/discord.py` | ~1500 |
| 2.5 | Web Dashboard 前端 | `web/` | ~5000 |
| 2.6 | 会话管理系统 | `spirit/sessions/` | ~2000 |
| **小计** | | | **~14,500** |

### Phase 3 — 桌宠与语音 🟡 (预计 2 周)

> 目标：桌面精灵 + 语音交互 可用

| 序号 | 任务 | 模块 | 预计行数 |
|------|------|------|----------|
| 3.1 | 桌宠主类 + 系统托盘 | `spirit/desktop/pet.py` + `tray_icon.py` | ~800 |
| 3.2 | 悬浮窗 UI | `spirit/desktop/pet_window.py` | ~600 |
| 3.3 | 监控/任务/知识面板 | `spirit/desktop/*_panel.py` | ~1500 |
| 3.4 | 语音引擎 | `spirit/voice/` | ~2000 |
| 3.5 | 桌面应用打包 | `desktop-app/` | ~1000 |
| 3.6 | 安全系统 | `spirit/security/` | ~2500 |
| **小计** | | | **~8,400** |

### Phase 4 — 高级功能 🟢 (预计 3 周)

> 目标：MoA、Profile、Goals、TUI、Computer Use 等

| 序号 | 任务 | 模块 | 预计行数 |
|------|------|------|----------|
| 4.1 | MoA 多模型协作 | `spirit/moa/` | ~1500 |
| 4.2 | Profile 多实例 | `spirit/profile/` | ~2000 |
| 4.3 | 目标系统 | `spirit/goals/` | ~1200 |
| 4.4 | TUI 终端界面 | `spirit/tui/` | ~3000 |
| 4.5 | Computer Use | `spirit/computer_use/` | ~2500 |
| 4.6 | 技能中心 | `spirit/skills_hub/` | ~3000 |
| 4.7 | 进程注册表 | `spirit/process/` | ~2000 |
| **小计** | | | **~15,200** |

### Phase 5 — 生态完善 🟢 (预计 2 周)

> 目标：代理服务器、检查点、集成、成就

| 序号 | 任务 | 模块 | 预计行数 |
|------|------|------|----------|
| 5.1 | 本地代理服务器 | `spirit/proxy/` | ~800 |
| 5.2 | 检查点系统 | `spirit/checkpoint/` | ~1200 |
| 5.3 | 外部集成 | `spirit/integrations/` | ~2500 |
| 5.4 | 成就系统 | `spirit/achievements/` | ~800 |
| 5.5 | Agent 辅助模块 | `agent/` 补充 (redact, i18n, insights...) | ~5000 |
| **小计** | | | **~10,300** |

---

## 四、总量统计

| 类别 | 已完成 | 待开发 | 总计 |
|------|--------|--------|------|
| **Python 代码** | 29,012 行 | ~44,000 行 | ~73,000 行 |
| **TypeScript 代码** | 2,600 行 | ~5,000 行 | ~7,600 行 |
| **CSS/JS 前端** | 723 行 | ~3,000 行 | ~3,700 行 |
| **文档** | 6,281 行 | — | 6,281 行 |
| **模块目录** | 6/24 (25%) | 18/24 (75%) | 24 |
| **工具数** | 59 | — | 59 |
| **测试数** | 401 | — | 401 |
| **完成度** | **~38%** | **~62%** | 100% |

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
| `hermes_cli/config.py` | `spirit/config.py` | ✅ 90% |
| `tools/` (93 files) | `spirit/tools/` (34 files) | ✅ 100% |
| `agent/transports/` | ❌ 未实现 | ❌ 0% |
| `agent/memory_manager.py` | `spirit/agent/memory_manager.py` | ✅ 80% |
| `gateway/` | `spirit/gateway/` (7 平台) | ⚠️ 60% |
| `hermes_cli/` | `spirit/cli/` (基础) | ⚠️ 20% |
| `cron/` | `spirit/tools/extra_tools.py` | ⚠️ 30% |
| `acp_adapter/` | ❌ 未实现 | ❌ 0% |

### 关键路径

```
Phase 1 (核心) → Phase 2 (多端) → Phase 3 (桌宠) → Phase 4 (高级) → Phase 5 (生态)
     2 周            2 周             2 周            3 周            2 周
                                                                    
总预计开发周期：~11 周（单人全职）
```

---

*文档位置：`spirit-agent-main/docs/07-development-checklist.md`*
