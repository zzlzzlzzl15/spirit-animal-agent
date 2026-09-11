"""Spirit Agent 任务系统 — 任务管理与自动化。

设计目标：
- 任务 CRUD（创建/读取/更新/删除）
- 任务状态机（pending -> running -> completed/failed）
- 子任务支持（嵌套任务树）
- 持久化存储（SQLite）
- 钩子集成（任务生命周期事件）
- 任务预检（TaskChecker）
- 任务分解（TaskPlanner）
"""

from spirit.task.models import Task, TaskStatus, TaskType
from spirit.task.manager import TaskManager
from spirit.task.checker import TaskChecker, CheckResult, MissingItem
from spirit.task.planner import TaskPlanner, DecomposeResult, SubtaskSpec
from spirit.task.executor import TaskExecutor, PreCheckError

__all__ = [
    "Task",
    "TaskStatus",
    "TaskType",
    "TaskManager",
    "TaskChecker",
    "CheckResult",
    "MissingItem",
    "TaskPlanner",
    "DecomposeResult",
    "SubtaskSpec",
    "TaskExecutor",
    "PreCheckError",
]
