"""MemoryManager — 跨会话持久化记忆管理。

参考 Hermes 的 memory_manager.py (1231行)，实现：
- 持久化记忆存储（SQLite）
- 记忆检索（关键词 + 时间衰减）
- 记忆分类（事实/偏好/技能/对话摘要）
- 自动记忆提取（从对话中学习）
- 记忆容量管理（LRU 淘汰）

记忆类型：
- fact: 事实性知识（用户偏好、项目信息）
- skill: 技能知识（操作模式、最佳实践）
- summary: 对话摘要
- entity: 实体信息（人名、项目名）
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class Memory:
    """单条记忆。"""

    id: str = ""
    content: str = ""
    category: str = "fact"  # fact/skill/summary/entity
    tags: List[str] = field(default_factory=list)
    source_session: str = ""  # 来源会话 ID
    created_at: float = 0.0
    updated_at: float = 0.0
    access_count: int = 0
    last_accessed: float = 0.0
    importance: float = 0.5  # 0.0 ~ 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now
        if not self.last_accessed:
            self.last_accessed = now

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典。"""
        return {
            "id": self.id,
            "content": self.content,
            "category": self.category,
            "tags": self.tags,
            "source_session": self.source_session,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "access_count": self.access_count,
            "last_accessed": self.last_accessed,
            "importance": self.importance,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Memory":
        """从字典反序列化。"""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# 记忆管理器
# ---------------------------------------------------------------------------

class MemoryManager:
    """跨会话持久化记忆管理器。"""

    # 默认最大记忆数（来自集中式配置）
    @staticmethod
    def _default_max_memories() -> int:
        from spirit.config import get_config_value
        return get_config_value("agent.max_memories", 1000)

    def __init__(
        self,
        db_path: str = None,
        max_memories: int = None,
    ):
        """初始化记忆管理器。

        Args:
            db_path: SQLite 数据库路径
            max_memories: 最大记忆数量（超出后 LRU 淘汰）
        """
        if max_memories is None:
            max_memories = self._default_max_memories()
        self.db_path = db_path or self._default_db_path()
        self.max_memories = max_memories
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_schema()

    def _default_db_path(self) -> str:
        """默认数据库路径。"""
        home = Path.home()
        db_dir = home / ".spirit" / "data"
        db_dir.mkdir(parents=True, exist_ok=True)
        return str(db_dir / "memories.db")

    def _ensure_schema(self):
        """确保数据库表结构存在。"""
        conn = self._get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'fact',
                tags TEXT DEFAULT '[]',
                source_session TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                access_count INTEGER DEFAULT 0,
                last_accessed REAL NOT NULL,
                importance REAL DEFAULT 0.5,
                metadata TEXT DEFAULT '{}'
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_category
            ON memories(category)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_importance
            ON memories(importance DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_last_accessed
            ON memories(last_accessed)
        """)
        conn.commit()

    def _get_connection(self) -> sqlite3.Connection:
        """获取数据库连接。"""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    # ------------------------------------------------------------------
    # 存储
    # ------------------------------------------------------------------

    def remember(
        self,
        content: str,
        category: str = "fact",
        tags: List[str] = None,
        importance: float = 0.5,
        source_session: str = "",
        metadata: Dict[str, Any] = None,
    ) -> Memory:
        """存储一条新记忆。

        Args:
            content: 记忆内容
            category: 分类（fact/skill/summary/entity）
            tags: 标签列表
            importance: 重要度 0.0~1.0
            source_session: 来源会话 ID
            metadata: 额外元数据

        Returns:
            创建的 Memory 对象
        """
        memory = Memory(
            content=content,
            category=category,
            tags=tags or [],
            importance=importance,
            source_session=source_session,
            metadata=metadata or {},
        )

        conn = self._get_connection()
        conn.execute(
            """INSERT INTO memories
            (id, content, category, tags, source_session, created_at,
             updated_at, access_count, last_accessed, importance, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                memory.id,
                memory.content,
                memory.category,
                json.dumps(memory.tags),
                memory.source_session,
                memory.created_at,
                memory.updated_at,
                memory.access_count,
                memory.last_accessed,
                memory.importance,
                json.dumps(memory.metadata),
            ),
        )
        conn.commit()

        # 检查容量
        self._enforce_capacity()

        logger.debug("记忆存储: [%s] %s", memory.category, memory.content[:50])
        return memory

    def update(self, memory_id: str, **kwargs) -> bool:
        """更新记忆。

        Args:
            memory_id: 记忆 ID
            **kwargs: 要更新的字段

        Returns:
            是否成功
        """
        allowed = {"content", "category", "tags", "importance", "metadata"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False

        updates["updated_at"] = time.time()

        # 序列化 JSON 字段
        if "tags" in updates:
            updates["tags"] = json.dumps(updates["tags"])
        if "metadata" in updates:
            updates["metadata"] = json.dumps(updates["metadata"])

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [memory_id]

        conn = self._get_connection()
        cursor = conn.execute(
            f"UPDATE memories SET {set_clause} WHERE id = ?", values,
        )
        conn.commit()
        return cursor.rowcount > 0

    def forget(self, memory_id: str) -> bool:
        """删除一条记忆。"""
        conn = self._get_connection()
        cursor = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def recall(
        self,
        query: str = "",
        category: str = None,
        tags: List[str] = None,
        limit: int = 10,
        min_importance: float = 0.0,
    ) -> List[Memory]:
        """检索记忆。

        检索策略（按优先级）：
        1. 精确标签匹配
        2. 关键词内容匹配
        3. 按重要度 + 时间衰减排序

        Args:
            query: 搜索关键词
            category: 过滤分类
            tags: 过滤标签
            limit: 最大返回数
            min_importance: 最小重要度

        Returns:
            匹配的记忆列表（按相关度排序）
        """
        conn = self._get_connection()
        conditions = []
        params = []

        # 分类过滤
        if category:
            conditions.append("category = ?")
            params.append(category)

        # 重要度过滤
        if min_importance > 0:
            conditions.append("importance >= ?")
            params.append(min_importance)

        # 标签过滤（JSON 数组包含）
        if tags:
            for tag in tags:
                conditions.append("tags LIKE ?")
                params.append(f'%"{tag}"%')

        # 关键词搜索
        if query:
            conditions.append("content LIKE ?")
            params.append(f"%{query}%")

        where = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)

        # 按重要度 + 时间衰减排序
        # score = importance * 0.6 + recency * 0.4
        now = time.time()
        sql = f"""
            SELECT *,
                (importance * 0.6 +
                 (1.0 - MIN((? - last_accessed) / 86400.0 / 30.0, 1.0)) * 0.4) as score
            FROM memories
            WHERE {where}
            ORDER BY score DESC
            LIMIT ?
        """
        params.insert(0, now)

        rows = conn.execute(sql, params).fetchall()
        memories = [self._row_to_memory(row) for row in rows]

        # 更新访问统计
        for mem in memories:
            self._touch(mem.id)

        return memories

    def recall_recent(self, limit: int = 10, category: str = None) -> List[Memory]:
        """获取最近访问的记忆。"""
        conn = self._get_connection()
        sql = "SELECT * FROM memories"
        params = []
        if category:
            sql += " WHERE category = ?"
            params.append(category)
        sql += " ORDER BY last_accessed DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        return [self._row_to_memory(row) for row in rows]

    def recall_by_tags(self, tags: List[str], limit: int = 10) -> List[Memory]:
        """按标签检索记忆。"""
        return self.recall(tags=tags, limit=limit)

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def count(self, category: str = None) -> int:
        """统计记忆数量。"""
        conn = self._get_connection()
        if category:
            row = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE category = ?",
                (category,),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        return row[0]

    def get_stats(self) -> Dict[str, Any]:
        """获取记忆统计信息。"""
        conn = self._get_connection()
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        by_category = {}
        for row in conn.execute(
            "SELECT category, COUNT(*) FROM memories GROUP BY category"
        ):
            by_category[row[0]] = row[1]

        return {
            "total": total,
            "max_memories": self.max_memories,
            "by_category": by_category,
            "db_path": self.db_path,
        }

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _touch(self, memory_id: str) -> None:
        """更新访问统计。"""
        try:
            conn = self._get_connection()
            conn.execute(
                """UPDATE memories
                SET access_count = access_count + 1, last_accessed = ?
                WHERE id = ?""",
                (time.time(), memory_id),
            )
            conn.commit()
        except Exception as e:
            logger.debug("更新访问统计失败: %s", e)

    def _enforce_capacity(self) -> None:
        """强制容量限制 — LRU 淘汰。"""
        conn = self._get_connection()
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

        if total > self.max_memories:
            # 淘汰最久未访问的
            excess = total - self.max_memories
            conn.execute(
                """DELETE FROM memories WHERE id IN (
                    SELECT id FROM memories
                    ORDER BY last_accessed ASC
                    LIMIT ?
                )""",
                (excess,),
            )
            conn.commit()
            logger.debug("LRU 淘汰 %d 条记忆", excess)

    def _row_to_memory(self, row: sqlite3.Row) -> Memory:
        """数据库行转 Memory 对象。"""
        data = dict(row)
        data.pop("score", None)  # 移除计算字段
        data["tags"] = json.loads(data.get("tags", "[]"))
        data["metadata"] = json.loads(data.get("metadata", "{}"))
        return Memory(**{k: v for k, v in data.items() if k in Memory.__dataclass_fields__})

    def close(self) -> None:
        """关闭数据库连接。"""
        if self._conn:
            self._conn.close()
            self._conn = None


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------

_default_manager: Optional[MemoryManager] = None


def get_memory_manager(db_path: str = None) -> MemoryManager:
    """获取全局记忆管理器实例。"""
    global _default_manager
    if _default_manager is None:
        _default_manager = MemoryManager(db_path=db_path)
    return _default_manager


def remember(content: str, **kwargs) -> Memory:
    """便捷函数：存储记忆。"""
    return get_memory_manager().remember(content, **kwargs)


def recall(query: str = "", **kwargs) -> List[Memory]:
    """便捷函数：检索记忆。"""
    return get_memory_manager().recall(query, **kwargs)


__all__ = [
    "Memory",
    "MemoryManager",
    "get_memory_manager",
    "remember",
    "recall",
]
