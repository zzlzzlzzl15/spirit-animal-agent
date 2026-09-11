"""历史会话搜索 — session_search（搜索过去的对话）。

参考 Hermes 的 tools/session_search_tool.py 设计：
- 搜索历史会话和消息
- 利用 FTS5 全文搜索
- 帮助回忆之前讨论过的内容
"""

import json
import logging
from pathlib import Path
from typing import List, Optional

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SESSION_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "session_search",
        "description": (
            "搜索历史会话和消息内容。\n\n"
            "当你想回忆之前讨论过的内容、查找之前的代码修改、\n"
            "或回顾某个话题的结论时使用。\n\n"
            "支持全文搜索，返回匹配的消息及所属会话信息。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回结果数量（默认 10，最大 30）",
                },
                "role": {
                    "type": "string",
                    "description": "过滤消息角色: user / assistant / tool（可选）",
                },
            },
            "required": ["query"],
        },
    },
}


def _session_search_impl(query: str, limit: int = 10, role: str = None) -> str:
    """搜索历史会话。"""
    limit = min(max(limit, 1), 30)

    try:
        from spirit.storage.session_db import SessionDB

        db = SessionDB()

        if role:
            # 带角色过滤的搜索
            rows = db.conn.execute(
                """SELECT m.*, s.source, s.model, s.started_at
                   FROM messages m
                   JOIN sessions s ON m.session_id = s.id
                   WHERE messages_fts MATCH ? AND m.role = ?
                   ORDER BY m.created_at DESC
                   LIMIT ?""",
                (query, role, limit),
            ).fetchall()
        else:
            rows = db.conn.execute(
                """SELECT m.*, s.source, s.model, s.started_at
                   FROM messages m
                   JOIN sessions s ON m.session_id = s.id
                   WHERE messages_fts MATCH ?
                   ORDER BY m.created_at DESC
                   LIMIT ?""",
                (query, limit),
            ).fetchall()

        results = []
        for row in rows:
            d = dict(row)
            # 截断内容
            content = d.get("content", "")
            if len(content) > 300:
                # 高亮匹配位置
                idx = content.lower().find(query.lower())
                if idx >= 0:
                    start = max(0, idx - 100)
                    end = min(len(content), idx + 200)
                    content = ("..." if start > 0 else "") + content[start:end] + ("..." if end < len(content) else "")
                else:
                    content = content[:300] + "..."

            results.append({
                "session_id": d.get("session_id", "")[:8],
                "role": d.get("role", ""),
                "content": content,
                "source": d.get("source", ""),
                "model": d.get("model", ""),
                "time": d.get("started_at", ""),
            })

        db.close()

        return json.dumps({
            "query": query,
            "results": results,
            "count": len(results),
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({"error": f"搜索失败: {e}"})


registry.register(
    name="session_search",
    toolset="memory",
    schema=SESSION_SEARCH_SCHEMA,
    handler=_session_search_impl,
    description="搜索历史会话",
    emoji="🔍",
)


# ---------------------------------------------------------------------------
# session_list — 列出最近会话
# ---------------------------------------------------------------------------

SESSION_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "session_list",
        "description": "列出最近的会话（不含消息内容）。",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "返回数量（默认 10）",
                },
                "source": {
                    "type": "string",
                    "description": "过滤来源: cli / api / websocket（可选）",
                },
            },
            "required": [],
        },
    },
}


def _session_list_impl(limit: int = 10, source: str = None) -> str:
    limit = min(max(limit, 1), 50)

    try:
        from spirit.storage.session_db import SessionDB

        db = SessionDB()
        sessions = db.list_sessions(limit=limit, source=source)

        results = []
        for s in sessions:
            # 统计消息数
            msg_count = db.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                (s["id"],),
            ).fetchone()[0]

            results.append({
                "id": s["id"][:8],
                "source": s.get("source", ""),
                "model": s.get("model", ""),
                "messages": msg_count,
                "started": s.get("started_at", ""),
                "ended": s.get("ended_at"),
            })

        db.close()

        return json.dumps({
            "sessions": results,
            "count": len(results),
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({"error": f"查询失败: {e}"})


registry.register(
    name="session_list",
    toolset="memory",
    schema=SESSION_LIST_SCHEMA,
    handler=_session_list_impl,
    description="列出最近会话",
    emoji="📋",
)
