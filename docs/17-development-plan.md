# 17 · Spirit Agent V3 开发规划

> **目标读者**：Spirit 维护者、贡献者、Release Manager
> **生效版本**：V0.2.3 → V3
> **状态**：草案 v1 · 2026-10-09
> **前置文档**：
> - [15-product-roadmap-v3.md](./15-product-roadmap-v3.md)（V3 产品规划）
> - [16-feature-roadmap.md](./16-feature-roadmap.md)（V3 功能规划）
> **核心约束**（来自用户明确约定）：
> 1. **不擅自对 Agent 项目做升级改动**——只有用户明确要求某功能/修复时才动源码
> 2. **任务收尾不在项目里新增文件**（规划文档除外）+ 不跑全量 pytest
> 3. **验证方式应轻量**——只跑直接相关的少量测试 + 一次性脚本
> 4. **Spirit 桌宠 Agent 不自改源码自我升级**（与 Phase 7 沙箱约束一致）

---

## 0. 开发规划总览

V3 共 25 项功能，分布如下（口径与 16-feature-roadmap.md 一致；M-P1.0 跨 R/M 支柱归在 P1）：

| 优先级 | 项数 | 估算总工时 | 完成节点 |
|--------|------|------------|----------|
| **P1（必做）** | 9 | ~13 周 | v0.2.3 - v0.4.0 |
| **P2（应该做）** | 9 | ~11 周 | v0.3.0 - v0.5.0 |
| **P3（可探索）** | 7 | ~9 周 | V3.0.0 |

**总投入估算**：~33 周（按 1 人 FTE，6-8 个月）

**关键节点**：
- **v0.2.3**（6 周）：错误恢复链 + 工具结果分类 + 并发超时修复 + provider 切换保护 + 上下文压缩鲁棒性 v2 + 准确率归因与提升
- **v0.3.0**（3 周）：截断多策略 + 活性检测 + provider 切换保护 v2 + 动态 prompt 组成
- **v0.4.0**（5 周）：桌宠进度推送 + HITL 面板 + BRS 接线
- **v0.5.0**（8 周）：成效 harness + 领域验证器 + 日报 + 经验确认 + wave 可视化 + 任务历史 + 摘要 + 状态可视化
- **V3.0.0**（11 周）：消息清理 + 敏感信息脱敏 + regression suite + strict-alternation 评估 + 多 session PoC + 经验导入导出 + 任务调度

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

| 编号 | 功能 | 工时 | 验证方式 |
|------|------|------|----------|
| R-P2.1 | 截断多策略 | 1 周 | 思考模型截断事故 + 单测 3 类策略 |
| R-P2.2 | 活性检测 | 0.5 周 | 长任务流式中断 + 单测 3 类 probe |
| R-P2.3 | provider 切换保护 v2 | 0.5 周 | 跨 provider 切换 + 单测 |
| R-P2.4 | 动态 prompt 组成 | 1 周 | 4 类任务 prompt 切换 + 单测 |

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
| **v0.4.0** | T+14 周 | 进度推送 + HITL 面板 + BRS 接线 | 桌宠 demo + evolution 真实跑 |
| **v0.5.0** | T+22 周 | 成效 harness + 领域验证器 + 日报 + 经验确认 + wave 可视化 + 任务历史 + 摘要 + 状态可视化 | 完整 V3 验证 |
| **V3.0.0** | T+33 周 | 消息清理 + 脱敏 + regression + strict-alternation 评估 + 多 session + 经验 I/O + 任务调度 | V3 大版本发布 |

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
