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

V3 共 39 项功能（25 项原始规划 + 14 项来自 Hermes↔Spirit 架构差距补完），分布如下：

| 优先级 | 项数 | 估算总工时 | 完成节点 |
|--------|------|------------|----------|
| **P1（必做）** | 9 | ~13 周 | v0.2.3 - v0.4.0 |
| **P2（应该做）** | 9 + 7 = 16 | ~11 + 7 = 18 周 | v0.3.0 - v0.5.0 |
| **P3（可探索）** | 7 + 7 = 14 | ~9 + 8 = 17 周 | V3.0.0 |

**总投入估算**：~48 周（按 1 人 FTE，9-12 个月）

**关键节点**：
- **v0.2.3**（6 周）：错误恢复链 + 工具结果分类 + 并发超时修复 + provider 切换保护 + 上下文压缩鲁棒性 v2 + 准确率归因与提升
- **v0.3.0**（3 周）：截断多策略 + 活性检测 + provider 切换保护 v2 + 动态 prompt 组成
- **v0.3.0 补完（v0.3.1-v0.3.5）**（7 周）：推理控制分离 + Provider 体系 + 消息清洗 + 会话持久化 + 思考块 scrubber + turn facade 拆分 + Micro compaction
- **v0.4.0**（5 周）：桌宠进度推送 + HITL 面板 + BRS 接线
- **v0.5.0**（8 周）：成效 harness + 领域验证器 + 日报 + 经验确认 + wave 可视化 + 任务历史 + 摘要 + 状态可视化
- **v0.5.0+ 长期**（7 周）：MoA 升级 + 上下文引擎抽象 + 子 agent lifecycle + Shell hooks + 后台 review + Relay 架构 + Provider adapter 矩阵
- **V3.0.0**（11 周）：消息清理 + 敏感信息脱敏 + regression suite + strict-alternation 评估 + 多 session PoC + 经验导入导出 + 任务调度

> **规划依据**：13 节"Hermes↔Spirit 架构差距分析"系统对比 `hermes-agent-main/agent/` (244 文件) vs `spirit-agent-main/spirit/agent/` (28 文件)，找出 50+ 关键架构模块缺口，按"依赖关系 + 实施难度 + 价值密度"排入 v0.3.0 之后到 v0.5.0+ 各个版本。

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
| **v0.4.0** | T+18 周 | 进度推送 + HITL 面板 + BRS 接线 | 桌宠 demo + evolution 真实跳 |
| **v0.5.0** | T+26 周 | 成效 harness + 领域验证器 + 日报 + 经验确认 + wave 可视化 + 任务历史 + 摘要 + 状态可视化 | 完整 V3 验证 |
| **v0.5.1** | T+28 周 | R-P4.1 MoA + R-P4.2 上下文引擎 | 4 MoA 拓扑 + SPI 替换 |
| **v0.5.2** | T+30 周 | R-P4.3 lifecycle + R-P4.4 shell hooks | 4 场景 lifecycle + first-use HITL |
| **v0.5.3** | T+32 周 | R-P4.5 后台 review | 跨 session 评估报告 |
| **v0.5.4** | T+34 周 | R-P4.6 Relay 架构 | 跨进程 LLM / tool 路由 |
| **v0.5.5** | T+36 周 | R-P4.7 Provider adapter 矩阵 | 6 provider 各 30+ 测试 |
| **V3.0.0** | T+44 周 | 消息清理 + 脱敏 + regression + strict-alternation 评估 + 多 session + 经验 I/O + 任务调度 | V3 大版本发布 |

> T = 2026-10-09（V3 规划启动）

---

## 10. 复盘节点

| 节点 | 复盘内容 | 决策 |
|------|----------|------|
| **T+6 周（v0.2.3 发版前）** | 错误恢复是否有效？AgentBench 回归是否达标？ | 是 → 继续 v0.3.0；否 → 加 1 周调试 |
| **T+14 周（v0.4.0 发版前）** | 桌宠进度推送是否打扰？BRS 是否真接线？ | 是 → 继续 v0.5.0；否 → 砍非核心 |
| **T+22 周（v0.5.0 发版前）** | 成效 harness 是否出数字？日报是否有用？ | 是 → 继续 V3.0.0；否 → 砍日报 / 砍 harness |
| **T+33 周（V3.0.0 发版前）** | P3 功能是否都值得做？ | 全部 → 发版；部分 → 砍 |

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
|------|------|----|----|-----|
| V3 P1（v0.2.4-v0.4.0）| 8 | 6 | 1（C-P1.2）| 1（M-P1.1）|
| V3 P2（v0.3.0-v0.5.0）| 9 + 7 = 16 | 0 | 0 | 16 |
| V3 P3（V3.0.0）| 7 + 7 = 14 | 0 | 0 | 14 |
| **合计** | **38** | **6** | **1** | **31** |

### 12.5 整体统计（V2 + V3）

| 维度 | 总项 | ✅ | ⏳ | ❌ |
|------|------|----|----|-----|
| V2 Phase 1-7 子项 | 41 | 41 | 0 | 0 |
| V2 仍待补完（低优先级）| 4 大类 | 0 | 0 | 4 |
| V3 P1（v0.2.4-v0.4.0）| 8 | 6 | 1（C-P1.2）| 1（M-P1.1）|
| V3 P2（v0.3.0 + v0.3.0 补完 + v0.5.0）| 9 + 7 = 16 | 0 | 0 | 16 |
| V3 P3（V3.0.0 + v0.5.0+ 长期）| 7 + 7 = 14 | 0 | 0 | 14 |
| **合计** | **83** | **47** | **1** | **35** |

**关键里程碑**：
- V2 七大阶段全部完成 → 41/41 子项 + 2,698+ 测试全通 + 35,761 行后端代码
- v0.2.4 R/M 支柱 P1 全收官 → C 支柱进度仅 C-P1.1 完成
- 当前 v0.4.0：C-P1.2 进行中（核心骨架 ✅，子任务待做） / M-P1.1 待开始
- v0.3.0 / v0.5.0 / V3.0.0 全部 P2/P3 项均未开始
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
