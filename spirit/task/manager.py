"""TaskManager — 任务管理器。

功能：
- 任务 CRUD
- 任务状态机转换
- 子任务管理
- 持久化存储
- 钩子集成
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from spirit.task.models import Task, TaskStatus, TaskType

logger = logging.getLogger(__name__)


class TaskManager:
    """任务管理器 — 管理任务生命周期。"""

    def __init__(
        self,
        db_path: str = None,
        hook_manager: Any = None,
    ):
        """初始化任务管理器。

        Args:
            db_path: SQLite 数据库路径
            hook_manager: 钩子管理器（可选）
        """
        self.db_path = db_path or self._default_db_path()
        self.hook_manager = hook_manager
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_schema()

    def _default_db_path(self) -> str:
        """默认数据库路径。"""
        home = Path.home()
        db_dir = home / ".spirit" / "data"
        db_dir.mkdir(parents=True, exist_ok=True)
        return str(db_dir / "tasks.db")

    def _ensure_schema(self):
        """确保数据库表结构存在。"""
        conn = self._get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                parent_id TEXT DEFAULT '',
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                task_type TEXT DEFAULT 'task',
                status TEXT DEFAULT 'pending',
                priority INTEGER DEFAULT 0,
                result TEXT DEFAULT '',
                error TEXT DEFAULT '',
                created_at REAL NOT NULL,
                started_at REAL DEFAULT 0,
                completed_at REAL DEFAULT 0,
                estimated_seconds REAL DEFAULT 0,
                tags TEXT DEFAULT '[]',
                metadata TEXT DEFAULT '{}',
                subtask_ids TEXT DEFAULT '[]'
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_status
            ON tasks(status)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_parent
            ON tasks(parent_id)
        """)
        conn.commit()

    def _get_connection(self) -> sqlite3.Connection:
        """获取数据库连接。"""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------

    def create(
        self,
        title: str,
        description: str = "",
        task_type: TaskType = TaskType.TASK,
        parent_id: str = "",
        priority: int = 0,
        tags: List[str] = None,
        metadata: Dict[str, Any] = None,
    ) -> Task:
        """创建新任务。

        Args:
            title: 任务标题
            description: 任务描述
            task_type: 任务类型
            parent_id: 父任务 ID（创建子任务时）
            priority: 优先级
            tags: 标签
            metadata: 元数据

        Returns:
            创建的 Task 对象
        """
        task = Task(
            title=title,
            description=description,
            task_type=task_type,
            parent_id=parent_id,
            priority=priority,
            tags=tags or [],
            metadata=metadata or {},
        )

        conn = self._get_connection()
        conn.execute(
            """INSERT INTO tasks
            (id, parent_id, title, description, task_type, status, priority,
             result, error, created_at, started_at, completed_at,
             estimated_seconds, tags, metadata, subtask_ids)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task.id,
                task.parent_id,
                task.title,
                task.description,
                task.task_type.value,
                task.status.value,
                task.priority,
                task.result,
                task.error,
                task.created_at,
                task.started_at,
                task.completed_at,
                task.estimated_seconds,
                json.dumps(task.tags),
                json.dumps(task.metadata),
                json.dumps(task.subtask_ids),
            ),
        )
        conn.commit()

        # 如果是子任务，更新父任务的 subtask_ids
        if parent_id:
            self._add_subtask_to_parent(parent_id, task.id)

        # 触发钩子
        if self.hook_manager:
            self.hook_manager.emit(
                "on_task_create",
                task_id=task.id,
                title=task.title,
                task_type=task.task_type.value,
                parent_id=parent_id,
            )

        logger.debug("任务创建: [%s] %s", task.id, task.title)
        return task

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def get(self, task_id: str) -> Optional[Task]:
        """获取任务。"""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return self._row_to_task(row) if row else None

    def list(
        self,
        status: TaskStatus = None,
        task_type: TaskType = None,
        parent_id: str = None,
        limit: int = 100,
    ) -> List[Task]:
        """列出任务。"""
        conn = self._get_connection()
        conditions = []
        params = []

        if status:
            conditions.append("status = ?")
            params.append(status.value)
        if task_type:
            conditions.append("task_type = ?")
            params.append(task_type.value)
        if parent_id is not None:
            conditions.append("parent_id = ?")
            params.append(parent_id)

        where = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)

        rows = conn.execute(
            f"SELECT * FROM tasks WHERE {where} ORDER BY priority DESC, created_at DESC LIMIT ?",
            params,
        ).fetchall()

        return [self._row_to_task(row) for row in rows]

    def get_subtasks(self, parent_id: str) -> List[Task]:
        """获取子任务列表。"""
        return self.list(parent_id=parent_id)

    # ------------------------------------------------------------------
    # 更新
    # ------------------------------------------------------------------

    def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        result: str = "",
        error: str = "",
    ) -> bool:
        """更新任务状态。

        Args:
            task_id: 任务 ID
            status: 新状态
            result: 结果（完成时）
            error: 错误信息（失败时）

        Returns:
            是否成功
        """
        task = self.get(task_id)
        if not task:
            return False

        now = time.time()
        updates = {"status": status.value}

        if status == TaskStatus.RUNNING:
            updates["started_at"] = now
        elif status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            updates["completed_at"] = now
            if result:
                updates["result"] = result
            if error:
                updates["error"] = error

        self._update_task(task_id, updates)

        # 触发钩子
        if self.hook_manager:
            hook_event = {
                TaskStatus.RUNNING: "on_task_start",
                TaskStatus.COMPLETED: "on_task_complete",
                TaskStatus.FAILED: "on_task_fail",
            }.get(status)

            if hook_event:
                self.hook_manager.emit(
                    hook_event,
                    task_id=task_id,
                    result=result,
                    error=error,
                )

        logger.debug("任务状态更新: [%s] %s -> %s", task_id, task.status.value, status.value)
        return True

    def complete(self, task_id: str, result: str = "") -> bool:
        """标记任务完成。"""
        return self.update_status(task_id, TaskStatus.COMPLETED, result=result)

    def fail(self, task_id: str, error: str = "") -> bool:
        """标记任务失败。"""
        return self.update_status(task_id, TaskStatus.FAILED, error=error)

    def cancel(self, task_id: str) -> bool:
        """取消任务。"""
        return self.update_status(task_id, TaskStatus.CANCELLED)

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------

    def delete(self, task_id: str) -> bool:
        """删除任务。"""
        conn = self._get_connection()
        cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def count(self, status: TaskStatus = None) -> int:
        """统计任务数量。"""
        conn = self._get_connection()
        if status:
            row = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = ?",
                (status.value,),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
        return row[0]

    def get_stats(self) -> Dict[str, Any]:
        """获取任务统计。"""
        conn = self._get_connection()
        total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        by_status = {}
        for row in conn.execute(
            "SELECT status, COUNT(*) FROM tasks GROUP BY status"
        ):
            by_status[row[0]] = row[1]

        return {
            "total": total,
            "by_status": by_status,
            "db_path": self.db_path,
        }

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _update_task(self, task_id: str, updates: Dict[str, Any]) -> None:
        """更新任务字段。"""
        if not updates:
            return

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [task_id]

        conn = self._get_connection()
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
        conn.commit()

    def _add_subtask_to_parent(self, parent_id: str, subtask_id: str) -> None:
        """添加子任务 ID 到父任务。"""
        parent = self.get(parent_id)
        if parent:
            subtask_ids = parent.subtask_ids + [subtask_id]
            self._update_task(parent_id, {"subtask_ids": json.dumps(subtask_ids)})

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        """数据库行转 Task 对象。"""
        data = dict(row)
        data["tags"] = json.loads(data.get("tags", "[]"))
        data["metadata"] = json.loads(data.get("metadata", "{}"))
        data["subtask_ids"] = json.loads(data.get("subtask_ids", "[]"))
        return Task.from_dict(data)

    def close(self) -> None:
        """关闭数据库连接。"""
        if self._conn:
            self._conn.close()
            self._conn = None


__all__ = ["TaskManager"]
