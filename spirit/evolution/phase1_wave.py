"""BRS 广度并行探索 + wave memory barrier —— Spirit Agent Phase 6.D（docs/13 §2.2 Phase 1 / §D1-D2）。

对标 RSIAgent ``explore/phase1_wave.py``：**广撒网**跑一批多样化练习项目，用一个 wave
（波次）的边界严格控制记忆合并：

1. **同一 pre-wave 快照**：一个 wave 内所有 Actor 从**同一份冻结记忆**出发（并行互不干扰）。
2. **并行执行 + 并行校验**：多 Actor 并行跑各自项目，Verifier 并行独立校验。
3. **wave memory barrier**：**仅当全部项目通过校验**，才按序**串行蒸馏**并合并记忆；
   任一分支失败/未完成 → **阻塞整个 wave，其记忆一律不合并**（防止半成品经验污染）。

铁律（docs/13 §6.3）：记忆只在"已验证经验"边界提交；未通过校验的分支记忆不合并。

**注入式可测试**：``execute`` / ``verifier`` / ``actor`` / ``memory`` 全部可注入，并行度可控
（``parallel=False`` 时确定性串行，便于测试）。整个 wave 离线可跑通。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from spirit.evolution.memory import EvolutionMemory
from spirit.evolution.memory_hash import compute_hash
from spirit.evolution.protocol import ActorLearning, TargetVerdict, Verdict
from spirit.evolution.roles import ActorRole
from spirit.evolution.verifier import Evidence, Verifier

logger = logging.getLogger(__name__)

# 与 loop 一致的 execute seam 签名
ExecuteFn = Callable[[str], Any]


@dataclass
class BranchOutcome:
    """wave 内单个项目分支的产出。"""

    task_id: str
    task: str
    verdict: Verdict
    learning: Optional[ActorLearning] = None
    committed: bool = False

    @property
    def passed(self) -> bool:
        return self.verdict.verdict is TargetVerdict.PASS


@dataclass
class WaveResult:
    """一个 wave 的汇总结果。"""

    outcomes: List[BranchOutcome] = field(default_factory=list)
    pre_wave_hash: str = ""
    post_wave_hash: str = ""
    merged: bool = False          # 记忆是否已合并（barrier 放行）
    reason: str = ""

    @property
    def success(self) -> bool:
        """全部通过 → wave 成功（记忆已合并）。"""
        return bool(self.outcomes) and all(o.passed for o in self.outcomes)

    @property
    def passed_ids(self) -> List[str]:
        return [o.task_id for o in self.outcomes if o.passed]

    @property
    def blocked_ids(self) -> List[str]:
        return [o.task_id for o in self.outcomes if not o.passed]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "merged": self.merged,
            "success": self.success,
            "pre_wave_hash": self.pre_wave_hash,
            "post_wave_hash": self.post_wave_hash,
            "reason": self.reason,
            "branches": [
                {
                    "task_id": o.task_id,
                    "task": o.task,
                    "verdict": o.verdict.verdict.value,
                    "reason": o.verdict.reason,
                    "committed": o.committed,
                }
                for o in self.outcomes
            ],
        }


def _coerce_evidence(task: str, raw: Any) -> Evidence:
    """把 execute 返回强制为 Evidence（fail-soft，与 loop.AttemptResult 同语义）。"""
    if isinstance(raw, Evidence):
        ev = raw
    elif isinstance(raw, dict):
        ev = Evidence.from_dict(raw)
    else:
        ev = Evidence(logs=str(raw or ""))
    if not ev.task:
        ev.task = task
    return ev


class Phase1Wave:
    """BRS 广度并行探索器（一个 wave）。"""

    def __init__(
        self,
        *,
        execute: Optional[ExecuteFn] = None,
        verifier: Optional[Verifier] = None,
        actor: Optional[ActorRole] = None,
        memory: Optional[EvolutionMemory] = None,
        parallel: bool = True,
        max_workers: int = 4,
        commit_memory: bool = True,
    ) -> None:
        self._execute = execute
        self.verifier = verifier or Verifier()
        self.actor = actor or ActorRole()
        self.memory = memory
        self.parallel = parallel
        self.max_workers = max(1, int(max_workers))
        self.commit_memory = commit_memory

    # -- 环境执行 ----------------------------------------------------------
    def _run_one(self, task_id: str, task: str) -> Evidence:
        if self._execute is None:
            return Evidence(task=task)
        try:
            raw = self._execute(task)
        except Exception as exc:  # 基础设施异常 → 空证据（校验会 UNVERIFIED，不当 PASS/FAIL）
            logger.warning("evolution.phase1_wave: 执行 %s 异常: %s", task_id, exc)
            return Evidence(task=task, logs=f"执行异常：{exc}")
        return _coerce_evidence(task, raw)

    def _map(self, fn: Callable[[Any], Any], items: List[Any]) -> List[Any]:
        """并行/串行 map（parallel=False 时确定性串行，便于测试）。"""
        if not self.parallel or len(items) <= 1:
            return [fn(it) for it in items]
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            return list(pool.map(fn, items))

    # -- 主入口 ------------------------------------------------------------
    def run(self, projects: List[Dict[str, str]]) -> WaveResult:
        """跑一个 wave。``projects`` 为 ``[{"task_id": ..., "task": ...}, ...]``。

        仅当全部分支 PASS 才合并记忆（wave memory barrier）；否则整 wave 记忆不落地。
        """
        projects = [self._normalize_project(i, p) for i, p in enumerate(projects or [])]
        result = WaveResult()
        if not projects:
            result.reason = "空 wave（无项目）"
            return result

        result.pre_wave_hash = compute_hash(self.memory) if self.memory else ""

        # 1) 并行执行（同一 pre-wave 记忆快照出发）
        evidences = self._map(
            lambda p: self._run_one(p["task_id"], p["task"]), projects
        )
        # 2) 并行独立校验
        verdicts = self._map(
            lambda pair: self.verifier.verify(
                pair[1]["task"], pair[0], task_id=pair[1]["task_id"]
            ),
            list(zip(evidences, projects)),
        )

        outcomes: List[BranchOutcome] = []
        for proj, ev, verdict in zip(projects, evidences, verdicts):
            outcomes.append(
                BranchOutcome(task_id=proj["task_id"], task=proj["task"], verdict=verdict)
            )
        result.outcomes = outcomes

        # 3) wave memory barrier：全部通过才串行蒸馏 + 合并
        blocked = result.blocked_ids
        if blocked:
            result.merged = False
            result.reason = (
                f"wave 被阻塞：{len(blocked)} 个分支未通过校验（{', '.join(blocked)}），"
                "整 wave 记忆不合并"
            )
            result.post_wave_hash = result.pre_wave_hash
            return result

        # 全通过 → 按序串行蒸馏并即时提交（barrier 放行）
        for outcome, ev in zip(outcomes, evidences):
            learning = self.actor.learn(
                outcome.task, ev.render(), task_id=outcome.task_id
            )
            outcome.learning = learning
            if self._commit(outcome, learning):
                outcome.committed = True

        result.merged = self.commit_memory and self.memory is not None
        result.reason = (
            f"全部 {len(outcomes)} 个分支通过，已按序串行蒸馏并合并记忆"
            if result.merged
            else f"全部 {len(outcomes)} 个分支通过（未启用记忆提交）"
        )
        result.post_wave_hash = compute_hash(self.memory) if self.memory else ""
        return result

    # -- 内部件 ------------------------------------------------------------
    @staticmethod
    def _normalize_project(index: int, proj: Any) -> Dict[str, str]:
        """把项目描述归一为 {task_id, task}（容错 str / dict）。"""
        if isinstance(proj, dict):
            task = str(proj.get("task", "") or "")
            task_id = str(proj.get("task_id", "") or f"wave{index}")
        else:
            task = str(proj or "")
            task_id = f"wave{index}"
        return {"task_id": task_id or f"wave{index}", "task": task}

    def _commit(self, outcome: BranchOutcome, learning: ActorLearning) -> bool:
        """把一条已验证经验合并进记忆（轨迹 + 洞察）。成功返回 True。"""
        if not self.commit_memory or self.memory is None:
            return False
        try:
            self.memory.add_trajectory(
                task_id=outcome.task_id,
                content=outcome.verdict.reason,
                outcome=outcome.verdict.verdict.value,
            )
            if learning.is_valid():
                self.memory.add_insight(
                    text=learning.memory or learning.diagnosis,
                    confidence=0.75,
                    source_task_id=outcome.task_id,
                    tags=["wave", "pass"],
                )
            return True
        except Exception as exc:  # pragma: no cover - 记忆写入失败不阻断
            logger.warning("evolution.phase1_wave: 记忆合并失败: %s", exc)
            return False


def run_wave(
    projects: List[Any],
    *,
    execute: Optional[ExecuteFn] = None,
    verifier: Optional[Verifier] = None,
    actor: Optional[ActorRole] = None,
    memory: Optional[EvolutionMemory] = None,
    parallel: bool = True,
) -> WaveResult:
    """一次性便捷入口：构造 :class:`Phase1Wave` 并跑一个 wave。"""
    wave = Phase1Wave(
        execute=execute, verifier=verifier, actor=actor, memory=memory, parallel=parallel
    )
    return wave.run(projects)


__all__ = [
    "BranchOutcome",
    "WaveResult",
    "Phase1Wave",
    "run_wave",
    "ExecuteFn",
]
