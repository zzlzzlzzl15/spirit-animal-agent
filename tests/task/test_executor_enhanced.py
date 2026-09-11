"""tests/task/test_executor_enhanced.py — 增强执行器测试。"""

import pytest
from unittest.mock import MagicMock, patch

from spirit.task.models import Task, TaskStatus, TaskType
from spirit.task.manager import TaskManager
from spirit.task.executor import TaskExecutor, PreCheckError
from spirit.task.checker import TaskChecker, CheckResult


# ---------------------------------------------------------------------------
# PreCheckError 测试
# ---------------------------------------------------------------------------

class TestPreCheckError:

    def test_error_message(self):
        e = PreCheckError(["missing dep", "no env"])
        assert "missing dep" in str(e)
        assert "no env" in str(e)
        assert e.failures == ["missing dep", "no env"]


# ---------------------------------------------------------------------------
# 增强执行器 — 预检集成
# ---------------------------------------------------------------------------

class TestExecutorWithChecker:

    def _make_executor(self, checker=None, hook_manager=None):
        """创建测试用执行器（使用内存 TaskManager）。"""
        tm = TaskManager(db_path=":memory:")
        executor = TaskExecutor(
            task_manager=tm,
            checker=checker,
            hook_manager=hook_manager,
        )
        return tm, executor

    def test_execute_with_passing_check(self):
        tm, executor = self._make_executor()
        task = tm.create(title="test task")

        checker = MagicMock()
        checker.check_prerequisites.return_value = CheckResult(passed=True)
        executor.checker = checker

        result = executor.execute(task.id, lambda t: "done")
        assert result == "done"
        checker.check_prerequisites.assert_called_once()

    def test_execute_with_failing_check(self):
        tm, executor = self._make_executor()
        task = tm.create(title="test task")

        checker = MagicMock()
        fail_result = CheckResult()
        fail_result.add_failure("missing dependency")
        checker.check_prerequisites.return_value = fail_result
        executor.checker = checker

        with pytest.raises(PreCheckError) as exc_info:
            executor.execute(task.id, lambda t: "done")

        assert "missing dependency" in str(exc_info.value)
        # Task should be marked failed
        updated_task = tm.get(task.id)
        assert updated_task.status == TaskStatus.FAILED

    def test_execute_without_checker(self):
        """没有 checker 时正常执行。"""
        tm, executor = self._make_executor()
        task = tm.create(title="test task")
        result = executor.execute(task.id, lambda t: "done")
        assert result == "done"

    def test_execute_emits_check_hook_on_pass(self):
        tm, executor = self._make_executor()
        task = tm.create(title="test task")

        checker = MagicMock()
        checker.check_prerequisites.return_value = CheckResult(passed=True)
        executor.checker = checker

        hm = MagicMock()
        executor.hook_manager = hm

        executor.execute(task.id, lambda t: "done")

        # 应该触发 on_task_check (passed=True)
        emit_calls = [c for c in hm.emit.call_args_list]
        check_calls = [c for c in emit_calls if c[0][0] == "on_task_check" or
                       (len(c[0]) > 0 and c[0][0] == "on_task_check")]
        # Also check emit calls with keyword args
        all_events = []
        for c in hm.emit.call_args_list:
            if c[0]:
                all_events.append(c[0][0])
        assert "on_task_check" in all_events
        assert "on_task_progress" in all_events

    def test_execute_emits_check_hook_on_fail(self):
        tm, executor = self._make_executor()
        task = tm.create(title="test task")

        checker = MagicMock()
        fail_result = CheckResult()
        fail_result.add_failure("bad")
        checker.check_prerequisites.return_value = fail_result
        executor.checker = checker

        hm = MagicMock()
        executor.hook_manager = hm

        with pytest.raises(PreCheckError):
            executor.execute(task.id, lambda t: "done")

        all_events = [c[0][0] for c in hm.emit.call_args_list if c[0]]
        assert "on_task_check" in all_events


# ---------------------------------------------------------------------------
# 增强执行器 — 进度钩子
# ---------------------------------------------------------------------------

class TestExecutorProgressHooks:

    def test_progress_hooks_on_success(self):
        tm = TaskManager(db_path=":memory:")
        hm = MagicMock()
        executor = TaskExecutor(task_manager=tm, hook_manager=hm)
        task = tm.create(title="test")

        executor.execute(task.id, lambda t: "result")

        # 应该有 progress 事件
        progress_calls = [
            c for c in hm.emit.call_args_list
            if c[0] and c[0][0] == "on_task_progress"
        ]
        assert len(progress_calls) >= 2  # 开始 + 完成

    def test_no_progress_hooks_without_hook_manager(self):
        tm = TaskManager(db_path=":memory:")
        executor = TaskExecutor(task_manager=tm)  # no hook_manager
        task = tm.create(title="test")
        # Should not crash
        result = executor.execute(task.id, lambda t: "done")
        assert result == "done"


# ---------------------------------------------------------------------------
# 原有功能回归测试
# ---------------------------------------------------------------------------

class TestExecutorRegression:

    def test_execute_basic(self):
        tm = TaskManager(db_path=":memory:")
        executor = TaskExecutor(task_manager=tm)
        task = tm.create(title="basic task")
        result = executor.execute(task.id, lambda t: "hello")
        assert result == "hello"
        assert tm.get(task.id).status == TaskStatus.COMPLETED

    def test_execute_failure(self):
        tm = TaskManager(db_path=":memory:")
        executor = TaskExecutor(task_manager=tm)
        task = tm.create(title="failing task")

        with pytest.raises(RuntimeError):
            executor.execute(task.id, lambda t: (_ for _ in ()).throw(RuntimeError("oops")))

        assert tm.get(task.id).status == TaskStatus.FAILED

    def test_execute_nonexistent_task(self):
        tm = TaskManager(db_path=":memory:")
        executor = TaskExecutor(task_manager=tm)
        with pytest.raises(ValueError):
            executor.execute("nonexistent", lambda t: "done")

    def test_execute_terminal_task(self):
        tm = TaskManager(db_path=":memory:")
        executor = TaskExecutor(task_manager=tm)
        task = tm.create(title="already done")
        tm.complete(task.id, result="previous result")
        result = executor.execute(task.id, lambda t: "new result")
        assert result == "previous result"  # Returns existing result

    def test_cancel_task(self):
        tm = TaskManager(db_path=":memory:")
        executor = TaskExecutor(task_manager=tm)
        task = tm.create(title="cancel me")
        executor.cancel_task(task.id)
        assert tm.get(task.id).status == TaskStatus.CANCELLED

    def test_shutdown(self):
        tm = TaskManager(db_path=":memory:")
        executor = TaskExecutor(task_manager=tm)
        executor.shutdown()
        # Should not crash on double shutdown
        executor.shutdown()
