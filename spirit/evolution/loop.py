"""DRS 单阶段自进化状态机 —— Spirit Agent Phase 6.C（docs/13 §2.2 Phase 2 / §Phase C）。

跑通"最小自进化闭环"（对标 RSIAgent ``core/self_evolving_loop.py`` 的 DRS 深挖）::

    Actor 试目标任务 → Verifier 独立校验
      ├─ PASS：按停止策略收敛（verifier_pass 即停 / curriculum_review 仍可要求再练）
      └─ FAIL/UNVERIFIED：Actor 蒸馏 diagnosis+memory → Curriculum 决定"再练 or 就绪"
              ├─ practice：执行练习项目（Actor→Verifier 内循环），每条已验证经验即时提交记忆
              └─ ready/converged：在**未改动的目标任务**上重试

铁律（docs/13 §2.3）：

- **STALLED 与 CONVERGED 必须可区分**：预算耗尽 / 无进展 → ``STALLED``，绝不当成功；
- 仅在**已验证经验**边界提交记忆（对标 wave memory barrier，C3）；
- 基础设施异常（UNVERIFIED）不当 PASS/FAIL——仍可学习但不计入"成功收敛"。

**注入式可测试**：环境执行经 ``execute`` seam 注入（返回 :class:`AttemptResult` /
:class:`~spirit.evolution.verifier.Evidence` / 纯字符串自动强制），三角色与记忆全部可注入，
故整条闭环可完全离线跑通。C5（与 ``spirit/goals`` 打通续跑）通过 :meth:`EvolutionLoop.state`
/ :meth:`EvolutionLoop.restore` 的可序列化状态实现，由外层持久化到 SessionDB。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from spirit.evolution.curriculum import CurriculumPlanner
from spirit.evolution.memory import EvolutionMemory
from spirit.evolution.protocol import (
    ActorLearning,
    CurriculumDecisionKind,
    EvolutionResult,
    EvolutionStatus,
    SelfEvolvingStart,
    TargetVerdict,
    Verdict,
)
from spirit.evolution.roles import ActorRole
from spirit.evolution.verifier import Evidence, Verifier

logger = logging.getLogger(__name__)

# 两种停止策略（docs/13 §2.2 Phase 2）
STOP_CURRICULUM_REVIEW = "curriculum_review"  # 默认：PASS 后 Curriculum 仍可要求再练
STOP_VERIFIER_PASS = "verifier_pass"          # PASS 即停

# 环境执行 seam：接收任务串，返回本次尝试的产物（观察 + 客观证据）
ExecuteFn = Callable[[str], Any]


# ---------------------------------------------------------------------------
# 单次尝试结果容器
# ---------------------------------------------------------------------------

@dataclass
class AttemptResult:
    """一次任务尝试的产物：``observation``（给 Actor 蒸馏）+ ``evidence``（给 Verifier 校验）。"""

    observation: str = ""
    evidence: Evidence = field(default_factory=Evidence)

    @classmethod
    def coerce(cls, task: str, raw: Any) -> "AttemptResult":
        """把 execute 的任意返回强制为 AttemptResult（fail-soft）。

        - :class:`AttemptResult` 原样返回（补 task）；
        - :class:`~spirit.evolution.verifier.Evidence` → 包一层，observation 用其 render()；
        - dict → 视作 Evidence.from_dict；
        - 其它（含 str）→ 作为 observation 文本，evidence.logs 承载同文本。
        """
        if isinstance(raw, AttemptResult):
            result = raw
        elif isinstance(raw, Evidence):
            result = cls(observation=raw.render(), evidence=raw)
        elif isinstance(raw, dict):
            ev = Evidence.from_dict(raw)
            result = cls(observation=ev.render(), evidence=ev)
        else:
            text = str(raw or "")
            result = cls(observation=text, evidence=Evidence(logs=text))
        if not result.evidence.task:
            result.evidence.task = task
        return result


# ---------------------------------------------------------------------------
# DRS 状态机
# ---------------------------------------------------------------------------

class EvolutionLoop:
    """单阶段（DRS）自进化循环：试目标 → 校验 → 蒸馏 → 练习 → 重试。

    所有协作者均可注入；缺省则用无 caller 的 fail-soft 角色（离线也能跑，只是
    Verifier→UNVERIFIED、Curriculum→STALLED，不会误判成功）。
    """

    def __init__(
        self,
        *,
        execute: Optional[ExecuteFn] = None,
        verifier: Optional[Verifier] = None,
        curriculum: Optional[CurriculumPlanner] = None,
        actor: Optional[ActorRole] = None,
        memory: Optional[EvolutionMemory] = None,
        stop_policy: str = STOP_CURRICULUM_REVIEW,
        budget: Optional[SelfEvolvingStart] = None,
        max_rounds: int = 8,
        commit_memory: bool = True,
    ) -> None:
        self._execute = execute
        self.verifier = verifier or Verifier()
        self.curriculum = curriculum or CurriculumPlanner(memory=memory)
        self.actor = actor or ActorRole()
        self.memory = memory
        self.stop_policy = stop_policy or STOP_CURRICULUM_REVIEW
        self.budget = budget or SelfEvolvingStart()
        self.max_rounds = max(1, int(max_rounds))
        self.commit_memory = commit_memory
        # 运行期轨迹（供可观测 / 续跑）
        self.history: List[Dict[str, Any]] = []

    # -- 环境执行 seam -----------------------------------------------------
    def set_execute(self, execute: ExecuteFn) -> "EvolutionLoop":
        self._execute = execute
        return self

    def _attempt(self, task: str) -> AttemptResult:
        """执行一次任务尝试。无 execute seam → 空尝试（evidence 为空 → 校验会 UNVERIFIED）。"""
        if self._execute is None:
            logger.debug("evolution.loop: 无 execute seam，返回空尝试")
            return AttemptResult(observation="", evidence=Evidence(task=task))
        try:
            raw = self._execute(task)
        except Exception as exc:  # 基础设施异常：不当 PASS/FAIL，交校验判 UNVERIFIED
            logger.warning("evolution.loop: 执行任务异常: %s", exc)
            return AttemptResult(
                observation=f"（执行异常：{exc}）",
                evidence=Evidence(task=task, logs=f"执行异常：{exc}"),
            )
        return AttemptResult.coerce(task, raw)

    # -- 迭代反馈注入（让“重试”成为“改进”而非盲目重做）-------------------
    @staticmethod
    def _with_feedback(task: str, feedback: str) -> str:
        """把上一轮校验反馈拼到任务串后，驱动 Actor 在本轮针对性改进（迭代而非重做）。"""
        if not feedback:
            return task
        return (
            f"{task}\n\n"
            "【上一轮校验反馈 · 请在本轮针对性改进，不要推倒重来】\n"
            f"{feedback}"
        )

    @staticmethod
    def _feedback_from(verdict: Verdict, decision: Any) -> str:
        """从校验结论 + 课程建议提炼下一轮的改进指引（两者皆空 → ""）。"""
        parts: List[str] = []
        reason = (getattr(verdict, "reason", "") or "").strip()
        if reason:
            parts.append(f"校验结论 {verdict.verdict.value}：{reason}")
        rationale = (getattr(decision, "rationale", "") or "").strip()
        if rationale:
            parts.append(f"课程建议：{rationale}")
        return "\n".join(parts)

    # -- 记忆提交（C3：仅在已验证经验边界提交）----------------------------
    def _commit(
        self, task_id: str, attempt: AttemptResult, verdict: Verdict, learning: ActorLearning
    ) -> None:
        """把一条已验证经验提交到三层记忆（轨迹 + 洞察）。记忆缺失/关闭则跳过。"""
        if not self.commit_memory or self.memory is None:
            return
        try:
            self.memory.add_trajectory(
                task_id=task_id,
                content=attempt.observation or attempt.evidence.render(),
                outcome=verdict.verdict.value,
            )
            # 仅当蒸馏合法（diagnosis 非空）才沉淀洞察
            if learning.is_valid():
                confidence = 0.8 if verdict.passed else 0.5
                text = learning.memory or learning.diagnosis
                self.memory.add_insight(
                    text=text,
                    confidence=confidence,
                    source_task_id=task_id,
                    tags=[verdict.verdict.value.lower()],
                )
        except Exception as exc:  # pragma: no cover - 记忆写入失败不阻断闭环
            logger.warning("evolution.loop: 记忆提交失败: %s", exc)

    # -- 主循环 ------------------------------------------------------------
    def run(self, target_task: str, *, task_id: str = "target") -> EvolutionResult:
        """在 ``target_task`` 上跑 DRS 闭环，返回 :class:`EvolutionResult`。"""
        self.history = []
        last_feedback = ""   # 上一轮校验/课程反馈 → 注入下一轮重试，实现“迭代改进”而非盲目重做
        for round_no in range(1, self.max_rounds + 1):
            if self.budget.budget_exhausted():
                return self._stalled("预算耗尽（未收敛）", target_task)
            self.budget.record_target_cycle()

            # 1) Actor 试目标任务（第 2 轮起带上一轮反馈，让 Agent 针对性改进而非推倒重来）
            attempt = self._attempt(self._with_feedback(target_task, last_feedback))
            attempt.evidence.task = target_task   # 校验仍针对原始任务，反馈不污染 evidence.task
            # 2) Verifier 独立校验
            verdict = self.verifier.verify(
                target_task, attempt.evidence, task_id=task_id
            )
            self._record_round(round_no, "target", target_task, verdict)

            # 3) PASS 分支
            if verdict.passed:
                # 提交已验证经验
                learning = self.actor.learn(
                    target_task, attempt.observation, task_id=task_id
                )
                self._commit(task_id, attempt, verdict, learning)
                if self.stop_policy == STOP_VERIFIER_PASS:
                    return self._converged("目标任务通过校验（verifier_pass）", target_task)
                # curriculum_review：PASS 后仍复盘，可能要求再练
                decision = self.curriculum.review(target_task, memory=self.memory)
                if decision.should_practice:
                    last_feedback = self._feedback_from(verdict, decision)
                    self._run_practice(decision, round_no)
                    continue  # 练习后带反馈重试目标任务（迭代改进）
                return self._converged(
                    f"目标任务通过且课程判定收敛：{decision.rationale or '无'}", target_task
                )

            # 4) FAIL / UNVERIFIED 分支：蒸馏 → 课程决策 → 练习/重试/停止
            learning = self.actor.learn(
                target_task, attempt.observation, task_id=task_id
            )
            # FAIL 也学习（PASS/FAIL 都能学习）；UNVERIFIED 不当成功但可沉淀轨迹
            if verdict.failed or verdict.unverified:
                self._commit(task_id, attempt, verdict, learning)

            decision = self.curriculum.review(
                target_task,
                history=f"第 {round_no} 轮校验：{verdict.verdict.value}（{verdict.reason}）",
                memory=self.memory,
            )
            kind = decision.decision
            if kind is CurriculumDecisionKind.CONVERGED:
                # 课程判定收敛但目标未 PASS —— 视为就绪收敛（记录 reason）
                return self._converged(
                    f"课程判定收敛：{decision.rationale or '无'}", target_task
                )
            if kind is CurriculumDecisionKind.STALLED:
                return self._stalled(
                    f"课程判定卡死：{decision.rationale or '无进展'}", target_task
                )
            # PRACTICE / READY：带反馈重试目标任务（迭代改进）；PRACTICE 先跑练习项目
            last_feedback = self._feedback_from(verdict, decision)
            if kind is CurriculumDecisionKind.PRACTICE:
                self._run_practice(decision, round_no)

        return self._stalled(f"达到最大轮次 {self.max_rounds} 仍未收敛", target_task)

    # -- 练习内循环 --------------------------------------------------------
    def _run_practice(self, decision: Any, round_no: int) -> None:
        """执行一个练习项目（Actor→Verifier 内循环），已验证经验即时提交记忆。"""
        practice_task = self.curriculum.next_task("", decision) or decision.next_task
        if not practice_task:
            return
        self.budget.record_practice()
        p_id = f"practice_r{round_no}"
        attempt = self._attempt(practice_task)
        verdict = self.verifier.verify(practice_task, attempt.evidence, task_id=p_id)
        learning = self.actor.learn(practice_task, attempt.observation, task_id=p_id)
        self._record_round(round_no, "practice", practice_task, verdict)
        if verdict.failed or verdict.passed or verdict.unverified:
            self._commit(p_id, attempt, verdict, learning)

    def _record_round(
        self, round_no: int, kind: str, task: str, verdict: Verdict
    ) -> None:
        self.history.append(
            {
                "round": round_no,
                "kind": kind,
                "task": task,
                "verdict": verdict.verdict.value,
                "reason": verdict.reason,
            }
        )

    # -- 结果构造 ----------------------------------------------------------
    def _projects(self) -> List[str]:
        return [h["task"] for h in self.history if h.get("task")]

    def _converged(self, reason: str, target_task: str) -> EvolutionResult:
        return EvolutionResult(
            status=EvolutionStatus.CONVERGED,
            memory=self._memory_digest(),
            projects=self._projects(),
            reason=reason,
        )

    def _stalled(self, reason: str, target_task: str) -> EvolutionResult:
        return EvolutionResult(
            status=EvolutionStatus.STALLED,
            memory=self._memory_digest(),
            projects=self._projects(),
            reason=reason,
        )

    def _memory_digest(self) -> str:
        """汇总当前记忆的顶层洞察（只读，供结果快照）。"""
        if self.memory is None:
            return ""
        try:
            insights = self.memory.insights(active_only=True)
        except Exception:  # pragma: no cover
            return ""
        ranked = sorted(insights, key=lambda i: i.confidence, reverse=True)
        return "\n".join(f"- [{i.confidence:.2f}] {i.text}" for i in ranked[:8])

    # -- C5：可序列化状态（供 goals/SessionDB 续跑）------------------------
    def state(self) -> Dict[str, Any]:
        """导出可序列化状态（预算计数 + 历史），供外层持久化以 /resume 续跑。"""
        return {
            "budget": self.budget.to_dict(),
            "stop_policy": self.stop_policy,
            "history": list(self.history),
        }

    def restore(self, state: Optional[Dict[str, Any]]) -> "EvolutionLoop":
        """从 :meth:`state` 导出的字典恢复循环（fail-soft）。"""
        state = state or {}
        try:
            self.budget = SelfEvolvingStart.from_dict(state.get("budget"))
        except Exception:  # pragma: no cover
            self.budget = SelfEvolvingStart()
        self.stop_policy = state.get("stop_policy") or self.stop_policy
        history = state.get("history")
        if isinstance(history, list):
            self.history = [h for h in history if isinstance(h, dict)]
        return self


def run_evolution(
    target_task: str,
    *,
    execute: Optional[ExecuteFn] = None,
    verifier: Optional[Verifier] = None,
    curriculum: Optional[CurriculumPlanner] = None,
    actor: Optional[ActorRole] = None,
    memory: Optional[EvolutionMemory] = None,
    stop_policy: str = STOP_CURRICULUM_REVIEW,
    max_rounds: int = 8,
    state: Optional[Dict[str, Any]] = None,
) -> EvolutionResult:
    """一次性便捷入口：构造 :class:`EvolutionLoop`（可 ``state`` 续跑）并运行。"""
    loop = EvolutionLoop(
        execute=execute,
        verifier=verifier,
        curriculum=curriculum,
        actor=actor,
        memory=memory,
        stop_policy=stop_policy,
        max_rounds=max_rounds,
    )
    if state:
        loop.restore(state)
    return loop.run(target_task)


__all__ = [
    "AttemptResult",
    "EvolutionLoop",
    "run_evolution",
    "ExecuteFn",
    "STOP_CURRICULUM_REVIEW",
    "STOP_VERIFIER_PASS",
]
