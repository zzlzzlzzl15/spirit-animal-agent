"""跨会话搜索。

参考 Hermes 的 tools/session_search_tool.py：
- 全文搜索（FTS5）
- 按时间/平台/模型过滤
- 结果排序和分页
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from spirit.storage.session_db import SessionDB

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """搜索结果。"""
    session_id: str
    message_id: int
    role: str
    content: str
    timestamp: str
    source: str
    model: str
    relevance_score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "message_id": self.message_id,
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "source": self.source,
            "model": self.model,
            "relevance_score": self.relevance_score,
        }


@dataclass
class SearchQuery:
    """搜索查询参数。"""
    query: str = ""
    source: Optional[str] = None  # 平台过滤
    model: Optional[str] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    role: Optional[str] = None  # user / assistant / system
    limit: int = 20
    offset: int = 0


class SessionSearch:
    """跨会话搜索引擎。

    支持：
    - 全文搜索（利用 SQLite FTS5）
    - 关键词匹配
    - 时间范围过滤
    - 平台/模型过滤
    - 结果排序（相关性/时间）

    Usage:
        search = SessionSearch(session_db)
        results = search.search("Python 错误")
    """

    def __init__(self, session_db: SessionDB):
        self.db = session_db

    def search(self, query: str | SearchQuery, **kwargs) -> List[SearchResult]:
        """执行搜索。

        Args:
            query: 搜索字符串或 SearchQuery 对象
            **kwargs: SearchQuery 参数覆盖

        Returns:
            搜索结果列表
        """
        if isinstance(query, str):
            sq = SearchQuery(query=query, **kwargs)
        else:
            sq = query

        if not sq.query and not sq.source and not sq.model:
            return []

        results = []

        try:
            # 尝试 FTS5 全文搜索
            if sq.query:
                fts_results = self._fts_search(sq)
                results.extend(fts_results)
            else:
                # 使用过滤条件查询
                filtered_results = self._filtered_search(sq)
                results.extend(filtered_results)

            # 排序
            results = self._sort_results(results, sq)

            # 分页
            results = results[sq.offset:sq.offset + sq.limit]

        except Exception as e:
            logger.error("搜索失败: %s", e)

        return results

    def _fts_search(self, sq: SearchQuery) -> List[SearchResult]:
        """FTS5 全文搜索。"""
        results = []

        try:
            # 构建 FTS 查询
            rows = self.db.conn.execute(
                """SELECT m.id, m.session_id, m.role, m.content, m.created_at,
                          s.source, s.model
                   FROM messages m
                   JOIN sessions s ON m.session_id = s.id
                   JOIN messages_fts f ON m.id = f.rowid
                   WHERE messages_fts MATCH ?
                   ORDER BY rank""",
                (sq.query,),
            ).fetchall()

            for row in rows:
                # 应用过滤
                if sq.source and row["source"] != sq.source:
                    continue
                if sq.model and row["model"] != sq.model:
                    continue
                if sq.role and row["role"] != sq.role:
                    continue
                if sq.start_date and row["created_at"] < sq.start_date.isoformat():
                    continue
                if sq.end_date and row["created_at"] > sq.end_date.isoformat():
                    continue

                results.append(SearchResult(
                    session_id=row["session_id"],
                    message_id=row["id"],
                    role=row["role"],
                    content=row["content"],
                    timestamp=row["created_at"],
                    source=row["source"],
                    model=row["model"],
                ))

        except Exception as e:
            logger.debug("FTS 搜索失败，回退到关键词搜索: %s", e)
            results = self._keyword_search(sq)

        return results

    def _keyword_search(self, sq: SearchQuery) -> List[SearchResult]:
        """关键词搜索（FTS 不可用时的回退方案）。"""
        results = []

        # 构建 SQL
        conditions = ["m.content LIKE ?"]
        params = [f"%{sq.query}%"]

        if sq.source:
            conditions.append("s.source = ?")
            params.append(sq.source)
        if sq.model:
            conditions.append("s.model = ?")
            params.append(sq.model)
        if sq.role:
            conditions.append("m.role = ?")
            params.append(sq.role)

        where_clause = " AND ".join(conditions)

        try:
            rows = self.db.conn.execute(
                f"""SELECT m.id, m.session_id, m.role, m.content, m.created_at,
                           s.source, s.model
                    FROM messages m
                    JOIN sessions s ON m.session_id = s.id
                    WHERE {where_clause}
                    ORDER BY m.created_at DESC
                    LIMIT ?""",
                params + [sq.limit + sq.offset],
            ).fetchall()

            for row in rows:
                # 计算简单相关性分数
                content = row["content"].lower()
                query_lower = sq.query.lower()
                score = content.count(query_lower) / max(len(content), 1)

                results.append(SearchResult(
                    session_id=row["session_id"],
                    message_id=row["id"],
                    role=row["role"],
                    content=row["content"],
                    timestamp=row["created_at"],
                    source=row["source"],
                    model=row["model"],
                    relevance_score=score,
                ))

        except Exception as e:
            logger.error("关键词搜索失败: %s", e)

        return results

    def _filtered_search(self, sq: SearchQuery) -> List[SearchResult]:
        """仅使用过滤条件搜索（无查询词）。"""
        results = []

        conditions = []
        params = []

        if sq.source:
            conditions.append("s.source = ?")
            params.append(sq.source)
        if sq.model:
            conditions.append("s.model = ?")
            params.append(sq.model)
        if sq.role:
            conditions.append("m.role = ?")
            params.append(sq.role)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        try:
            rows = self.db.conn.execute(
                f"""SELECT m.id, m.session_id, m.role, m.content, m.created_at,
                           s.source, s.model
                    FROM messages m
                    JOIN sessions s ON m.session_id = s.id
                    WHERE {where_clause}
                    ORDER BY m.created_at DESC
                    LIMIT ?""",
                params + [sq.limit + sq.offset],
            ).fetchall()

            for row in rows:
                results.append(SearchResult(
                    session_id=row["session_id"],
                    message_id=row["id"],
                    role=row["role"],
                    content=row["content"],
                    timestamp=row["created_at"],
                    source=row["source"],
                    model=row["model"],
                ))

        except Exception as e:
            logger.error("过滤搜索失败: %s", e)

        return results

    def _sort_results(
        self,
        results: List[SearchResult],
        sq: SearchQuery,
    ) -> List[SearchResult]:
        """排序结果。"""
        if sq.query:
            # 按相关性排序
            return sorted(results, key=lambda r: r.relevance_score, reverse=True)
        else:
            # 按时间排序
            return sorted(results, key=lambda r: r.timestamp, reverse=True)

    def get_session_summary(self, session_id: str) -> Dict[str, Any]:
        """获取会话摘要。"""
        try:
            session = self.db.get_session(session_id)
            if not session:
                return {}

            messages = self.db.get_messages(session_id)

            return {
                "session_id": session_id,
                "source": session.get("source"),
                "model": session.get("model"),
                "started_at": session.get("started_at"),
                "ended_at": session.get("ended_at"),
                "message_count": len(messages),
                "user_messages": sum(1 for m in messages if m["role"] == "user"),
                "assistant_messages": sum(1 for m in messages if m["role"] == "assistant"),
            }
        except Exception as e:
            logger.error("获取会话摘要失败: %s", e)
            return {}
