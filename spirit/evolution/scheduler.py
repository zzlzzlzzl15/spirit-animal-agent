"""每日定时自主进化调度引擎 —— Spirit Agent Phase 6.F（docs/13 §F3-F4）。

补全 ``cronjob`` 空壳所需的**调度引擎**：支持 interval（每 N 秒）与 daily（每日 HH:MM）
两类计划，``tick(now)`` 由外层驱动（守护线程 / 外部 cron / 事件循环均可），到点执行动作、
汇总结果并生成**日报**（可注入 ``push`` 投递给用户渠道）。

设计要点：

- **注入式时钟**：``clock`` 默认 ``datetime.now``，测试可传固定时间，完全离线确定性；
- **动作注入**：``action`` 是任意无参可调用（如"跑一轮 BRS/DRS + 收集领域信息"）；
- **fail-soft**：单个动作异常不影响其它任务，异常记入日报；
- **传输无关**：``tick`` 返回结构化 :class:`TickReport`（dict 化），投递经 ``push`` seam。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

Action = Callable[[], Any]
Clock = Callable[[], datetime]
Push = Callable[[str], Any]

INTERVAL = "interval"
DAILY = "daily"


def _default_clock() -> datetime:
    return datetime.now()


# ---------------------------------------------------------------------------
# 计划任务
# ---------------------------------------------------------------------------

@dataclass
class ScheduledTask:
    """一个计划任务：interval（每 ``every_seconds`` 秒）或 daily（每日 ``daily_at``）。"""

    name: str
    action: Action
    kind: str = INTERVAL
    every_seconds: Optional[float] = None
    daily_at: Optional[str] = None       # "HH:MM"
    enabled: bool = True
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
    run_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "every_seconds": self.every_seconds,
            "daily_at": self.daily_at,
            "enabled": self.enabled,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "next_run": self.next_run.isoformat() if self.next_run else None,
            "run_count": self.run_count,
        }


@dataclass
class TaskRun:
    """单次任务执行结果。"""

    name: str
    ok: bool
    output: Any = None
    error: str = ""
    at: Optional[datetime] = None


@dataclass
class TickReport:
    """一次 ``tick`` 的汇总（含所有到期任务的执行结果）。"""

    at: Optional[datetime] = None
    runs: List[TaskRun] = field(default_factory=list)
    report: str = ""

    @property
    def ran(self) -> int:
        return len(self.runs)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.runs if not r.ok)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "at": self.at.isoformat() if self.at else None,
            "ran": self.ran,
            "failed": self.failed,
            "runs": [
                {"name": r.name, "ok": r.ok, "error": r.error, "output": str(r.output)}
                for r in self.runs
            ],
            "report": self.report,
        }


def _parse_daily(at: Optional[str]) -> Optional[timedelta]:
    """把 "HH:MM" 解析为当天零点的 timedelta 偏移；非法 → None。"""
    if not at:
        return None
    try:
        hh, mm = str(at).strip().split(":")[:2]
        hour, minute = int(hh), int(mm)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return timedelta(hours=hour, minutes=minute)
    except (ValueError, AttributeError):
        pass
    return None


# ---------------------------------------------------------------------------
# 调度引擎
# ---------------------------------------------------------------------------

class Scheduler:
    """轻量调度引擎：登记任务 → ``tick(now)`` 执行到期任务 → 生成日报。"""

    def __init__(self, *, clock: Optional[Clock] = None, push: Optional[Push] = None) -> None:
        self._clock = clock or _default_clock
        self._push = push
        self.tasks: Dict[str, ScheduledTask] = {}

    def now(self) -> datetime:
        return self._clock()

    # -- 任务登记 ----------------------------------------------------------
    def add_interval_task(
        self, name: str, action: Action, *, every_seconds: float, enabled: bool = True
    ) -> ScheduledTask:
        task = ScheduledTask(
            name=name, action=action, kind=INTERVAL,
            every_seconds=float(every_seconds), enabled=enabled,
        )
        task.next_run = self._compute_next_run(task, self.now())
        self.tasks[name] = task
        return task

    def add_daily_task(
        self, name: str, action: Action, *, at: str = "09:00", enabled: bool = True
    ) -> ScheduledTask:
        task = ScheduledTask(
            name=name, action=action, kind=DAILY, daily_at=at, enabled=enabled
        )
        task.next_run = self._compute_next_run(task, self.now())
        self.tasks[name] = task
        return task

    def remove_task(self, name: str) -> bool:
        return self.tasks.pop(name, None) is not None

    def list_tasks(self) -> List[ScheduledTask]:
        return list(self.tasks.values())

    def get_task(self, name: str) -> Optional[ScheduledTask]:
        return self.tasks.get(name)

    # -- next_run 计算 -----------------------------------------------------
    def _compute_next_run(self, task: ScheduledTask, now: datetime) -> Optional[datetime]:
        if task.kind == INTERVAL:
            return now + timedelta(seconds=task.every_seconds or 0)
        offset = _parse_daily(task.daily_at)
        if offset is None:
            logger.warning("evolution.scheduler: 非法 daily_at %r，任务 %s 不调度",
                           task.daily_at, task.name)
            return None
        candidate = now.replace(hour=0, minute=0, second=0, microsecond=0) + offset
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    # -- 到期判定 ----------------------------------------------------------
    def due_tasks(self, now: Optional[datetime] = None) -> List[ScheduledTask]:
        now = now or self.now()
        due: List[ScheduledTask] = []
        for task in self.tasks.values():
            if not task.enabled or task.next_run is None:
                continue
            if now >= task.next_run:
                due.append(task)
        return due

    # -- 主入口 ------------------------------------------------------------
    def tick(self, now: Optional[datetime] = None) -> TickReport:
        """执行所有到期任务，汇总为 :class:`TickReport`（并生成日报文本）。"""
        now = now or self.now()
        report = TickReport(at=now)
        for task in self.due_tasks(now):
            run = self._run_task(task, now)
            report.runs.append(run)
            task.last_run = now
            task.run_count += 1
            task.next_run = self._compute_next_run(task, now)
        report.report = self.build_report(report)
        if report.runs and self._push is not None:
            self._deliver(report.report)
        return report

    def _run_task(self, task: ScheduledTask, now: datetime) -> TaskRun:
        try:
            output = task.action()
            return TaskRun(name=task.name, ok=True, output=output, at=now)
        except Exception as exc:
            logger.warning("evolution.scheduler: 任务 %s 执行失败: %s", task.name, exc)
            return TaskRun(name=task.name, ok=False, error=str(exc), at=now)

    # -- 日报（F4）---------------------------------------------------------
    def build_report(self, tick_report: TickReport) -> str:
        """把一次 tick 的结果渲染成日报文本。"""
        when = tick_report.at.strftime("%Y-%m-%d %H:%M") if tick_report.at else "（未知时间）"
        lines = [f"【自进化日报 · {when}】",
                 f"本轮执行 {tick_report.ran} 个任务，失败 {tick_report.failed} 个。"]
        for run in tick_report.runs:
            if run.ok:
                summary = str(run.output) if run.output is not None else "完成"
                lines.append(f"  ✓ {run.name}：{summary}")
            else:
                lines.append(f"  ✗ {run.name}：{run.error}")
        if not tick_report.runs:
            lines.append("  （本轮无到期任务）")
        return "\n".join(lines)

    def _deliver(self, report_text: str) -> None:
        try:
            self._push(report_text)  # type: ignore[misc]
        except Exception as exc:  # pragma: no cover - 投递失败不影响调度
            logger.warning("evolution.scheduler: 日报投递失败: %s", exc)


__all__ = [
    "Scheduler",
    "ScheduledTask",
    "TaskRun",
    "TickReport",
    "INTERVAL",
    "DAILY",
]
