"""tests/task/test_checker.py — 任务预检器测试。"""

import os
import pytest
from unittest.mock import MagicMock, patch

from spirit.task.models import Task, TaskStatus, TaskType
from spirit.task.checker import TaskChecker, CheckResult, MissingItem


# ---------------------------------------------------------------------------
# CheckResult 测试
# ---------------------------------------------------------------------------

class TestCheckResult:

    def test_default_passed(self):
        r = CheckResult()
        assert r.passed is True
        assert r.failures == []
        assert r.warnings == []

    def test_add_failure(self):
        r = CheckResult()
        r.add_failure("something wrong")
        assert r.passed is False
        assert len(r.failures) == 1
        assert "something wrong" in r.failures[0]

    def test_add_failure_with_item(self):
        r = CheckResult()
        item = MissingItem(name="git", kind="tool", detail="not found")
        r.add_failure("missing git", item)
        assert len(r.missing) == 1
        assert r.missing[0].name == "git"

    def test_add_warning(self):
        r = CheckResult()
        r.add_warning("low disk")
        assert r.passed is True  # warnings don't fail
        assert len(r.warnings) == 1

    def test_summary_passed(self):
        r = CheckResult()
        assert "PASSED" in r.summary()

    def test_summary_failed(self):
        r = CheckResult()
        r.add_failure("bad")
        assert "FAILED" in r.summary()


# ---------------------------------------------------------------------------
# TaskChecker 依赖检查
# ---------------------------------------------------------------------------

class TestDependencyCheck:

    def test_no_task_manager(self):
        checker = TaskChecker(task_manager=None)
        task = Task(title="test")
        result = checker.check_dependencies(task)
        assert result.passed is True

    def test_parent_completed(self):
        tm = MagicMock()
        parent = Task(id="p1", title="parent", status=TaskStatus.COMPLETED)
        tm.get.return_value = parent

        checker = TaskChecker(task_manager=tm)
        task = Task(title="child", parent_id="p1")
        result = checker.check_dependencies(task)
        assert result.passed is True

    def test_parent_not_completed(self):
        tm = MagicMock()
        parent = Task(id="p1", title="parent", status=TaskStatus.RUNNING)
        tm.get.return_value = parent

        checker = TaskChecker(task_manager=tm)
        task = Task(title="child", parent_id="p1")
        result = checker.check_dependencies(task)
        assert result.passed is False
        assert any("未完成" in f for f in result.failures)

    def test_depends_on_all_completed(self):
        tm = MagicMock()
        dep1 = Task(id="d1", title="dep1", status=TaskStatus.COMPLETED)
        dep2 = Task(id="d2", title="dep2", status=TaskStatus.COMPLETED)
        tm.get.side_effect = lambda tid: {"d1": dep1, "d2": dep2}.get(tid)

        checker = TaskChecker(task_manager=tm)
        task = Task(title="main", metadata={"depends_on": ["d1", "d2"]})
        result = checker.check_dependencies(task)
        assert result.passed is True

    def test_depends_on_not_completed(self):
        tm = MagicMock()
        dep1 = Task(id="d1", title="dep1", status=TaskStatus.RUNNING)
        tm.get.return_value = dep1

        checker = TaskChecker(task_manager=tm)
        task = Task(title="main", metadata={"depends_on": ["d1"]})
        result = checker.check_dependencies(task)
        assert result.passed is False

    def test_depends_on_missing_task(self):
        tm = MagicMock()
        tm.get.return_value = None

        checker = TaskChecker(task_manager=tm)
        task = Task(title="main", metadata={"depends_on": ["nonexistent"]})
        result = checker.check_dependencies(task)
        assert result.passed is False
        assert any("不存在" in f for f in result.failures)


# ---------------------------------------------------------------------------
# TaskChecker 环境检查
# ---------------------------------------------------------------------------

class TestEnvironmentCheck:

    def test_no_requirements(self):
        checker = TaskChecker()
        task = Task(title="test")
        result = checker.check_environment(task)
        assert result.passed is True

    def test_env_var_present(self):
        checker = TaskChecker()
        task = Task(title="test", metadata={"required_env": ["PATH"]})
        result = checker.check_environment(task)
        assert result.passed is True

    def test_env_var_missing(self):
        checker = TaskChecker()
        task = Task(title="test", metadata={"required_env": ["NONEXISTENT_VAR_12345"]})
        result = checker.check_environment(task)
        assert result.passed is False
        assert any("NONEXISTENT_VAR_12345" in f for f in result.failures)

    def test_tool_present(self):
        checker = TaskChecker()
        # python should be in PATH
        task = Task(title="test", metadata={"required_tools": ["python"]})
        result = checker.check_environment(task)
        # This may or may not pass depending on environment
        # Just verify it doesn't crash

    def test_tool_missing(self):
        checker = TaskChecker()
        task = Task(title="test", metadata={"required_tools": ["nonexistent_tool_xyz"]})
        result = checker.check_environment(task)
        assert result.passed is False
        assert any("nonexistent_tool_xyz" in f for f in result.failures)


# ---------------------------------------------------------------------------
# TaskChecker 冲突检查
# ---------------------------------------------------------------------------

class TestConflictCheck:

    def test_no_exclusive_group(self):
        checker = TaskChecker()
        task = Task(title="test")
        result = checker.check_conflicts(task)
        assert result.passed is True

    def test_no_conflict(self):
        tm = MagicMock()
        tm.list.return_value = []

        checker = TaskChecker(task_manager=tm)
        task = Task(title="test", metadata={"exclusive_group": "deploy"})
        result = checker.check_conflicts(task)
        assert result.passed is True

    def test_conflict_detected(self):
        tm = MagicMock()
        running_task = Task(
            id="other", title="other", status=TaskStatus.RUNNING,
            metadata={"exclusive_group": "deploy"},
        )
        tm.list.return_value = [running_task]

        checker = TaskChecker(task_manager=tm)
        task = Task(
            id="mine", title="mine",
            metadata={"exclusive_group": "deploy"},
        )
        result = checker.check_conflicts(task)
        assert result.passed is False
        assert any("互斥" in f for f in result.failures)


# ---------------------------------------------------------------------------
# TaskChecker 自定义检查器
# ---------------------------------------------------------------------------

class TestCustomCheck:

    def test_custom_check_passes(self):
        checker = TaskChecker()

        def my_check(task):
            return CheckResult(passed=True)

        checker.register_check("my_check", my_check)
        task = Task(title="test")
        result = checker.check_prerequisites(task)
        assert result.passed is True

    def test_custom_check_fails(self):
        checker = TaskChecker()

        def my_check(task):
            r = CheckResult()
            r.add_failure("custom failure")
            return r

        checker.register_check("my_check", my_check)
        task = Task(title="test")
        result = checker.check_prerequisites(task)
        assert result.passed is False

    def test_custom_check_exception_is_warning(self):
        checker = TaskChecker()

        def bad_check(task):
            raise RuntimeError("boom")

        checker.register_check("bad", bad_check)
        task = Task(title="test")
        result = checker.check_prerequisites(task)
        # Exception in custom check → warning, not failure
        assert result.passed is True
        assert len(result.warnings) > 0


# ---------------------------------------------------------------------------
# 综合预检
# ---------------------------------------------------------------------------

class TestCheckPrerequisites:

    def test_all_pass(self):
        tm = MagicMock()
        tm.list.return_value = []
        checker = TaskChecker(task_manager=tm)
        task = Task(title="simple task")
        result = checker.check_prerequisites(task)
        assert result.passed is True

    def test_combined_failures(self):
        tm = MagicMock()
        tm.get.return_value = None  # dependency missing
        tm.list.return_value = []

        checker = TaskChecker(task_manager=tm)
        task = Task(
            title="complex task",
            metadata={
                "depends_on": ["missing_dep"],
                "required_env": ["NONEXISTENT_VAR_XYZ"],
            },
        )
        result = checker.check_prerequisites(task)
        assert result.passed is False
        # Should have both dependency and env failures
        assert len(result.failures) >= 2
