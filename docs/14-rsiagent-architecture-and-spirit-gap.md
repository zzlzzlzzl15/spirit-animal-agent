# 14 · RSIAgent 自动化规划架构解析 × Spirit 差距评估

> 本文回答三个问题：
> 1. **RSIAgent 到底是什么架构，它真能实现「AI 自动化规划」吗？**（§2、§5）
> 2. **我现在的 Spirit 离它有多远？**（§3、§4）
> 3. **在守住项目铁律的前提下，最高杠杆的补齐动作是什么？**（§6）
>
> 依据来源：`RSIAgent/docs/ARCHITECTURE.md`、`RSIAgent/docs/PAPER.md`、`RSIAgent/README.md`（arXiv:2609.15364）；
> 本地 `spirit/evolution/`（README + curriculum.py + verifier.py）、`spirit/autonomy/`（explorer.py + integration.py + bridge.py）、`docs/13-self-evolution-rsi.md`。
> 所有关于 Spirit 的接线论断均以 `Grep` 实测为准（见 §4.3）。

---

## 0. 摘要（TL;DR）

- **RSIAgent 确实实现了「自动化规划」，但要精确理解它是哪一种规划**：它不是通用目标规划器（不是「给一句人生目标就自己拆解人生」），而是**自动化课程规划 + 接地式递归自我改进（RSI）**——Curriculum Agent 自己构造信息量大的探索/练习任务、Actor 在**真实虚拟机环境**里操作、Verifier 用**密封官方评测器**接地裁决、经验蒸馏进可复用记忆。**同一冻结权重下**，OSWorld 2.0 partial-credit 从 71.97 → 78.98、ALE Near-term 83.75 → 84.82。**这是有实证的。**
- **但证据口径必须打折看**：RSI 那一列**并非全部重新实测**——OSWorld 只用了 41 条 RSI 实测（其余 41 条保留 baseline），ALE 只用 19 条实测（其余 48 条保留 baseline），且含选择性重试、不同预算 checkpoint、本地重评。作者原话：这**不是匹配协议的打榜提交**。增益是真实的，但**温和且口径混杂**。
- **Spirit 与 RSIAgent 的差距，不在「机制」而在「接地」**：Spirit 的 `evolution/` 已经**忠实复刻**了三角色闭环、DRS 状态机、BRS wave barrier、记忆冻结、HITL、领域化调度（14 文件 / 214 测试全绿）。**机制层基本对齐。**
- **决定性差距是 GROUNDING（接地层）**：RSIAgent 有真 VM guest + 视觉观察 + **密封官方评测器**给出客观分数；Spirit 只有沙箱执行 + 文件 diff + 退出码 + 领域指标 + LLM 回退，**没有密封评测器、没有客观成效度量**。这直接导致 RSIAgent 论文自陈的三大失败模式之一——**「校验放过不完整结果」——在 Spirit 上被放大**：没有客观裁判，自我改进就无法被证明真的改进了。
- **操作化差距**：BRS（`Phase1Wave`）和 Phase 3（`FrozenMemory`）在 Spirit 里**代码存在、单元测试通过，但没接进常驻自主循环**（`autonomy/` 只引用了 `EvolutionLoop`=DRS）。也就是说 Spirit 现在实际只跑「单目标深挖」，广度并行探索和测试时冻结复用**尚未上线**。
- **Spirit 反超 RSIAgent 的地方**：常驻不停机循环、运行时「给方向」入口（桌宠输入框 + WS 命令）、沙箱写护栏、HITL 气泡提醒、桌宠 UX。**RSIAgent 本质是一个批量 benchmark runner，Spirit 是一个活着的桌面伴侣。** 二者定位不同。
- **一句话结论**：**Spirit 已经把 RSIAgent 的「大脑结构」搬过来了，缺的是「考场和裁判」。** 最高杠杆不是再加循环机制，而是**给用户在意的领域配一个可靠的客观验证器 + 一套能出成效数字的度量 harness**。

---

## 1. 先厘清三个极易混淆的概念

工作区里有三套「自我改进」相关的东西，名字都像，**但完全不是一回事**。混为一谈会导致错误的路线决策。

| 系统 | 出处 | 改什么 | 与 Spirit 的关系 | 用户态度 |
|------|------|--------|------------------|----------|
| **RSIAgent** | arXiv:2609.15364（Aether AI + UCSD + UIC） | **不改源码、不碰权重**；靠三角色闭环在环境里探索、把经验蒸馏进记忆 | **Spirit `evolution/` 就是对标它建的**；也是本次「自动化规划」问题的主角 | 用户想学它 |
| **RRSI** | arXiv:2609.24972（Google Research） | **改 harness 源码**（prompt/控制流/工具/记忆）；外层一拨 Claude 改代码，内层冻结 policy 跑任务 | 工作区 `rrsi/` 仓；**是另一条技术路线** | **用户此前明确拒绝过**（「不让 Agent 自改源码自我升级」） |
| **Spirit autonomy/evolution** | 本地 `spirit/` | 对标 RSIAgent 的本地实现，training-free、无需 GPU、harness 级 | 就是「我的 Spirit」 | 用户的项目 |

> ⚠️ **关键区分**：RSIAgent ≠ RRSI。
> - RSIAgent = **让 Agent 在环境里练习、积累经验**（经验进记忆，代码不变）。
> - RRSI = **让 Agent 改自己的 harness 源码**（代码本身被进化）。
> 用户任务里问的「rsiagent 这个自动化规划」指的是**前者**。Spirit 走的也是**前者**的路线，这与用户「不许自改源码」的铁律天然一致。

---

## 2. RSIAgent 架构精要

### 2.1 定位

**RSIAgent: Autonomous Exploration for Recursive Self-improvement in New Environments**（ authors: Sibo Zhu, Shicheng Fan, Xinyue Wang, Wenyi Wu, Kun Zhou, Biwei Huang）。

- **training-free**：不微调、不碰模型权重，同一冻结 LLM 全程。
- **递归自我改进（RSI）**：靠「探索 → 校验 → 蒸馏经验 → 复用记忆」的闭环，让 Agent 在**陌生环境**里越做越好。
- **本质是一个带严格隔离与接地裁决的 benchmark runner**：跑 OSWorld 2.0（108 任务）与 Agents' Last Exam（ALE，67 Near-term 任务）。

### 2.2 三角色 + Host（职责逐条对齐 `ARCHITECTURE.md`）

```
        ┌─────────────┐   派练习/目标任务    ┌─────────────┐
        │ Curriculum  │ ───────────────────▶ │    Actor    │
        │   Agent     │                      │    Agent    │
        │（规划/复盘）│ ◀─────────────────── │（执行/蒸馏）│
        └─────────────┘   学习进度/就绪信号   └──────┬──────┘
               ▲                                    │ 候选产物
               │ 裁决反馈                            ▼
               │                             ┌─────────────┐
               └──────────────────────────── │  Verifier   │
                  PASS / FAIL / unresolved    │    Agent    │
                                              │（独立校验） │
                                              └─────────────┘
        ────────────────── Host harness（强制隔离 · 记录产物 · 合法边界提交记忆 · 调用密封官方评测器）──────────────────
```

| 角色 | 输入与权限（原文口径） | 关键约束 |
|------|------------------------|----------|
| **Actor Agent** | 操作 guest（真 VM）、读取自己的持久记忆、蒸馏自己**已验证**的经验 | 执行 + 学习合一 |
| **Verifier Agent** | 用**自己独立的持久上下文**检查候选证据（跨同一任务的多次修订）；给 PASS / FAIL / unresolved | **看不到 Actor 私有推理/产物**；它对 guest 的改动在 Actor 恢复前**被回滚**（但不回滚共享远端服务） |
| **Curriculum Agent** | 复盘学习进度、选下一个练习/目标任务 | **不打分、不替 Actor 写记忆**；默认记忆视图**只读** |
| **Host harness** | 强制隔离、记录产物、在合法边界提交记忆、**调用密封官方评测器** | 评测输出是 host 产物，不回流学习 |

> 这套「Actor 执行 / Verifier 独立接地 / Curriculum 只规划不打分 / Host 管隔离与密封评测」的**四权分立**，是 RSIAgent 能声称「自我改进可信」的**制度根基**——它从**结构上**防止 Agent 自嗨作弊。

### 2.3 三阶段（BRS → DRS → 测试时冻结复用）

**Phase 1 · BRS（Broad Recursive Self-exploration，广度并行）**
- Curriculum **一次出一批（wave）**练习项目。
- 每个分支从**同一份 pre-wave 记忆快照**出发，独立执行，各自拿到接地的 Verifier 裁决。
- **wave memory barrier**：**所有分支都完成有效校验**后，同一批 Actor 才**按 authored 顺序串行蒸馏**经验；下一 wave 只在这些记忆提交落盘后才开始。
- 未完成/被隔离的分支**阻塞整个 wave**，其未发布记忆**不合并**。预算检查只在完整 wave 边界发生（Curriculum 可提前宣布饱和）。

**Phase 2 · DRS（Deep Recursive Self-exploration，深度串行）**
- **先真刀真枪试目标任务**——PASS 和 FAIL **都能**被执行的 Actor 学习。
- 默认 `curriculum_review`：即便目标 PASS，Curriculum 仍复盘学到的经验，可**要求再练**或**返回就绪**；若练了，**必须在未改动的目标上重试**才算成功收敛。
- 备选 `verifier_pass`：目标 PASS + 记忆提交后即停。
- **铁律**：两种策略都**绝不把基础设施错误变成 PASS/FAIL**；`STALLED`/预算耗尽必须与「成功收敛」可区分。

**Phase 3 · 测试时冻结记忆复用**
- Actor 用**冻结记忆快照**，在**同一套框架**里跑；每次评测**重置交互历史 + 任务环境**。
- Actor↔Verifier 交互**先结束**，官方评测器**后运行**；**关闭 Curriculum 与记忆更新**。
- 评测器输出是 **host 产物，不能回流**到该协议运行的后续学习阶段。
- 记录冻结配置哈希、任务发布身份、记忆哈希、终态。**基础设施失败记为未评分，不是官方零分。**

### 2.4 接地与隔离（RSIAgent 的「杀手锏」）

- **真 VM guest**：Actor 在真实操作系统里点鼠标、敲命令、跑 GUI 应用（OSWorld / ALE）。
- **视觉观察**：Actor 通过截图看环境状态。
- **密封官方评测器（sealed official evaluator）**：任务成不成，**由 benchmark 官方评测器说了算**，不是 Agent 自评。
- **客观分数**：partial-credit / full-credit 百分比。

> 这一层是 RSIAgent 与 Spirit 之间**最本质、最难跨越**的鸿沟，详见 §4.2。

### 2.5 实测结果 **+ 报告口径警告**（务必一起看）

| Benchmark | Partial w/o RSI | Partial **with RSI** | Full-credit w/o RSI | Full-credit **with RSI** |
|-----------|:---:|:---:|:---:|:---:|
| OSWorld 2.0（0808 offline，82 任务） | 71.97 | **78.98** | 37.80 | **42.68** |
| ALE Near-term（67 任务） | 83.75 | **84.82** | 49.25 | **50.75** |

**⚠️ 报告口径（作者在 PAPER.md 明写，不可忽略）：**
- **OSWorld**：82 任务（含 T082 setup 失败记 0）。RSI 那一行**只用了 41 条非诊断性 RSI 实测分**（含回退与记录在案的重试），**其余 41 条保留 baseline 分**。
- **ALE**：全 67 任务。RSI 行**只用 19 条 RSI 列实测**，**保留 48 条 baseline**；含本地修正评分、公开标签迁移限定的 ECG 结果、一个 no-BRS 的 Tax Form 变体。
- **对照范围**：**保留的 baseline 不是新的 RSI 评测**；选定的 run、checkpoint、预算、评测范围**并未完全匹配**。
- 作者定性：这些是**手稿报告结果，不是新的结果文件审计，也不是本发布做的匹配协议打榜提交**（"not a matched-protocol leaderboard submission"）。

> **怎么读这张表**：增益方向是**真实且一致的**（两个 benchmark、partial 与 full-credit 四个口径全部为正），说明「接地式 RSI」确实有效；但**幅度温和**（OSWorld +7 partial / ALE +1 partial），且**统计口径混杂**——不能当成「同条件重复实验的平均增益」，更不能当成严格对照的 SOTA 打榜。**这恰恰印证了 §5 的批判性结论：能用，但要清醒。**

### 2.6 三大失败模式（论文自陈，属**根本限度**而非 bug）

1. **Insufficiently Targeted Exploration（探索没打中弱点）**：练习项目未必覆盖真正的短板，练了也白练。
2. **Incomplete Verification（校验放过不完整结果）**：Verifier 可能把「其实没做完」的结果判成 PASS，脏经验入记忆。
3. **Unreliable Memory Consolidation（记忆固化错误规则）**：一次错误的蒸馏可能被沉淀成「规则」，之后反复误导。

> 这三条**对 Spirit 同样成立，甚至更严重**——因为 Spirit 缺客观裁判，第 2、3 条风险被放大（§4.2）。

---

## 3. Spirit 现状（对标实现）

### 3.1 `spirit/evolution/` — 自进化子系统（Phase 6.A–6.F 全部完成）

**14 文件 / 214 测试全绿**，`docs/13-self-evolution-rsi.md` 是设计蓝本。三角色闭环与 RSIAgent **一一对应**：

| Spirit 模块 | 对标 RSIAgent | 状态 |
|-------------|---------------|:---:|
| `protocol.py`（数据类+枚举） | 角色协议 | ✅ |
| `memory.py`（三层记忆：轨迹/洞察/技能，落盘 `SPIRIT_HOME/evolution_memory/`） | Actor 持久记忆 | ✅ |
| `roles.py`（三角色 prompt + 注入式 `LLMCaller`，复用 `goals.judge`） | 三角色 | ✅ |
| `verifier.py`（`Evidence` 客观证据 + `DomainVerifier` + `Verifier` 编排） | Verifier Agent | ✅ |
| `curriculum.py`（`CurriculumPlanner`：只读复盘 + 派任务/停止） | Curriculum Agent | ✅ |
| `loop.py`（`EvolutionLoop`：DRS 状态机 + 预算/STALLED + 可序列化续跑） | **Phase 2 DRS** | ✅ |
| `memory_hash.py`（哈希 + `freeze` 冻结 + `FrozenMemory` 只读门面） | **Phase 3 测试时复用** | ✅ 建好 |
| `phase1_wave.py`（`Phase1Wave`：BRS 广度并行 + wave memory barrier） | **Phase 1 BRS** | ✅ 建好 |
| `hitl.py`（auto/always/never + 低置信度/分叉触发 + 超时 fail-open） | —（Spirit 增强） | ✅ |
| `scheduler.py`（interval/daily 调度 + 日报） | —（Spirit 增强） | ✅ |
| `domains/`（`Domain` 接口 + `FinanceDomain` 金融示例，注入式数据源/回测） | 领域适配 | ✅ 示例 |

**已忠实复刻的关键铁律（与 RSIAgent 同源）：**
- **Verifier 结构性隔离**：`Evidence` 数据类**根本不含**承载 Actor 私有推理/记忆的字段 → Verifier 从**接口层面**就无法「自嗨」，客观信号 > 模型自评（先跑确定性验证器，无法客观裁决才回退 LLM）。
- **基础设施异常 → `UNVERIFIED`**，**绝不当 PASS/FAIL**（`verifier.py` line 17 明写）。
- **Curriculum fail-open**：无 caller / 无法决策 → `STALLED`，**绝不臆断 `converged`**（`curriculum.py` line 24-25）。
- **STALLED 与 CONVERGED 严格可区分**。

### 3.2 `spirit/autonomy/` — 常驻自主循环（Phase 7，Spirit 的「活着」层）

这是 Spirit **超出** RSIAgent 的部分，把 Phase 6 的能力单元编排成一个**常驻、可交互**的桌面伴侣：

- **`AutonomyLoop`（explorer.py）**：常驻单线程循环，`run_forever` 按 `interval_hours` 轮询；`set_domain(direction, run_now=True)` 支持**运行时给方向**（用 `_pending_run` 事件让下一次 poll 消费执行，避免与常驻循环并发）。
- **实际执行链**：`AutonomyLoop` **只通过 `self._evolution.run()` 调用 `EvolutionLoop`（DRS）做单目标深挖**（explorer.py line 33/129/232，`Grep` 实测确认只 import 了 `EvolutionLoop` + `STOP_CURRICULUM_REVIEW`）。
- **给方向入口（本会话刚接通）**：桌宠左键 → 系统面板 → 输入方向 → 「开始探索」→ WS 命令 `autonomy_set_domain` → `bridge.on_set_domain` → `set_domain` 置 `_pending_run` → 循环消费 → `propose`（`domain_getter` 读 `autonomy.domain`）→ 围绕新方向探索。
- **HITL**：遇 STALLED/分叉 → 决策经 WS 事件 `autonomy_decision` 推给桌宠 → 气泡提醒 + 系统面板应答。
- **配置**：`config.yaml` 的 `autonomy` 段（`enabled/domain/run_on_start/interval_hours=5.0/stop_policy=curriculum_review/max_rounds=5/max_iterations=40/hitl_timeout=300`）。优先级：显式参数 > `autonomy.*` > 代码缺省。

### 3.3 Spirit 的「接地」现状（诚实版）

Spirit 的 Verifier **有接地能力，但接的是「弱接地」信号**：
- 沙箱执行（`make_execute`）+ 产物证据（`_sandbox_snapshot` / `_collect_products` 填 `Evidence`）。
- 确定性验证器：`ExitCodeVerifier`（退出码）/ `FileContentVerifier`（文件内容）/ `PatternVerifier`（正则）/ `MetricThresholdVerifier`（指标阈值，如金融夏普）。
- 客观无法裁决时 → 回退 LLM（`VerifierRole`，只看 `Evidence.render()`）。
- `FinanceDomain` 提供了「回测夏普 > 阈值」的**领域客观指标**范例——**但它是注入式示例，没有接真实数据源、没有 benchmark 成效 harness。**

> **一句话**：Spirit 的接地是「沙箱 + 文件 diff + 退出码 + 领域指标 + LLM」，**缺 RSIAgent 那两样最硬的东西：密封官方评测器 + 视觉真环境**。

---

## 4. 差距分析（量化 + 定性）

### 4.1 五维差距总表

| 维度 | RSIAgent | Spirit 现状 | 差距等级 |
|------|----------|-------------|:---:|
| **① 机制/协议层** | 三角色 + BRS/DRS/Phase3 + HITL + 调度 | `evolution/` **全部复刻**（14 文件/214 测试） | 🟢 **基本对齐** |
| **② 接地/环境层** | 真 VM guest + 视觉 + **密封官方评测器** + 客观分数 | 沙箱 + 文件 diff + 退出码 + 领域指标 + LLM，**无密封评测器/无视觉** | 🔴 **决定性差距** |
| **③ 操作化层** | BRS 广度并行 + Phase3 冻结复用**都在跑** | BRS(`Phase1Wave`)/Phase3(`FrozenMemory`) **代码在、测试过，但未接进 `autonomy/`**；实际只跑 DRS 单目标 | 🟡 **接线差距** |
| **④ 度量层** | OSWorld/ALE 出**成效数字**（有 benchmark 证据） | 只有单元测试证明**机制离线正确**，**无成效证据** | 🔴 **证据差距** |
| **⑤ 交互/常驻层** | 批量 benchmark runner，跑完即止 | **常驻不停机 + 运行时给方向 + 桌宠 UI + HITL 气泡 + 沙箱写护栏** | 🟢 **Spirit 反超** |

### 4.2 决定性差距：GROUNDING（接地层）——展开

这是**唯一真正卡住 Spirit 的瓶颈**，其余都是工程量问题。

- **RSIAgent 能证明「自我改进真的发生了」**：因为裁判是**密封官方评测器**，Agent 无法influence评分，分数是客观的（71.97→78.98 是硬数字）。
- **Spirit 目前无法证明**：没有客观裁判时，Verifier 要么依赖领域指标（当前只有金融示例，且未接真数据），要么回退 LLM 自评。而 LLM 自评正是 RSIAgent 论文警告的**「Incomplete Verification」**失败模式的温床——**「校验放过不完整结果」→ 脏经验入记忆 → 「Unreliable Memory Consolidation」固化错误规则**，形成自我强化的幻觉。
- **后果**：Spirit 现在**能跑自我改进的「动作」，但改进的「效果」无法被客观度量**。你可以说「它练了」，但很难说「它真的变强了 X%」。

> **这不是加机制能解决的**——这是「有没有考场和裁判」的问题。§6 的最高杠杆动作正是针对它。

### 4.3 操作化差距：BRS / Phase3「建好但未接线」——`Grep` 实测证据

**论断**：`Phase1Wave`（BRS）与 `FrozenMemory`（Phase 3）在 Spirit 里**只存在于 `evolution/`，从未被 `autonomy/` 引用**。

**实测命令与结果**（本次核验）：
```
Grep(regex="Phase1Wave|FrozenMemory|open_readonly|memory_hash|freeze|wave",
     path="spirit/autonomy/")   →  Found 0 matches.   ← autonomy 完全没碰 BRS/Phase3

Grep(regex="Phase1Wave|FrozenMemory", path="spirit/", glob="*.py")
     →  Found 16 matches，全部落在 evolution/{phase1_wave,memory_hash,__init__}.py   ← 只在 evolution 内部

Grep(regex="import.*evolution|EvolutionLoop|self\._evolution",
     path="spirit/autonomy/")   →  explorer.py: from spirit.evolution.loop import EvolutionLoop, STOP_CURRICULUM_REVIEW
                                    explorer.py: self._evolution = EvolutionLoop(...) ; self._evolution.run(...)
```

**含义**：Spirit 的常驻自主循环**目前只跑 DRS（单目标深挖）**。RSIAgent 的**广度并行探索（Phase 1 BRS）**和**测试时冻结记忆复用（Phase 3）**这两块，Spirit **有代码、有测试，但没上线到实际运行链路**。这是「从 benchmark runner 视角看功能完整、但从常驻产品视角看只激活了 1/3 阶段」的状态。

### 4.4 度量层差距

- RSIAgent：跑 OSWorld/ALE → 出 partial/full-credit 百分比 → 可对比 baseline。
- Spirit：`tests/evolution/` 214 测试证明**机制在离线打桩下逻辑正确**（FakeCaller/RaisingCaller），但**没有任何「Spirit 在某领域自我改进后成效提升 X」的实测数字**。
- **差距性质**：单元测试证明的是「机器零件装对了」，benchmark 证明的是「这台车真的跑得更快了」。Spirit 只有前者。

### 4.5 Spirit 反超 RSIAgent 的地方（别妄自菲薄）

| 能力 | Spirit | RSIAgent |
|------|:---:|:---:|
| 常驻不停机循环（`run_forever`） | ✅ | ❌（批量跑完即止） |
| **运行时给方向**（桌宠输入框 + `autonomy_set_domain` WS） | ✅ | ❌（benchmark 任务预设） |
| 沙箱写护栏（`make_execute` + snapshot） | ✅ | 部分（VM 隔离） |
| HITL 气泡提醒 + 面板应答 | ✅ | ❌（无人值守 runner） |
| 桌宠 UX / 情感化交互 | ✅ | ❌ |

> RSIAgent 是**科研级 benchmark runner**，Spirit 是**产品级桌面伴侣**。Spirit 在「人机协同 + 常驻 + 可交互」上反而更成熟。**差距是「接地与度量」，不是「工程完整度」。**

---

## 5. 批判性评估：RSIAgent 架构**真能**实现「AI 自动化规划」吗？

**结论先行：能，但是「有界地能」——它实现的是特定意义上的自动化规划，且强依赖三个前提，证据正向但口径混杂。**

### 5.1 它确实做到了什么（正面）

1. **自动化课程规划是真的**：Curriculum Agent **不靠人工排任务**，而是用「既往结果 + 积累知识」自己选信息量大的探索任务、自己决定是否继续练习。这就是「自动化规划」的核心——**规划由模型自主产生**。
2. **接地式递归自我改进是真的**：Verifier 用密封评测器裁决、经验只从**已验证**边界蒸馏入记忆、记忆在后续任务复用。**同冻结权重**下 OSWorld +7 partial / ALE +1 partial，**方向一致、可复现于两个 benchmark**。
3. **制度性防作弊是真的**：四权分立 + Verifier 看不到 Actor 私有推理 + 评测器密封 + 基础设施错误绝不转 PASS/FAIL。这让「自我改进」不至于沦为「自我催眠」。

### 5.2 但必须看清「自动化规划」的确切含义（限定）

**① 它是「自动化探索/课程规划以学习环境」，不是「通用目标规划器」。**
- RSIAgent 的规划发生在**一个有明确任务集、有官方评测器的 benchmark 环境**里（OSWorld 的 108 个桌面任务、ALE 的 67 个任务）。Curriculum 规划的是「接下来练哪个项目」，不是「给我一个模糊人生目标我自己拆解成子目标」。
- **换个没有明确任务边界、没有客观评测的开放域，它的规划能力就失去支点。**

**② 它强依赖「客观可验证性」。**
- 整套机制的信用锚点是**密封官方评测器**。一旦某领域**无法构造客观验证器**（如「写一篇好文章」「做一个好看的设计」），Verifier 就只能回退 LLM 自评，**三大失败模式立刻回来**，自我改进的可信度崩塌。
- **这是硬前提，不是可调参数。**

**③ 证据正向，但口径混杂、增益温和。**
- RSI 列**不是全部重新实测**（OSWorld 41+41、ALE 19+48 保留 baseline），含选择性重试、不匹配预算、本地重评；作者自己声明「非匹配协议打榜」。
- 增益量级：OSWorld partial +7.01、full +4.88；ALE partial +1.07、full +1.50。**ALE 的增益小到接近噪声地板**。
- **正确的读法**：「接地式 RSI 有稳定正向效果」成立；「RSI 让 Agent 脱胎换骨」**不成立**。

**④ 三大失败模式是根本限度，不是待修 bug。**
- 「探索没打中弱点 / 校验放过不完整结果 / 记忆固化错误规则」——这三条**源于「用 LLM 做规划与蒸馏」这件事本身的不确定性**，只能靠更好的验证器和记忆治理**缓解**，无法**根除**。

### 5.3 对 Spirit 的直接启示

- **机制移植是忠实的**——Spirit 的 `evolution/` 没有偷工减料，铁律（UNVERIFIED / STALLED≠CONVERGED / Verifier 结构隔离）都在。
- **Spirit 甚至在人机协同维度反超**（常驻 + 给方向 + HITL + UX）。
- **但 Spirit 继承了 RSIAgent 的全部限度，且因为缺接地而放大**：RSIAgent 至少有密封评测器兜底，Spirit 连这个都没有。**所以 Spirit 现在「自我改进」的可信度，取决于用户能否为具体领域配一个客观验证器。**

---

## 6. 给 Spirit 的可行路线（守铁律 · 按杠杆排序）

> **铁律约束**：不自作主张升级 Agent、不在项目里留痕、任何源码改动须用户明确要求。以下**只是路线建议，不含任何已执行改动**；每一项都需用户点头才动手。

**🥇 杠杆 1（最高）：为「用户真正在意的领域」配一个客观验证器 + 成效度量 harness。**
- 这是**唯一能跨越 §4.2 决定性差距**的动作。
- 具体形态：选定 1 个领域（如金融/代码/数据处理），把 `FinanceDomain` 从「注入式示例」升级为「接真实数据源 + 跑出可对比数字」的 `Domain` 实现；用一个**客观指标**（夏普/回测收益/单测通过率/评测脚本分数）做 Verifier 的确定性裁决，**替代 LLM 自评**。
- 收益：让「Spirit 自我改进后成效 +X%」第一次**可被证明**，直接补上 §4.4 度量层差距。

**🥈 杠杆 2：把 BRS(`Phase1Wave`) 与 Phase3(`FrozenMemory`) 接进 `autonomy/`。**
- 代码已存在、测试已过（§4.3），**只差接线**——工程量小、风险低。
- 具体形态：让 `AutonomyLoop` 在「给方向」后先跑一个 BRS wave 广度铺开、再进 DRS 深挖；需要「稳定复用已学经验」时用 `freeze` + `FrozenMemory` 冻结。
- 收益：从「只跑 1/3 阶段」补齐到「BRS→DRS→Phase3 全生命周期在线」，对齐 RSIAgent 完整操作化。

**🥉 杠杆 3：记忆治理，缓解「Unreliable Memory Consolidation」。**
- 利用已有 `deprecate_insight`（证伪沉底）+ `decay_insights`（置信度衰减），加一条「被后续 Verifier 反复否证的洞察自动降权/淘汰」的策略。
- 收益：降低脏经验固化风险（§2.6 失败模式 3）。

**杠杆 4（可选）：探索针对性，缓解「Insufficiently Targeted Exploration」。**
- 让 Curriculum 的 `progress_summary` 复盘更聚焦「近期 FAIL 的共性弱点」，优先派能打中短板的练习。
- 收益：提高练习命中率（§2.6 失败模式 1）。

**❌ 明确不建议**：引入 RRSI 式「改 harness 源码」路线——**与用户铁律冲突**，且 RSIAgent 路线已足够。

---

## 7. 参考

- **RSIAgent**：`RSIAgent/README.md`、`RSIAgent/docs/ARCHITECTURE.md`、`RSIAgent/docs/PAPER.md`；arXiv:2609.15364（*Autonomous Exploration for Recursive Self-improvement in New Environments*）。
- **RRSI**（对照，非本文主角）：arXiv:2609.24972（*Regularized Recursive Self-Improvement of Agent Harnesses*）；本地 `rrsi/ARCHITECTURE.zh-CN.md`。
- **Spirit 自进化设计**：`docs/13-self-evolution-rsi.md`、`spirit/evolution/README.md`。
- **Spirit 自主循环**：`spirit/autonomy/{explorer,integration,bridge}.py`、`spirit/config.py`（`autonomy` 段）。
- **核验方法**：§4.3 的接线论断均由 `Grep` 实测（`Phase1Wave|FrozenMemory` 在 `autonomy/` 命中 0、在 `evolution/` 命中 16）。

---

*本文档由差距分析任务生成并存储于 `docs/`，遵循仓库 `NN-topic.md` 编号约定（承接 `13-self-evolution-rsi.md`）。存储位置可按需调整。*
