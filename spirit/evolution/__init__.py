"""Spirit 自进化子系统（RSI · Phase 6）—— 参考 RSIAgent 范式。

让 Spirit 具备**领域内自主进化**能力：自主探索、自我构建任务、经验沉淀复用、
可选人在环（docs/13-self-evolution-rsi.md）。核心是三角色闭环::

    Curriculum（派任务）→ Actor（执行 + 蒸馏）→ Verifier（独立校验）→ 记忆沉淀

设计原则（training-free、无需 GPU、harness 级进化）：进化发生在 prompt / 工具路由 /
memory / 技能注册表，**不碰模型权重**；反思必须接外部 Verifier（防"自嗨"）。

Phase 6.A（本包当前范围）已落地：

- :mod:`spirit.evolution.protocol` —— 角色协议数据类
  （``TargetVerdict`` / ``EvolutionStatus`` / ``ActorLearning`` / ``Verdict`` /
  ``CurriculumDecision`` / ``EvolutionResult`` / ``SelfEvolvingStart``）。
- :mod:`spirit.evolution.memory` —— 三层进化记忆（轨迹池 / 洞察库 / 技能库），
  落盘 ``<SPIRIT_HOME>/evolution_memory/``。
- :mod:`spirit.evolution.roles` —— 三角色 prompt 模板 + model slot 解析 +
  注入式 ``llm_caller``（复用 :data:`spirit.goals.judge.LLMCaller` 签名）。

Phase 6.B 已落地：

- :mod:`spirit.evolution.verifier` —— 独立校验闭环：:class:`Evidence`（客观证据，
  结构性屏蔽 Actor 私有推理）+ :class:`DomainVerifier` 接口（客观信号 > 模型自评）
  + :class:`Verifier` 编排（客观优先 → LLM 回退 → 探针回滚保护，复用
  :mod:`spirit.checkpoint`；基础设施异常 → UNVERIFIED）。

Phase 6.C–6.F 已落地（Phase 6 全部完成）：

- :mod:`spirit.evolution.curriculum` —— :class:`CurriculumPlanner`：只读复盘记忆 +
  派任务/停止决策（借 :mod:`spirit.goals.judge` fail-open 范式）。
- :mod:`spirit.evolution.loop` —— :class:`EvolutionLoop`：DRS 单阶段状态机（试目标→
  校验→蒸馏→练习→重试），预算/STALLED 与 CONVERGED 可区分，可序列化状态供续跑。
- :mod:`spirit.evolution.memory_hash` —— 记忆哈希 + 冻结快照 + 校验 + :class:`FrozenMemory`
  只读复用（Phase 3）。
- :mod:`spirit.evolution.phase1_wave` —— :class:`Phase1Wave`：BRS 广度并行 + wave
  memory barrier（全通过才按序串行蒸馏合并，任一失败阻塞整 wave）。
- :mod:`spirit.evolution.hitl` —— :class:`HumanInTheLoop`：auto/always/never 模式 +
  低置信度/分叉触发 + 超时 fail-open + 建议回灌记忆（source=user）。
- :mod:`spirit.evolution.scheduler` —— :class:`Scheduler`：interval/daily 调度引擎 +
  tick + 日报（补全 cron 空壳）。
- :mod:`spirit.evolution.domains` —— 领域无关接口 :class:`Domain` + 金融示例
  :class:`FinanceDomain`（注入式数据源/回测，客观夏普阈值裁决）。

一站式::

    from spirit.evolution import make_verifier, get_memory
    verifier = make_verifier(caller=my_llm_caller)   # 无 caller 时 fail-soft → UNVERIFIED
    verdict = verifier.verify(task="...", evidence="...")
    mem = get_memory()
    mem.add_insight("回测夏普>1 才纳入", confidence=0.8, source_task_id="t1")

记忆单例惰性创建（``get_memory()``），**无导入期副作用**（不建目录、不读盘）。
"""

from __future__ import annotations

import logging
from typing import Optional

from spirit.evolution.memory import (
    EvolutionMemory,
    Insight,
    SkillEntry,
    Trajectory,
    evolution_memory_root,
)
from spirit.evolution.protocol import (
    ActorLearning,
    CurriculumDecision,
    CurriculumDecisionKind,
    EvolutionResult,
    EvolutionStatus,
    RoleName,
    SelfEvolvingStart,
    TargetVerdict,
    Verdict,
)
from spirit.evolution.roles import (
    ActorRole,
    CurriculumRole,
    Role,
    RoleSpec,
    VerifierRole,
    build_role_caller,
    make_actor,
    make_curriculum,
    make_verifier,
    resolve_model,
)
from spirit.evolution.verifier import (
    DomainVerifier,
    Evidence,
    ExitCodeVerifier,
    FileContentVerifier,
    MetricThresholdVerifier,
    PatternVerifier,
    Verifier,
)
from spirit.evolution.curriculum import CurriculumPlanner
from spirit.evolution.loop import (
    AttemptResult,
    EvolutionLoop,
    run_evolution,
    STOP_CURRICULUM_REVIEW,
    STOP_VERIFIER_PASS,
)
from spirit.evolution.memory_hash import (
    FrozenMemory,
    MemorySnapshot,
    compute_hash,
    freeze,
    list_snapshots,
    load_snapshot,
)
from spirit.evolution.phase1_wave import (
    BranchOutcome,
    Phase1Wave,
    WaveResult,
    run_wave,
)
from spirit.evolution.hitl import (
    HITLMode,
    HumanAnswer,
    HumanInTheLoop,
    HumanQuery,
    make_hitl,
)
from spirit.evolution.scheduler import (
    ScheduledTask,
    Scheduler,
    TaskRun,
    TickReport,
)
from spirit.evolution.domains import Domain, DomainInfo, FinanceDomain

logger = logging.getLogger(__name__)

_memory: Optional[EvolutionMemory] = None


def get_memory() -> EvolutionMemory:
    """返回进程级惰性单例 :class:`EvolutionMemory`（按调用解析 SPIRIT_HOME）。"""
    global _memory
    if _memory is None:
        _memory = EvolutionMemory()
    return _memory


def reset_memory() -> None:
    """重置全局记忆单例（测试切换 SPIRIT_HOME 后调用以清缓存）。"""
    global _memory
    _memory = None


__all__ = [
    # protocol
    "TargetVerdict",
    "EvolutionStatus",
    "RoleName",
    "CurriculumDecisionKind",
    "ActorLearning",
    "Verdict",
    "CurriculumDecision",
    "EvolutionResult",
    "SelfEvolvingStart",
    # memory
    "EvolutionMemory",
    "Trajectory",
    "Insight",
    "SkillEntry",
    "evolution_memory_root",
    "get_memory",
    "reset_memory",
    # roles
    "Role",
    "RoleSpec",
    "ActorRole",
    "VerifierRole",
    "CurriculumRole",
    "make_actor",
    "make_verifier",
    "make_curriculum",
    "build_role_caller",
    "resolve_model",
    # verifier (Phase 6.B)
    "Evidence",
    "DomainVerifier",
    "ExitCodeVerifier",
    "FileContentVerifier",
    "PatternVerifier",
    "MetricThresholdVerifier",
    "Verifier",
    # curriculum + loop (Phase 6.C)
    "CurriculumPlanner",
    "AttemptResult",
    "EvolutionLoop",
    "run_evolution",
    "STOP_CURRICULUM_REVIEW",
    "STOP_VERIFIER_PASS",
    # memory_hash + phase1_wave (Phase 6.D)
    "FrozenMemory",
    "MemorySnapshot",
    "compute_hash",
    "freeze",
    "list_snapshots",
    "load_snapshot",
    "BranchOutcome",
    "Phase1Wave",
    "WaveResult",
    "run_wave",
    # hitl (Phase 6.E)
    "HITLMode",
    "HumanQuery",
    "HumanAnswer",
    "HumanInTheLoop",
    "make_hitl",
    # scheduler (Phase 6.F)
    "Scheduler",
    "ScheduledTask",
    "TaskRun",
    "TickReport",
    # domains (Phase 6.F)
    "Domain",
    "DomainInfo",
    "FinanceDomain",
]
