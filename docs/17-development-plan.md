# 17 · Spirit Agent V3 开发规划

> **目标读者**：Spirit 维护者、贡献者、Release Manager
> **生效版本**：V0.2.3 → V3
> **状态**：草案 v2 · 2026-10-10
> **前置文档**：
> - [15-product-roadmap-v3.md](./15-product-roadmap-v3.md)（V3 产品规划）
> - [16-feature-roadmap.md](./16-feature-roadmap.md)（V3 功能规划）
> **架构差距文档**：
> - [`Hermes Spirit 架构模块对比.md`](../../Hermes%20Spirit%20架构模块对比.md)（244 vs 28 文件，227 缺口，18 节详解）
> **核心约束**（来自用户明确约定）：
> 1. **不擅自对 Agent 项目做升级改动**——只有用户明确要求某功能/修复时才动源码
> 2. **任务收尾不在项目里新增文件**（规划文档除外）+ 不跑全量 pytest
> 3. **验证方式应轻量**——只跑直接相关的少量测试 + 一次性脚本
> 4. **Spirit 桌宠 Agent 不自改源码自我升级**（与 Phase 7 沙箱约束一致）

---

## 0. 开发规划总览

V3 共 **74 项功能**（原 38 项 + 13 项前端显示层补完 + 23 项 P1-P4 完整缺口 §14.8），分布如下：

| 优先级 | 项数 | 估算总工时 | 完成节点 |
|--------|------|------------|----------|
| **P1（必做）** | 9 | ~13 周 | v0.2.3 - v0.4.0 |
| **P2（应该做）** | 9 + 7 = 16 | ~11 + 7 = 18 周 | v0.3.0 - v0.5.0 |
| **P3（可探索）** | 7 + 7 = 14 | ~9 + 8 = 17 周 | V3.0.0 |
| **前端显示层 P0-P1** | 7 | ~3.8 周 | v0.3.6 - v0.3.8 |
| **前端显示层 P2** | 3 | ~1.3 周 | v0.3.9 |
| **前端显示层 P3** | 3 | ~2.5 周 | v0.3.10 + v0.4.1 |
| **前端 UX P1-P4 §14.8** | 16 项新增 + 7 项已含 | ~17 周 | v0.3.11 - v0.5.6 |

**总投入估算**：~72 周（按 1 人 FTE，14-16 个月）

**关键节点**：
- **v0.2.3**（6 周）：错误恢复链 + 工具结果分类 + 并发超时修复 + provider 切换保护 + 上下文压缩鲁棒性 v2 + 准确率归因与提升
- **v0.3.0**（3 周）：截断多策略 + 活性检测 + provider 切换保护 v2 + 动态 prompt 组成
- **v0.3.0 补完（v0.3.1-v0.3.5）**（7 周）：推理控制分离 + Provider 体系 + 消息清洗 + 会话持久化 + 思考块 scrubber + turn facade 拆分 + Micro compaction
- **v0.3.0 补完（v0.3.6-v0.3.10）**（6 周）：Spirit 前端显示层补完 v1 —— KawaiiSpinner 思考动画 + 文件 diff 内联 + 友好动词 + 智能 preview + shell 摘要 + think 状态机 + StatusOutputMixin + bridge 多行 + 单写者流控 + skin engine
- **v0.3.11**（2 周）：Compaction 进度展示 + MoA 进度 + Plan display（§14.8 P1 #3/#4/#5）
- **v0.3.12**（3 周）：Stream diag + Turn failure copy + Display 模块拆分 + 完整 Redact 模块（§14.8 P2 #9-12）
- **v0.4.0**（1 周）：桌宠进度推送 + HITL 面板 + BRS 接线
- **v0.4.1-v0.4.4**（4 周）：TUI Gateway 主体（§14.8 P3 #14）—— TUI MVP / status bar / stream panel / TUI 1.0 GA
- **v0.4.5**（5 周）：Web 前端 + Gateway 4 层回调管道（§14.8 P3 #15/#16）
- **v0.4.6**（2 周）：Double-channel 广播 + i18n 多语言（§14.8 P3 #17/#18）
- **v0.5.0**（8 周）：成效 harness + 领域验证器 + 日报 + 经验确认 + wave 可视化 + 任务历史 + 摘要 + 状态可视化
- **v0.5.6**（3 周）：Image routing + WebSocket chunk_delay + Sidebar 完整字段 + Prompt plan preview（§14.8 P4 #19-23）
- **v0.5.0+ 长期**（7 周）：MoA 升级 + 上下文引擎抽象 + 子 agent lifecycle + Shell hooks + 后台 review + Relay 架构 + Provider adapter 矩阵
- **V3.0.0**（11 周）：消息清理 + 敏感信息脱敏 + regression suite + strict-alternation 评估 + 多 session PoC + 经验导入导出 + 任务调度

> **规划依据**：13 节"Hermes↔Spirit 架构差距分析"系统对比 `hermes-agent-main/agent/` (244 文件) vs `spirit-agent-main/spirit/agent/` (28 文件)，找出 50+ 关键架构模块缺口。14 节"Spirit 前端显示层差距分析"系统对比 `hermes-agent-main/agent/display.py` (1225 行) + `status_output.py` + `stream_delivery.py` + `think_scrubber.py` + `notification_presentation.py`，找出 **23 项 P1-P4 完整缺口**（详见 §14.8）。

---

## 1. v0.2.3：可靠性补丁 + 准确率归因（6 周）

**目标**：解决 AgentBench / SWE-bench 暴露的 5 个致命问题 + 把 Spirit 准确率从 62% 提升到 ≥72%。

| 编号 | 功能 | 工时 | 负责人 | 验证方式 |
|------|------|------|--------|----------|
| R-P1.1 | 错误恢复链 | 1.5 周 | — | AgentBench db 回归 + 单测 5 类处理器 |
| R-P1.2 | 工具结果分类 | 1 周 | — | AgentBench db-19 不再循环 + 单测 5 类 |
| R-P1.3 | 并发工具超时修复 | 0.5 周 | — | AgentBench OS 不再 420s + 单测 liveness |
| R-P1.4 | provider 切换保护 | 0.5 周 | — | SWE-bench 小样本对比 + 单测 |
| R-P1.5 | 上下文压缩鲁棒性 v2 | 1 周 | — | 长任务失忆修复 + 单测 6 类 |
| M-P1.0 | 准确率归因 + 提升 | 0.5 周 | — | 归因报告 + AgentBench / SWE-bench 准确率提升 |
| — | 文档更新 + 集成测试 | 1 周 | — | 手测 3 个回归场景 + release notes |

**验收标准**：
- AgentBench db 类别工具失败循环次数 ≤2 次/任务（vs 现状 ~8）
- AgentBench OS 类别 420s 超时事故率 0%（vs 现状 100%）
- AgentBench kg 类别 strict-alternation 400 诊断报告完成（决策是否进 M-P3）
- Spirit 准确率从 62% 提升到 ≥72%（SWE-bench / AgentBench 综合）
- 长任务 30+ 步后不再失忆（context_compressor v2）
- 单测覆盖率 100%（新模块）
- 不破坏现有 fallback 切换逻辑
- 不破坏现有 2,858 项既有测试

**代码组织**：
```
新增文件：
  spirit/agent/turn_recovery.py           # 错误恢复链（400-600 行）
  spirit/agent/tool_result_classification.py  # 工具结果分类（150-250 行）
  spirit/agent/liveness.py                # 活性检测（80-120 行，含在 tool_executor 重构里）
  spirit/agent/context_compressor_v2.py   # 上下文压缩 v2（400-600 行，align Hermes stall/rejection/route_pin）
  spirit/agent/accuracy_attribution.py    # 准确率归因（80-120 行）

修改文件：
  spirit/agent/conversation_loop.py       # 集成 turn_recovery + accuracy_attribution
  spirit/agent/tool_executor.py           # 集成结果分类 + 拆分超时
  spirit/agent/error_handler.py           # 集成修复处理器
  spirit/agent/context_compressor.py      # 调用 v2 模块 + 保留 v1 兼容
```

**验证脚本**（一次性，不入项目）：
```bash
# 跑 AgentBench 回归对比
python C:\Users\Administrator\AppData\Local\Temp\agentbench_eval.py \
  --categories db os --limit 5 --agents Spirit --max-iters 8 \
  --task-timeout 90 --output ab_v0.2.3_regression.jsonl

# 对比 v0.2.2 vs v0.2.3 准确率
python C:\Users\Administrator\AppData\Local\Temp\compare_ab.py \
  ab_v0.2.2.jsonl ab_v0.2.3_regression.jsonl
```

---

## 2. v0.3.0：可靠性完善 + Prompt 工程（3 周）

**目标**：把"截断 / 活性 / provider / Prompt 组成"四个可靠性问题补齐。

| 编号 | 功能 | 工时 | 验证方式 | 状态 |
|------|------|------|----------|------|
| R-P2.1 | 截断多策略 | 1 周 | 思考模型截断事故 + 单测 3 类策略 | ✅ v0.3.0 |
| R-P2.2 | 活性检测 | 0.5 周 | 长任务流式中断 + 单测 3 类 probe | ✅ v0.3.0 |
| R-P2.3 | provider 切换保护 v2 | 0.5 周 | 跨 provider 切换 + 单测 | ⏳ 待开发 |
| R-P2.4 | 动态 prompt 组成 | 1 周 | 4 类任务 prompt 切换 + 单测 | ⏳ 待开发 |


**验收标准**：
- 思考模型截断事故处理 3 策略全跑
- 活性检测覆盖流式 / 工具 / 后台 3 类
- provider 切换成功率 ≥95%
- 动态 prompt 组成：4 类任务（代码 / 问答 / 探索 / 桌宠）prompt 可独立配置
- 不破坏 v0.2.3 既有功能

**代码组织**：
```
新增文件：
  spirit/agent/turn_truncation.py        # 截断多策略（150-250 行）
  spirit/agent/turn_liveness.py          # 活性检测（80-120 行）
  spirit/agent/prompt_composer.py        # 动态 prompt 组成（200-300 行）

修改文件：
  spirit/agent/conversation_loop.py      # 集成 turn_truncation
  spirit/agent/tool_executor.py          # 挂 liveness probe
  spirit/agent/prompt_builder.py         # 重构为 prompt_composer + 保留旧 API
```

### 2.1 v0.3.0 补完 (v0.3.1-v0.3.5)：Hermes↔Spirit 差距补完（7 项，7 周）

> **为什么是补完**：v0.3.0 原始规划的 4 项是「可靠性补丁」。完成 R-P2.2 后扫 Hermes 的 `agent/` 目录发现还有 50+ 个关键架构模块 Spirit 缺失。按"依赖 / 价值密度 / 实施难度"拆为 7 项 R-P3.x，按 v0.3.1-v0.3.5 节奏补完。完整依据见 §13。

| 编号 | 功能 | 工时 | 依据（Hermes 参考模块） | 验证方式 |
|------|------|------|--------------------------|----------|
| **R-P3.1** | 推理控制分离 | 1 周 | `reasoning_effort.py` / `reasoning_params.py` / `reasoning_timeouts.py` / `reasoning_summaries.py`（共 32 KB） | 3 类推理 provider (DeepSeek-R1 / OpenAI-o1 / Claude-thinking) 切换 + 单测 |
| **R-P3.2** | Provider 体系升级 | 1 周 | `provider_registry.py` / `provider_base.py` / `provider_projection.py` / `provider_media.py`（共 19 KB） | 6 类 provider 适配 + registry 热插拔 + 单测 |
| **R-P3.3** | 消息清洗 | 1 周 | `message_sanitization.py`（36 KB）+ `message_metadata.py` + `message_content.py` | 4 类清洗 + 3 类 metadata 投射 + 单测 |
| **R-P3.4** | 会话持久化 mixin | 0.5 周 | `session_persistence.py`（25 KB）+ `session_activity.py` | 重启后上下文可恢复 + 单测 |
| **R-P3.5** | 思考块 scrubber | 0.5 周 | `think_scrubber.py`（9 KB）+ `repetition_guard.py`（4 KB） | 3 类 provider think 块正确剥离 + repetition 检测防循环 + 单测 |
| **R-P3.6** | turn facade 拆分 | 2 周 | `turn_iteration_prep.py` / `turn_facade.py` / `turn_facade_lease.py` / `turn_finalizer.py` / `turn_final_response.py`（共 130+ KB，27 个子文件） | 把 `conversation_loop.py` 1300 行拆为 5-8 个 module-level pure function（Hermes 风格），单测覆盖率 100% |
| **R-P3.7** | Micro compaction | 0.5 周 | `micro_compaction.py`（25 KB）+ `native_compaction.py`（18 KB） | 单步增量压缩 + native provider 原生压缩联动 + 单测 |

**验收标准**：
- 推理控制：3 类推理 provider 都能独立配 reasoning effort / max_tokens / timeout
- Provider 体系：registry 支持运行时增删 provider，零重启热插拔
- 消息清洗：surrogates / control chars / oversized images / close_interrupted 4 类 100% 命中
- 会话持久化：kill -9 后重启能恢复会话上下文（最少 24h 内）
- 思考块 scrubber：DeepSeek-R1 / OpenAI-o1 / Claude-thinking 三家 think 块 100% 剥离，repetition guard 检测到 3 轮同构响应即触发重生成
- turn facade 拆分：`conversation_loop.py` 降到 400 行以下，5-8 个独立模块（每个 < 300 行），单测覆盖率 100%
- Micro compaction：单步压缩 < 100ms，原生压缩 provider 走 native 路径

**代码组织**：
```
新增文件（v0.3.0 补完 7 项）：
  spirit/agent/reasoning_controller.py       # R-P3.1（150-200 行）
  spirit/agent/reasoning_timeouts.py         # R-P3.1（80-120 行）
  spirit/agent/reasoning_summaries.py        # R-P3.1（80-100 行）
  spirit/agent/provider_registry.py          # R-P3.2（200-300 行）
  spirit/agent/provider_base.py              # R-P3.2（100-150 行）
  spirit/agent/provider_projection.py        # R-P3.2（80-120 行）
  spirit/agent/message_sanitization.py       # R-P3.3（200-300 行）
  spirit/agent/message_metadata.py           # R-P3.3（80-120 行）
  spirit/agent/message_content.py            # R-P3.3（60-80 行）
  spirit/agent/session_persistence.py        # R-P3.4（150-200 行）
  spirit/agent/think_scrubber.py             # R-P3.5（80-120 行）
  spirit/agent/repetition_guard.py           # R-P3.5（60-100 行）
  spirit/agent/turn_iteration_prep.py        # R-P3.6（150-200 行）
  spirit/agent/turn_facade.py                # R-P3.6（100-150 行）
  spirit/agent/turn_facade_lease.py          # R-P3.6（100-150 行）
  spirit/agent/turn_final_response.py        # R-P3.6（100-150 行）
  spirit/agent/turn_finalizer.py             # R-P3.6（150-200 行）
  spirit/agent/micro_compaction.py           # R-P3.7（150-200 行）
  spirit/agent/native_compaction.py          # R-P3.7（100-150 行）

修改文件：
  spirit/agent/conversation_loop.py          # R-P3.6 拆分主循环
  spirit/agent/turn_context.py               # R-P3.3 集成 message_sanitization
  spirit/agent/provider_switch_protection.py # R-P3.2 重构为 registry 模式
  spirit/agent/tool_executor.py              # R-P3.1 读 reasoning_effort
  spirit/storage/session_db.py               # R-P3.4 集成 session_persistence
```

**验证脚本**（一次性，不入项目）：
```bash
# R-P3.1 推理 provider A/B
python C:\Users\Administrator\AppData\Local\Temp\reasoning_ab.py \
  --providers deepseek-r1 openai-o1 claude-thinking \
  --effort low medium high \
  --output reasoning_ab_v0.3.1.jsonl

# R-P3.2 provider 热插拔
python C:\Users\Administrator\AppData\Local\Temp\provider_hotplug.py \
  --add anthropic --remove openai --add bedrock \
  --output provider_hotplug_v0.3.2.json

# R-P3.6 conversation_loop 拆分回归
python C:\Users\Administrator\AppData\Local\Temp\loop_refactor_regression.py \
  --old conversation_loop_old.py --new conversation_loop.py \
  --test-suite tests/agent/test_conversation_loop.py
```

---

## 3. v0.4.0：桌宠 + BRS 接线（5 周）

**目标**：桌宠能监控长任务 + HITL 决策 + 自进化 BRS 真实跑。

| 编号 | 功能 | 工时 | 验证方式 |
|------|------|------|----------|
| C-P1.1 | 长任务进度推送 | 1 周 | 桌宠 demo + 进度准确性 + 单测 |
| C-P1.2 | HITL 决策面板 | 1.5 周 | 桌宠气泡分类 + 3 类触发 + 单测 |
| M-P1.1 | BRS 广度并行接线 | 2 周 | autonomy/ 跑 wave + 真实 BRS 跑通 + 单测 |
| — | 集成测试 + release notes | 0.5 周 | 手测 + release notes |

**验收标准**：
- 桌宠能实时显示主任务进度
- 3 类 HITL 触发条件全跑通
- `autonomy/` 跑 BRS 模式能产出多分支轨迹
- wave memory barrier 真实生效
- 不破坏现有 DRS 模式

**代码组织**：
```
新增文件：
  spirit/autonomy/wave_runner.py          # BRS 接线（200-300 行）
  spirit/desktop/progress_broadcaster.py  # 进度推送（100-150 行）
  spirit/desktop/hitl_panel.py            # HITL 决策面板（150-200 行）

修改文件：
  spirit/autonomy/explorer.py             # 集成 wave_runner
  spirit/conversation_loop.py             # 集成 progress_broadcaster
  spirit/autonomy/hitl.py                 # 扩展触发条件
  spirit/desktop/ws_server.py             # HITL 面板 WS 协议
  desktop-app/...                         # 桌宠 Vue 组件新增
```

---

## 4. v0.5.0：可量化 + 桌面伴侣（8 周）

**目标**：自进化有数字 + 桌宠有记忆。

| 编号 | 功能 | 工时 | 验证方式 |
|------|------|------|----------|
| M-P1.2 | 成效度量 harness | 2 周 | harness 跑通 + 30 任务 benchmark 跑出 Δ 数字 |
| M-P1.3 | 领域验证器扩充 | 2 周 | 4 个领域验证器 + 扩展文档 |
| M-P2.1 | 自进化日报 | 1 周 | 日报内容 + 桌宠推送 |
| M-P2.2 | 经验沉淀确认 | 1 周 | HITL 流程 + 桌宠气泡 + 记忆写入 |
| M-P2.3 | wave 状态可视化 | 1 周 | 桌宠 wave 详情面板 + 状态 API |
| C-P2.1 | 任务历史回看 | 0.5 周 | 桌宠历史面板 + 搜索 + 导出 |
| C-P2.2 | 任务结束摘要 | 0.5 周 | 长任务完成推送 |
| C-P2.3 | 桌宠状态可视化 | 0.5 周 | 5 种状态正确显示 |
| — | 集成测试 + release notes | 0.5 周 | 手测 + release notes |

**验收标准**：
- harness 能跑"前后 Δ 数字"
- 4 个领域验证器（Finance / Code / Doc / Data）都能跑
- 日报每天推送桌宠
- 经验沉淀前桌宠弹气泡确认
- 桌宠能查看任务历史 + 搜索 + 导出
- 5 种状态正确显示
- 不破坏 v0.4.0 既有功能

**代码组织**：
```
新增文件：
  spirit/evolution/metrics_harness.py     # 成效 harness（200-300 行）
  spirit/evolution/domains/code_domain.py # Code 验证器（100-150 行）
  spirit/evolution/domains/doc_domain.py  # Doc 验证器（100-150 行）
  spirit/evolution/domains/data_domain.py # Data 验证器（100-150 行）
  spirit/evolution/daily_report.py        # 日报（80-120 行）
  spirit/evolution/experience_review.py   # 经验沉淀确认（80-120 行）
  spirit/desktop/task_history.py          # 任务历史（150-200 行）
  spirit/desktop/status_state_machine.py  # 状态机（80-120 行）

修改文件：
  spirit/evolution/scheduler.py           # 集成日报
  spirit/evolution/loop.py                # 集成经验确认
  spirit/evolution/phase1_wave.py         # 暴露状态 API
  spirit/conversation_loop.py             # 集成结束摘要
  desktop-app/...                         # 桌宠 Vue 组件多
```

### 4.1 v0.5.0+ 长期 (v0.5.1+)：Hermes↔Spirit 架构深度补完（7 项，7 周）

> **为什么是长期**：这些模块对运行可靠性、可测试性、可观测性有显著提升，但价值密度低于 R-P3.x（不是 P0/P1 bug），适合作为 v0.5.0 后的增量补完。

| 编号 | 功能 | 工时 | 依据（Hermes 参考模块） | 验证方式 |
|------|------|------|--------------------------|----------|
| **R-P4.1** | MoA 体系升级 | 1 周 | `moa_loop.py`（78 KB）+ `moa_alternation.py` + `moa_trace.py` | 4 类 MoA 拓扑（layered / parallel / sequential / recursive） + trace 追踪 + 单测 |
| **R-P4.2** | 上下文引擎抽象 | 0.5 周 | `context_engine.py`（13 KB） | 统一 SPI，prompt_builder / compressor / cache 都能插拔 |
| **R-P4.3** | 子 agent lifecycle | 0.5 周 | `subagent_lifecycle.py`（18 KB） | spawn / cancel / timeout / leak 检测 4 场景 + 单测 |
| **R-P4.4** | Shell hooks first-use consent | 0.5 周 | `shell_hooks.py`（32 KB） | 首次运行 shell 命令跳桌宠 HITL 确认 + 黑名单加阐 + 单测 |
| **R-P4.5** | 后台 review 复用 | 1 周 | `background_review.py`（80 KB） | 跨 session 后台评评架（代码 diff / 决策质量 / 人机协议） + 单测 |
| **R-P4.6** | Relay 架构 | 2 周 | `relay_runtime.py` / `relay_llm.py` / `relay_tools.py` | 代理调用型路由（LLM 跳板 / Tool 远端） + 心跳 / 重连 / 单测 |
| **R-P4.7** | Provider adapter 矩阵 | 2 周 | `anthropic_*.py` / `bedrock_*.py` / `codex_*.py` 等 12+ 文件 | 6 个 provider adapter 独立维护，各自接底 SDK，兼底 OpenAI 格式转换 + 单测 |

**验收标准**：
- MoA：4 拓扑都跑通，trace JSON 能重现 MoA 决策路径
- 上下文引擎：拆出抽象后 prompt_builder / compressor 可独立替换
- 子 agent lifecycle：4 场景都能在 5s 内发现 leak / cancel / timeout
- Shell hooks：first-use 跳 HITL，用户点确认后才执行
- 后台 review：能在会话结束后异步评分 + 生成报告
- Relay：能跨进程路由 LLM / tool 调用，2 个进程同时跳同一 LLM 负载均衡
- Provider adapter：6 provider 各 30+ 测试，限流 / 错误重试 / streaming 适配全覆盖

**代码组织**：
```
新增文件（v0.5.0+ 长期 7 项）：
  spirit/moa/moa_topology.py              # R-P4.1（150-200 行）
  spirit/moa/moa_trace.py                 # R-P4.1（80-120 行）
  spirit/agent/context_engine.py          # R-P4.2（150-200 行）
  spirit/agent/subagent_lifecycle.py      # R-P4.3（150-200 行）
  spirit/agent/shell_hooks.py             # R-P4.4（200-300 行）
  spirit/evolution/background_review.py   # R-P4.5（200-300 行）
  spirit/relay/relay_runtime.py           # R-P4.6（200-300 行）
  spirit/relay/relay_llm.py               # R-P4.6（100-150 行）
  spirit/relay/relay_tools.py             # R-P4.6（100-150 行）
  spirit/providers/anthropic_adapter.py   # R-P4.7（200-300 行）
  spirit/providers/bedrock_adapter.py     # R-P4.7（200-300 行）
  spirit/providers/codex_adapter.py       # R-P4.7（200-300 行）
  spirit/providers/deepseek_adapter.py    # R-P4.7（150-200 行）
  spirit/providers/zhipu_adapter.py       # R-P4.7（150-200 行）
  spirit/providers/openai_compatible.py   # R-P4.7（150-200 行）

修改文件：
  spirit/moa/loop.py                       # R-P4.1 集成 moa_topology
  spirit/agent/conversation_loop.py        # R-P4.2 接 context_engine SPI
  spirit/autonomy/agent_pool.py            # R-P4.3 集成 lifecycle
  spirit/agent/tool_executor.py            # R-P4.4 集成 shell_hooks
  spirit/desktop/hitl_panel.py             # R-P4.4 加 first-use 提示
  spirit/evolution/scheduler.py            # R-P4.5 接入 background_review
  spirit/llm_pool.py                       # R-P4.6 走 relay
  spirit/providers/registry.py             # R-P4.7 重构为 adapter 矩阵
```

---

## 5. V3.0.0：完整收尾（11 周）

**目标**：P3 功能 + 全 V3 集成验证。

| 编号 | 功能 | 工时 | 验证方式 |
|------|------|------|----------|
| R-P3.1 | 消息清理 | 1.5 周 | 4 类清洗 + 单测 |
| R-P3.2 | 敏感信息脱敏 | 1.5 周 | 5 类脱敏 + 入库验证 |
| M-P3.1 | 成效 regression suite | 1.5 周 | CI 集成 + 报警 |
| M-P3.2 | strict-alternation 适配评估 | 1 周 | kg-2 / 5 / 9 复现 + 诊断 + 决策 |
| C-P3.1 | 多 session 监控 PoC | 2 周 | 多 session 列表 + 旁路查看 |
| C-P3.2 | 团队经验导入导出 | 2 周 | 导出 / 导入 / 签名验证 |
| C-P3.3 | 任务优先级调度 | 2 周 | 任务队列 + 优先级 + 暂停/取消 |
| — | V3 集成 + 全量回归 + release notes | 0.5 周 | V3 大版本发布 |

**验收标准**：
- 4 类消息清洗（non_ascii / surrogates / strip_images / close_interrupted）正确
- 5 类敏感信息脱敏（API key / 邮箱 / 手机号 / 身份证 / 信用卡）正确
- CI 集成 regression suite，Δ < 阈值才合入
- 多 session PoC 能跑（不含远程共享）
- 经验导入导出能跑通 + 签名验证工作
- 任务队列能跑 + 优先级排序正确
- V3 大版本发布说明完整

**代码组织**：
```
新增文件：
  spirit/agent/message_sanitization.py   # 消息清理（200-300 行）
  spirit/agent/redact.py                  # 敏感信息脱敏（150-200 行）
  spirit/evolution/regression_suite.py    # regression suite（150-200 行）
  spirit/desktop/multi_session.py         # 多 session 监控（200-300 行）
  spirit/desktop/experience_io.py         # 经验导入导出（150-200 行）
  spirit/desktop/task_queue.py            # 任务队列（200-300 行）

修改文件：
  spirit/agent/turn_context.py            # 集成 message_sanitization
  spirit/storage/session_db.py            # 集成 redact
  spirit/agent/conversation_loop.py       # 集成 4 处脱敏
  spirit/evolution/loop.py                # 集成 regression
  spirit/desktop/ws_server.py             # 集成多 session / 任务队列
```

---

## 6. 跨版本持续工作

这些工作横跨所有版本，不属于单个功能项：

### 6.1 文档维护

| 文档 | 维护频率 | 维护人 |
|------|----------|--------|
| `docs/15-product-roadmap-v3.md` | 每季度复盘 | — |
| `docs/16-feature-roadmap.md` | 每完成一个 P1 更新 | — |
| `docs/17-development-plan.md` | 每发版更新 | — |
| `docs/07-development-checklist.md` | 每周更新（沿用 V2 习惯） | — |
| `RELEASE_NOTES_vX.Y.Z.md` | 每个发版 | — |

### 6.2 评测基线

每次发版前**必跑** 3 个评测对比：

| 评测 | 用途 | 跑法 |
|------|------|------|
| **AgentBench**（自定义） | 暴露 db / kg 工具失败 + OS 420s | `agentbench_eval.py --limit 5` |
| **SWE-bench**（已有） | 主准确率 | `benchmarks/swe/` |
| **Evolution mini benchmark** | 自进化成效 | 30 任务 harness |

### 6.3 不在项目里加的验证脚本

按用户约束，**所有验证脚本放 `C:\Users\Administrator\AppData\Local\Temp\`**，不留在项目里：

```
C:\Users\Administrator\AppData\Local\Temp\
  ├── agentbench_eval.py                # 已存在，沿用
  ├── compare_ab.py                     # AgentBench 前后对比
  ├── swe_bench_compare.py              # SWE-bench 前后对比
  ├── evolution_metrics.py             # Evolution 成效 harness
  ├── regression_runner.py              # CI regression
  ├── compressor_long_task_stress.py    # R-P1.5 长任务失忆压力测试
  ├── prompt_composer_ab.py             # R-P2.4 动态 prompt 4 类任务 A/B 对比
  └── strict_alternation_probe.py       # M-P3.2 kg 类别 strict-alternation 400 复现 + 诊断
```

### 6.4 v0.2.12 hotfix 候选清单

回归测试与生产监控发现的**单点 bug**汇总到此表，按优先级进 v0.2.12 hotfix：

| 编号 | Bug | 位置 | 影响 | 优先级 |
|------|-----|------|------|--------|
| HF-001 | LLM 探活 think 块剥离正则锚点错（用 `</tool_response>` 而非 `<think></think>`）| `spirit/llm_pool.py:40` `_THINK_RE` 定义 | 批量闸门误选“只输出推理没结论”的 provider，可能浪费整批预算 | 高 |

**HF-001 详情**：

- **失败测试**：`tests/agent/test_llm_pool.py::TestProbe::test_think_block_stripped_before_check`
- **触发场景**：mock API 返回 `content = "<think>x</think>   "`（只有 think 块，无 tool 调用）
- **现状行为**：`_THINK_RE = re.compile(r".*?</tool_response>", re.S)` 匹配不到纯 think 块，整段保留 → `strip()` 后非空 → probe 返回 `True`
- **期望行为**：剥离 think 后空 → probe 返回 `False`
- **根因**：think 块结束标签是 `<think></think>` / `[/THINK]`，不是 `</tool_response>`（那是 tool 调用响应的结束）
- **修复方向**：

  ```python
  # spirit/llm_pool.py:40 改为：
  _THINK_RE = re.compile(r".*?</think>", re.S | re.I)
  ```

  + probe 内同时读 `reasoning_content` 字段（line 154 注释提到但代码没读）
- **对照正确实现**：`spirit/cli/main_enhanced.py:48` `re.sub(r'.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE)` ✓

---

## 7. 风险与回退

| 风险 | 概率 | 影响 | 缓解 | 回退方案 |
|------|------|------|------|----------|
| R-P1.1 错误恢复引入新 bug | 🟡 中 | 🟡 重要 | 单测 + AgentBench 回归 | 关闭 turn_recovery 走原逻辑 |
| M-P1.1 BRS 烧 token 太多 | 🟡 中 | 🟡 重要 | Curriculum 早停 + HITL 预算告警 | 退回到 DRS 单目标 |
| C-P1.1 进度推送打扰用户 | 🟡 中 | 🟢 一般 | 默认折叠 + 重要节点才弹 | 关闭进度推送 |
| M-P1.3 领域验证器被欺骗 | 🟡 中 | 🔴 致命 | 交叉验证 + 二次回放 | 关闭领域，回退 LLM 自评 |
| 范围膨胀 | 🔴 高 | 🟡 重要 | 严格 P1/P2/P3 分级 | 每季度砍非核心 |

---

## 8. 与 V2 阶段的衔接

V3 不重做 V2 已完成的事，**只补缺口 + 加新能力**：

| V2 已完成 | V3 不重做 | V3 加什么 |
|-----------|---------|-----------|
| Phase 1 基础组件（IterationBudget / TurnContext / ErrorHandler） | ✅ 沿用 | — |
| Phase 2 主循环重构 | ✅ 沿用 | 加 turn_recovery / turn_truncation |
| Phase 3 集成测试 + 高级功能 | ✅ 沿用 | 加 tool_result_classification |
| Phase 4 高级功能（进程注册表 / MoA / 技能中心 / Computer Use / TUI） | ✅ 沿用 | — |
| Phase 5 大量测试 | ✅ 沿用 | 加 ≥500 项 V3 测试 |
| Phase 6 自进化 RSI（14 文件） | ✅ 沿用 | 加 BRS 接线 + 成效 harness |
| Phase 7 Autonomy 常驻（4 文件） | ✅ 沿用 | 加 HITL 面板 + 任务监控 |

---

## 9. 发版节奏

| 版本 | 计划时间 | 主要内容 | 验证 |
|------|---------|---------|------|
| **v0.2.3** | T+6 周 | 错误恢复 + 工具分类 + 并发超时 + provider 切换 + 压缩 v2 + 准确率归因 | AgentBench / SWE-bench 回归（准确率 62%→72%） |
| **v0.3.0** | T+9 周 | 截断多策略 + 活性检测 + provider v2 + 动态 prompt 组成 | AgentBench 完整 + SWE-bench 小样本 |
| **v0.3.1** | T+10 周 | R-P3.1 推理控制分离 | 3 推理 provider effort/timeout 单测 |
| **v0.3.2** | T+11 周 | R-P3.2 Provider 体系升级 | 6 provider 热插拔回归 |
| **v0.3.3** | T+12 周 | R-P3.3 消息清洗 | 4 类清洗 + metadata 投射单测 |
| **v0.3.4** | T+12.5 周 | R-P3.4 会话持久化 + R-P3.5 思考块 scrubber | kill -9 重启恢复 + 3 provider think 块单测 |
| **v0.3.5** | T+13 周 | R-P3.6 turn facade 拆分 (第一批 5) + R-P3.7 Micro compaction | conversation_loop 400 行以下 + 100% 单测覆盖 |
| **v0.3.6** | T+13.5 周 | D-P1.1 KawaiiSpinner + D-P1.4 文件 diff 内联（§14 P0）| 思考动画 + write_file 后 unified diff 内联 |
| **v0.3.7** | T+14.5 周 | D-P1.2 友好动词表 + D-P1.3 _PRIMARY_ARGS 智能 preview | "Reading config.py" 而非 "read_file config.py" |
| **v0.3.8** | T+16 周 | D-P1.5 shell 智能摘要 + D-P1.6 think 状态机 + D-P1.7 StatusOutputMixin | cd && npm install 显示 npm install + 跨 chunk think 不漏 |
| **v0.3.9** | T+17 周 | D-P2.1 bridge tool 多行 + D-P2.2 StreamDeliveryMixin + D-P2.3 tool 边界行 | tool_call 多子工具展开 + CLI/桌宠 同序 |
| **v0.3.10** | T+18 周 | D-P3.1 skin engine 主题 | 用户可切深色/高对比度 |
| **v0.3.11** | T+19.5 周 | D-P1.8 Compaction 进度 + D-P1.9 MoA 进度 + D-P1.10 Plan display（§14.8 P1 #3-5） | 用户看到上下文压缩、多模型参考聚合、LLM 计划 |
| **v0.3.12** | T+21.5 周 | D-P2.4 Stream diag + D-P2.5 Turn failure copy + D-P2.6 Display 模块拆分 + D-P2.7 完整 Redact（§14.8 P2 #9-12） | cli/main.py 减到 800 行以下 + Redact 覆盖路径/邮箱 |
| **v0.4.0** | T+24.5 周 | 进度推送 + HITL 面板 + BRS 接线 | 桌宠 demo + evolution 真实跳 |
| **v0.4.1** | T+25 周 | D-P3.4 TUI Gateway MVP（§14.8 P3 #14）| 多行输入 + scrollback + Ctrl+C/L/R |
| **v0.4.2** | T+26 周 | D-P3.4 status bar + D-P4.3 Sidebar 完整字段（§14.8 P3 #14 + P4 #22）| TUI 顶部状态条 + 7 字段完整显示 |
| **v0.4.3** | T+27 周 | D-P3.4 stream panel + event router | 流式输出 panel + 接 progress_notifier |
| **v0.4.4** | T+28 周 | D-P3.4 TUI 1.0 GA + D-P3.8 i18n 集成 | 多语言 i18n + 键位可配 + 皮肤集成 |
| **v0.4.5** | T+30 周 | D-P3.5 Web 前端 + D-P3.6 Gateway 4 层回调管道（§14.8 P3 #15-16） | 浏览器聊天 UI + Gateway 状态桥接多平台 |
| **v0.4.6** | T+32 周 | D-P3.7 Double-channel 广播（§14.8 P3 #17）| CLI + Gateway 物理隔离双通道 |
| **v0.5.0** | T+37 周 | 成效 harness + 领域验证器 + 日报 + 经验确认 + wave 可视化 + 任务历史 + 摘要 + 状态可视化 | 完整 V3 验证 |
| **v0.5.1** | T+39 周 | R-P4.1 MoA + R-P4.2 上下文引擎 | 4 MoA 拓扑 + SPI 替换 |
| **v0.5.2** | T+41 周 | R-P4.3 lifecycle + R-P4.4 shell hooks | 4 场景 lifecycle + first-use HITL |
| **v0.5.3** | T+43 周 | R-P4.5 后台 review | 跨 session 评估报告 |
| **v0.5.4** | T+45 周 | R-P4.6 Relay 架构 | 跨进程 LLM / tool 路由 |
| **v0.5.5** | T+47 周 | R-P4.7 Provider adapter 矩阵 | 6 provider 各 30+ 测试 |
| **v0.5.6** | T+47 周 | D-P4.1 Image routing + D-P4.2 WebSocket chunk_delay + D-P4.4 Prompt plan preview（§14.8 P4 #19-23） | 图片路由 + 流式 throttle + Plan preview |
| **V3.0.0** | T+57 周 | 消息清理 + 脱敏 + regression + strict-alternation 评估 + 多 session + 经验 I/O + 任务调度 | V3 大版本发布 |

> T = 2026-10-09（V3 规划启动）

---

## 10. 复盘节点

| 节点 | 复盘内容 | 决策 |
|------|----------|------|
| **T+6 周（v0.2.3 发版前）** | 错误恢复是否有效？AgentBench 回归是否达标？ | 是 → 继续 v0.3.0；否 → 加 1 周调试 |
| **T+13 周（v0.3.5 发版前）** | turn facade 拆分是否到 400 行以下？Micro compaction 是否 < 100ms？ | 是 → 继续 v0.3.6 显示层补完；否 → 砍 P3 只做 P0 |
| **T+17 周（v0.3.8 发版前）** | KawaiiSpinner + 文件 diff + 友好动词 是否让 CLI “看起来灵动”？ | 是 → 继续 v0.3.9 P2；否 → 砍 P2 只做 P3 |
| **T+18 周（v0.3.10 发版前）** | skin engine 是否能跑？3 个主题是否可用？ | 是 → 继续 v0.3.11 P1 显示层补完；否 → 推迟 skin |
| **T+19.5 周（v0.3.11 发版前）** | Compaction 进度 + MoA 进度 + Plan display 是否让用户看到“后台进程”？ | 是 → 继续 v0.3.12 P2；否 → 砍 P1 后续 |
| **T+21.5 周（v0.3.12 发版前）** | cli/main.py 是否减到 800 行以下？Redact 是否覆盖路径/邮箱？Turn failure copy 是否友好？ | 是 → 继续 v0.4.0；否 → 加 1 周调试 |
| **T+24.5 周（v0.4.0 发版前）** | 桌宠进度推送是否打扰？BRS 是否真接线？ | 是 → 继续 v0.4.1 TUI；否 → 砍非核心 |
| **T+28 周（v0.4.4 发版前）** | TUI Gateway 1.0 GA 是否能跟 cli 并存？i18n 是否能独立切换？ | 是 → 继续 v0.4.5 Web；否 → 砍 TUI 收 |
| **T+32 周（v0.4.6 发版前）** | Double-channel 广播是否能让 CLI / Gateway 互不打断？ | 是 → 继续 v0.5.0；否 → 砍 Double-channel |
| **T+37 周（v0.5.0 发版前）** | 成效 harness 是否出数字？日报是否有用？ | 是 → 继续 v0.5.1-6；否 → 砍日报 / 砍 harness |
| **T+47 周（v0.5.6 发版前）** | Image routing + chunk_delay + Sidebar 7 字段 + Plan preview 是否让 P4 锦上添花？ | 是 → 继续 V3.0.0；否 → 砍 P4 只留 V3 必做 |
| **T+57 周（V3.0.0 发版前）** | P3 功能是否都值得做？ | 全部 → 发版；部分 → 砍 |

---

## 11. 参考

- **V3 产品愿景**：[15-product-roadmap-v3.md](./15-product-roadmap-v3.md)
- **V3 功能模块**：[16-feature-roadmap.md](./16-feature-roadmap.md)
- **现有发版记录**：`RELEASE_NOTES_v0.0.2.md` → `RELEASE_NOTES_v0.2.2.md`
- **现有功能清单**：`docs/07-development-checklist.md`
- **自进化定位**：`docs/14-rsiagent-architecture-and-spirit-gap.md`
- **架构差距**：上一轮 Spirit vs Hermes 源码对比（19 文件 vs 100+ 文件）
- **评测证据**：SWE-bench 双 Agent 评测 / AgentBench 自定义评测

---

## 12. 开发状态总表（V2 + V3）

> **当前真实进度**（规划视角见 §0-§11）：v0.2.4 已发布、v0.4.0 进行中（C-P1.2）
> **最后更新**：2026-10-10（v0.3.0 R-P2.2 已交付 ✅ + Hermes↔Spirit 架构差距规划补完 v0.3.1-v0.5.5 14 项 R-P3.x/R-P4.x）
> **配套清单**：[07-development-checklist.md §6](./07-development-checklist.md)（V2 完整模块级 + 同样的总表）

### 12.1 V2 阶段（Phase 1-7）完成状态

| 阶段 | 目标 | 状态 | 子项 | 行数 | 测试 | 备注 |
|------|------|------|------|------|------|------|
| Phase 1 | 核心闭环 | ✅ | 7/7 | ~5,400 | 111+ | 记忆 / 初始化 / Transport / 任务 / 钩子 / 凭据 / 配置 |
| Phase 2 | 多端接入 | ✅ | 6/6 | ~1,500 | - | API / 网关 / 7 平台 / Web / 会话 / VSCode |
| Phase 3 | 桌宠与语音 | ✅ | 6/6 | ~3,200 | 998+ | 桌宠 / 语音 / 打包 / 审批门禁 |
| Phase 4 | 高级功能 | ✅ | 7/7 | ~13,904 | 1,272 | MoA / Profile / Goals / TUI / Computer Use / 技能中心 / 进程注册表 |
| Phase 5 | 生态完善 | ✅ | 5/5 | ~5,057 | 418 | 代理 / 检查点 / 集成 / 成就 / Agent 辅助 |
| Phase 6 | 自进化 RSI | ✅ | 6/6 (A-F) | ~5,200 | 215 | protocol / verifier / curriculum / BRS / HITL / 领域化 |
| Phase 7 | 常驻自主探索 | ✅ | 4/4 (A-D) | ~1,500 | 42 | 沙箱 / 常驻循环 / 桌宠桥接 / 部署集成 |
| **合计** | **核心闭环 100%** | **✅ 7/7** | **41/41** | **~35,761** | **2,698+** | **V2 全部 Phase 完成** |

### 12.2 仍待补完（V2 阶段中未做项）

| 编号 | 模块 | 优先级 | 状态 | 说明 |
|------|------|--------|------|------|
| 2.10 | `spirit/dashboard/`（Web Dashboard 后端）| 🟡 中 | ❌ | 4 文件未建（web_server / auth_provider / session_export / skin_engine）；前端 `web/` 基础版 ✅ |
| 2.11 | `spirit/security/`（独立安全扫描）| 🟡 中 | ❌ | 6 文件未建（tirith_scanner / threat_patterns / skills_guard / url_safety / supply_chain_audit / security_advisories）；命令审批 + 工具护栏 ✅ |
| - | `acp_adapter/` | 🟢 低 | ❌ | 0%（未启动，Hermes 对应模块未移植）|
| 1.1 | `agent/` 5 个低优先级辅助模块 | 🟢 低 | ❌ | `model_metadata` / `title_generator` / `learning_graph` / `shell_hooks` / `system_prompt` 未建；`redact` ✅ / `i18n` ✅ / `insights` ✅ |

### 12.3 V3 路线图进度（按发版节点）

#### v0.2.3（✅ 已发布 2026-10-09）—— 桌宠空气墙根治 + V2.1 MoA/Tool Search + V3 路线图

| 类别 | 交付 | 状态 |
|------|------|------|
| 桌宠 BUG | 空气墙根治（精细命中 + 5s 防御 timer + 托盘兜底）| ✅ |
| 工具系统 | `tool_search`（3 个 bridge 工具，Hermes Progressive Tool Disclosure 同款）+ `agency_agents`（282 specialists lazy router）| ✅ |
| 规划文档 | `docs/14-rsiagent-architecture-and-spirit-gap.md` / `15-product-roadmap-v3.md` / `16-feature-roadmap.md` / `17-development-plan.md` 4 份 | ✅ |

#### v0.2.4（✅ 已发布 2026-10-09）—— V3 P1 节点首收官（R/M 支柱）

| 编号 | 功能 | 新增文件 | 行数 | 验证 | 状态 |
|------|------|---------|------|------|------|
| R-P1.1 | 错误恢复链（5 类处理器）| `spirit/agent/turn_recovery.py` | 613 | 24/24 | ✅ |
| R-P1.2 | 工具结果分类（5 类）| `spirit/agent/tool_result_classification.py` | 411 | 27/27 | ✅ |
| R-P1.3 | 并发工具超时修复（3 层超时 + liveness）| `spirit/agent/tool_liveness.py` | 360 | 63/63 | ✅ |
| R-P1.4 | provider 切换保护 | `spirit/agent/provider_switch_protection.py` | 313 | 72/72 | ✅ |
| R-P1.5 | 上下文压缩鲁棒性 v2 | `spirit/agent/context_compressor_v2.py` | 432 | 68/68 | ✅ |
| M-P1.0 | 准确率归因 + 提升 | `spirit/agent/accuracy_attribution.py` | 442 | 82/82 | ✅ |
| **合计** | **6 项 + 4 处 conversation_loop 集成** | **6 新增 + 2 修改** | **~2,571 净增** | **336/336** | |

#### v0.3.0（❌ 计划）—— 可靠性完善 + Prompt 工程

| 编号 | 功能 | 估算 | 状态 | 备注 |
|------|------|------|------|------|
| R-P2.1 | 截断多策略 | 1.5 周 | ✅ | `spirit/agent/turn_truncation.py`（347 行，6 策略 + 8/8 验证）|
| R-P2.2 | 活性检测 | 1 周 | ✅ | `spirit/agent/turn_liveness.py`（323 行，全场景 liveness）|
| R-P2.3 | provider 切换保护 v2 | 0.5 周 | ❌ | - |
| R-P2.4 | 动态 prompt 组成 | 1 周 | ❌ | `spirit/agent/prompt_composer.py`（200-300 行，重构 `prompt_builder`）|

#### v0.3.0 补完（v0.3.1-v0.3.5）—— Hermes↔Spirit 架构差距（§13）

| 编号 | 功能 | 估算 | 状态 | 依据（Hermes 参考） |
|------|------|------|------|----------------------|
| R-P3.1 | 推理控制分离 | 1 周 | ❌ | `reasoning_effort.py` / `reasoning_params.py` / `reasoning_timeouts.py` |
| R-P3.2 | Provider 体系升级 | 1 周 | ❌ | `provider_registry.py` / `provider_base.py` / `provider_projection.py` |
| R-P3.3 | 消息清洗 | 1 周 | ❌ | `message_sanitization.py` (36 KB) |
| R-P3.4 | 会话持久化 mixin | 0.5 周 | ❌ | `session_persistence.py` (25 KB) |
| R-P3.5 | 思考块 scrubber | 0.5 周 | ❌ | `think_scrubber.py` (9 KB) + `repetition_guard.py` (4 KB) |
| R-P3.6 | turn facade 拆分 | 2 周 | ❌ | 27 个 `turn_*.py` (130+ KB) |
| R-P3.7 | Micro compaction | 0.5 周 | ❌ | `micro_compaction.py` (25 KB) + `native_compaction.py` (18 KB) |

#### v0.3.0 补完（v0.3.6-v0.3.10）—— Spirit 前端显示层补完（§14 P0-P3）

| 编号 | 功能 | 估算 | 状态 | 依据（Hermes 参考） |
|------|------|------|------|----------------------|
| D-P1.1 | KawaiiSpinner 思考动画 | 0.5 周 | ❌ | `display.py` 812-947 KawaiiSpinner + kawaii 颜文字 + 思考动词 |
| D-P1.2 | 友好 tool 动词表 _TOOL_VERBS | 0.5 周 | ❌ | `display.py` 503-518 _TOOL_VERBS 23 工具友好动词 |
| D-P1.3 | _PRIMARY_ARGS + _PREVIEW_BUILDERS 智能 preview | 1 周 | ❌ | `display.py` 353-461 23 工具专属 preview builder |
| D-P1.4 | LocalEditSnapshot 文件 diff 内联 | 0.5 周 | ❌ | `display.py` 106-807 LocalEditSnapshot + render_edit_diff_with_delta |
| D-P1.5 | summarize_shell_command shell 智能摘要 | 0.5 周 | ❌ | `display.py` 186-296 _SHELL_SILENT_HEADS / _SHELL_PIPE_TAIL_HEADS |
| D-P1.6 | StreamingThinkScrubber 状态机 | 0.5 周 | ❌ | `think_scrubber.py` 27 状态机 reset/feed/flush |
| D-P1.7 | StatusOutputMixin 状态/警告安全输出 | 0.5 周 | ❌ | `status_output.py` 16 StatusOutputMixin |
| D-P2.1 | bridge tool 多行展开 | 0.5 周 | ❌ | `display.py` 1150 _cute_bridge_rows |
| D-P2.2 | StreamDeliveryMixin 单写者流控 | 0.5 周 | ❌ | `stream_delivery.py` 19 _call_quietly / _stream_writer_superseded |
| D-P2.3 | tool 完成后 _TOOL_DONE_BORDER 边界行 | 0.3 周 | ❌ | `display.py` 末端 done 边界行 |
| D-P3.1 | skin engine 主题 | 1 周 | ❌ | `hermes_cli/skin_engine.py` 主题引擎 |

#### v0.4.1（❌ 计划）—— Spirit 前端显示层 P3 收官

| 编号 | 功能 | 估算 | 状态 | 依据（Hermes 参考） |
|------|------|------|------|----------------------|
| D-P3.2 | Slack live status 短语 | 0.5 周 | ❌ | `display.py` 567 build_status_phrase 49 字符截断 |
| D-P3.3 | plugin tool labels 多源化 | 1 周 | ❌ | `display.py` 529 tool_labels_for_call + `tools/tool_labels.py` 插件标签 |

#### v0.3.11（❌ 计划）—— Spirit 前端 UX P1 补完（§14.8 P1 #3-5）

| 编号 | 功能 | 估算 | 状态 | 依据（Hermes 参考） |
|------|------|------|------|----------------------|
| D-P1.8 | Compaction 进度展示 before/after | 0.5 周 | ❌ | `agent/compaction_display.py` (3 KB) context_compressor_v2 调用点 emit progress |
| D-P1.9 | MoA 多模型参考 + 聚合进度 | 0.5 周 | ❌ | `agent/moa_loop.py` (78 KB) 事件 `moa.reference` / `moa.aggregating` |
| D-P1.10 | Plan display todo 工具输出 | 0.5 周 | ❌ | `agent/plan_prompt.py` todo_list emit PLAN_DISPLAY phase |

#### v0.3.12（❌ 计划）—— Spirit 前端 UX P2 架构层补完（§14.8 P2 #9-12）

| 编号 | 功能 | 估算 | 状态 | 依据（Hermes 参考） |
|------|------|------|------|----------------------|
| D-P2.4 | Stream diag 流式诊断 | 0.5 周 | ❌ | `agent/stream_diag.py` (2 KB) TTFB + chunk_intervals debug 通道 |
| D-P2.5 | Turn failure copy 友好错误文案 | 0.5 周 | ❌ | `agent/turn_failure_copy.py` (3 KB) 6 类错误友好化 |
| D-P2.6 | Display 模块拆分（cli/main.py 1487 → 800 行）| 1 周 | ❌ | `agent/display.py` + `status_output.py` + `notification_presentation.py` 三模块拆分 |
| D-P2.7 | 完整 Redact 模块（信用卡/邮箱/path/env var/token）| 0.5 周 | ❌ | `agent/redact.py` (4 KB) 6 类脱敏 |

#### v0.4.1-v0.4.4（❌ 计划）—— Spirit 前端 UX P3 TUI Gateway 主体（§14.8 P3 #14）

| 编号 | 功能 | 估算 | 状态 | 依据（Hermes 参考） |
|------|------|------|------|----------------------|
| D-P3.4.1 | TUI Gateway MVP（多行输入 + scrollback）| 1.5 周 | ❌ | `tui_gateway/prompt_turn.py` 多行输入 + 历史 + 自动补全 |
| D-P3.4.2 | status bar + keybindings | 0.5 周 | ❌ | `tui_gateway/status_bar.py` 7 字段实时刷新 |
| D-P3.4.3 | stream panel + event router | 1 周 | ❌ | `tui_gateway/stream_panel.py` + `event_router.py` 接 progress_notifier |
| D-P3.4.4 | TUI Gateway 1.0 GA + i18n 集成 | 1 周 | ❌ | `tui_gateway/` 6 文件 + D-P3.8 集成 |

#### v0.4.5-v0.4.6（❌ 计划）—— Spirit 前端 UX P3 平台层扩展（§14.8 P3 #15-18）

| 编号 | 功能 | 估算 | 状态 | 依据（Hermes 参考） |
|------|------|------|------|----------------------|
| D-P3.5 | Web 前端（浏览器聊天 UI）| 3 周 | ❌ | `web/` 5 文件 + Vue 3 / Svelte 5 + WebSocket |
| D-P3.6 | Gateway 4 层回调管道 | 2 周 | ❌ | `gateway/run.py` (123 文件) 4 层架构：emit → callback → dedupe → sender |
| D-P3.7 | Double-channel 广播 CLI + Gateway | 1 周 | ❌ | `agent/AGENTS.md` `_emit_status` 双通道 |
| D-P3.8 | i18n 多语言（zh-CN / en-US / ja-JP）| 1 周 | ❌ | `agent/i18n.py` + `locales/` 3 文件 |

#### v0.5.6（❌ 计划）—— Spirit 前端 UX P4 细节打磨（§14.8 P4 #19-23）

| 编号 | 功能 | 估算 | 状态 | 依据（Hermes 参考） |
|------|------|------|------|----------------------|
| D-P4.1 | Image routing / display 多模态图片 | 1 周 | ❌ | `agent/image_routing.py` + `image_eviction_policy.py` cache + evict 30MB |
| D-P4.2 | WebSocket 流式 chunk_delay throttle | 0.5 周 | ❌ | `gateway/ws_stream.py` 50ms throttle 防打爆桌宠 |
| D-P4.3 | Sidebar status bar 完整字段 | 0.5 周 | ❌ | `agent/status_output.py` 补全 model / ctx% / Σ usage / duration / msgs / tools / token 7 字段 |
| D-P4.4 | Prompt plan preview LLM 决策路径 | 0.5 周 | ❌ | `agent/plan_prompt.py` 检测 `<plan>...</plan>` 块 emit PLAN_DISPLAY |

#### v0.5.0+ 长期（v0.5.1-v0.5.5）—— Hermes↔Spirit 深度补完（§13）

| 编号 | 功能 | 估算 | 状态 | 依据（Hermes 参考） |
|------|------|------|------|----------------------|
| R-P4.1 | MoA 体系升级 | 1 周 | ❌ | `moa_loop.py` (78 KB) + `moa_alternation.py` + `moa_trace.py` |
| R-P4.2 | 上下文引擎抽象 | 0.5 周 | ❌ | `context_engine.py` (13 KB) |
| R-P4.3 | 子 agent lifecycle | 0.5 周 | ❌ | `subagent_lifecycle.py` (18 KB) |
| R-P4.4 | Shell hooks first-use | 0.5 周 | ❌ | `shell_hooks.py` (32 KB) |
| R-P4.5 | 后台 review 复用 | 1 周 | ❌ | `background_review.py` (80 KB) |
| R-P4.6 | Relay 架构 | 2 周 | ❌ | `relay_runtime.py` + `relay_llm.py` + `relay_tools.py` |
| R-P4.7 | Provider adapter 矩阵 | 2 周 | ❌ | `anthropic_*` / `bedrock_*` / `codex_*` 等 12+ 文件 |

#### v0.4.0（⏳ 进行中）—— 桌宠 + BRS 接线（当前所在版本）

| 编号 | 功能 | 估算 | 状态 | 备注 |
|------|------|------|------|------|
| C-P1.1 | 长任务进度推送 | 1 周 | ✅ | `progress_notifier.py`（7 phase 全局 sink）+ `ws_server` 桥接 + `conversation_loop` 10 处埋点 + App.vue 气泡；E2E 28 progress 事件全验证 |
| C-P1.2 | HITL 决策面板 | 1.5 周 | ⏳ 进行中 | 核心骨架已建：`agent/hitl_channel.py` 全局通道 + `DecisionKind` 4 类 + `HumanQuery.kind` + `bridge` payload 透传 + `start_autonomy` 注册全局通道 + `tool_guardrails` HITL 信号（`hitl_decision` / `force_halt`）。下一步：`conversation_loop` 工具失败触发 + 前端按 kind 分类 + 第 2/3 类触发（经验沉淀确认 / provider 切换）|
| M-P1.1 | BRS 广度并行接线 | 2 周 | ❌ | `spirit/autonomy/wave_runner.py`（待开始）|

#### v0.5.0（❌ 计划）—— 可量化 + 桌面伴侣

| 编号 | 功能 | 估算 | 状态 | 备注 |
|------|------|------|------|------|
| M-P1.2 | 成效度量 harness | 2 周 | ❌ | `evolution/metrics_harness.py` |
| M-P1.3 | 领域验证器扩充 | 2 周 | ❌ | `evolution/domains/{code,doc,data}_domain.py`（+ finance）4 个 |
| M-P2.1 | 自进化日报 | 1 周 | ❌ | `evolution/daily_report.py` |
| M-P2.2 | 经验沉淀确认 | 1 周 | ❌ | HITL 流程 + 桌宠气泡 + 记忆写入 |
| M-P2.3 | wave 状态可视化 | 1 周 | ❌ | 桌宠 wave 详情面板 + 状态 API |
| C-P2.1 | 任务历史回看 | 1.5 周 | ❌ | 桌宠历史面板 + 搜索 + 导出 |
| C-P2.2 | 任务结束摘要推送 | 1 周 | ❌ | 长任务完成推送 |
| C-P2.3 | 桌宠状态可视化 | 0.5 周 | ❌ | 5 状态机（空闲/思考/工具/等待 HITL/自主探索）|

#### V3.0.0（❌ 计划）—— 完整收尾

| 编号 | 功能 | 估算 | 状态 | 备注 |
|------|------|------|------|------|
| R-P3.1 | 消息清理 | 1.5 周 | ❌ | `agent/message_sanitization.py`（4 类清洗）|
| R-P3.2 | 敏感信息脱敏 | 1.5 周 | ❌ | `agent/redact.py`（5 类脱敏）|
| M-P3.1 | 成效 regression suite | 1.5 周 | ❌ | CI 集成 + 报警 |
| M-P3.2 | strict-alternation 适配评估 | 1 周 | ❌ | 评估导向（阶段 1+2 必做，阶段 3 视结果）|
| C-P3.1 | 多 session 监控 PoC | 2 周 | ❌ | 多 session 列表 + 旁路查看 |
| C-P3.2 | 团队经验导入导出 | 2 周 | ❌ | 导出 / 导入 / 签名验证 |
| C-P3.3 | 任务优先级调度 | 2 周 | ❌ | 任务队列 + 优先级 / 暂停 / 取消 |

### 12.4 V3 进度统计

| 类别 | 总项 | ✅ | ⏳ | ❌ |
|------|------|----|----|----|
| V3 P1（v0.2.4-v0.4.0）| 8 | 6 | 1（C-P1.2）| 1（M-P1.1）|
| V3 P2（v0.3.0-v0.5.0）| 9 + 7 = 16 | 0 | 0 | 16 |
| V3 P3（V3.0.0）| 7 + 7 = 14 | 0 | 0 | 14 |
| **前端显示层 P0-P1**（v0.3.6-v0.3.8）| 7 | 0 | 0 | 7 |
| **前端显示层 P2**（v0.3.9）| 3 | 0 | 0 | 3 |
| **前端显示层 P3**（v0.3.10 + v0.4.1）| 3 | 0 | 0 | 3 |
| **前端 UX P1-P4 §14.8**（v0.3.11 - v0.5.6）| 16 | 0 | 0 | 16 |
| **合计** | **74** | **6** | **1** | **67** |

### 12.5 整体统计（V2 + V3）

| 维度 | 总项 | ✅ | ⏳ | ❌ |
|------|------|----|----|----|
| V2 Phase 1-7 子项 | 41 | 41 | 0 | 0 |
| V2 仍待补完（低优先级）| 4 大类 | 0 | 0 | 4 |
| V3 P1（v0.2.4-v0.4.0）| 8 | 6 | 1（C-P1.2）| 1（M-P1.1）|
| V3 P2（v0.3.0 + v0.3.0 补完 + v0.5.0）| 9 + 7 = 16 | 0 | 0 | 16 |
| V3 P3（V3.0.0 + v0.5.0+ 长期）| 7 + 7 = 14 | 0 | 0 | 14 |
| **前端显示层**（§14 P0-P3，v0.3.6-v0.4.1）| 13 | 0 | 0 | 13 |
| **前端 UX P1-P4**（§14.8，v0.3.11 - v0.5.6）| 16 | 0 | 0 | 16 |
| **合计** | **119** | **47** | **1** | **71** |

**关键里程碑**：
- V2 七大阶段全部完成 → 41/41 子项 + 2,698+ 测试全通 + 35,761 行后端代码
- v0.2.4 R/M 支柱 P1 全收官 → C 支柱进度仅 C-P1.1 完成
- 当前 v0.4.0：C-P1.2 进行中（核心骨架 ✅，子任务待做） / M-P1.1 待开始
- v0.3.0 / v0.5.0 / V3.0.0 全部 P2/P3 项均未开始
- **§14.8 P1-P4 完整缺口**：v0.3.11（2 周）→ v0.3.12（3 周）→ v0.4.1-v0.4.6（11 周）→ v0.5.6（3 周），总计 19 周将 CLI / TUI / Web 三端 UX 全面对齐 Hermes
- V2 阶段（Phase 1-7）全部完成，详见 [07-development-checklist.md §一/§二/§三](./07-development-checklist.md)

### 12.6 v0.2.12 hotfix 候选（紧急）

| 编号 | Bug | 测试 | 优先级 | 备注 |
|------|-----|------|--------|------|
| HF-001 | LLM 探活 think 块剥离正则锚点错（用 `</tool_response>` 而非 `<think></think>`）| `test_llm_pool.py::TestProbe::test_think_block_stripped_before_check`（当前 failed）| 高 | 批量场景可能误选“只输出推理没结论”的 provider，浪费整批预算 |

**修复方案**：

```python
# spirit/llm_pool.py:40 改为：
_THINK_RE = re.compile(r".*?</think>", re.S | re.I)
```

+ probe 内同时读 `reasoning_content` 字段（line 154 注释提到但代码没读）

**对照正确实现**：`spirit/cli/main_enhanced.py:48` 已用 `.*?</think>` ✓

---

## 13. Hermes↔Spirit 架构差距分析（v0.3.0 补完规划依据）

> **完整对比文件**：[`Hermes Spirit 架构模块对比.md`](../../Hermes%20Spirit%20架构模块对比.md)（18 节，864 行）
>
> **本节用途**：上一轮 Spirit vs Hermes 源码对比只覆盖了 19 vs 100+ 文件，仅 19 项差距。本节重新扫描 `hermes-agent-main/agent/` （244 文件） vs `spirit-agent-main/spirit/agent/` （28 文件），发现 50+ 个**架构级**模块 Spirit 缺失。按重要性 × 依赖关系 拆分为 14 项 R-P3.x / R-P4.x 补完。

### 13.1 总览

| 维度 | Hermes | Spirit | 缺口 |
|------|--------|--------|------|
| `agent/` 顶层文件 | 244 | 28 | 227 (93%) |
| 核心对话循环 (`turn_*`) | 29 | 2 | 27 |
| Prompt 组成 (`prompt_*`) | 4 | 0 (Spirit 叫 prompt_builder) | 4 |
| 推理控制 (`reasoning_*`) | 4 | 0 | 4 |
| Provider 体系 | 19 | 0 | 19 |
| MoA 推理 | 3 | (Spirit 已有 spirit/moa/ 旧版) | 3 |
| 消息处理 (`message_*`) | 3 | 0 | 3 |
| 压缩 (`compaction_*`) | 3 | 1 (v1) | 2 |
| 流式 (`stream_*`) | 3 | 1 | 2 |
| 思考块 / Repetition guard | 2 | 0 | 2 |
| 子 agent 体系 | 2 | 部分 | 2 |
| 会话管理 (`session_*`) | 2 | 0 | 2 |
| 钩子系统 (`shell_hooks`) | 1 | 0 | 1 |
| 后台评估 (`background_review`) | 1 | 0 | 1 |
| 上下文引擎 | 1 | 0 | 1 |

**调用方式 link pattern**：
- Hermes 风格：`module-level pure function(agent)` + mixin （`turn_facade.TurnFacadeMixin`）
- Spirit 风格：`agent instance method` （`agent.get_system_prompt()`）
- **重构方向**：Spirit 的大方法拆为 module-level pure function，接收 `agent` 作 first arg

### 13.2 P0 缺口（v0.3.0 必须补完）

#### R-P3.1 推理控制分离（1 周）— 依据 Hermes 4 文件

| Hermes 文件 | 字节 | 职责 |
|-------------|------|------|
| `reasoning_effort.py` | 13 KB | reasoning effort 映射 (low/medium/high) |
| `reasoning_params.py` | 12 KB | 推理参数构造 (max_tokens / temperature) |
| `reasoning_timeouts.py` | 4 KB | 推理超时阶梯 |
| `reasoning_summaries.py` | 3 KB | 推理摘要提取（provider 多样化）|

**Spirit 现状**：`tool_executor.py` 内散落，没有独立模块。3 类推理 provider 切换需要重写代码。

**应补文件**：`spirit/agent/reasoning_controller.py` / `reasoning_timeouts.py` / `reasoning_summaries.py`。

#### R-P3.2 Provider 体系升级（1 周）— 依据 Hermes 4 文件

| Hermes 文件 | 字节 | 职责 |
|-------------|------|------|
| `provider_registry.py` | 9 KB | provider registry 热插拔 |
| `provider_base.py` | 2 KB | provider ABC 接口 |
| `provider_projection.py` | 2 KB | provider 能力投影 |
| `provider_media.py` | 6 KB | 多模 provider 适配 |

**Spirit 现状**：`spirit/providers/` 仅有 3 文件。`provider_switch_protection.py` 在 agent/ 目录下 (313 行)，跟 Hermes 架构方向不同。

**应补文件**：`spirit/agent/provider_registry.py` / `provider_base.py` / `provider_projection.py`。`provider_switch_protection.py` 重构为 registry 模式。

#### R-P3.3 消息清洗（1 周）— 依据 Hermes 3 文件

| Hermes 文件 | 字节 | 职责 |
|-------------|------|------|
| `message_sanitization.py` | 36 KB | 4 类清洗（surrogates / control / oversized / close_interrupted）|
| `message_metadata.py` | 1.5 KB | 元数据投射 |
| `message_content.py` | 1.3 KB | 内容抽取 |

**Spirit 现状**：清洗逻辑散在 `turn_context.py` + `redact.py`（22882 bytes，是脱敏而非清洗）。

**应补文件**：`spirit/agent/message_sanitization.py` / `message_metadata.py` / `message_content.py`。

#### R-P3.4 会话持久化 mixin（0.5 周）— 依据 Hermes 2 文件

| Hermes 文件 | 字节 | 职责 |
|-------------|------|------|
| `session_persistence.py` | 25 KB | 重启后恢复上下文 |
| `session_activity.py` | 4 KB | session 活性追踪 |

**Spirit 现状**：`spirit/storage/session_db.py` 有持久化但没 mixin 抽象。kill -9 后不能完整恢复。

**应补文件**：`spirit/agent/session_persistence.py` + `session_activity.py`。

#### R-P3.5 思考块 scrubber（0.5 周）— 依据 Hermes 2 文件

| Hermes 文件 | 字节 | 职责 |
|-------------|------|------|
| `think_scrubber.py` | 9 KB | 多家 think 块剥离（DeepSeek / OpenAI / Claude）|
| `repetition_guard.py` | 4 KB | repetition 检测防循环 |

**Spirit 现状**：`spirit/cli/main_enhanced.py:48` 有 `.*?</think>` 正则但是单 provider 硬编码。3 类 provider think 块不能统一处理。

**应补文件**：`spirit/agent/think_scrubber.py` + `repetition_guard.py`。**修复 HF-001** 的完整方案见这里（不用 HF 临时修复）。

### 13.3 P1 缺口（v0.3.0 推荐补完）

#### R-P3.6 turn facade 拆分（2 周）— 依据 Hermes 27 文件

Hermes 把 `conversation_loop` 拆为 27 个 `turn_*.py`：

| Hermes 文件 | 字节 | 职责 |
|-------------|------|------|
| `turn_iteration_prep.py` | 27 KB | 迭代准备（上下文 / 预算 / 消息预处理）|
| `turn_truncation.py` | 36 KB | 截断多策略（R-P2.1 已规划）|
| `turn_empty_response.py` | 14 KB | 空响应处理 |
| `turn_overflow.py` | 26 KB | 超长响应处理 |
| `turn_response_check.py` | 18 KB | 响应检查 |
| `turn_facade.py` | 12 KB | turn facade 主入口 |
| `turn_api_error.py` | 22 KB | API 错误处理 |
| `turn_preflight.py` | 22 KB | preflight 门禁 |
| `turn_facade_lease.py` | 19 KB | facade 生命周期 |
| `turn_final_response.py` | 17 KB | 最终响应构造 |
| `turn_finalizer.py` | 39 KB | 终结器 |
| 其他 16 个 | - | retry / explainers / failure_copy / preflight_gate 等 |

**Spirit 现状**：`conversation_loop.py` 1300+ 行（600+ KB），单文件容纳 27 个子流程耦合。

**风险**：单点修改风险高（任何一行改动都可能影响 27 个流程）；单测覆盖率难上去。

**应补文件**：渐进式拆分。第一批 5 个：`turn_iteration_prep.py` / `turn_facade.py` / `turn_facade_lease.py` / `turn_final_response.py` / `turn_finalizer.py`。**目标**：`conversation_loop.py` 降到 400 行以下。

#### R-P3.7 Micro compaction（0.5 周）— 依据 Hermes 2 文件

| Hermes 文件 | 字节 | 职责 |
|-------------|------|------|
| `micro_compaction.py` | 25 KB | 单步增量压缩 |
| `native_compaction.py` | 18 KB | native provider 原生压缩 |

**Spirit 现状**：`context_compressor.py` (19 KB) + `context_compressor_v2.py` (15 KB) 是 batch 压缩，没有 micro step 压缩。native 压缩未接。

**应补文件**：`spirit/agent/micro_compaction.py` + `native_compaction.py`。

### 13.4 P2 缺口（v0.5.0+ 长期补完）

见 §4.1，此处仅列名：

| 编号 | 功能 | Hermes 依据 | 价值 |
|------|------|--------------|------|
| R-P4.1 | MoA 体系升级 | `moa_loop.py` (78 KB) + 2 文件 | 4 拓扑 + trace |
| R-P4.2 | 上下文引擎抽象 | `context_engine.py` (13 KB) | SPI 统一 |
| R-P4.3 | 子 agent lifecycle | `subagent_lifecycle.py` (18 KB) | leak / cancel / timeout |
| R-P4.4 | Shell hooks first-use | `shell_hooks.py` (32 KB) | HITL 增强 |
| R-P4.5 | 后台 review 复用 | `background_review.py` (80 KB) | 跨 session 评估 |
| R-P4.6 | Relay 架构 | `relay_runtime.py` + 2 文件 | 跨进程路由 |
| R-P4.7 | Provider adapter 矩阵 | `anthropic_*` / `bedrock_*` / `codex_*` (12+ 文件) | 6 provider 独立维护 |

### 13.5 关键洞察

1. **Spirit 不是简单"减少 Hermes 模块"，而是架构方向不同**：
   - Spirit 选择**单文件**大模块（`conversation_loop.py` 1300 行）+ **自命名**辅助模块（`accuracy_attribution` / `provider_switch_protection`）
   - Hermes 选择**单职责**多文件（27 个 `turn_*.py`）+ **统一命名**（`turn_iteration_prep`）
   - 两者在功能覆盖 80% 重叠，但 Spirit 可测试性 / 可维护性较差

2. **Spirit 的"独有"文件 = Hermes 的"应有"模块的 Spirit 重命名版**：
   - `accuracy_attribution.py` (Spirit) ↔ Hermes 同名
   - `provider_switch_protection.py` (Spirit) ↔ Hermes `provider_registry.py` 的子集
   - `context_compressor_v2.py` (Spirit) ↔ Hermes `conversation_compression.py` 的 v2

3. **真正缺失的不是"文件"，是"原子化"**：
   - Hermes 27 个 `turn_*.py` = 27 个独立可测的子流程
   - Spirit 1 个 `conversation_loop.py` = 27 个子流程耦合
   - 移植 1 个 `turn_*.py` 比改 1 行 `conversation_loop.py` 风险小

4. **调用方式 (link pattern) 关键观察**：
   - Hermes 倾向 **mixin / module-level pure function**（如 `turn_facade.TurnFacadeMixin`）
   - Spirit 倾向 **agent instance method**（如 `agent.get_system_prompt()`）
   - 重构方向：把 Spirit 的大方法拆成 module-level pure function，接受 `agent` 作 first arg（Hermes 风格）

### 13.6 补完节奏

```
v0.3.0  (T+9)   — R-P2.1 / R-P2.2 / R-P2.3 / R-P2.4 完成
v0.3.1  (T+10)  — R-P3.1 推理控制分离 (1 周)
v0.3.2  (T+11)  — R-P3.2 Provider 体系升级 (1 周)
v0.3.3  (T+12)  — R-P3.3 消息清洗 (1 周)
v0.3.4  (T+12.5)— R-P3.4 会话持久化 (0.5 周) + R-P3.5 思考块 scrubber (0.5 周)
v0.3.5  (T+13)  — R-P3.6 turn facade 拆分 第一批 (1 周) + R-P3.7 Micro compaction (0.5 周) 同步
v0.4.0  (T+18)  — C-P1.1 / C-P1.2 / M-P1.1
v0.5.0  (T+26)  — M-P1.2 / M-P1.3 / M-P2.1-3 / C-P2.1-3
v0.5.1  (T+28)  — R-P4.1 MoA + R-P4.2 上下文引擎 (1.5 周)
v0.5.2  (T+30)  — R-P4.3 lifecycle + R-P4.4 shell hooks (1 周)
v0.5.3  (T+32)  — R-P4.5 后台 review (1 周)
v0.5.4  (T+34)  — R-P4.6 Relay 架构 (2 周)
v0.5.5  (T+36)  — R-P4.7 Provider adapter 矩阵 (2 周)
V3.0.0  (T+44)  — R-P3.1-3 / M-P3.1-2 / C-P3.1-3
```

### 13.7 不补的模块（明确放弃）

以下 Hermes 模块 Spirit 不补（理由：价值低 / Spirit 架构方向不需要）：

- `relay_runtime.py` 等 3 个 Relay 文件 — Spirit 走进程内调用，不跨进程路由
- `acp_adapter/` / `acp_registry/` — 桌面终端协议，Spirit 不支持该协议
- `background_review.py` 部分功能 — Spirit 用 R-P4.5 子集
- 桌宠侧 Hermes `web/` / `tui_gateway/` — Spirit 走 `desktop-app/`，架构方向不同

---

## 14. Spirit 前端显示层差距分析（v0.3.1-P4 之后）

> **完整对比文件**：`hermes-agent-main/agent/display.py`（1225 行）+ `status_output.py`（229 行）+ `stream_delivery.py`（356 行）+ `think_scrubber.py`（176 行）+ `notification_presentation.py`（3 KB）+ `inline_tool_executors.py`（13 KB）。
>
> **本节用途**：上一轮 Spirit v0.3.1 已经把 Hermes 的 5 个 callback + 6 个 ProgressPhase 对齐了"事件总线"，但**渲染层**还差 5 个高价值模块。本节按 P0-P4 排序，逐项给出落地路径。

### 14.0 v0.3.1 已对齐（前端显示层基础）

| Hermes 能力 | spirit v0.3.1 状态 |
|---|---|
| `reasoning_callback` 流式 reasoning | ✅ `REASONING_DELTA` + `REASONING_END` + `_ReasoningStream` |
| `stream_delta_callback` 流式最终回答 | ✅ `STREAM_TEXT_DELTA` + `STREAM_TEXT_END` + `_StreamTextBox` |
| `tool_gen_callback` 工具参数生成期 | ✅ `TOOL_GEN_START`（`┊ ⚡ preparing…`）|
| `tool_progress_callback("tool.started")` | ✅ `TOOL_STARTED`（含 args 预览）|
| `tool_progress_callback("tool.completed")` | ✅ `TOOL_COMPLETED`（含 result + is_error + duration）|
| `progress_notifier` 全局事件总线 | ✅ 完整（notify_progress + 多个 sink）|
| 14 个工具 emoji 表 | ✅ `_CUTE_TOOL_EMOJI` |
| `_format_cute_tool_message`（完成行）| ✅ 含耗时 + 错误检测 |
| `think_scrubber` 状态机（部分）| ⚠️ 部分（仅一次性 regex，跨 chunk 漏标签）|
| tool 参数脱敏（api_key/token/password）| ✅ `_redact_tool_args_for_display`（部分键名级）|
| 4 个 display.* 配置开关 | ✅ `show_tool_args` / `show_tool_result` / `tool_result_max_len` / `show_stream_text` |

### 14.1 P0 — 核心 UX 差距（影响"看起来流式"的基础稳定性）

#### P0-1 KawaiiSpinner 思考动画（0.5 周）

**Hermes 是什么**：`agent/display.py:812 KawaiiSpinner` —— 9 种 spinner 动画（dots / bounce / grow / arrows / star / moon / pulse / brain / sparkle）+ 10 个 kawaii 等待颜文字 `(｡◕‿◕｡)` + 15 个思考动词（pondering / cogitating / …）+ kawaii 思考颜文字 `(◔_◔)`。

**Spirit 现状**：仅 dim cyan spinner，无 kawaii 表情 + 无思考动词轮换。CLI 看起来"机械"。

**落地**：
- 新建 `spirit/cli/kawaii_spinner.py`（200-300 行）
- `_CUTE_KAWAII_WAITING` / `_CUTE_KAWAII_THINKING` / `_CUTE_THINKING_VERBS` 表
- 在 `THINKING` phase 替换 `console.print("💭 thinking…")` 为 `KawaiiSpinner(face + verb)`

**验收**：CLI 长时间思考时能看到 `(◔_◔) pondering…` 轮换动画。

#### P0-2 文件编辑内联 diff 渲染（0.5 周）

**Hermes 是什么**：`agent/display.py:106-807` 整套 LocalEditSnapshot + capture_local_edit_snapshot + extract_edit_diff + _render_inline_unified_diff + render_edit_diff_with_delta。write_file 完成后立即内联显示 unified diff（保留 6 文件 / 80 行上限），颜色化 +/- 行。

**Spirit 现状**：TOOL_COMPLETED 只显示 `"📝 write_file foo.py 1.2s"`，**用户看不到修改了什么**。需要切到 git diff 才能对比。

**落地**：
- 新建 `spirit/agent/local_edit_snapshot.py`（80-120 行）：工具调用前的 fs 快照
- 在 `tool_executor.py` 中 `TOOL_STARTED` 前调 `capture_local_edit_snapshot(name, args)`
- 在 `_render_progress` 中 `TOOL_COMPLETED` 调用 `render_edit_diff_with_delta(name, result, snapshot, print_fn)` 内联打印 unified diff
- 默认折叠为 `┊ review diff (5 lines)`，展开后才显示完整 diff

**验收**：write_file 完成后立即看到 unified diff，行号 + 颜色化 + 6 文件上限正确。

#### P0-3 友好 tool 动词表 `_TOOL_VERBS`（0.5 周）

**Hermes 是什么**：`agent/display.py:503-518 _TOOL_VERBS` 23 个工具的友好动词：
```
"web_search": "Searching the web", "read_file": "Reading", "write_file": "Writing",
"browser_navigate": "Browsing", "terminal": "Running", "execute_code": "Running code",
"image_generate": "Generating image", "delegate_task": "Delegating", "memory": "Updating memory",
"cronjob_manage": "Scheduling", "clarify": "Asking", "todo_list": "Updating tasks",
"skill_view": "Reading skill", "skills_list": "Listing skills", "session_search": "Searching past sessions",
…
```

**Spirit 现状**：14 个 emoji + 工具名（read_file / write_file / terminal / search_files / delegate_task 等），但**没有动词短语**。用户看到 `📄 read_file` 而不是 `📖 Reading config.py`。

**落地**：
- 完善 `_format_cute_tool_message`：在 emoji + name 之前插入 `get_tool_verb(name)` 友好动词
- 处理 `_TOOL_VERBS_NO_PREVIEW`（skills_list / session_search 不带 preview）
- 处理 `_TOOL_VERBS_FOR_CONNECTOR`（web_search / search_files 用 " for " 连接）

**验收**：工具调用完成行显示 `┊ 📖 Reading config.py  0.3s` 而不是 `┊ 📄 read_file config.py  0.3s`。

#### P0-4 工具调用预览 `_PRIMARY_ARGS` 23 工具专属 builder（1 周）

**Hermes 是什么**：`agent/display.py:353-461 _PRIMARY_ARGS + _PREVIEW_BUILDERS`：
- `_PRIMARY_ARGS`：23 个工具 → 主参数键（terminal.command / web_search.query / read_file.path / image_generate.prompt / delegate_task.goal / cronjob_manage.action / …）
- `_PREVIEW_BUILDERS`：browser_exec / delegate_task / process_manage / todo_list / terminal / execute_code / read_file / memory / send_message / skill_view / session_search / tool_call / tool_search / tool_describe 共 14 个工具**专属**预览器

**Spirit 现状**：`_format_tool_args_preview` 单一通用函数，对所有工具一刀切（`{key}: {truncated_value}`），无法理解 terminal 是 command 还是 delegate_task 是 goal 还是 skill_view 是 file_path。

**落地**：
- 新建 `spirit/cli/tool_preview.py`（200-300 行）
- `_PRIMARY_ARGS` 表（23 工具）+ `_PREVIEW_BUILDERS` 表（14 工具专属 builder）
- `_preview_terminal` / `_preview_browser_exec` / `_preview_delegate_task` / `_preview_memory` / `_preview_send_message` / `_preview_skill_view` / `_preview_todo_list` / `_preview_read_file` 8 个专属 builder
- 替换 `_format_tool_args_preview` 为 `build_tool_preview(name, args)`

**验收**：工具调用开始行显示 `┊ 💻 $         npm install` 而不是 `┊ 💻 terminal   {"command": "npm install..."}`。

### 14.2 P1 — 重要 UX 提升（应该做，可探索）

#### P1-1 `summarize_shell_command` shell 命令智能摘要（0.5 周）

**Hermes 是什么**：`agent/display.py:186-296` —— 自动去掉 `cd / pushd / export / source` 等 silent head，合并 `&& / || / ;` 复合命令，剥掉管道 tail（`| head / tail / wc / sort / uniq`），展开为 `_PRETTY_UNIFIED`：
- `cd /tmp && npm install && cd /tmp && npm test` → `npm install + 2 commands`
- `cat foo.log | head -20` → `cat foo.log`（剥掉 `| head`）
- `echo "$_exit=0 -- some debug"` → 保留为边界 echo

**Spirit 现状**：直接显示原始 shell 命令（多行 + 长 + cd/pipe 噪音）。

**落地**：
- 在 `spirit/cli/tool_preview.py` 加 `_SHELL_SILENT_HEADS` / `_SHELL_PIPE_TAIL_HEADS` 表
- 加 `_split_shell_compound` / `_clean_shell_segment` / `_summarize_shell_command` 函数
- 在 `_preview_terminal` / `_preview_execute_code` 中调用

**验收**：`cd /tmp && npm install` 显示为 `npm install`，`cat foo | head -20` 显示为 `cat foo`。

#### P1-3 `StreamingThinkScrubber` 状态机（0.5 周）—— 替代一次性 regex

**Hermes 是什么**：`agent/think_scrubber.py:27 StreamingThinkScrubber` —— 状态机 `reset() / feed() / flush()`，跨 chunk 不会漏 `<think>` 标签。专门为 `MiniMax-M2.7` 这种把 `<think>` 和内容分开发的模型设计（#17924 修复）。

**Spirit 现状**：`_STREAM_OPENER_RE` 一次性 regex，跨 chunk 的 `<think>` 可能漏 → 思考内容泄露到最终回答。

**落地**：
- 新建 `spirit/agent/think_scrubber.py`（80-120 行）：抄 Hermes 状态机（reset/feed/flush）
- 集成到 `_call_llm` 流式循环：chunk → scrubber.feed(chunk) → visible_text 推 STREAM_TEXT_DELTA
- 收尾：scrubber.flush() 推 STREAM_TEXT_END

**验收**：3 类 provider（DeepSeek-R1 / OpenAI-o1 / Claude-thinking）的 think 块 100% 剥离，跨 chunk 不漏。

#### P1-4 `StatusOutputMixin` 状态/警告/通知安全输出（0.5 周）

**Hermes 是什么**：`agent/status_output.py:16 StatusOutputMixin` —— 4 类方法：
- `_safe_print(*args, diagnostic=False)`：broken pipe / closed stdout 容错，路由 `_print_fn`
- `_vprint(*args, force=False)`：verbose 打印，token 流式时自动抑制（避免输出打架）
- `_should_start_quiet_spinner()`：静默模式启动判断（ACP / 非 TTY 跳过）
- `_emit_status_kind(kind, message)` + `_warning_presentation_enabled()`：warning 通知去重 + 平台感知

**Spirit 现状**：直接 `console.print(...)`，无 broken pipe 容错，token 流式时 + 警告同时输出会错位。

**落地**：
- 新建 `spirit/cli/status_output.py`（150-200 行）：`safe_print` / `vprint` / `_should_start_quiet_spinner` / `_emit_status_kind`
- 集成到 `_render_progress`：warning 类 phase → `vprint(force=True)` + dedupe + TTY safe
- 处理 `-Q` (suppress_status_output) 标志：ACP 协议 / machine-readable 模式自动抑制

**验收**：CLI +Q 模式下不输出 `[tool] / [done]`；流式期间 warning 不会和 token 撞行。

### 14.3 P2 — 增强体验（V3.0 收尾，可探索）

#### P2-1 bridge tool 多行展开（0.5 周）

**Hermes 是什么**：`agent/display.py:1150 _cute_bridge_rows` —— `tool_call / tool_search / tool_describe` 这类一次调用多个子工具的，会展开多行：
```
┊ 🔍 tool_call  fetch_weather
┊ 🔍 tool_call  get_calendar   [exit 1]
┊ 🔍 tool_call  search_docs
┊ ⏱  0.8s
```

**Spirit 现状**：单行 `┊ tool_call (3 calls)` 不展开。

**落地**：
- 完善 `_format_cute_tool_message`：检测 `tool_call / tool_search / tool_describe` → 展开多行
- 新增 `_bridge_tool_labels(name, args)` 拉取 plugin 标签

**验收**：`tool_call [fetch_weather, get_calendar, search_docs]` 展开为 3 行各自 emoji + 完成时间。

#### P2-2 `StreamDeliveryMixin` 单写者流控（0.5 周）

**Hermes 是什么**：`agent/stream_delivery.py:19 StreamDeliveryMixin` —— `_call_quietly(cb, *args)` + `_deliver_to_stream_callbacks(text)` + `_stream_writer_superseded()`：
- 多个 sink（CLI / TUI / 桌宠）按顺序调，但确保"单写者"
- 旧的 writer 被 superseded 时自动停止
- 避免 CLI 和 桌宠同时写 `STREAM_TEXT_DELTA` 导致乱序

**Spirit 现状**：`STREAM_TEXT_DELTA` 直接调 `notify_progress`，多个 sink 同时拿到 chunk，乱序由 sink 自己负责（ws_server 没问题，CLI 假设 FIFO）。

**落地**：
- 新建 `spirit/agent/stream_delivery.py`（80-120 行）：`_call_quietly` + 单写者仲裁
- `progress_notifier.notify_progress` 内增加 sequence_id 序号，sink 按序号聚合

**验收**：CLI + 桌宠同时订阅时，文本流序号一致，无乱序。

#### P2-3 tool 工具后 `_TOOL_DONE_BORDER` 视觉边界（0.3 周）

**Hermes 是什么**：每次工具完成后的 "┊ ⌐■-■  0.8s" 之前有视觉边界（"│ tool result" / `┊ ⏱  done`）。

**Spirit 现状**：TOOL_COMPLETED 紧接下一行 REASONING_END，视觉无间隔。

**落地**：在 `_render_progress.TOOL_COMPLETED` 末尾输出 `┊ ⏱  done` 边界行。

### 14.4 P3 — 长期演进（V3.5+，锦上添花）

#### P3-1 Skin engine 主题引擎（1 周）

**Hermes 是什么**：`hermes_cli/skin_engine.py` —— 用户可定义 `colors / tool_emojis / spinner.wings / spinner.waiting_faces / spinner.thinking_faces / spinner.thinking_verbs / tool_prefix`，运行时通过 `set_active_skin` 切换。

**Spirit 现状**：硬编码 dim cyan / yellow / red。

**落地**：
- 新建 `spirit/cli/skin_engine.py`（150-200 行）：`Skin` dataclass + `set_active_skin` + `get_active_skin`
- `display.py` 改造：`_get_skin()` 优先取 skin → fallback 默认值
- 增加 `spirit.skin` 配置（`display.skin: default | dark | high_contrast | custom`）

**验收**：用户可切到 dark 主题后所有颜色 + spinner + 颜文字自动应用。

#### P3-2 Slack live status 短语 `build_status_phrase`（0.5 周）

**Hermes 是什么**：`agent/display.py:567 build_status_phrase` —— 输出 `"is reading foo.py"` / `"is searching the web for X"`，用于 Slack `setStatus`（49 字符上限）。

**Spirit 现状**：无 Slack 集成，但桌宠可能需要类似"状态文本"。

**落地**：
- 新建 `spirit/cli/status_phrase.py`（60-80 行）：`build_status_phrase(name, args, max_len=49)`
- 桌宠状态文本 API 用此生成（"正在读取 foo.py"）

#### P3-3 pre/post tool 文档化 bridge tool labels（1 周）

**Hermes 是什么**：`agent/display.py:529 tool_labels_for_call` —— `tools/tool_labels.py` 提供 plugin / MCP / 自定义工具的标签系统（label.text + label.emoji + label.preview）。

**Spirit 现状**：插件工具无统一标签。

**落地**：
- 新建 `spirit/cli/tool_labels.py`（150-200 行）：`ToolLabel` dataclass + `labels_for_call(name, args)`
- 插件 manifest 加 `label:` 字段
- `_format_cute_tool_message` 优先用 plugin label

### 14.5 关键洞察

1. **Spirit v0.3.1 已对齐 Hermes 5 callback 事件总线（80% 价值）**：
   - 6 个新 ProgressPhase 完整映射 Hermes 的 tool.gen_start / tool.started / tool.completed / stream_delta / stream_end / reasoning.available
   - 14 个工具 emoji + 工具完成行耗时 + 错误检测
   - 多 sink 透传（CLI + 桌宠 + ws_server）
   - 但**渲染层**（kawaii 动画 / 文件 diff / 友好动词 / 智能预览）还是 Spirit CLI 自己的弱项：
     - 用户看不到工具修改了什么（write_file 后无 diff）
     - 用户看不到"是什么操作"（read_file / write_file / terminal 看起来都差不多）
     - 用户看不到 shell 命令的"意图"（cd && npm install → 显示成噪音）

2. **P0-2（文件 diff）影响最大**：
   - 用户最常做的事：让 agent 改文件 → 看 diff 确认 → 决定是否接受
   - 当前必须切到 git diff 才能看到，**agent 给的输出对用户没意义**
   - 1 周内可完成，回报巨大

3. **P0-1（kawaii）+ P0-3（动词）+ P0-4（智能 preview）**是"看起来像活"的三件套：
   - 缺一个，"机器人感"明显
   - 三个一起做，CLI 体验从"机械"变"灵动"
   - 总工时 2 周，回报是所有用户每次启动都能感受到

4. **P1-3（think 状态机）修补 v0.3.1 已知漏洞**：
   - 当前 `_STREAM_OPENER_RE` 是 per-chunk regex，跨 chunk 漏 `<think>` 是已知 bug（HF-001 类似）
   - 状态机是"教科书"解法，0.5 周

5. **P3-1（skin engine）是 V3 终极目标**：
   - 让用户/团队能自定义主题（深色 / 高对比度 / 品牌色）
   - 是 Spirit 走向"产品"而非"工具"的关键一步
   - 推迟到 V3.5 不影响核心价值

### 14.6 补完节奏

```
v0.3.0  (T+9)   — R-P2.1 / R-P2.2 / R-P2.3 / R-P2.4 完成
v0.3.1  (T+10)  — R-P3.1 推理控制分离 (1 周)
v0.3.2  (T+11)  — R-P3.2 Provider 体系升级 (1 周)
v0.3.3  (T+12)  — R-P3.3 消息清洗 (1 周)
v0.3.4  (T+12.5)— R-P3.4 会话持久化 (0.5 周) + R-P3.5 思考块 scrubber (0.5 周)
v0.3.5  (T+13)  — R-P3.6 turn facade 拆分 第一批 (1 周) + R-P3.7 Micro compaction (0.5 周) 同步
─────────────────────────────────────────────────────────────────  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
v0.3.6  (T+13.5)— D-P1.1 KawaiiSpinner (0.5 周) + D-P1.4 P0-2 文件 diff (0.5 周)
v0.3.7  (T+14.5)— D-P1.2 _TOOL_VERBS 友好动词 (0.5 周) + D-P1.3 _PRIMARY_ARGS 智能 preview (1 周)
v0.3.8  (T+16)  — D-P1.5 summarize_shell_command (0.5 周) + D-P1.6 think 状态机 (0.5 周)
                  + D-P1.7 StatusOutputMixin (0.5 周)
v0.3.9  (T+17)  — D-P2.1 bridge tool 多行 (0.5 周) + D-P2.2 StreamDeliveryMixin (0.5 周)
                  + D-P2.3 tool 边界行 (0.3 周)
v0.3.10 (T+18)  — D-P3.1 skin engine (1 周)
─────────────────────────────────────────────────────────────────  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
v0.4.0  (T+19)  — C-P1.1 / C-P1.2 / M-P1.1
v0.4.1  (T+19.5)— D-P3.2 Slack status phrase (0.5 周) + D-P3.3 plugin tool labels (1 周)
v0.5.0  (T+27)  — M-P1.2 / M-P1.3 / M-P2.1-3 / C-P2.1-3
v0.5.1  (T+29)  — R-P4.1 MoA + R-P4.2 上下文引擎
v0.5.2-v0.5.5    (R-P4.3 / R-P4.4 / R-P4.5 / R-P4.6 / R-P4.7)
V3.0.0  (T+45)  — R-P3.1-3 / M-P3.1-2 / C-P3.1-3
```

### 14.7 不补的模块（明确放弃）

以下 Hermes 显示层模块 Spirit 不补（理由：架构方向不同 / 价值低）：

- `agent/inline_tool_executors.py` —— Hermes 的"工具直执"绕过主循环，Spirit 走 tool_executor 统一路径
- `agent/stream_single_writer.py` —— Hermes 单进程多 agent 共用 stream，Spirit 一进程一 agent
- `agent/notification_presentation.py` —— Hermes notification 平台层（Slack/Telegram），Spirit 无此集成
- `hermes_cli/skin_engine.py` 中部分功能（自定义 spinner.wings 等）—— Spirit 不支持 plugin 主题

---

### 14.8 Spirit 前端 UX 完整缺口清单（P1-P4，23 项）

> **本节详细清单**：上一节（§14.1-14.4）从 5 个主要 Hermes 显示模块提取的 13 项核心差距。这一节从用户全面使用角度重新扫描 hermes-agent-main/agent/ + gateway/ + tui_gateway/ + web/，按"用户场景 · 架构层 · 平台层 · 细节打磨"重新分为 P1-P4 类，总计 23 项，其中 13 项已在 §14.1-14.4 中并到 R-P3.x / D-P1.x，本节重点抽取原有设计遗漏的 16 项 + 补充说明 7 项交叉项。

#### P1 — 核心 UX 差距（影响"看起来流式"的基础稳定性）—— 5 项

| # | Hermes 能力 | Hermes 文件 | Spirit 状态 | 编号 | 工时 |
|---|------------|-----------|----------|------|------|
| 1 | Single-writer 锁防 superseded stream 覆盖（重试时新流和旧流打架）| `agent/stream_single_writer.py` (`_stream_writer_superseded`) | ❌ 无。重试/多 stream 时旧 chunk 可能覆盖新 chunk 的 box | **D-P2.2** | 0.5 周 |
| 2 | 状态化 think scrubber 跨 chunk 标签正确处理（`<think>` 被切到两个 chunk 时不漏）| `agent/think_scrubber.py` (`_stream_think_scrubber` per-delta) | ❌ 用静态 `_STREAM_OPENER_RE`，chunk切到标签中间会丢/漏 | **D-P1.6** | 0.5 周 |
| 3 | Compaction 进度展示 上下文压缩时显示 "before X / after Y" | `agent/compaction_display.py` | ❌ 压缩对用户透明，看不到发生了什么 | **D-P1.8** | 0.5 周 |
| 4 | MoA 进度 多模型参考 + 聚合进度（`moa.reference / moa.aggregating`）| `agent/moa_loop.py` (事件 `moa.*`) | ❌ Spirit MoA 模块存在但进度不外推 | **D-P1.9** | 0.5 周 |
| 5 | Plan display LLM 生成的计划展示（todo 工具输出）| `agent/plan_prompt.py` | ❌ Spirit 有 todo 工具但计划不展示 | **D-P1.10** | 0.5 周 |

**P1 影响**：用户实际用起来会发现"重试时推理/回答错位"（#1）、"跨 chunk 的 think 块偶尔泄露"（#2）、"上下文自动压顶了突然变短"（#3）。

**D-P1.8 Compaction 进度落地路径**：
- 新建 `spirit/agent/compaction_display.py`（80-120 行）
- 在 `context_compressor_v2.compress()` 调 `_emit_compaction_progress(before_tokens, after_tokens, ratio)` 走 progress_notifier
- `_render_progress` 加 `COMPACTION_PROGRESS` phase 分支：显示 `┊ 🗜  compacted 850→ 284 tokens (-66%)`
- 默认折叠为一句话，展开后才显示 before/after + ratio

**D-P1.9 MoA 进度落地路径**：
- 在 `spirit/moa/loop.py` 里 `moa.reference` / `moa.aggregating` 事件点 emit `notify_progress(ProgressPhase.MOA_REFERENCE / MOA_AGGREGATING, ...)`
- `_render_progress` 加 2 个新 phase 分支：`┊ 🌀 referencing 3 models` + `┊ 🌀 aggregating (DeepSeek + Claude + GPT-4)`
- 与 P-P2.4 (MoA 升级) 共享 progress 事件

**D-P1.10 Plan display 落地路径**：
- todo_list 工具 emit `notify_progress(ProgressPhase.PLAN_DISPLAY, ...)`，注入 `todos` 字段
- `_render_progress.PLAN_DISPLAY` 渲染：
```
┊ 📋 plan
  ┊  1. [✓] read_file config.yaml
  ┊  2. [✓] patch config.yaml
  ┊  3. [⋯] terminal run tests
  ┊  4. [ ] commit changes
  ┊ ⏱  2.1s
```
- 进度状态：`[✓] done / [⋯] in_progress / [ ] pending`

#### P2 — 架构层差距（影响"看起来一致"的稳定性）—— 8 项

| # | Hermes 能力 | Hermes 文件 | Spirit 状态 | 编号 | 工时 |
|---|------------|-----------|----------|------|------|
| 6 | Reasoning 超时 思考阶段看门狗（防模型卡在思考）| `agent/reasoning_timeouts.py` | ❌ | **R-P3.1** | 1 周 |
| 7 | Reasoning 滚动摘要 长推理自动压缩显示（避免 💭 滚屏上万行）| `agent/reasoning_summaries.py` | ❌ 现在只折叠前 5 行 | **R-P3.1** | 1 周 |
| 8 | Reasoning effort 档位 控制思考深度（high/medium/low）| `agent/reasoning_effort.py` | ❌ config 有字段但 Spirit 没用 | **R-P3.1** | 1 周 |
| 9 | Stream diag 流式诊断（chunk 间隔、首字节延迟）| `agent/stream_diag.py` | ❌ 没有 debug 通道 | **D-P2.4** | 0.5 周 |
| 10 | Turn failure copy 友好错误文案（不是抛 stack trace）| `agent/turn_failure_copy.py` | ❌ 出错就是原始异常 | **D-P2.5** | 0.5 周 |
| 11 | Display 模块拆分 独立 display / status / notification 三模块 | `agent/display.py` + `agent/status_output.py` + `agent/notification_presentation.py` | ⚠️ §14.0-14.4 中部分已做（kawaii / verb / preview），但 cli/main.py 1487 行仍未拆 | **D-P2.6** | 1 周 |
| 12 | 完整 Redact 模块 信用卡/邮箱/path/env var/token 脱敏 | `agent/redact.py` | ⚠️ Spirit inline 9 keys，缺路径/邮箱等 | **D-P2.7** | 0.5 周 |
| 13 | Message sanitization 上下文清理（敏感数据不出现在对话中）| `agent/message_sanitization.py` (36 KB) | ❌ | **R-P3.3** | 1 周 |

**P2 落地路径补充**：

**D-P2.4 Stream diag 落地路径**：
- 新建 `spirit/agent/stream_diag.py`（100-150 行）
- 在 `_call_llm` 流式循环中记录：first_chunk_latency / chunk_count / chunk_intervals / total_bytes / stop_reason
- `notify_progress(ProgressPhase.STREAM_DIAG, extra={...})` 推送（仅 `--debug` 模式下开启）
- `_render_progress.STREAM_DIAG` 渲染为 `┊ ⏱  TTFB 1.2s, 47 chunks, avg interval 0.18s` 一行

**D-P2.5 Turn failure copy 落地路径**：
- 新建 `spirit/agent/turn_failure_copy.py`（100-150 行）
- 分类：API 错误 / provider 不可用 / 模型拒绝 / 工具超时 / 用户取消 / context overflow
- 每类对应友好文案 + 修复建议
- 集成到 `conversation_loop._call_llm` 异常路径 + `tool_executor.execute` 错误兜底
- `e.g. ⚠️ 模型返回了空响应。这是 provider X 在思考模型上常见问题，试试：1) /model 切到 Claude 2) /retry 3) --reasoning none`

**D-P2.6 Display 模块拆分落地路径**：
- 当前 `spirit/cli/main.py` 1487 行（已含 KawaiiSpinner / _format_cute_tool_message / _StreamTextBox）
- 拆分：
  - `spirit/cli/kawaii_spinner.py`（§14 D-P1.1）
  - `spirit/cli/tool_cute.py`（_format_cute_tool_message + _CUTE_TOOL_EMOJI）
  - `spirit/cli/tool_preview.py`（D-P1.3 _PRIMARY_ARGS + _PREVIEW_BUILDERS）
  - `spirit/cli/status_output.py`（D-P1.7）
  - `spirit/cli/stream_text_box.py`（_StreamTextBox）
- cli/main.py 仅保留：事件路由 + _render_progress + 顶级命令分发（应 减到 600-800 行）

**D-P2.7 完整 Redact 模块落地路径**：
- 新建 `spirit/agent/redact.py`（150-200 行）
- 当前 `_redact_tool_args_for_display` 是 inline 在 tool_executor.py，只覆盖 9 个 key
- 升级到：API key / token / password (9 keys) + 信用卡 (Luhn) / 邮箱 (regex) / 手机号 (中国 +1 / 国际) / 身份证 (18 位) / IPv4 地址 (可选) / env var (PATH=/home/x)
- 集成到：tool_executor 工具结果 + conversation_loop 发出去的 request + ws_server 推送前的 final scrub
- 红 ACL：`display.redact_*` 4 个开关控制

#### P3 — 平台层差距（多端 UX，spirit 当前主要服务 CLI + 桌宠）—— 5 项

| # | Hermes 能力 | Hermes 规模 | Spirit 状态 | 编号 | 工时 |
|---|------------|-----------|----------|------|------|
| 14 | TUI Gateway 终端 UI 多行 scrollback + 实时编辑 | `tui_gateway/` 79 文件 | ❌ Spirit 只有 CLI `console.print` | **D-P3.4** | 4 周 |
| 15 | Web 前端 浏览器聊天 UI | `web/` 10 文件 + `ui-tui/` | ❌ | **D-P3.5** | 3 周 |
| 16 | Gateway 4 层回调管道 状态通知桥接多平台 | `gateway/run.py` 123 文件 | ❌ Spirit ws_server.py 只透传原始 event | **D-P3.6** | 2 周 |
| 17 | Double-channel 广播 CLI + Gateway 同时收（`_emit_status`）| `agent/AGENTS.md` | ❌ Spirit 只有 progress_notifier 单通道 | **D-P3.7** | 1 周 |
| 18 | i18n 多语言 | `agent/i18n.py` + `locales/` 17 文件 | ❌ | **D-P3.8** | 1 周 |

**P3 落地路径补充**：

**D-P3.4 TUI Gateway 落地路径**：
- Spirit 当前只有 `console.print`（Rich）一次性输出，**没有 TUI 状态栏 / scrollback / 多行编辑**
- 选型：参考 Hermes `tui_gateway/prompt_turn.py`（基于 prompt_toolkit + TextArea）
- 新建 `spirit/tui_gateway/` 6 文件：
  - `__init__.py`（启动入口）
  - `prompt_turn.py`（多行输入 + 历史 + 自动补全）
  - `status_bar.py`（model / ctx% / duration / token 实时显示）
  - `stream_panel.py`（流式输出 scrollback）
  - `event_router.py`（接 progress_notifier 路由到 TUI widget）
  - `keybindings.py`（Ctrl+C / Ctrl+L / Ctrl+R / Enter / Shift+Enter）
- 启动：`spirit --tui` 或 `display.interface=tui` in config

**D-P3.5 Web 前端落地路径**：
- Spirit 现有 `web/` 基础版（来自 V2），无聊天界面
- 参考 Hermes `web/` 10 文件（chat.html + main.ts + ws.ts + components）
- 新建 `spirit/web/` 5 文件：
  - `chat.html`（聊天 UI shell）
  - `main.ts`（Vue 3 / Svelte 5 组件）
  - `ws.ts`（WebSocket 接 progress_notifier）
  - `theme.css`（深色/浅色主题 + skin engine 集成）
  - `manifest.json`（PWA 配置）
- 与现有 `desktop-app/` 桌宠共享 WS 协议

**D-P3.6 Gateway 4 层回调管道落地路径**：
- 当前 `spirit/desktop/ws_server.py` 只透传原始 progress event 到桌宠
- Hermes `gateway/run.py` 是 4 层架构：
  - 第 1 层：`AIAgent` 内部 `_emit_status`
  - 第 2 层：`run_agent._call_callback("status_callback", ...)`
  - 第 3 层：`gateway/warning_notifications`（platform-aware dedup）
  - 第 4 层：`gateway/sender`（推 Slack / Telegram / Discord / WebSocket）
- Spirit 升级：把 `_emit_status` 拆 4 层，每层独立可测
- 配套：新增 `spirit/gateway/sender_base.py` + `spirit/gateway/warning_dedupe.py`

**D-P3.7 Double-channel 广播落地路径**：
- 当前 `notify_progress(phase, extra)` 只走 1 个 sink 列表（CLI + 桌宠）
- Hermes 是双通道：CLI + Gateway，物理隔离（一个 stream 给 Slack、一个给桌宠）
- Spirit 升级：把 `notify_progress` 拆为：
  - `_emit_progress_strict(phase, extra)` —— 推 progress_notifier sink list
  - `_emit_progress_gateway(phase, extra)` —— 推 gateway warning_notifications
- CLI / 桌宠两个独立 sink，互不干扰

**D-P3.8 i18n 落地路径**：
- 当前所有 CLI 输出都是 hardcoded 中/英文
- 新建 `spirit/i18n.py`（80-120 行）+ `spirit/locales/{zh-CN,en-US,ja-JP}.json`
- 所有 `_render_progress` / `KawaiiSpinner` / `cute_tool_message` 输出走 `t("tool.read_file", path=...)`
- 配置：`display.language: zh-CN / en-US / ja-JP`
- 默认：检测系统 locale，自动 fallback

#### P4 — 细节打磨（5 项）

| # | Hermes 能力 | Hermes 文件 | Spirit 状态 | 编号 | 工时 |
|---|------------|-----------|----------|------|------|
| 19 | Image routing / display 多模态图片路由 + 展示 | `agent/image_routing.py` + `image_eviction_policy.py` | ❌ | **D-P4.1** | 1 周 |
| 20 | Provider 投影（provider_projection.py）agent-as-provider 拼接 | `agent/provider_projection.py` | ❌ | **R-P3.2** | 1 周 |
| 21 | WebSocket 流式 + 分块延迟 chunk_delay 配置 | `gateway/ws_stream.py` | ❌ | **D-P4.2** | 0.5 周 |
| 22 | Sidebar status bar 完整字段 model / ctx% / Σ usage / duration / msgs / tools / token | `agent/status_output.py` | ⚠️ Spirit 有部分字段（来自 Phase 3 V2）| **D-P4.3** | 0.5 周 |
| 23 | Prompt plan preview 显示 LLM 决策路径 | `agent/plan_prompt.py` + `agent/prompt_builder.py` | ❌ | **D-P4.4** | 0.5 周 |

**P4 落地路径补充**：

**D-P4.1 Image routing / display 落地路径**：
- Spirit 多模态输入走 `read_file` 路径，但没路由优化（cache / eviction / dedup）
- 新建 `spirit/agent/image_routing.py`（100-150 行）
- 3 类策略：in-memory cache / disk cache / evict-by-size（30 MB 上限）
- 集成到 `conversation_loop._call_llm`：检测 user 消息有图片 → 走 image routing → 缓存命中直接复用，未命中走读盘 + resize
- `display.show_image` 工具：CLI 模式打印 metadata（不显示图片），TUI 模式占位

**D-P4.2 WebSocket chunk_delay 落地路径**：
- 当前 ws_server 直推 chunk，**没有 throttle**（高频小包可能打爆桌宠）
- 新增 `gateway.ws_stream.throttle(chunk_delay=0.05)`：每 chunk 后 sleep 50ms
- 配置：`_progress_sink` 加 `chunk_delay_ms` 字段（默认 0，桌宠模式自动 50ms）

**D-P4.3 Sidebar status bar 完整字段落地路径**：
- 当前 Phase 3 V2 已有简化版：`model / duration / msgs`
- 补全：`ctx%` (progress_bar) · `Σ usage` (in_t + out_t 累加) · `tools` (调用次数) · `token` (本次会话 token)
- 新建 `spirit/cli/status_bar.py`（80-120 行）
- 在 CLI 顶部输出状态条，每 1s 刷新一次（threading.Timer）

**D-P4.4 Prompt plan preview 落地路径**：
- LLM 在 `_call_llm` 调用前可能先生成 plan（`plan_prompt.py` 注入的提示）
- Spirit 透明丢弃 plan，**用户看不到 LLM 的思考路径**
- 新建 `spirit/agent/plan_prompt.py`（60-80 行）：
  - 检测响应首段 `<plan>...</plan>` 块
  - emit `notify_progress(ProgressPhase.PLAN_DISPLAY, extra={"plan": "..."})`
  - `_render_progress.PLAN_DISPLAY` 渲染为：
```
┊ 📋 LLM plan
  ┊  1. read config.yaml
  ┊  2. patch LLM provider setting
  ┊  3. verify with /model command
```
- 与 D-P1.10 Plan display 互补：D-P1.10 是 todo 工具的进度，D-P4.4 是 LLM 内部 plan 的展示

---

### 14.9 P1-P4 23 项整合发版节奏

```
─────────────────────────────────────────────────────────────────
v0.3.0  (T+9)   — R-P2.1 / R-P2.2 / R-P2.3 / R-P2.4 完成
v0.3.1-v0.3.5   — Hermes↔Spirit 架构补完 7 项 (T+10 - T+13)
v0.3.6-v0.3.10  — Spirit 前端显示层补完 v1 (T+13.5 - T+18) ← 上一轮
v0.3.11 (T+19.5)— D-P1.8 Compaction 进度 + D-P1.9 MoA 进度 + D-P1.10 Plan display (2 周)
v0.3.12 (T+21.5)— D-P2.4 Stream diag + D-P2.5 Turn failure copy + D-P2.6 Display 模块拆分 + D-P2.7 完整 Redact (3 周)
v0.4.0  (T+24.5)— C-P1.1 / C-P1.2 / M-P1.1 原有 3 项
v0.4.1  (T+25)  — D-P3.4 TUI Gateway 主体 (4 周 → v0.4.1-v0.4.4 分批发版)
                  v0.4.1: TUI Gateway MVP (多行输入 + scrollback)
                  v0.4.2: status bar + keybindings (与 D-P4.3 同步)
                  v0.4.3: stream panel + event router
                  v0.4.4: TUI Gateway 1.0 GA (含多语言)
v0.4.5  (T+32)  — D-P3.5 Web 前端 (3 周) + D-P3.6 Gateway 4 层 (2 周)
v0.4.6  (T+35)  — D-P3.7 Double-channel 广播 (1 周) + D-P3.8 i18n (1 周)
v0.5.0  (T+37)  — M-P1.2 / M-P1.3 / M-P2.1-3 / C-P2.1-3 原有 8 项
v0.5.6  (T+47)  — D-P4.1 Image routing + D-P4.2 WebSocket chunk_delay + D-P4.3 Sidebar 完整字段 + D-P4.4 Prompt plan preview (3 周)
v0.5.1-v0.5.5   — R-P4.x 长期 5 项 (T+49 - T+57)
V3.0.0  (T+62)  — R-P3.1-3 / M-P3.1-2 / C-P3.1-3 收尾 (11 周)
─────────────────────────────────────────────────────────────────
```

### 14.10 总结：V3 总规划变化

| 维度 | 原 V3 | 新 V3 | 增量 |
|------|------|------|------|
| **V3 功能总数** | 51 | **74** | +23 (P1-P4 完整缺口) |
| **总工时** | 55 周 | **72 周** | +17 周 |
| **发版节点** | v0.2.3 - V3.0 | v0.2.3 - V3.0 | +v0.3.11 / v0.3.12 / v0.4.1-v0.4.6 / v0.5.6 |
| **T = 起点** | 2026-10-09 | 2026-10-09 | 不变 |

**关键洞察**：
1. **V3 规划压缩到 14 个月（72 周）**，可能需要拆为 V3.1（v0.2.3 - v0.3.12）+ V3.2（v0.4.0 - v0.5.6）两个阶段
2. **P1-P2 是基础体验（不动灵魂）**：单写者锁、think scrubber、compaction 显示、Redact、display 拆分、TUI Gateway —— 都是用户"看起来对"的基础
3. **P3 是平台扩展（多端）**：TUI / Web / Gateway 4 层 / Double-channel / i18n —— 是"哪里都能用"的关键
4. **P4 是锦上添花**：Image routing / chunk_delay / Sidebar 完整字段 / plan preview —— 是"细节拉开差距"的部分

---

---
