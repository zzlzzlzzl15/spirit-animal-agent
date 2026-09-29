# spirit/evolution — 自进化子系统（RSI · Phase 6）

让 Spirit 具备**领域内自主进化**能力：自主探索、自我构建任务、经验沉淀复用、可选人在环。
范式参考 [RSIAgent](../../../RSIAgent/)（training-free、**无需 GPU**、harness 级进化，不碰模型权重）。
完整设计与论证见 [`docs/13-self-evolution-rsi.md`](../../docs/13-self-evolution-rsi.md)。

## 三角色闭环

```
Curriculum（派任务）→ Actor（执行 + 蒸馏）→ Verifier（独立校验）→ 记忆沉淀 → 下次更强
```

| 角色 | 职责 | 关键约束 |
|------|------|----------|
| **Actor** | 操作环境、蒸馏已验证经验 | 执行 + 学习合一，诊断不得为空 |
| **Verifier** | 独立校验候选证据 | **看不到 Actor 私有推理/记忆**；客观信号 > 模型自评；异常判 UNVERIFIED |
| **Curriculum** | 复盘进度、派下一个任务、决定继续/停止 | **不打分、不写 Actor 记忆**；stalled 必须与 converged 可区分 |

## 模块一览（Phase 6.A–6.F 全部完成 · 214 测试）

| 文件 | 内容 | 阶段 |
|------|------|:---:|
| `protocol.py` | 角色协议数据类：`TargetVerdict` / `EvolutionStatus` / `ActorLearning` / `Verdict` / `CurriculumDecision` / `EvolutionResult` / `SelfEvolvingStart` | 6.A |
| `memory.py` | 三层进化记忆（轨迹池 / 洞察库 / 技能库），落盘 `<SPIRIT_HOME>/evolution_memory/` | 6.A |
| `roles.py` | 三角色 prompt 模板 + model slot 解析 + 注入式 `llm_caller`（复用 `goals.judge.LLMCaller` 签名） | 6.A |
| `verifier.py` | 独立校验闭环：`Evidence`（客观证据）+ `DomainVerifier` + `Verifier` 编排（客观优先→LLM 回退→探针回滚保护） | 6.B |
| `curriculum.py` | `CurriculumPlanner`：只读复盘记忆 + 派任务/停止决策 | 6.C |
| `loop.py` | `EvolutionLoop`：DRS 状态机（试目标→校验→蒸馏→练习→重试）+ 预算/STALLED + 可序列化续跑 | 6.C |
| `memory_hash.py` | 记忆哈希 + 冻结快照 + 校验 + `FrozenMemory` 只读复用 | 6.D |
| `phase1_wave.py` | `Phase1Wave`：BRS 广度并行 + wave memory barrier | 6.D |
| `hitl.py` | `HumanInTheLoop`：auto/always/never + 低置信度/分叉触发 + 超时 fail-open + 建议回灌 | 6.E |
| `scheduler.py` | `Scheduler`：interval/daily 调度引擎 + tick + 日报（补全 cron 空壳） | 6.F |
| `domains/` | `Domain` 领域无关接口 + `FinanceDomain` 金融示例（注入式数据源/回测） | 6.F |
| `__init__.py` | 公共 API（61 导出）+ 惰性记忆单例 `get_memory()` | — |

## 设计约定（对齐全仓 Spirit 子系统）

- **按调用解析路径**：记忆根目录每次操作读 `spirit.config.SPIRIT_HOME`，测试 monkeypatch 立即生效；无导入期副作用。
- **注入式可测试 seam**：三角色的 LLM 调用通过 `llm_caller` 注入，离线用 `FakeCaller` 打桩。
- **fail-soft 铁律**：无 caller / 调用失败 / 解析失败时，Verifier→`UNVERIFIED`、Curriculum→`STALLED`，绝不把基础设施异常误判为 PASS/FAIL 或语义化决策。
- **洞察库可观测**：每条洞察带 `confidence` + `source_task_id` + `status`，支持被证伪后沉底（`deprecate_insight`）与置信度衰减（`decay_insights`）。

## 快速使用

```python
from spirit.evolution import make_verifier, get_memory, EvolutionLoop, Verifier, ExitCodeVerifier

verifier = make_verifier(caller=my_llm_caller)   # 无 caller 时 fail-soft → UNVERIFIED
verdict = verifier.verify(task="...", evidence="...")   # -> Verdict(verdict, reason, confidence)

mem = get_memory()
mem.add_insight("回测夏普>1 才纳入", confidence=0.8, source_task_id="t1")

# DRS 单阶段自进化闭环（execute 为注入式环境 seam）
loop = EvolutionLoop(
    execute=lambda task: my_env_run(task),          # 返回 Evidence / AttemptResult / str
    verifier=Verifier(domain_verifiers=[ExitCodeVerifier(0)]),
    memory=mem,
)
result = loop.run("目标任务")        # -> EvolutionResult(status=CONVERGED|STALLED, ...)
state = loop.state()                 # 可序列化，供外层持久化以 /resume 续跑
```

## 全生命周期（docs/13 §2.2）

- **Phase 1 BRS**（广度）：`Phase1Wave` 并行探索一批项目 + wave memory barrier。
- **Phase 2 DRS**（深度）：`EvolutionLoop` 深挖目标任务（试→校验→蒸馏→练习→重试）。
- **Phase 3 复用**：`freeze` 冻结记忆快照 → `FrozenMemory` 只读复用（关闭记忆更新）。
- **HITL**：`HumanInTheLoop` 低置信度/分叉点询问，超时 fail-open 自主继续。
- **领域化 + 每日定时**：`FinanceDomain`（示例）+ `Scheduler` 每日触发进化 + 日报推送。

## 测试

```
pytest tests/evolution/ -p no:asyncio
```
