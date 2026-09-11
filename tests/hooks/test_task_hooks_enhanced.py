"""tests/hooks/test_task_hooks_enhanced.py — 增强任务钩子测试。"""

import pytest
from unittest.mock import MagicMock, patch

from spirit.hooks.hook_manager import HookManager, HookEvent
from spirit.hooks.task_hooks import register_task_hooks


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _make_agent(task_manager=None, status_callback=None):
    """创建模拟 agent。"""
    agent = MagicMock()
    agent.task_manager = task_manager
    agent.status_callback = status_callback
    return agent


# ---------------------------------------------------------------------------
# HookEvent 常量测试
# ---------------------------------------------------------------------------

class TestHookEventConstants:

    def test_task_check_event_exists(self):
        assert hasattr(HookEvent, "ON_TASK_CHECK")
        assert HookEvent.ON_TASK_CHECK == "on_task_check"

    def test_task_decompose_event_exists(self):
        assert hasattr(HookEvent, "ON_TASK_DECOMPOSE")
        assert HookEvent.ON_TASK_DECOMPOSE == "on_task_decompose"

    def test_all_task_events(self):
        task_events = [
            HookEvent.ON_TASK_CREATE,
            HookEvent.ON_TASK_START,
            HookEvent.ON_TASK_COMPLETE,
            HookEvent.ON_TASK_FAIL,
            HookEvent.ON_TASK_PROGRESS,
            HookEvent.ON_TASK_CHECK,
            HookEvent.ON_TASK_DECOMPOSE,
            HookEvent.ON_SUBTASK_CREATE,
            HookEvent.ON_SUBTASK_COMPLETE,
            HookEvent.ON_SUBTASK_FAIL,
        ]
        assert len(task_events) == 10
        assert len(set(task_events)) == 10  # all unique


# ---------------------------------------------------------------------------
# 任务创建钩子
# ---------------------------------------------------------------------------

class TestTaskCreateHook:

    def test_create_logs(self):
        hm = HookManager()
        agent = _make_agent()
        register_task_hooks(hm, agent)
        # Should not raise
        hm.emit(HookEvent.ON_TASK_CREATE, task_id="t1", title="Test", task_type="task")

    def test_create_calls_status_callback(self):
        hm = HookManager()
        callback = MagicMock()
        agent = _make_agent(status_callback=callback)
        register_task_hooks(hm, agent)
        hm.emit(HookEvent.ON_TASK_CREATE, task_id="t1", title="Test", task_type="task")
        callback.assert_called_once()
        args = callback.call_args
        assert args[0][0] == "task_created"

    def test_create_callback_failure_isolated(self):
        hm = HookManager()
        callback = MagicMock(side_effect=RuntimeError("boom"))
        agent = _make_agent(status_callback=callback)
        register_task_hooks(hm, agent)
        # Should not raise
        hm.emit(HookEvent.ON_TASK_CREATE, task_id="t1", title="Test", task_type="task")


# ---------------------------------------------------------------------------
# 任务预检钩子
# ---------------------------------------------------------------------------

class TestTaskCheckHook:

    def test_check_passed(self):
        hm = HookManager()
        agent = _make_agent()
        register_task_hooks(hm, agent)
        hm.emit(HookEvent.ON_TASK_CHECK, task_id="t1", passed=True)

    def test_check_failed_calls_callback(self):
        hm = HookManager()
        callback = MagicMock()
        agent = _make_agent(status_callback=callback)
        register_task_hooks(hm, agent)
        hm.emit(
            HookEvent.ON_TASK_CHECK,
            task_id="t1",
            passed=False,
            failures=["missing dep"],
        )
        callback.assert_called_once()
        args = callback.call_args
        assert args[0][0] == "task_check_failed"


# ---------------------------------------------------------------------------
# 任务进度钩子
# ---------------------------------------------------------------------------

class TestTaskProgressHook:

    def test_progress_emits(self):
        hm = HookManager()
        callback = MagicMock()
        agent = _make_agent(status_callback=callback)
        register_task_hooks(hm, agent)
        hm.emit(
            HookEvent.ON_TASK_PROGRESS,
            task_id="t1",
            progress=0.5,
            message="half done",
        )
        callback.assert_called_once()
        call_args = callback.call_args
        assert call_args[0][0] == "task_progress"


# ---------------------------------------------------------------------------
# 任务完成钩子
# ---------------------------------------------------------------------------

class TestTaskCompleteHook:

    def test_complete_calls_callback(self):
        hm = HookManager()
        callback = MagicMock()
        agent = _make_agent(status_callback=callback)
        register_task_hooks(hm, agent)
        hm.emit(HookEvent.ON_TASK_COMPLETE, task_id="t1", result="done")
        callback.assert_called_once()

    def test_complete_triggers_auto_start(self):
        """任务完成时自动启动就绪子任务。"""
        from spirit.task.models import Task, TaskStatus

        # 创建一个 pending 子任务，其依赖已满足
        child_task = Task(
            id="child1", title="child", status=TaskStatus.PENDING,
            parent_id="parent1",
            metadata={"depends_on": ["dep1"]},
        )

        dep_task = Task(id="dep1", title="dep", status=TaskStatus.COMPLETED)

        tm = MagicMock()
        tm.get_subtasks.return_value = [child_task]
        tm.get.side_effect = lambda tid: {"dep1": dep_task}.get(tid)

        hm = HookManager()
        agent = _make_agent(task_manager=tm)
        register_task_hooks(hm, agent)

        hm.emit(HookEvent.ON_TASK_COMPLETE, task_id="parent1", result="done")

        # 验证子任务被标记为 running
        tm.update_status.assert_called_once()
        call_args = tm.update_status.call_args
        assert call_args[0][0] == "child1"
        assert call_args[0][1] == TaskStatus.RUNNING


# ---------------------------------------------------------------------------
# 任务失败钩子
# ---------------------------------------------------------------------------

class TestTaskFailHook:

    def test_fail_calls_callback(self):
        hm = HookManager()
        callback = MagicMock()
        agent = _make_agent(status_callback=callback)
        register_task_hooks(hm, agent)
        hm.emit(HookEvent.ON_TASK_FAIL, task_id="t1", error="something broke")
        callback.assert_called_once()
        assert callback.call_args[0][0] == "task_failed"


# ---------------------------------------------------------------------------
# 任务分解钩子
# ---------------------------------------------------------------------------

class TestTaskDecomposeHook:

    def test_decompose_calls_callback(self):
        hm = HookManager()
        callback = MagicMock()
        agent = _make_agent(status_callback=callback)
        register_task_hooks(hm, agent)
        hm.emit(
            HookEvent.ON_TASK_DECOMPOSE,
            task_id="t1",
            subtask_count=3,
            strategy="template",
        )
        callback.assert_called_once()
        assert callback.call_args[0][0] == "task_decomposed"


# ---------------------------------------------------------------------------
# 无 agent 组件时的防御性测试
# ---------------------------------------------------------------------------

class TestDefensiveNoAgent:

    def test_no_task_manager(self):
        hm = HookManager()
        agent = _make_agent(task_manager=None)
        register_task_hooks(hm, agent)
        # Complete should not crash even without task_manager
        hm.emit(HookEvent.ON_TASK_COMPLETE, task_id="t1", result="done")

    def test_no_status_callback(self):
        hm = HookManager()
        agent = _make_agent(status_callback=None)
        register_task_hooks(hm, agent)
        # All events should work without status_callback
        hm.emit(HookEvent.ON_TASK_CREATE, task_id="t1", title="Test", task_type="task")
        hm.emit(HookEvent.ON_TASK_START, task_id="t1")
        hm.emit(HookEvent.ON_TASK_PROGRESS, task_id="t1", progress=0.5)
        hm.emit(HookEvent.ON_TASK_COMPLETE, task_id="t1", result="done")
        hm.emit(HookEvent.ON_TASK_FAIL, task_id="t1", error="err")
