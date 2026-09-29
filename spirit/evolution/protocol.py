"""自进化角色协议数据类 —— Spirit Agent Phase 6.A。

对标 RSIAgent（``core/self_evolving_loop.py`` 的状态与数据结构，见
``docs/13-self-evolution-rsi.md`` §2.3），按 Spirit「精简子集 + 可测试」约定
裁剪为**纯数据类 + 枚举**：无网络、无 IO、无导入期副作用，可完全离线测试。

三角色闭环（docs/13 §2.1）::

    Curriculum（派任务）→ Actor（执行 + 蒸馏）→ Verifier（独立校验）

铁律（docs/13 §2.3）：

- ``STALLED``（卡死）与预算耗尽必须与"成功收敛"（``CONVERGED``）可区分；
- 基础设施错误**不得**被当成 PASS/FAIL —— 用 ``UNVERIFIED``；
- :class:`ActorLearning` 的 ``diagnosis`` 必须非空（诊断是学习的前提）；
- :class:`Verdict` 的 ``reason`` 必须非空（裁决必须可解释、可审计）。

所有数据类都提供 ``to_dict``/``from_dict`` 以便落盘（:mod:`spirit.evolution.memory`）
与跨进程传输；``from_dict`` 对缺失/非法字段一律 fail-soft（不抛）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class TargetVerdict(str, Enum):
    """Verifier 对目标任务的裁决（docs/13 §2.3）。"""

    PASS = "PASS"
    FAIL = "FAIL"
    UNVERIFIED = "UNVERIFIED"  # 基础设施异常 / 证据不足 —— 不计入学习


class EvolutionStatus(str, Enum):
    """一轮进化后的状态（docs/13 §2.3）。

    ``READY_FOR_RETRY`` / ``STALLED`` 必须与 ``CONVERGED`` 可区分。
    """

    READY_FOR_RETRY = "READY_FOR_RETRY"  # 已蒸馏经验，可重试目标任务
    STALLED = "STALLED"                  # 卡死（预算耗尽 / 无进展），非成功
    CONVERGED = "CONVERGED"              # 成功收敛（目标 PASS 或课程判定就绪）


class RoleName(str, Enum):
    """自进化三角色（docs/13 §2.1）。"""

    ACTOR = "actor"
    VERIFIER = "verifier"
    CURRICULUM = "curriculum"


class CurriculumDecisionKind(str, Enum):
    """Curriculum 的继续/停止决策类型（docs/13 §2.2 Phase 2 停止策略）。"""

    PRACTICE = "practice"    # 需要再练：派一个练习项目
    READY = "ready"          # 就绪：可重试目标任务
    CONVERGED = "converged"  # 收敛：结束进化
    STALLED = "stalled"      # 卡死：无进展，停止（区别于成功）


# ---------------------------------------------------------------------------
# 容错解析辅助
# ---------------------------------------------------------------------------

def parse_verdict_value(raw: Any) -> TargetVerdict:
    """把任意输入容错解析为 :class:`TargetVerdict`。

    无法识别时返回 ``UNVERIFIED``（铁律：未知/异常绝不当成 PASS 或 FAIL）。
    """
    if isinstance(raw, TargetVerdict):
        return raw
    text = str(raw or "").strip().upper()
    for member in TargetVerdict:
        if member.value == text:
            return member
    # 常见同义词归一
    aliases = {
        "PASSED": TargetVerdict.PASS,
        "OK": TargetVerdict.PASS,
        "SUCCESS": TargetVerdict.PASS,
        "FAILED": TargetVerdict.FAIL,
        "ERROR": TargetVerdict.UNVERIFIED,
        "INFRA": TargetVerdict.UNVERIFIED,
        "UNKNOWN": TargetVerdict.UNVERIFIED,
        "": TargetVerdict.UNVERIFIED,
    }
    return aliases.get(text, TargetVerdict.UNVERIFIED)


def parse_status_value(raw: Any) -> EvolutionStatus:
    """把任意输入容错解析为 :class:`EvolutionStatus`（无法识别→ ``STALLED``）。"""
    if isinstance(raw, EvolutionStatus):
        return raw
    text = str(raw or "").strip().upper()
    for member in EvolutionStatus:
        if member.value == text:
            return member
    return EvolutionStatus.STALLED


def parse_decision_kind(raw: Any) -> CurriculumDecisionKind:
    """把任意输入容错解析为 :class:`CurriculumDecisionKind`（无法识别→ ``STALLED``）。"""
    if isinstance(raw, CurriculumDecisionKind):
        return raw
    text = str(raw or "").strip().lower()
    for member in CurriculumDecisionKind:
        if member.value == text:
            return member
    aliases = {
        "retry": CurriculumDecisionKind.READY,
        "ready_for_retry": CurriculumDecisionKind.READY,
        "done": CurriculumDecisionKind.CONVERGED,
        "stop": CurriculumDecisionKind.CONVERGED,
        "wave": CurriculumDecisionKind.PRACTICE,
    }
    return aliases.get(text, CurriculumDecisionKind.STALLED)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class ActorLearning:
    """Actor 蒸馏产物（docs/13 §2.3）：``memory`` + ``diagnosis``。

    ``diagnosis`` **必须非空**——没有诊断就不算学习。
    """

    memory: str = ""
    diagnosis: str = ""
    task_id: str = ""

    def is_valid(self) -> bool:
        """诊断非空即视为合法学习产物。"""
        return bool(self.diagnosis and self.diagnosis.strip())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "memory": self.memory,
            "diagnosis": self.diagnosis,
            "task_id": self.task_id,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ActorLearning":
        data = data or {}
        return cls(
            memory=str(data.get("memory", "") or ""),
            diagnosis=str(data.get("diagnosis", "") or ""),
            task_id=str(data.get("task_id", "") or ""),
        )


@dataclass
class Verdict:
    """Verifier 的裁决 + 理由（docs/13 §B3：理由必须非空）。"""

    verdict: TargetVerdict = TargetVerdict.UNVERIFIED
    reason: str = ""
    task_id: str = ""
    confidence: float = 1.0

    def __post_init__(self) -> None:
        # 允许传入原始字符串，统一归一为枚举
        self.verdict = parse_verdict_value(self.verdict)

    @property
    def passed(self) -> bool:
        return self.verdict is TargetVerdict.PASS

    @property
    def failed(self) -> bool:
        return self.verdict is TargetVerdict.FAIL

    @property
    def unverified(self) -> bool:
        return self.verdict is TargetVerdict.UNVERIFIED

    def is_valid(self) -> bool:
        """理由非空即视为合法裁决（可解释、可审计）。"""
        return bool(self.reason and self.reason.strip())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "task_id": self.task_id,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "Verdict":
        data = data or {}
        try:
            confidence = float(data.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        return cls(
            verdict=parse_verdict_value(data.get("verdict")),
            reason=str(data.get("reason", "") or ""),
            task_id=str(data.get("task_id", "") or ""),
            confidence=confidence,
        )


@dataclass
class CurriculumDecision:
    """Curriculum 的派任务 + 继续/停止决策（docs/13 §2.1 / §C1）。

    Curriculum **不打分、不写 Actor 记忆**；只出下一步任务与决策。
    """

    decision: CurriculumDecisionKind = CurriculumDecisionKind.STALLED
    next_task: str = ""
    rationale: str = ""

    def __post_init__(self) -> None:
        self.decision = parse_decision_kind(self.decision)

    @property
    def should_practice(self) -> bool:
        return self.decision is CurriculumDecisionKind.PRACTICE

    @property
    def should_stop(self) -> bool:
        return self.decision in (
            CurriculumDecisionKind.CONVERGED,
            CurriculumDecisionKind.STALLED,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision.value,
            "next_task": self.next_task,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "CurriculumDecision":
        data = data or {}
        return cls(
            decision=parse_decision_kind(data.get("decision")),
            next_task=str(data.get("next_task", "") or ""),
            rationale=str(data.get("rationale", "") or ""),
        )


@dataclass
class EvolutionResult:
    """一轮进化的结果（docs/13 §2.3）：``status`` + ``memory`` + ``projects`` + ``reason``。"""

    status: EvolutionStatus = EvolutionStatus.STALLED
    memory: str = ""
    projects: List[str] = field(default_factory=list)
    reason: str = ""

    def __post_init__(self) -> None:
        self.status = parse_status_value(self.status)

    @property
    def converged(self) -> bool:
        return self.status is EvolutionStatus.CONVERGED

    @property
    def stalled(self) -> bool:
        return self.status is EvolutionStatus.STALLED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "memory": self.memory,
            "projects": list(self.projects),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "EvolutionResult":
        data = data or {}
        projects = data.get("projects") or []
        if not isinstance(projects, list):
            projects = [str(projects)]
        return cls(
            status=parse_status_value(data.get("status")),
            memory=str(data.get("memory", "") or ""),
            projects=[str(p) for p in projects],
            reason=str(data.get("reason", "") or ""),
        )


@dataclass
class SelfEvolvingStart:
    """进化计数器 + 预算（docs/13 §2.3 / §C4）。

    预算耗尽必须与"成功收敛"可区分：:meth:`budget_exhausted` 单独判定，
    由外层 loop 据此产出 ``STALLED`` 而非 ``CONVERGED``。
    """

    target_cycles: int = 0
    evolutions: int = 0
    practice_projects: int = 0
    max_target_cycles: int = 8
    max_evolutions: int = 16
    max_practice_projects: int = 32

    def record_target_cycle(self) -> None:
        self.target_cycles += 1

    def record_evolution(self) -> None:
        self.evolutions += 1

    def record_practice(self) -> None:
        self.practice_projects += 1

    def budget_exhausted(self) -> bool:
        """任一计数达到上限即视为预算耗尽。"""
        return (
            self.target_cycles >= self.max_target_cycles
            or self.evolutions >= self.max_evolutions
            or self.practice_projects >= self.max_practice_projects
        )

    def remaining(self) -> Dict[str, int]:
        """各维度剩余预算（非负）。"""
        return {
            "target_cycles": max(0, self.max_target_cycles - self.target_cycles),
            "evolutions": max(0, self.max_evolutions - self.evolutions),
            "practice_projects": max(0, self.max_practice_projects - self.practice_projects),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_cycles": self.target_cycles,
            "evolutions": self.evolutions,
            "practice_projects": self.practice_projects,
            "max_target_cycles": self.max_target_cycles,
            "max_evolutions": self.max_evolutions,
            "max_practice_projects": self.max_practice_projects,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "SelfEvolvingStart":
        data = data or {}

        def _int(key: str, default: int) -> int:
            try:
                return int(data.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            target_cycles=_int("target_cycles", 0),
            evolutions=_int("evolutions", 0),
            practice_projects=_int("practice_projects", 0),
            max_target_cycles=_int("max_target_cycles", 8),
            max_evolutions=_int("max_evolutions", 16),
            max_practice_projects=_int("max_practice_projects", 32),
        )


__all__ = [
    "TargetVerdict",
    "EvolutionStatus",
    "RoleName",
    "CurriculumDecisionKind",
    "ActorLearning",
    "Verdict",
    "CurriculumDecision",
    "EvolutionResult",
    "SelfEvolvingStart",
    "parse_verdict_value",
    "parse_status_value",
    "parse_decision_kind",
]
