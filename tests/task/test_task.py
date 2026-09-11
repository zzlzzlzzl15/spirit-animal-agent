"""tests/task/test_task.py — 任务系统测试。"""

import pytest
import time
from spirit.task.models import Task, TaskStatus, TaskType
from spirit.task.manager import TaskManager


class TestTaskModel:
    """Task 数据模型测试。"""

    def test_create_task(self):
        """测试创建任务。"""
        task = Task(title="Test task", description="A test")
        assert task.id  # 自动生成
        assert task.title == "Test task"
        assert task.status == TaskStatus.PENDING
        assert task.created_at > 0

    def test_is_terminal(self):
        """测试终态判断。"""
        task = Task(title="test")
        assert task.is_terminal is False
        task.status = TaskStatus.COMPLETED
        assert task.is_terminal is True
        task.status = TaskStatus.FAILED
        assert task.is_terminal is True
        task.status = TaskStatus.CANCELLED
        assert task.is_terminal is True
        task.status = TaskStatus.RUNNING
        assert task.is_terminal is False

    def test_duration(self):
        """测试执行时长。"""
        task = Task(title="test", started_at=100.0, completed_at=150.0)
        assert task.duration == 50.0

    def test_duration_not_completed(self):
        """测试未完成的任务时长。"""
        task = Task(title="test", started_at=100.0)
        assert task.duration == 0.0

    def test_to_dict_and_back(self):
        """测试序列化/反序列化。"""
        task = Task(title="test", tags=["a", "b"], metadata={"key": "val"})
        data = task.to_dict()
        restored = Task.from_dict(data)
        assert restored.title == "test"
        assert restored.tags == ["a", "b"]
        assert restored.metadata["key"] == "val"

    def test_summary(self):
        """测试摘要生成。"""
        task = Task(title="Build feature")
        summary = task.summary()
        assert "Build feature" in summary
        assert "pending" in summary


class TestTaskManager:
    """TaskManager 管理器测试。"""

    def test_create_task(self, task_manager):
        """测试创建任务。"""
        task = task_manager.create(title="Test task")
        assert task.title == "Test task"
        assert task.status == TaskStatus.PENDING
        assert task_manager.count() == 1

    def test_get_task(self, task_manager):
        """测试获取任务。"""
        created = task_manager.create(title="Get me")
        retrieved = task_manager.get(created.id)
        assert retrieved is not None
        assert retrieved.title == "Get me"

    def test_get_nonexistent(self, task_manager):
        """测试获取不存在的任务。"""
        assert task_manager.get("nonexistent") is None

    def test_list_tasks(self, task_manager):
        """测试列出任务。"""
        task_manager.create(title="Task 1")
        task_manager.create(title="Task 2")
        task_manager.create(title="Task 3")
        tasks = task_manager.list()
        assert len(tasks) == 3

    def test_list_by_status(self, task_manager):
        """测试按状态过滤。"""
        t1 = task_manager.create(title="Done")
        task_manager.create(title="Pending")
        task_manager.complete(t1.id, result="done!")
        completed = task_manager.list(status=TaskStatus.COMPLETED)
        pending = task_manager.list(status=TaskStatus.PENDING)
        assert len(completed) == 1
        assert len(pending) == 1

    def test_update_status_running(self, task_manager):
        """测试状态更新为运行中。"""
        task = task_manager.create(title="Run me")
        task_manager.update_status(task.id, TaskStatus.RUNNING)
        updated = task_manager.get(task.id)
        assert updated.status == TaskStatus.RUNNING
        assert updated.started_at > 0

    def test_complete_task(self, task_manager):
        """测试完成任务。"""
        task = task_manager.create(title="Complete me")
        task_manager.complete(task.id, result="success!")
        updated = task_manager.get(task.id)
        assert updated.status == TaskStatus.COMPLETED
        assert updated.result == "success!"
        assert updated.completed_at > 0

    def test_fail_task(self, task_manager):
        """测试失败任务。"""
        task = task_manager.create(title="Fail me")
        task_manager.fail(task.id, error="something broke")
        updated = task_manager.get(task.id)
        assert updated.status == TaskStatus.FAILED
        assert updated.error == "something broke"

    def test_cancel_task(self, task_manager):
        """测试取消任务。"""
        task = task_manager.create(title="Cancel me")
        task_manager.cancel(task.id)
        updated = task_manager.get(task.id)
        assert updated.status == TaskStatus.CANCELLED

    def test_delete_task(self, task_manager):
        """测试删除任务。"""
        task = task_manager.create(title="Delete me")
        assert task_manager.delete(task.id) is True
        assert task_manager.get(task.id) is None
        assert task_manager.count() == 0

    def test_subtasks(self, task_manager):
        """测试子任务。"""
        parent = task_manager.create(title="Parent")
        child1 = task_manager.create(title="Child 1", parent_id=parent.id)
        child2 = task_manager.create(title="Child 2", parent_id=parent.id)

        subtasks = task_manager.get_subtasks(parent.id)
        assert len(subtasks) == 2

        # 父任务应记录子任务 ID
        updated_parent = task_manager.get(parent.id)
        assert child1.id in updated_parent.subtask_ids
        assert child2.id in updated_parent.subtask_ids

    def test_get_stats(self, task_manager):
        """测试统计。"""
        task_manager.create(title="A")
        task_manager.create(title="B")
        stats = task_manager.get_stats()
        assert stats["total"] == 2
        assert "by_status" in stats

    def test_hooks_triggered(self, tmp_db, hook_manager):
        """测试钩子触发。"""
        from spirit.hooks.hook_manager import HookManager
        events = []
        hook_manager.register("on_task_create", lambda **kw: events.append("created"))
        hook_manager.register("on_task_complete", lambda **kw: events.append("completed"))

        mgr = TaskManager(db_path=tmp_db, hook_manager=hook_manager)
        task = mgr.create(title="Hook test")
        mgr.complete(task.id)

        assert "created" in events
        assert "completed" in events
        mgr.close()
