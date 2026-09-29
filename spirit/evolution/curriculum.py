"""Curriculum 课程规划者 —— Spirit Agent Phase 6.C（docs/13 §Phase C / §2.1）。

职责：**复盘学习进度 → 出下一个练习/目标任务 + 继续/停止决策**。铁律：Curriculum
**不打分、不写 Actor 记忆**，记忆视图默认只读；``stalled`` 必须与 ``converged`` 严格区分。

本模块在 :class:`~spirit.evolution.roles.CurriculumRole`（LLM 决策）之上，补一层
"进度复盘"编排：从 :class:`~spirit.evolution.memory.EvolutionMemory` 只读地汇总近期
洞察与轨迹结果，渲染成进度上下文喂给 Curriculum 角色，产出结构化
:class:`~spirit.evolution.protocol.CurriculumDecision`。裁决范式对齐
:mod:`spirit.goals.judge`（fail-open：无法决策时不臆断成功）。
"""

from __future__ import annotations

import logging
from typing import List, Optional

from spirit.evolution.memory import EvolutionMemory
from spirit.evolution.protocol import CurriculumDecision, CurriculumDecisionKind
from spirit.evolution.roles import LLMCaller, CurriculumRole

logger = logging.getLogger(__name__)

# 无法复盘/无 caller 时的安全默认决策（绝不臆断 converged）
_STALL = CurriculumDecisionKind.STALLED


class CurriculumPlanner:
    """课程规划器：只读复盘记忆 + 调用 Curriculum 角色产出决策。"""

    def __init__(
        self,
        *,
        role: Optional[CurriculumRole] = None,
        caller: Optional[LLMCaller] = None,
        memory: Optional[EvolutionMemory] = None,
        insight_limit: int = 8,
        trajectory_limit: int = 8,
    ) -> None:
        self.role = role or CurriculumRole(caller)
        self.memory = memory
        self.insight_limit = insight_limit
        self.trajectory_limit = trajectory_limit

    # -- 进度复盘（只读记忆视图）------------------------------------------
    def progress_summary(self, memory: Optional[EvolutionMemory] = None) -> str:
        """从记忆只读地汇总"已沉淀洞察 + 近期轨迹结果"，作为 Curriculum 上下文。

        无记忆或记忆为空时返回占位文本（不抛）。
        """
        mem = memory or self.memory
        if mem is None:
            return "（无历史记忆）"
        lines: List[str] = []
        try:
            insights = mem.insights(active_only=True)
        except Exception as exc:  # pragma: no cover - 记忆读取失败降级
            logger.debug("evolution.curriculum: 读取洞察失败: %s", exc)
            insights = []
        if insights:
            ranked = sorted(insights, key=lambda i: i.confidence, reverse=True)
            lines.append("已沉淀洞察（按置信度）：")
            for ins in ranked[: self.insight_limit]:
                lines.append(f"  - [{ins.confidence:.2f}] {ins.text}")
        try:
            trajectories = mem.trajectories()
        except Exception as exc:  # pragma: no cover
            logger.debug("evolution.curriculum: 读取轨迹失败: %s", exc)
            trajectories = []
        if trajectories:
            lines.append("近期探索结果：")
            for tr in trajectories[-self.trajectory_limit:]:
                outcome = tr.outcome or "（未裁决）"
                lines.append(f"  - {tr.task_id}: {outcome}")
        return "\n".join(lines) if lines else "（无历史记忆）"

    # -- 决策 --------------------------------------------------------------
    def review(
        self,
        target_task: str,
        *,
        history: str = "",
        memory: Optional[EvolutionMemory] = None,
    ) -> CurriculumDecision:
        """复盘进度并决定下一步（练习/就绪/收敛/卡死）。

        进度上下文 = 调用方给的 ``history`` + 记忆只读汇总。无 caller → fail-open
        返回 ``STALLED``（由 :class:`~spirit.evolution.roles.CurriculumRole` 保证）。
        """
        summary = self.progress_summary(memory)
        progress = "\n\n".join(p for p in (history, summary) if p)
        decision = self.role.decide(target_task, progress)
        # 决策类型归一（role 已保证是 CurriculumDecisionKind）
        if decision.decision not in set(CurriculumDecisionKind):
            decision.decision = _STALL
        return decision

    def next_task(self, target_task: str, decision: CurriculumDecision) -> str:
        """返回下一步该执行的任务：练习任务优先，否则回退到目标任务。"""
        if decision.should_practice and decision.next_task:
            return decision.next_task
        return target_task


__all__ = ["CurriculumPlanner"]
