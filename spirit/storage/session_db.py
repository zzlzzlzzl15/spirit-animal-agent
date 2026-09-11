"""SQLite 会话存储 — 持久化对话历史。

参考 Hermes 的 hermes_state.py 设计：
- WAL 模式支持并发读写
- FTS5 全文搜索
- 会话分裂（压缩触发时）
"""

import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 默认数据库路径
DEFAULT_DB_PATH = Path.home() / ".spirit" / "state.db"


class SessionDB:
    """会话数据库 — 管理会话和消息的持久化存储。"""

    def __init__(self, db_path: Path = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")

        self._init_schema()
        logger.debug("数据库已连接: %s", self.db_path)

    def _init_schema(self):
        """初始化数据库表结构。"""
        self.conn.executescript("""
            -- 会话表
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL DEFAULT 'cli',
                model TEXT NOT NULL,
                system_prompt TEXT,
                cwd TEXT,
                parent_session_id TEXT REFERENCES sessions(id),
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP,
                end_reason TEXT,
                metadata TEXT DEFAULT '{}'
            );

            -- 消息表
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                tool_call_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 全文搜索索引
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content,
                content='messages',
                content_rowid='id'
            );

            -- FTS 同步触发器
            CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content)
                VALUES (new.id, new.content);
            END;

            -- 索引
            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_started
                ON sessions(started_at DESC);
        """)
        self.conn.commit()

    # ------------------------------------------------------------------
    # 会话操作
    # ------------------------------------------------------------------

    def create_session(
        self,
        session_id: str = None,
        source: str = "cli",
        model: str = "",
        system_prompt: str = None,
        cwd: str = None,
        **kwargs,
    ) -> str:
        """创建新会话。返回 session_id。"""
        sid = session_id or str(uuid.uuid4())
        self.conn.execute(
            """INSERT INTO sessions (id, source, model, system_prompt, cwd, metadata)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (sid, source, model, system_prompt, cwd, json.dumps(kwargs)),
        )
        self.conn.commit()
        return sid

    def end_session(self, session_id: str, reason: str = "user_exit"):
        """结束会话（First Reason Wins 机制）。
        
        如果会话已经标记为 'compression'，后续的 'agent_close' 不会覆盖。
        这确保了压缩链的完整性。
        
        Args:
            session_id: 会话 ID
            reason: 结束原因（compression/agent_close/user_exit/resumed_other）
        """
        # First Reason Wins: 检查是否已有结束原因
        existing = self.conn.execute(
            "SELECT end_reason FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        
        if existing and existing['end_reason']:
            # 已有结束原因，不覆盖（除非是 compression 且新原因是 agent_close）
            if existing['end_reason'] == 'compression':
                logger.debug(
                    "会话 %s 已标记为 compression，跳过 %s",
                    session_id[:8], reason
                )
                return
        
        self.conn.execute(
            """UPDATE sessions SET ended_at = CURRENT_TIMESTAMP, end_reason = ?
               WHERE id = ? AND ended_at IS NULL""",
            (reason, session_id),
        )
        self.conn.commit()
        logger.info("会话已结束: %s (原因: %s)", session_id[:8], reason)

    def reopen_session(self, session_id: str):
        """重新打开已结束的会话（用于 /resume）。
        
        Args:
            session_id: 会话 ID
            
        Returns:
            bool: 是否成功重新打开
        """
        row = self.conn.execute(
            "SELECT ended_at FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        
        if not row:
            logger.warning("会话不存在: %s", session_id[:8])
            return False
        
        if not row['ended_at']:
            logger.debug("会话 %s 仍在活跃中", session_id[:8])
            return True
        
        # 清除结束标记
        self.conn.execute(
            """UPDATE sessions SET ended_at = NULL, end_reason = NULL
               WHERE id = ?""",
            (session_id,),
        )
        self.conn.commit()
        logger.info("会话已重新打开: %s", session_id[:8])
        return True

    def branch_session(
        self,
        parent_session_id: str,
        new_session_id: str = None,
        branch_name: str = None,
    ) -> str:
        """创建会话分支（用于 /branch）。
        
        Args:
            parent_session_id: 父会话 ID
            new_session_id: 新会话 ID（可选，自动生成）
            branch_name: 分支名称（可选）
            
        Returns:
            str: 新会话 ID
        """
        import uuid as uuid_module
        
        # 获取父会话信息
        parent = self.get_session(parent_session_id)
        if not parent:
            raise ValueError(f"父会话不存在: {parent_session_id}")
        
        # 生成新会话 ID
        new_sid = new_session_id or str(uuid_module.uuid4())
        
        # 创建新会话记录（带 parent_session_id 链接）
        metadata = {
            "branch_name": branch_name or f"branch-{int(time.time())}",
            "branched_from": parent_session_id,
        }
        
        self.conn.execute(
            """INSERT INTO sessions (id, source, model, system_prompt, cwd, parent_session_id, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                new_sid,
                parent['source'],
                parent['model'],
                parent.get('system_prompt'),
                parent.get('cwd'),
                parent_session_id,
                json.dumps(metadata),
            ),
        )
        
        # 复制所有消息到新会话
        messages = self.get_messages(parent_session_id)
        for msg in messages:
            tool_calls = None
            if msg.get('tool_calls'):
                tool_calls = json.dumps(msg['tool_calls']) if isinstance(msg['tool_calls'], (dict, list)) else msg['tool_calls']
            
            self.conn.execute(
                """INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    new_sid,
                    msg['role'],
                    msg.get('content', ''),
                    tool_calls,
                    msg.get('tool_call_id'),
                ),
            )
        
        self.conn.commit()
        logger.info(
            "会话已分支: %s → %s (%s)",
            parent_session_id[:8], new_sid[:8], metadata['branch_name']
        )
        
        return new_sid

    def get_session(self, session_id: str) -> Optional[Dict]:
        """获取会话信息。"""
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, limit: int = 20, source: str = None) -> List[Dict]:
        """列出最近的会话。"""
        query = "SELECT * FROM sessions"
        params = []
        if source:
            query += " WHERE source = ?"
            params.append(source)
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 消息操作
    # ------------------------------------------------------------------

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: str = None,
        tool_call_id: str = None,
    ):
        """保存一条消息。"""
        self.conn.execute(
            """INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, role, content, tool_calls, tool_call_id),
        )
        self.conn.commit()

    def get_messages(self, session_id: str) -> List[Dict]:
        """获取会话的所有消息。"""
        rows = self.conn.execute(
            """SELECT * FROM messages WHERE session_id = ? ORDER BY created_at""",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def save_messages(self, session_id: str, messages: List[Dict]):
        """批量保存消息。"""
        for msg in messages:
            tool_calls = None
            if "tool_calls" in msg and msg["tool_calls"]:
                tool_calls = json.dumps(msg["tool_calls"])
            self.conn.execute(
                """INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    session_id,
                    msg.get("role", ""),
                    msg.get("content", ""),
                    tool_calls,
                    msg.get("tool_call_id"),
                ),
            )
        self.conn.commit()

    # ------------------------------------------------------------------
    # 全文搜索
    # ------------------------------------------------------------------

    def search_messages(self, query: str, limit: int = 20) -> List[Dict]:
        """全文搜索消息（FTS5）。"""
        rows = self.conn.execute(
            """SELECT m.*, s.source, s.model
               FROM messages m
               JOIN sessions s ON m.session_id = s.id
               JOIN messages_fts f ON m.id = f.rowid
               WHERE messages_fts MATCH ?
               ORDER BY m.created_at DESC
               LIMIT ?""",
            (query, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------

    def close(self):
        """关闭数据库连接。"""
        if self.conn:
            self.conn.close()
