"""spirit/storage/sessions.py — Session CRUD mixin for SessionDB.

v0.3.0 (Hermes-style): extracted from session_db.py.

Responsibilities:
- create_session / end_session / reopen_session / branch_session
- get_session / list_sessions
- set_session_title (used by auto-title generator)
- update_session_counters (incremental counter updates after message append)
- list_sessions ordering: started_at DESC, id DESC (tie-breaker for
  stable ordering when multiple sessions share the same second)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# SQL constants used across the mixin.
_CREATE_SESSION_SQL = """
INSERT INTO sessions (
    id, source, model, system_prompt_hash, cwd,
    parent_session_id, metadata, tags, branch_label, fork_of,
    title, title_source, last_touched_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_GET_SESSION_SQL = "SELECT * FROM sessions WHERE id = ?"

_LIST_SESSIONS_SQL = """
SELECT * FROM sessions
{source_filter}
ORDER BY started_at DESC, id DESC
LIMIT ?
"""

_END_SESSION_SQL = """
UPDATE sessions SET ended_at = CURRENT_TIMESTAMP, end_reason = ?
WHERE id = ? AND ended_at IS NULL
"""

_REOPEN_SESSION_SQL = """
UPDATE sessions SET ended_at = NULL, end_reason = NULL
WHERE id = ?
"""


def _row_to_dict(row: sqlite3.Row | None) -> Optional[Dict]:
    return dict(row) if row else None


class SessionSessionsMixin:
    """CRUD + title for sessions table. Mixed into SessionDB via MRO.

    Expects:
        self.conn (sqlite3.Connection)
        self.db_path (Path)
    """

    # ── Create ──────────────────────────────────────────────
    def create_session(
        self,
        session_id: str = None,
        source: str = "cli",
        model: str = "",
        system_prompt: str = None,
        system_prompt_hash: str = None,
        cwd: str = None,
        parent_session_id: str = None,
        branch_name: str = None,
        branched_from: str = None,
        title: str = None,
        title_source: str = None,
        tags: list | None = None,
        metadata_extra: dict | None = None,
        **kwargs,
    ) -> str:
        """Create new session. Returns session_id (auto-generated if not provided).

        v0.3.0 adds:
        - system_prompt_hash: dedupe against system_prompts table
        - title / title_source: auto-title fields
        - tags: list of strings (stored as JSON)
        - last_touched_at: liveness probe (epoch seconds)
        """
        sid = session_id or str(uuid.uuid4())
        # Promote system_prompt to system_prompts table (dedupe by hash)
        if system_prompt and not system_prompt_hash:
            import hashlib
            system_prompt_hash = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:32]
            self._register_system_prompt(system_prompt_hash, system_prompt)

        meta: dict = {}
        if kwargs:
            meta.update(kwargs)
        if metadata_extra:
            meta.update(metadata_extra)
        meta_json = json.dumps(meta, ensure_ascii=False)
        tags_json = json.dumps(tags or [], ensure_ascii=False)

        now = time.time()
        self.conn.execute(
            _CREATE_SESSION_SQL,
            (
                sid,
                source,
                model or "",
                system_prompt_hash,
                cwd,
                parent_session_id,
                meta_json,
                tags_json,
                branch_name,
                branched_from,
                title,
                title_source,
                now,
            ),
        )
        self.conn.commit()
        logger.debug("会话已创建: %s (source=%s)", sid[:8], source)
        return sid

    # ── Read ────────────────────────────────────────────────
    def get_session(self, session_id: str) -> Optional[Dict]:
        """Get a session by id."""
        row = self.conn.execute(_GET_SESSION_SQL, (session_id,)).fetchone()
        return _row_to_dict(row)

    def list_sessions(
        self,
        limit: int = 20,
        source: str = None,
        include_archived: bool = False,
        only_favorites: bool = False,
    ) -> List[Dict]:
        """List recent sessions, newest first.

        v0.3.0 fix: ORDER BY started_at DESC, id DESC — the id tie-breaker
        fixes unstable ordering when multiple sessions are created in the
        same second (common when user opens CLI rapidly).
        """
        clauses: list[str] = []
        params: list = []
        if source:
            clauses.append("source = ?")
            params.append(source)
        if not include_archived:
            clauses.append("(is_archived IS NULL OR is_archived = 0)")
        if only_favorites:
            clauses.append("is_favorite = 1")
        source_filter = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = _LIST_SESSIONS_SQL.format(source_filter=source_filter)
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ── End / Reopen ────────────────────────────────────────
    def end_session(self, session_id: str, reason: str = "user_exit") -> None:
        """End session — First Reason Wins.

        If already ended with reason "compression", don't overwrite.
        Uses ``WHERE id = ? AND ended_at IS NULL`` so concurrent calls
        don't trample each other.
        """
        existing = self.conn.execute(
            "SELECT end_reason FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if existing and existing["end_reason"]:
            if existing["end_reason"] == "compression":
                logger.debug("会话 %s 已 compression，跳过 %s", session_id[:8], reason)
                return
        self.conn.execute(_END_SESSION_SQL, (reason, session_id))
        self.conn.commit()
        logger.info("会话已结束: %s (原因: %s)", session_id[:8], reason)

    def reopen_session(self, session_id: str) -> bool:
        """Clear ended_at / end_reason. Used by /resume."""
        row = self.conn.execute(
            "SELECT ended_at FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            logger.warning("会话不存在: %s", session_id[:8])
            return False
        if not row["ended_at"]:
            logger.debug("会话 %s 仍在活跃中", session_id[:8])
            return True
        self.conn.execute(_REOPEN_SESSION_SQL, (session_id,))
        self.conn.commit()
        logger.info("会话已重新打开: %s", session_id[:8])
        return True

    # ── Branch ──────────────────────────────────────────────
    def branch_session(
        self,
        parent_session_id: str,
        new_session_id: str = None,
        branch_name: str = None,
        copy_messages: bool = True,
    ) -> str:
        """Create a child session (for /branch). Copies messages by default.

        Hermes-style: child session is a full snapshot of parent (messages
        + metadata) so /branch gives user a sandbox to mutate without
        affecting the original.

        Args:
            parent_session_id: parent session to branch from
            new_session_id: new session id (auto-generated if not provided)
            branch_name: human-readable branch label
            copy_messages: if True (default), copy all messages from
                parent to child. If False, child starts empty.
        """
        parent = self.get_session(parent_session_id)
        if not parent:
            raise ValueError(f"父会话不存在: {parent_session_id}")

        new_sid = new_session_id or str(uuid.uuid4())
        branch_name = branch_name or f"branch-{int(time.time())}"
        meta = {
            "branch_name": branch_name,
            "branched_from": parent_session_id,
        }
        now = time.time()
        self.conn.execute(
            _CREATE_SESSION_SQL,
            (
                new_sid,
                parent["source"],
                parent["model"],
                parent.get("system_prompt_hash"),
                parent.get("cwd"),
                parent_session_id,  # parent_session_id
                json.dumps(meta, ensure_ascii=False),
                parent.get("tags") or "[]",
                branch_name,
                parent_session_id,
                None,  # title
                None,  # title_source
                now,
            ),
        )

        # Copy messages (default behavior, matches v0.2.x contract)
        if copy_messages:
            parent_msgs = self.get_messages(parent_session_id, active_only=False)
            if parent_msgs:
                # Use save_messages batch path for single-transaction speed
                self.save_messages(new_sid, parent_msgs)

        self.conn.commit()
        logger.info(
            "会话已分支: %s → %s (%s, msgs=%d)",
            parent_session_id[:8], new_sid[:8], branch_name,
            len(parent_msgs) if copy_messages else 0,
        )
        return new_sid

    # ── Title ───────────────────────────────────────────────
    def set_session_title(
        self,
        session_id: str,
        title: str,
        source: str = "auto",
    ) -> None:
        """Set session title (called by auto-title generator).

        Args:
            session_id: target session
            title: title text (≤ 80 chars recommended)
            source: "auto" (LLM-generated) or "user" (manually set)
        """
        # First-reason-wins: don't overwrite a user-set title with auto
        existing = self.conn.execute(
            "SELECT title, title_source FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if existing and existing["title_source"] == "user" and source == "auto":
            logger.debug("会话 %s 已有 user title，跳过 auto", session_id[:8])
            return
        self.conn.execute(
            "UPDATE sessions SET title = ?, title_source = ? WHERE id = ?",
            (title, source, session_id),
        )
        self.conn.commit()

    # ── Counters ────────────────────────────────────────────
    def update_session_counters(
        self,
        session_id: str,
        *,
        message_delta: int = 0,
        user_delta: int = 0,
        assistant_delta: int = 0,
        tool_delta: int = 0,
        total_tokens_delta: int = 0,
        input_tokens_delta: int = 0,
        output_tokens_delta: int = 0,
        cost_cents_delta: int = 0,
    ) -> None:
        """Increment session counters atomically.

        Called after each batch flush so counters stay in sync with messages table.
        """
        self.conn.execute(
            """
            UPDATE sessions SET
                message_count = message_count + ?,
                user_message_count = user_message_count + ?,
                assistant_message_count = assistant_message_count + ?,
                tool_call_count = tool_call_count + ?,
                total_tokens = total_tokens + ?,
                input_tokens = input_tokens + ?,
                output_tokens = output_tokens + ?,
                cost_cents = cost_cents + ?,
                last_touched_at = ?
            WHERE id = ?
            """,
            (
                message_delta,
                user_delta,
                assistant_delta,
                tool_delta,
                total_tokens_delta,
                input_tokens_delta,
                output_tokens_delta,
                cost_cents_delta,
                time.time(),
                session_id,
            ),
        )
        self.conn.commit()

    # ── Touch ───────────────────────────────────────────────
    def touch_session(self, session_id: str) -> None:
        """Refresh last_touched_at — call periodically during long turns.

        Cheap O(1) update; doesn't acquire any lock.
        """
        self.conn.execute(
            "UPDATE sessions SET last_touched_at = ? WHERE id = ?",
            (time.time(), session_id),
        )
        self.conn.commit()

    def mark_session_inactive(self, session_id: str, end_reason: str = "task_done") -> bool:
        """Mark a session inactive and set ended_at + end_reason if not already set.

        Idempotent: if ended_at is already non-NULL, the row is left alone
        ("first reason wins", same as end_session()).
        Returns True if a row was changed.
        """
        try:
            cur = self.conn.execute(
                "UPDATE sessions "
                "SET active = 0, ended_at = COALESCE(ended_at, ?), "
                "    end_reason = COALESCE(end_reason, ?), "
                "    last_touched_at = ? "
                "WHERE id = ? AND active = 1",
                (time.time(), end_reason, time.time(), session_id),
            )
            self.conn.commit()
            return cur.rowcount > 0
        except Exception as e:  # noqa: BLE001
            logger.debug("mark_session_inactive 失败: %s", e)
            return False

    # ── Helper: register system_prompt (dedupe) ─────────────
    def _register_system_prompt(self, content_hash: str, prompt: str) -> None:
        """Insert into system_prompts (no-op if hash already exists)."""
        self.conn.execute(
            "INSERT OR IGNORE INTO system_prompts (hash, prompt) VALUES (?, ?)",
            (content_hash, prompt),
        )
        self.conn.commit()
