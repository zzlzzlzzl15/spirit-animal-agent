# Spirit Agent 自进化能力设计（RSI · 参考 RSIAgent）

> **最后更新**: 2026-09-28
> **状态**: ✅ **全部完成** · **Phase A–F（6.A–6.F）均已落地**（`spirit/evolution/`：14 文件 + **214 测试全通**，2026-09-28；全量回归 2654 passed/54 skipped/1 预存环境失败）· **已在主计划 [07-development-checklist.md](./07-development-checklist.md) 登记为 Phase 6（本文 Phase A–F 即 6.A–6.F）**
> **参考框架**: [RSIAgent](../../RSIAgent/)（已 clone 到仓库根目录，Apache-2.0，arXiv:2609.15364）
> **关联文档**: [00-product-vision-v2.md](./00-product-vision-v2.md) · [03-spirit-agent-architecture.md](./03-spirit-agent-architecture.md) · [06-implementation-roadmap.md](./06-implementation-roadmap.md) · [07-development-checklist.md](./07-development-checklist.md)

---

## 一、背景与目标

### 1.1 用户诉求

希望 Spirit 具备**领域内自主进化**能力，具体表现为：

1. **自主探索**：在某个领域（如金融量化，仅为示例，框架需领域无关）自己收集信息、探索环境；
2. **自我构建任务**：不依赖人工派活，Agent 自己决定"下一步该学什么、练什么"；
3. **自我进化**：把探索经验沉淀为可复用知识，越用越强；
4. **可选人在环（HITL）**：
   - **无反馈时** → Agent 闷头自主进化，不打扰用户；
   - **需要拍板时**（选路线 / 重大决策 / 低置信度）→ 主动询问用户，并把用户建议纳入后续决策。

### 1.2 为什么选 RSIAgent 范式

RSIAgent（Aether AI + UCSD + UIC，2026-09）与上述诉求高度吻合：

| 特性 | 说明 | 对本项目的价值 |
|------|------|--------------|
| **Training-free** | 不微调、不 RL，模型权重全程冻结 | ✅ **无需 GPU**，用现有远程 API（Qwen/MiniMax）即可跑 |
| **三角色闭环** | Curriculum（派任务）+ Actor（执行）+ Verifier（校验） | 直接对应"自我构建任务 + 自主探索 + 防自嗨" |
| **两阶段探索** | BRS 广撒网 → DRS 深挖硬骨头 | 对应"先摸清领域、再攻坚难点" |
| **冻结记忆复用** | 经验沉淀进 Memory 后冻结，下游直接复用 | 对应"越用越强"的进化本质 |
| **模型无关** | 走 OpenRouter 式 transport，换 API 即可 | 复用 Spirit 已有的 ProviderProfile 注册表 |

> **实测背书**：RSIAgent 在 OSWorld 2.0 上 partial-credit 从 71.97 → **78.98**（+7.01），反超 GPT-6 Astra；ALE 83.75 → **84.82**。**同一 harness、同一权重**，仅靠 RSI 闭环即获增益——证明"进化外壳而非权重"路线可行。

### 1.3 核心认知（务必对齐）

- **记忆 ≠ 进化**：记忆是名词（存下来），进化是动词（把存下来的变成下次更好的自己）。判断真进化看三点：①做完主动**复盘**？②复盘抽象成**跨任务洞察**？③下次真被**读回改写决策**？
- **进化优先发生在 harness，而非 weights**：改 prompt / 工具路由 / memory / 技能注册表，比改权重更便宜、更易回滚。本项目走 harness 级进化。
- **反思必须接外部验证器**：无外部反馈的纯自我反思会把对的改错（"LLMs Cannot Self-Correct Reasoning Yet", ICLR'24：GSM8K 95.5%→89.0%）。Verifier 是进化的安全带，**不可省**。

---

## 二、RSIAgent 架构精要（移植蓝本）

### 2.1 三角色职责（来自 `RSIAgent/docs/ARCHITECTURE.md`）

| 角色 | 输入与权限 | 关键约束 |
|------|-----------|---------|
| **Actor Agent** | 操作环境、读自己的持久记忆、蒸馏自己已验证的经验 | 执行 + 学习合一 |
| **Verifier Agent** | 独立检查候选证据，跨同一任务的多次修订保持自己的上下文；给出 PASS / FAIL / UNVERIFIED | **看不到 Actor 的私有推理与记忆**（防作弊）；自身对环境的改动会被回滚 |
| **Curriculum Agent** | 复盘学习进度，选下一个练习或目标任务；决定是否继续练习 | **不打分、不写 Actor 的记忆**；记忆视图默认只读 |
| **Host Harness** | 强制隔离、记录产物、在合法边界提交记忆、调用密封官方评测器 | 评测分数**留在闭环之外** |

### 2.2 三阶段生命周期（来自 `RSIAgent/core/self_evolving_loop.py`）

```
Phase 1 · BRS（Broad Recursive Self-exploration，广度）
  Curriculum 出一批多样化练习项目 → 多个 Actor 从"同一份 pre-wave 记忆快照"并行执行
  → Verifier 并行校验 → 全部通过后，Actor 按序串行蒸馏经验（wave memory barrier）
  → 记忆提交持久化后，才开下一 wave。未完成的分支阻塞整个 wave，其记忆不合并。

Phase 2 · DRS（Deep Recursive Self-exploration，深度）
  先真刀真枪试一个"目标任务" → PASS/FAIL 都能学习
  → FAIL：同一 Actor 蒸馏 diagnosis+memory → Curriculum 决定"再练 or 就绪"
  → 若练习：practice 的 Actor→Verifier 内循环 → 每条已验证经验即时更新记忆
  → 练习后必须在"未改动的目标任务"上重新尝试，才算自然完成
  停止策略：curriculum_review（默认，PASS 后仍可要求再练） / verifier_pass（PASS 即停）

Phase 3 · Test-time 记忆复用
  记忆冻结快照 → 关闭 Curriculum 与记忆更新 → Actor 用冻结记忆执行任务
  → 仍走同一套 action-verification 循环 → 最后跑密封官方评测器（分数不回流学习）
```

### 2.3 关键状态与数据结构

| 类型 | 取值 | 含义 |
|------|------|------|
| `TargetVerdict` | PASS / FAIL / UNVERIFIED | Verifier 对目标任务的裁决 |
| `EvolutionStatus` | READY_FOR_RETRY / STALLED | 进化后是否可重试 / 是否卡死 |
| `ActorLearning` | memory + diagnosis | Actor 蒸馏产物（诊断必须非空） |
| `EvolutionResult` | status + memory + projects + reason | 一轮进化结果 |
| `SelfEvolvingStart` | target_cycles / evolutions / practice_projects | 计数器（预算与停止判定用） |

> **铁律**：`STALLED` 与预算耗尽必须与"成功收敛"可区分；基础设施错误**不得**被当成 PASS/FAIL，也不得被当成语义化的 Curriculum 决策。

---

## 三、RSIAgent → Spirit 架构映射

Spirit 已有大量可复用地基，**不从零造**。映射如下：

| RSIAgent 组件 | Spirit 对应 / 复用点 | 状态 | 差距 |
|--------------|---------------------|:---:|------|
| Actor Agent（执行） | `spirit/agent/`（ReAct 主循环 + 工具全套） | ✅ 有 | 包一层"探索角色"prompt |
| Verifier Agent（校验） | 新建 `spirit/evolution/verifier.py` | ❌ 无 | **独立上下文 + 证据校验**，隔离 Actor 私有记忆 |
| Curriculum Agent（派任务） | 新建 `spirit/evolution/curriculum.py`；可借 `spirit/goals/judge.py` 的裁决范式 | ❌ 无 | 出练习/目标任务 + 停止判定 |
| 自进化状态机 | 新建 `spirit/evolution/loop.py`（对标 `core/self_evolving_loop.py`） | ❌ 无 | Phase1/2/3 生命周期 |
| 持久记忆（进化态） | `spirit/skills_hub/`（技能库）+ `memora/memory_manager`（长期记忆） | 🔶 半 | 加"洞察库"结构（带 confidence+source） |
| 冻结记忆快照 | 借 `RSIAgent/explore/memory_hash.py` 思路 | ❌ 无 | 记忆哈希 + 快照冻结 |
| 续跑/恢复 | `spirit/goals/`（Ralph Loop，已有 judge→continue→resume） | ✅ 有 | 直接复用其 store/loop 范式 |
| 每日定时触发 | `spirit/tools/extra_tools.py` 的 `cronjob`（仅空壳） | ⚠️ 30% | **需补调度引擎**（见 roadmap） |
| 模型 transport | `spirit/providers/`（ProviderProfile 注册表）+ `agent/transports/` | ✅ 有 | 三角色各配一个 model slot |
| HITL 询问-恢复 | `spirit/gateway/`（澄清队列）+ `goals/` 中断恢复 | 🔶 半 | 加"低置信度触发询问"钩子 |
| 领域环境（示例金融） | 新建 `spirit/evolution/domains/`（领域适配器） | ❌ 无 | 领域无关接口 + 金融示例实现 |

> **移植原则**：只搬 RSIAgent 的**闭环机制与角色协议**（`core/` + `explore/`），**不搬**其 VM 基准（`benchmarks/osworld`、`benchmarks/ale` 需 Linux+Docker+KVM+167GB 镜像，与本项目无关）。Spirit 的"环境"就是它自己的工具集 + 领域适配器。

---

## 四、目标拆解（分阶段实施）

> 采用渐进式：先跑通最小闭环，再加广度/深度/HITL/领域化。每阶段可独立验收。

### Phase A · 地基与角色协议（1-2 周）

**目标**：搭出 `spirit/evolution/` 骨架，三角色能通过 Spirit 的 transport 各跑一次 LLM 调用。

- [x] A1 新建 `spirit/evolution/` 包（`__init__.py` + `README`）
- [x] A2 定义角色协议数据类（对标 RSIAgent）：`TargetVerdict` / `EvolutionStatus` / `ActorLearning` / `EvolutionResult`（+ `Verdict` / `CurriculumDecision` / `RoleName` / `SelfEvolvingStart`）→ `protocol.py`
- [x] A3 `spirit/evolution/roles.py`：三角色的 prompt 模板 + model slot 解析（复用 `providers` + `transports`；注入式 `llm_caller` 复用 `goals.judge.LLMCaller` 签名）
- [x] A4 `spirit/evolution/memory.py`：进化记忆结构（见 §六，三层：轨迹池/洞察库/技能库），落盘到 `SPIRIT_HOME/evolution_memory/`
- [x] A5 单元测试：三角色能对合成输入产出合法结构（无网络，mock LLM）

**验收**：✅ `pytest tests/evolution/` 全绿（77 项）；三角色 prompt 能解析出结构化输出，无 caller/解析失败时 fail-soft（Verifier→UNVERIFIED、Curriculum→STALLED）。

### Phase B · Verifier 独立校验闭环（1-2 周）

**目标**：Actor 执行 → Verifier 独立校验 → 产出 grounded verdict，杜绝"自嗨"。

- [x] B1 `spirit/evolution/verifier.py`：独立上下文，**屏蔽 Actor 私有推理/记忆**（`Evidence` 结构性地不承载私有推理字段），只看“任务要求 + 候选证据（环境状态/日志/产物）”
- [x] B2 证据采集：`Evidence` 归拢命令退出码 / 日志 / 文件快照 / 产物 / 环境状态 / 领域客观指标作为 candidate evidence
- [x] B3 Verifier 输出 PASS/FAIL/UNVERIFIED + 理由（理由必须非空，`_ensure_reason` 兜底）
- [x] B4 隔离与回滚：探针（`probe`）对环境的试探性改动在校验后经 `spirit/checkpoint` 快照回滚（复用而非重写）
- [x] B5 **领域验证器接口**：`DomainVerifier.verify(task, evidence) -> Optional[Verdict]` + 确定性验证器（`ExitCodeVerifier`/`FileContentVerifier`/`PatternVerifier`/`MetricThresholdVerifier`）；**客观信号 > 模型自评**，无法客观裁决时才回退 LLM

**验收**：✅ `pytest tests/evolution/test_verifier.py` 全绿（30 项）；成功/失败任务分别稳定判 PASS/FAIL（客观验证器）；基础设施异常（探针报错/无 caller）判 UNVERIFIED（不误判为 FAIL）；探针改动被回滚。

### Phase C · Curriculum 自派任务 + 单阶段进化循环（2 周）

**目标**：跑通"Actor 试目标 → Verifier 校验 → FAIL 则 Actor 蒸馏 → Curriculum 决定练/就绪 → 重试"的最小自进化闭环（对标 RSIAgent Phase 2 / DRS）。

- [x] C1 `spirit/evolution/curriculum.py`：复盘学习进度 → 出下一个练习/目标任务 + 继续/停止决策（借 `goals/judge.py` 的裁决范式）
- [x] C2 `spirit/evolution/loop.py`：DRS 状态机（target attempt → verify → learn → practice → retry），含 `curriculum_review` / `verifier_pass` 两种停止策略
- [x] C3 记忆提交边界：仅在"已验证经验"处提交记忆（对标 wave memory barrier）
- [x] C4 预算与 STALLED 判定：`SelfEvolvingStart` 计数器 + 预算耗尽/卡死与"成功收敛"可区分
- [x] C5 与 `spirit/goals/` 打通：`EvolutionLoop.state()/restore()` 导出可序列化状态，供外层持久化到 SessionDB 以 `/resume` 续跑

**验收**：✅ `pytest tests/evolution/test_curriculum.py tests/evolution/test_loop.py` 全绿（31 项）；在可重复玩具领域任务上闭环能自主迭代 N 轮，记忆单调增长，"卡死"被正确标记为 STALLED 而非假成功。

### Phase D · 广度探索 BRS + 记忆冻结复用（2 周）

**目标**：补齐 Phase 1（并行广度）与 Phase 3（冻结记忆测试时复用）。

- [x] D1 `spirit/evolution/phase1_wave.py`：Curriculum 出一批多样化项目 → 多 Actor 从同一 pre-wave 快照并行执行 → 全部校验通过后按序串行蒸馏
- [x] D2 wave memory barrier：未完成分支阻塞整 wave，其记忆不合并（`Phase1Wave.run`：任一分支非 PASS → `merged=False`，主记忆零写入）
- [x] D3 `spirit/evolution/memory_hash.py`：记忆哈希（sha256）+ 冻结快照（manifest）+ 校验（`MemorySnapshot.verify` 可检测篡改）
- [x] D4 Phase 3 测试时复用：`FrozenMemory` 只读门面（关闭记忆更新，写操作一律拒绝）+ 快照哈希可校验
- [x] D5 技能库层已就绪：`memory.SkillEntry` + `add_skill`（带 `source_insight_ids` 溯源）可固化稳定经验；结晶进 `skills_hub` 的桥接为可选后续

**验收**：✅ `pytest tests/evolution/test_memory_hash.py tests/evolution/test_phase1_wave.py` 全绿（38 项）；BRS 一轮能并行产出多条经验并正确按序合并（任一失败则阻塞）；冻结记忆快照在 Phase 3 被只读复用，哈希可校验（篡改即失效）。

### Phase E · 可选人在环 HITL（1-2 周）

**目标**：实现"无反馈自跑、需拍板才问、且采纳用户建议"。

- [x] E1 触发条件：Curriculum/Verifier 输出**低置信度**、或遇到**路线分叉/重大决策**时，标记 `need_human`（`HumanInTheLoop.should_ask`）
- [x] E2 询问通道：注入式 `ask` seam（`(query, timeout) -> Optional[str]`），可桥接 `spirit/gateway/` 澄清队列推送给用户活跃渠道
- [x] E3 阻塞-恢复：无响应/超时/异常则**fail-open 自主决策并继续**（`consult` 取首选项或注入 `auto_decide`，不卡死）
- [x] E4 建议持久化：用户路线选择写入记忆（`add_insight` 带 `tags=[user,hitl]`、confidence=0.95），后续决策优先参考
- [x] E5 配置开关：`evolution.hitl.mode = auto | always | never`（`HITLMode` + `resolve_mode`，对标 AutoGen human_input_mode 语义）

**验收**：✅ `pytest tests/evolution/test_hitl.py` 全绿（20 项）；`never` 全程不打扰；`auto` 仅低置信度/分叉点询问且超时自主继续；用户建议在后续轮次被读回并影响决策。

### Phase F · 领域化 + 每日定时自主运行（1-2 周）

**目标**：把闭环接到具体领域（金融量化为示例）并每日自动跑。

- [x] F1 `spirit/evolution/domains/base.py`：领域无关接口（`collect_info` / `propose_tasks` / `verify` / `reward_signal`，均 fail-soft 默认实现）
- [x] F2 `spirit/evolution/domains/finance.py`：金融示例——信息收集（注入式行情/新闻数据源）、任务生成（因子/策略探索）、验证器（回测夏普阈值，复用 `MetricThresholdVerifier`）
- [x] F3 **补全调度引擎**：`spirit/evolution/scheduler.py`（interval/daily 计划 + `tick(now)` 到点执行 + 注入式时钟，不再依赖 `cron/` 空壳）
- [x] F4 每日任务：`add_daily_task(at="09:00")` 定时触发一轮进化 + 收集领域信息 + `build_report` 生成日报经注入式 `push` 投递给用户
- [x] F5 可观测：进化轨迹（`EvolutionLoop.history`）/记忆增长（`memory.stats`）/波次结果（`WaveResult.to_dict`）/调度日报（`TickReport.to_dict`）均可结构化导出；dashboard 可视化为可选后续

**验收**：✅ `pytest tests/evolution/test_domains.py tests/evolution/test_scheduler.py` 全绿（42 项）；金融领域回测夏普 ≥ 阈值稳定判 PASS、低于阈值判 FAIL、无指标判 UNVERIFIED；调度器能按 interval/daily 到点执行、生成日报、单任务异常不影响其它。

---

## 五、新增模块结构

```
spirit/evolution/
├── __init__.py
├── roles.py              # 三角色 prompt 模板 + model slot 解析（复用 providers/transports）
├── protocol.py           # TargetVerdict/EvolutionStatus/ActorLearning/EvolutionResult 数据类
├── verifier.py           # Verifier：独立上下文 + 证据校验 + 隔离回滚
├── curriculum.py         # Curriculum：出任务 + 继续/停止决策（借 goals/judge 范式）
├── loop.py               # DRS 自进化状态机（对标 core/self_evolving_loop.py）
├── phase1_wave.py        # BRS 广度并行探索 + wave memory barrier
├── memory.py             # 进化记忆（轨迹池/洞察库/技能库三层）
├── memory_hash.py        # 记忆哈希 + 冻结快照
├── hitl.py               # 人在环：触发判定 + 询问 + 超时自主 + 建议回灌
├── scheduler.py          # 每日定时自主进化触发（补全 cron 空壳）
└── domains/
    ├── base.py           # 领域无关接口
    └── finance.py        # 金融量化示例（信息收集/任务生成/回测验证）
```

**复用而非重写的现有模块**：
- `spirit/agent/` → Actor 的执行内核（ReAct + 工具）
- `spirit/goals/` → 续跑/恢复/judge 裁决范式（`loop.py`/`judge.py`/`store.py`）
- `spirit/skills_hub/` → 技能库（经验结晶为可复用 skill）
- `spirit/providers/` + `spirit/agent/transports/` → 三角色的模型调用
- `spirit/gateway/` → HITL 询问推送渠道
- `memora` / `memory_manager` → 长期记忆底座

---

## 六、记忆与验证器设计（进化质量的核心）

### 6.1 三层记忆分离（工程铁律，勿混用）

| 层 | 内容 | 关键字段 | 类比 |
|----|------|---------|------|
| **轨迹池** | 原始探索经验（动作-观察序列） | task_id, timestamp | 原材料 |
| **洞察库** | 跨任务可复用的抽象结论 | **confidence**, **source_task_id**, status(active/deprecated) | 提炼物 |
| **技能库** | 固化的可执行解法（复用 `skills_hub`） | name, trigger, code/procedure | 成品 |

> **洞察库必须带 `confidence` + `source_task_id`**：没置信度无法"被证伪就沉底"；没来源出了事故查不到根。可观测性是自进化的安全带。

### 6.2 验证器接地（防空转）

- Verifier **独立上下文**，看不到 Actor 私有推理/记忆，只依据**客观证据**（环境状态、日志、产物、回测结果）裁决。
- 领域验证器优先级：**客观信号 > 模型自评**。金融用回测/数据校验；代码用测试（Spirit 已有 SWE-bench 思路）。
- 基础设施异常 → `UNVERIFIED`（不计入学习，不当 FAIL）。

### 6.3 记忆单调性与回滚

- 仅在"已验证经验"边界提交记忆；未通过校验的分支记忆不合并。
- 冻结快照 + 哈希，保证 Phase 3 复用的是确定版本，可审计、可回滚。

---

## 七、人在环（HITL）行为契约

| 场景 | 行为 |
|------|------|
| 正常探索、置信度充足 | **不打扰**，自主进化 |
| 路线分叉 / 重大决策 / 低置信度 | **主动询问**用户（推送到活跃渠道），附带选项与利弊 |
| 用户给出反馈/选择 | 注入 Curriculum 上下文，写入记忆（source=user），后续优先参考 |
| 询问后超时无响应 | **fail-open**：按当前最优自主决策并继续，不卡死；事后可在日报中说明 |
| `hitl.mode=never` | 全程自主，永不询问 |
| `hitl.mode=always` | 每个关键节点都询问（调试/高管控场景） |

> 复用 `spirit/goals/` 的中断-恢复机制与 `spirit/gateway/` 的澄清队列，避免另造一套。

---

## 八、里程碑与验收总览

| 阶段 | 交付 | 核心验收 | 依赖 |
|------|------|---------|------|
| A | 角色协议骨架 ✅ 完成（77 测试）| 三角色产出合法结构（mock LLM） | 无 |
| B | Verifier 闭环 ✅ 完成（30 测试）| 稳定判 PASS/FAIL，异常判 UNVERIFIED | A |
| C | DRS 单阶段进化 ✅ 完成（31 测试）| 自主迭代 N 轮，STALLED 可区分 | A,B |
| D | BRS + 冻结复用 ✅ 完成（38 测试）| 并行经验按序合并，快照哈希可校验 | C |
| E | 可选 HITL ✅ 完成（20 测试）| never/auto 行为正确，建议被回灌 | C |
| F | 领域化 + 每日定时 ✅ 完成（42 测试）| 金融示例回测裁决 + 调度日报 | C,D,E |

---

## 九、风险与边界

1. **进化质量三限**（RSIAgent 论文失败分析）：练习可能没打中弱点、校验可能放过不完整结果、记忆可能固化错误规则。→ 对策：Verifier 接地 + 洞察库 confidence 衰减 + 定期人工抽检。
2. **成本**：三角色 × 多轮 × 并行 = LLM 调用量大。→ 对策：预算计数器（`SelfEvolvingStart`）+ 三角色可用不同档位模型（如 Actor 用强模型、Verifier/Curriculum 用便宜模型）。
3. **cron 调度是前置依赖**：当前 `cronjob` 仅空壳，Phase F 前必须补全调度引擎（见 roadmap）。
4. **不碰权重**：本项目是 harness 级进化，**不涉及模型训练**，无需 GPU。若未来要权重级 RL，另见 Agent Lightning 路线（需 Linux + 大显存）。
5. **领域验证器是成败关键**：没有可靠外部验证器的领域，自进化易空转。接入新领域前先确认其 `reward_signal` 可客观计算。

---

## 十、参考

- **RSIAgent 仓库**：[`../../RSIAgent/`](../../RSIAgent/)（本地已 clone）
  - 架构：`RSIAgent/docs/ARCHITECTURE.md`
  - 自进化状态机：`RSIAgent/core/self_evolving_loop.py`
  - Curriculum：`RSIAgent/explore/charter.py` · 记忆哈希：`RSIAgent/explore/memory_hash.py`
  - 广度探索：`RSIAgent/explore/phase1_wave.py` · 深度：`RSIAgent/explore/phase2_recovery.py`
- **论文**：RSIAgent: Autonomous Exploration for Recursive Self-improvement in New Environments, arXiv:2609.15364
- **延伸范式**：Voyager（技能库+自动课程）· Reflexion（语言反思）· ExpeL（经验学习）· Agent0（课程-执行对抗）
- **综述**：A Survey of Self-Evolving Agents, arXiv:2507.21046 · Awesome-RSI: `github.com/lobehub/awesome-rsi`
