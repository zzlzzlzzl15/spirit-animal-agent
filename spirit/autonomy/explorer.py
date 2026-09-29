"""常驻自主探索循环（Autonomy Loop）—— Spirit Agent Phase 7.B。

解决"任务做完就停"的问题：把 Phase 6 的能力单元串成**永不停机的外层循环**::

    空闲/定时触发 → propose 候选探索目标（>1 时 HITL 分叉询问）
      → EvolutionLoop(DRS) 执行深挖 → 遇 STALLED/分叉 → HITL 问用户
         （超时/无应答 → fail-open 自主决策）
      → 探索笔记写入 Sandbox（只写沙箱）→ 沉淀记忆 → 提议下一个 …（不回到提示符）

形态（用户决策）：**桌宠内置**常驻 + **限定沙箱目录可写**。故本循环：

- 由 :class:`~spirit.evolution.scheduler.Scheduler` 驱动（interval 探索 + daily 日报），
  ``run_forever`` / ``start`` 提供常驻线程；``step`` / ``tick`` 可单步、离线、确定性测试；
- 决策经 :class:`~spirit.evolution.hitl.HumanInTheLoop`：该问就问（桥接桌宠气泡），
  没回答就 ``fail-open`` 自主决策并继续（铁律，不卡死）；
- 一切写操作经 :class:`~spirit.autonomy.sandbox.Sandbox` 护栏，绝不触碰主代码库。

注入式可测试：propose / execute / verifier / actor / curriculum / memory / hitl /
sandbox / clock / sleep 全部可注入，整条循环可完全离线跑通。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from spirit.evolution.curriculum import CurriculumPlanner
from spirit.evolution.hitl import HumanInTheLoop, HumanQuery
from spirit.evolution.loop import EvolutionLoop, STOP_CURRICULUM_REVIEW
from spirit.evolution.memory import EvolutionMemory
from spirit.evolution.protocol import EvolutionResult, EvolutionStatus
from spirit.evolution.roles import ActorRole, LLMCaller
from spirit.evolution.scheduler import Scheduler, TickReport
from spirit.evolution.verifier import Verifier

from spirit.autonomy.sandbox import Sandbox

logger = logging.getLogger(__name__)

# 默认探索间隔（秒）与日报时间
DEFAULT_INTERVAL_SECONDS = 300.0
DEFAULT_DAILY_AT = "09:00"
IDLE = "IDLE"  # 无候选目标时的空转状态（区别于 STALLED/CONVERGED）


# ---------------------------------------------------------------------------
# 单轮探索结果
# ---------------------------------------------------------------------------

@dataclass
class ExploreCycle:
    """一轮自主探索的结果（含决策来源与沙箱笔记路径）。"""

    cycle_no: int = 0
    task: str = ""
    status: str = IDLE                 # IDLE / CONVERGED / STALLED / READY_FOR_RETRY
    decision_source: str = "none"      # user | auto | none
    decision_text: str = ""
    note_path: str = ""
    summary: str = ""

    @property
    def idle(self) -> bool:
        return self.status == IDLE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cycle_no": self.cycle_no,
            "task": self.task,
            "status": self.status,
            "decision_source": self.decision_source,
            "decision_text": self.decision_text,
            "note_path": self.note_path,
            "summary": self.summary,
        }


# propose seam：接收记忆，返回候选探索目标列表
ProposeFn = Callable[[Optional[EvolutionMemory]], List[str]]
SleepFn = Callable[[float], Any]


# ---------------------------------------------------------------------------
# 常驻自主循环
# ---------------------------------------------------------------------------

class AutonomyLoop:
    """永不停机的自主探索外层循环（桌宠内置形态）。"""

    def __init__(
        self,
        *,
        propose: Optional[ProposeFn] = None,
        caller: Optional[LLMCaller] = None,
        execute: Optional[Callable[[str], Any]] = None,
        verifier: Optional[Verifier] = None,
        actor: Optional[ActorRole] = None,
        curriculum: Optional[CurriculumPlanner] = None,
        memory: Optional[EvolutionMemory] = None,
        hitl: Optional[HumanInTheLoop] = None,
        sandbox: Optional[Sandbox] = None,
        clock: Optional[Callable[[], datetime]] = None,
        push: Optional[Callable[[str], Any]] = None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        daily_at: str = DEFAULT_DAILY_AT,
        max_cycles: Optional[int] = None,
        stop_policy: str = STOP_CURRICULUM_REVIEW,
        max_rounds: int = 5,
        sleep: Optional[SleepFn] = None,
    ) -> None:
        self._propose = propose
        self._caller = caller
        self.memory = memory
        self.sandbox = sandbox or Sandbox()
        self.hitl = hitl or HumanInTheLoop(memory=memory)
        # 常驻探索缺省：对任务自动迭代打磨——curriculum_review（PASS 后仍可要求再练/再改）
        # + 多轮次，配合 loop 的“上一轮反馈注入”让每轮针对性改进直至真收敛（而非一次即停）。
        self.stop_policy = stop_policy or STOP_CURRICULUM_REVIEW
        self.max_rounds = max(1, int(max_rounds))
        self._evolution = EvolutionLoop(
            execute=execute, verifier=verifier, actor=actor,
            curriculum=curriculum or CurriculumPlanner(memory=memory),
            memory=memory,
            stop_policy=self.stop_policy,
            max_rounds=self.max_rounds,
        )
        self._clock = clock or datetime.now
        self._sleep = sleep or time.sleep
        self.interval_seconds = float(interval_seconds)
        self.max_cycles = max_cycles
        self.cycles: List[ExploreCycle] = []
        self._cycle_no = 0
        self._stop = threading.Event()

        # Scheduler 驱动：interval 探索 + daily 日报
        self.scheduler = Scheduler(clock=self._clock, push=push)
        self.scheduler.add_interval_task(
            "autonomy_explore", self.step, every_seconds=self.interval_seconds
        )
        self.scheduler.add_daily_task("autonomy_report", self.summary, at=daily_at)

    # -- 目标提议 ---------------------------------------------------------
    def propose_next(self) -> Optional[str]:
        """产出下一个探索目标；多候选时经 HITL 分叉询问（无应答自主取首选）。"""
        candidates = self._candidates()
        candidates = [c for c in candidates if str(c).strip()]
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        answer = self.hitl.consult(HumanQuery(
            question="自主探索遇到路线分叉：下一步优先探索哪个方向？",
            options=candidates,
            is_fork=True,
            confidence=0.4,           # 分叉=低置信，触发询问
            task_id=f"auto-fork-{self._cycle_no + 1}",
        ))
        chosen = (answer.text or "").strip()
        return chosen if chosen in candidates else candidates[0]

    def _candidates(self) -> List[str]:
        if self._propose is not None:
            try:
                raw = self._propose(self.memory)
                return list(raw or [])
            except Exception as exc:
                logger.warning("autonomy.explorer: propose 异常 → 空候选: %s", exc)
                return []
        return self._default_propose()

    def _default_propose(self) -> List[str]:
        """无注入 propose 时用 LLM 生成候选探索目标；无 caller/异常 → []（fail-soft）。"""
        if self._caller is None:
            return []
        prompt = (
            "你是自主探索规划器。基于当前记忆，提出 1-3 个值得探索的新任务方向，"
            "每行一个，不要编号、不要解释。\n当前记忆摘要：\n"
            + (CurriculumPlanner(memory=self.memory).progress_summary(self.memory))
        )
        try:
            raw = self._caller(
                [{"role": "user", "content": prompt}], 0.7, 300, 30.0
            )
        except Exception as exc:
            logger.warning("autonomy.explorer: 提议调用失败 → 空候选: %s", exc)
            return []
        return [line.strip(" -·*") for line in str(raw or "").splitlines() if line.strip()]

    # -- 单轮探索（可单步测试）-------------------------------------------
    def step(self) -> ExploreCycle:
        """执行一轮自主探索：提议 → 深挖 → 决策 → 写沙箱笔记 → 沉淀。"""
        self._cycle_no += 1
        cycle = ExploreCycle(cycle_no=self._cycle_no)

        target = self.propose_next()
        if not target:
            cycle.status = IDLE
            cycle.summary = "无候选探索目标，空转等待下一轮"
            self.cycles.append(cycle)
            return cycle

        cycle.task = target
        result: EvolutionResult = self._evolution.run(
            target, task_id=f"auto-{self._cycle_no}"
        )
        cycle.status = result.status.value

        # 卡死/分叉 → 问用户；无应答 fail-open 自主决策并继续
        if result.status is EvolutionStatus.STALLED:
            answer = self.hitl.consult(HumanQuery(
                question=f"探索「{target}」陷入停滞，如何继续？",
                options=["换一个新方向继续探索", "在同方向降低难度重试", "暂停自主探索"],
                context=result.reason or "",
                is_fork=True,
                confidence=0.3,
                task_id=f"auto-{self._cycle_no}",
            ))
            cycle.decision_source = "user" if answer.from_user else "auto"
            cycle.decision_text = answer.text

        cycle.note_path = self._write_note(cycle, result)
        cycle.summary = result.reason or result.status.value
        self.cycles.append(cycle)
        return cycle

    def _write_note(self, cycle: ExploreCycle, result: EvolutionResult) -> str:
        """把本轮探索笔记写入沙箱（唯一写出口）。"""
        rel = f"notes/cycle-{cycle.cycle_no:04d}.md"
        body = (
            f"# 自主探索笔记 #{cycle.cycle_no}\n\n"
            f"- 目标: {cycle.task}\n"
            f"- 状态: {cycle.status}\n"
            f"- 决策来源: {cycle.decision_source}\n"
            f"- 决策: {cycle.decision_text or '（无）'}\n"
            f"- 结论: {result.reason or result.memory or '（无）'}\n"
        )
        try:
            self.sandbox.write_text(rel, body)
            return rel
        except Exception as exc:  # 沙箱写失败不阻断循环
            logger.warning("autonomy.explorer: 笔记写入失败: %s", exc)
            return ""

    # -- 调度驱动 ---------------------------------------------------------
    def tick(self, now: Optional[datetime] = None) -> TickReport:
        """驱动 Scheduler：到点执行探索 / 生成日报。"""
        return self.scheduler.tick(now or self._clock())

    def run_forever(self, *, stop: Optional[threading.Event] = None,
                    poll: float = 1.0) -> None:
        """常驻循环：周期性 ``tick`` 直到 ``stop`` 被置位或达 ``max_cycles``。"""
        stop_event = stop or self._stop
        while not stop_event.is_set():
            self.tick()
            if self.max_cycles is not None and len(self.cycles) >= self.max_cycles:
                break
            stop_event.wait(poll)

    def start(self) -> threading.Thread:
        """以守护线程启动常驻循环。"""
        self._stop.clear()
        thread = threading.Thread(target=self.run_forever, daemon=True,
                                  name="spirit-autonomy")
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return not self._stop.is_set()

    # -- 日报 -------------------------------------------------------------
    def summary(self) -> str:
        """近期探索摘要（供 daily 日报推送）。"""
        recent = self.cycles[-5:]
        lines = [f"【自主探索摘要 · 共 {len(self.cycles)} 轮】"]
        if not recent:
            lines.append("  （尚无探索记录）")
        for c in recent:
            lines.append(f"  #{c.cycle_no} [{c.status}] {c.task or '（空转）'}")
        return "\n".join(lines)

    def state(self) -> Dict[str, Any]:
        return {
            "cycle_no": self._cycle_no,
            "cycles": [c.to_dict() for c in self.cycles],
            "running": self.running,
        }


__all__ = ["AutonomyLoop", "ExploreCycle", "ProposeFn", "IDLE",
           "DEFAULT_INTERVAL_SECONDS", "DEFAULT_DAILY_AT"]
