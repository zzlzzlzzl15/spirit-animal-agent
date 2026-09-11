"""Task 数据模型。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class TaskStatus(str, Enum):
    """任务状态。"""

    PENDING = "pending"       # 等待执行
    RUNNING = "running"       # 执行中
    COMPLETED = "completed"   # 已完成
    FAILED = "failed"         # 失败
    CANCELLED = "cancelled"   # 已取消
    PAUSED = "paused"         # 暂停


class TaskType(str, Enum):
    """任务类型。"""

    TASK = "task"             # 普通任务
    SUBTASK = "subtask"       # 子任务
    DELEGATED = "delegated"   # 委托任务
    CRON = "cron"             # 定时任务
    BLUEPRINT = "blueprint"   # 蓝图任务


@dataclass
class Task:
    """任务实体。"""

    # 标识
    id: str = ""
    parent_id: str = ""       # 父任务 ID（子任务）
    title: str = ""
    description: str = ""

    # 分类
    task_type: TaskType = TaskType.TASK
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 0         # 优先级（越高越优先）

    # 结果
    result: str = ""
    error: str = ""

    # 时间
    created_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    estimated_seconds: float = 0.0

    # 元数据
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # 子任务
    subtask_ids: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())[:8]
        if not self.created_at:
            self.created_at = time.time()

    @property
    def is_terminal(self) -> bool:
        """是否处于终态。"""
        return self.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        )

    @property
    def duration(self) -> float:
        """执行时长（秒）。"""
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        return 0.0

    @property
    def progress(self) -> float:
        """进度百分比（0.0 ~ 1.0）。"""
        if self.status == TaskStatus.PENDING:
            return 0.0
        if self.status == TaskStatus.COMPLETED:
            return 1.0
        if self.status == TaskStatus.FAILED:
            return 0.0
        # 基于子任务计算
        if self.subtask_ids:
            # 需要外部提供子任务状态
            return 0.5  # 默认 50%
        return 0.5

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典。"""
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "title": self.title,
            "description": self.description,
            "task_type": self.task_type.value,
            "status": self.status.value,
            "priority": self.priority,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "estimated_seconds": self.estimated_seconds,
            "tags": self.tags,
            "metadata": self.metadata,
            "subtask_ids": self.subtask_ids,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Task":
        """从字典反序列化。"""
        data = dict(data)
        if "task_type" in data and isinstance(data["task_type"], str):
            data["task_type"] = TaskType(data["task_type"])
        if "status" in data and isinstance(data["status"], str):
            data["status"] = TaskStatus(data["status"])
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def summary(self) -> str:
        """生成任务摘要。"""
        status_emoji = {
            TaskStatus.PENDING: "⏳",
            TaskStatus.RUNNING: "🔄",
            TaskStatus.COMPLETED: "✅",
            TaskStatus.FAILED: "❌",
            TaskStatus.CANCELLED: "🚫",
            TaskStatus.PAUSED: "⏸️",
        }
        emoji = status_emoji.get(self.status, "❓")
        return f"{emoji} [{self.id}] {self.title} ({self.status.value})"


__all__ = ["Task", "TaskStatus", "TaskType"]
