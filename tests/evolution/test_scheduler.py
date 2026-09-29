"""调度引擎测试（Phase 6.F）：interval/daily 计划 + tick 执行 + 日报 + fail-soft。"""

from datetime import datetime, timedelta

from spirit.evolution.scheduler import (
    DAILY,
    INTERVAL,
    Scheduler,
    ScheduledTask,
    TickReport,
)

BASE = datetime(2026, 9, 28, 8, 0, 0)


class FakeClock:
    def __init__(self, now=BASE):
        self.now = now

    def __call__(self):
        return self.now


# --------------------------------------------------------------------------
# interval 计划
# --------------------------------------------------------------------------

def test_interval_not_due_before_elapsed():
    clock = FakeClock()
    sched = Scheduler(clock=clock)
    sched.add_interval_task("t", lambda: "ok", every_seconds=60)
    assert sched.due_tasks(clock.now) == []


def test_interval_due_after_elapsed():
    clock = FakeClock()
    sched = Scheduler(clock=clock)
    sched.add_interval_task("t", lambda: "ok", every_seconds=60)
    later = BASE + timedelta(seconds=61)
    due = sched.due_tasks(later)
    assert len(due) == 1 and due[0].name == "t"


def test_interval_next_run_recomputed_after_tick():
    clock = FakeClock()
    sched = Scheduler(clock=clock)
    task = sched.add_interval_task("t", lambda: "ok", every_seconds=60)
    later = BASE + timedelta(seconds=61)
    sched.tick(later)
    assert task.run_count == 1
    assert task.last_run == later
    assert task.next_run == later + timedelta(seconds=60)


# --------------------------------------------------------------------------
# daily 计划
# --------------------------------------------------------------------------

def test_daily_next_run_today_when_before_time():
    clock = FakeClock(BASE)  # 08:00
    sched = Scheduler(clock=clock)
    task = sched.add_daily_task("daily", lambda: "x", at="09:00")
    assert task.next_run == BASE.replace(hour=9, minute=0)


def test_daily_next_run_tomorrow_when_after_time():
    clock = FakeClock(BASE.replace(hour=10))  # 10:00
    sched = Scheduler(clock=clock)
    task = sched.add_daily_task("daily", lambda: "x", at="09:00")
    assert task.next_run == (BASE + timedelta(days=1)).replace(hour=9, minute=0)


def test_daily_runs_at_scheduled_time():
    clock = FakeClock(BASE)
    sched = Scheduler(clock=clock)
    sched.add_daily_task("daily", lambda: "跑进化", at="09:00")
    # 08:00 未到期
    assert sched.tick(BASE).ran == 0
    # 09:00 到期
    report = sched.tick(BASE.replace(hour=9, minute=0))
    assert report.ran == 1


def test_invalid_daily_at_not_scheduled():
    clock = FakeClock(BASE)
    sched = Scheduler(clock=clock)
    task = sched.add_daily_task("bad", lambda: "x", at="25:99")
    assert task.next_run is None
    assert sched.due_tasks(BASE + timedelta(days=10)) == []


# --------------------------------------------------------------------------
# tick 执行 / fail-soft / 日报
# --------------------------------------------------------------------------

def test_tick_no_due_tasks_empty_report():
    clock = FakeClock()
    sched = Scheduler(clock=clock)
    sched.add_interval_task("t", lambda: "ok", every_seconds=60)
    report = sched.tick(BASE)
    assert report.ran == 0
    assert "无到期任务" in report.report


def test_action_exception_recorded_not_raised():
    clock = FakeClock(BASE)
    sched = Scheduler(clock=clock)

    def boom():
        raise RuntimeError("进化崩了")

    sched.add_interval_task("t", boom, every_seconds=60)
    report = sched.tick(BASE + timedelta(seconds=61))
    assert report.ran == 1
    assert report.failed == 1
    assert report.runs[0].ok is False
    assert "进化崩了" in report.report


def test_build_report_contains_success_output():
    clock = FakeClock(BASE)
    sched = Scheduler(clock=clock)
    sched.add_interval_task("采集行情", lambda: "收集 3 标的", every_seconds=60)
    report = sched.tick(BASE + timedelta(seconds=61))
    assert "自进化日报" in report.report
    assert "采集行情" in report.report
    assert "收集 3 标的" in report.report


def test_push_invoked_when_runs_exist():
    clock = FakeClock(BASE)
    pushed = []
    sched = Scheduler(clock=clock, push=lambda text: pushed.append(text))
    sched.add_interval_task("t", lambda: "ok", every_seconds=60)
    sched.tick(BASE + timedelta(seconds=61))
    assert len(pushed) == 1
    assert "自进化日报" in pushed[0]


def test_push_not_invoked_when_no_runs():
    clock = FakeClock(BASE)
    pushed = []
    sched = Scheduler(clock=clock, push=lambda text: pushed.append(text))
    sched.add_interval_task("t", lambda: "ok", every_seconds=60)
    sched.tick(clock.now)
    assert pushed == []


def test_push_exception_does_not_break_tick():
    clock = FakeClock(BASE)

    def bad_push(text):
        raise RuntimeError("渠道不可用")

    sched = Scheduler(clock=clock, push=bad_push)
    sched.add_interval_task("t", lambda: "ok", every_seconds=60)
    report = sched.tick(BASE + timedelta(seconds=61))  # 不应抛
    assert report.ran == 1


# --------------------------------------------------------------------------
# 任务管理 / 序列化
# --------------------------------------------------------------------------

def test_remove_and_list_and_get():
    sched = Scheduler(clock=FakeClock())
    sched.add_interval_task("a", lambda: 1, every_seconds=10)
    sched.add_daily_task("b", lambda: 2, at="09:00")
    assert len(sched.list_tasks()) == 2
    assert sched.get_task("a").kind == INTERVAL
    assert sched.get_task("b").kind == DAILY
    assert sched.remove_task("a") is True
    assert sched.remove_task("nope") is False
    assert len(sched.list_tasks()) == 1


def test_disabled_task_not_due():
    clock = FakeClock(BASE + timedelta(seconds=61))
    sched = Scheduler(clock=clock)
    sched.add_interval_task("t", lambda: "ok", every_seconds=60, enabled=False)
    assert sched.due_tasks(clock.now) == []


def test_task_and_report_to_dict():
    clock = FakeClock(BASE)
    sched = Scheduler(clock=clock)
    task = sched.add_interval_task("t", lambda: "ok", every_seconds=60)
    d = task.to_dict()
    assert d["name"] == "t" and d["kind"] == INTERVAL
    report = sched.tick(BASE + timedelta(seconds=61))
    rd = report.to_dict()
    assert rd["ran"] == 1 and rd["failed"] == 0
    assert isinstance(rd["runs"], list)
