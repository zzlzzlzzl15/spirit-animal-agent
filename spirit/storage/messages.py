"""spirit/storage/messages.py — Message append/batch/rewind mixin.

v0.3.0 (Hermes-style): extracted from session_db.py.

Responsibilities:
- save_message / save_messages (batch)
- get_messages (with optional filters)
- append_messages_batch (single-transaction batch INSERT — Hermes-style)
- search_messages (FTS5)
- mark_message_observed (replay dedup flag)
- update_session_counters after batch

The single-row INSERT path is preserved for backward compatibility
(add_message() calls save_message). The batch path is the new
Hermes-style primary interface used by spirit/agent/agent.py's
_persist_session() entry point.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
# v0.3.4 fix: insert_message 显式传入本地时间 created_at
from datetime import datetime
from typing import Any, Dict, List, Optional

from .errors import classify_persistence_error

logger = logging.getLogger(__name__)


# Single INSERT for one message — matches Hermes 26-column layout.
# Column count: 4 base + 3 tool + 3 stats + 5 reasoning + 2 replay +
# 4 compression + 2 active + 1 api + 4 display + 1 created_at = 27 columns.
# v0.3.4 fix: 显式传入 created_at = 本地时间（不再依赖表 default CURRENT_TIMESTAMP=UTC）
_INSERT_MESSAGE_SQL = """
INSERT INTO messages (
    session_id, role, content, tool_call_id,
    tool_calls, tool_name, effect_disposition,
    timestamp, token_count, finish_reason,
    reasoning, reasoning_content, reasoning_details, codex_reasoning_items, codex_message_items,
    platform_message_id, observed, _compressed_summary, compressed_summary,
    active, compacted, api_content,
    display_kind, display_metadata, display_identity, display_order,
    created_at
) VALUES (
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?, ?
)
"""

# SQL for read with optional active-only filter
_GET_MESSAGES_SQL = """
SELECT * FROM messages
WHERE session_id = ? {active_filter}
ORDER BY id
"""


def _row_to_message_dict(row: sqlite3.Row | None) -> Optional[Dict]:
    """Convert a sqlite3.Row to a dict.

    Note: tool_calls is stored as JSON string (SQLite binding can't take list).
    Callers should json.loads() themselves if needed — we keep the raw str
    contract from v0.2.11 (tests in test_session_persistence.py rely on this).
    """
    if row is None:
        return None
    return dict(row)


class SessionMessagesMixin:
    """Message append/batch/read/search. Mixed into SessionDB via MRO.

    Expects:
        self.conn (sqlite3.Connection)
        self.db_path (Path)
        # From SessionSessionsMixin:
        update_session_counters(...)
    """

    # ── Single insert (legacy path, still used by add_message) ──
    def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: str | list | dict = None,
        tool_call_id: str = None,
        tool_name: str = None,
        effect_disposition: str = None,
        timestamp: float | None = None,
        token_count: int = 0,
        finish_reason: str = None,
        reasoning: str = None,
        reasoning_content: str = None,
        reasoning_details: str = None,
        codex_reasoning_items: str = None,
        codex_message_items: str = None,
        platform_message_id: str = None,
        api_content: str = None,
        display_kind: str = None,
        display_metadata: str | dict = None,
        display_identity: bytes | str = None,
        display_order: int = None,
        compressed_summary: str = None,
        _compressed_summary: int = 0,
        compacted: int = 0,
        **kwargs,
    ) -> int:
        """Save a single message. Returns the row id.

        Returns:
            int: row id (for replay dedup / tests)

        Notes:
            - tool_calls: pass list/dict → auto json.dumps; pass str → use as-is
            - display_metadata: pass dict → auto json.dumps; pass str → use as-is
            - display_identity: BLOB column (bytes) for fast comparison
        """
        import time
        ts = timestamp if timestamp is not None else time.time()
        if tool_calls is not None and not isinstance(tool_calls, str):
            tool_calls = json.dumps(tool_calls, ensure_ascii=False)
        if display_metadata is not None and not isinstance(display_metadata, str):
            display_metadata = json.dumps(display_metadata, ensure_ascii=False)
        # display_identity is BLOB; accept bytes or str
        if isinstance(display_identity, str):
            display_identity = display_identity.encode("utf-8", errors="surrogatepass")

        try:
            cur = self.conn.execute(
                _INSERT_MESSAGE_SQL,
                (
                    session_id, role, content, tool_call_id,
                    tool_calls, tool_name, effect_disposition,
                    ts, token_count, finish_reason,
                    reasoning, reasoning_content, reasoning_details, codex_reasoning_items, codex_message_items,
                    platform_message_id, 0, _compressed_summary, compressed_summary,  # observed=0 default
                    1, compacted,  # active=1, compacted=0
                    api_content,
                    display_kind, display_metadata, display_identity, display_order,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),  # v0.3.4 fix: created_at = 本地时间
                ),
            )
            self.conn.commit()
            # Update counters (single row delta)
            self._bump_counters_for_message(session_id, role, tool_calls, token_count)
            return cur.lastrowid
        except Exception as e:  # noqa: BLE001
            kind = classify_persistence_error(e)
            logger.debug("save_message 失败 (%s): %s", kind, e)
            return -1

    # ── Batch insert (Hermes-style: single transaction) ─────
    def save_messages(self, session_id: str, messages: List[Dict[str, Any]]) -> int:
        """Batch save messages in one transaction.

        Hermes spirit's _db_flush_write: collects un-flushed dicts,
        stamps _DB_PERSISTED_MARKER after success, single commit.

        Returns:
            int: number of rows inserted (-1 on failure)
        """
        if not messages:
            return 0
        try:
            cursor = self.conn.cursor()
            import time
            now = time.time()
            for msg in messages:
                # Normalize fields
                role = msg.get("role", "")
                content = msg.get("content")
                tool_calls = msg.get("tool_calls")
                if tool_calls is not None and not isinstance(tool_calls, str):
                    tool_calls = json.dumps(tool_calls, ensure_ascii=False)
                tool_call_id = msg.get("tool_call_id")
                tool_name = msg.get("tool_name")
                effect_disposition = msg.get("effect_disposition")
                timestamp = msg.get("timestamp", now)
                token_count = msg.get("token_count", 0)
                finish_reason = msg.get("finish_reason")
                reasoning = msg.get("reasoning")
                reasoning_content = msg.get("reasoning_content")
                reasoning_details = msg.get("reasoning_details")
                codex_reasoning_items = msg.get("codex_reasoning_items")
                codex_message_items = msg.get("codex_message_items")
                platform_message_id = msg.get("platform_message_id")
                api_content = msg.get("api_content")
                display_kind = msg.get("display_kind")
                display_metadata = msg.get("display_metadata")
                if display_metadata is not None and not isinstance(display_metadata, str):
                    display_metadata = json.dumps(display_metadata, ensure_ascii=False)
                display_identity = msg.get("display_identity")
                if isinstance(display_identity, str):
                    display_identity = display_identity.encode("utf-8", errors="surrogatepass")
                display_order = msg.get("display_order")
                compressed_summary = msg.get("compressed_summary")
                _compressed_summary = msg.get("_compressed_summary", 0)
                compacted = msg.get("compacted", 0)

                cursor.execute(
                    _INSERT_MESSAGE_SQL,
                    (
                        session_id, role, content, tool_call_id,
                        tool_calls, tool_name, effect_disposition,
                        timestamp, token_count, finish_reason,
                        reasoning, reasoning_content, reasoning_details, codex_reasoning_items, codex_message_items,
                        platform_message_id, 0, _compressed_summary, compressed_summary,
                        1, compacted, api_content,
                        display_kind, display_metadata, display_identity, display_order,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),  # v0.3.4 fix
                    ),
                )
            self.conn.commit()
            # Bulk counter update
            user_d = sum(1 for m in messages if m.get("role") == "user")
            asst_d = sum(1 for m in messages if m.get("role") == "assistant")
            tool_d = sum(
                len(m.get("tool_calls") or [])
                for m in messages
                if isinstance(m.get("tool_calls"), list)
            )
            tok_d = sum(int(m.get("token_count") or 0) for m in messages)
            self.update_session_counters(
                session_id,
                message_delta=len(messages),
                user_delta=user_d,
                assistant_delta=asst_d,
                tool_delta=tool_d,
                total_tokens_delta=tok_d,
            )
            return len(messages)
        except Exception as e:  # noqa: BLE001
            kind = classify_persistence_error(e)
            logger.debug("save_messages 失败 (%s): %s", kind, e)
            return -1

    # ── Read ────────────────────────────────────────────────
    def get_messages(
        self,
        session_id: str,
        active_only: bool = True,
        include_observed: bool = True,
    ) -> List[Dict]:
        """Get all messages for a session, ordered by id (insertion order).

        Args:
            session_only: filter by active=1 (skip soft-deleted)
            include_observed: include messages with observed=1 (default True)
        """
        if active_only:
            active_filter = "AND active = 1"
        else:
            active_filter = ""
        sql = _GET_MESSAGES_SQL.format(active_filter=active_filter)
        rows = self.conn.execute(sql, (session_id,)).fetchall()
        return [_row_to_message_dict(r) for r in rows]

    def get_message(self, message_id: int) -> Optional[Dict]:
        """Get one message by row id."""
        row = self.conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return _row_to_message_dict(row)

    # ── Hermes-aligned v0.4.0 in-flight repair (referenced by storage_*.py middleware) ──

    def save_messages_with_repair(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
    ) -> tuple[int, Dict[str, int]]:
        """Batch save with Hermes-style in-flight transcript repair.

        Wraps ``save_messages`` so an assistant message carrying
        ``_row_id`` is reconciled with the committed SQLite row inside
        the active write transaction:

        - blank target row → UPDATE in place (the live bytes ARE the
          durable copy); the new dict is dropped from the INSERT batch
          so no duplicate row is created.
        - non-blank target row (concurrent winner) → adopt the
          canonical content via ``_canonical_content``; dropped from the
          INSERT batch entirely.
        - watermark compaction may have soft-archived the target row
          and created a clone; the clone is detected by
          ``(session_id, timestamp)`` and used as the UPDATE target.

        After commit, ``sync_flushed_message_markers`` stamps
        ``_DB_PERSISTED_MARKER`` on every durable dict (INSERT, in-place
        UPDATE, and adopted winner) and syncs canonical fields
        (``_row_id``, ``timestamp``, ``content``).

        Returns:
            (rows_inserted, repair_counts) where ``repair_counts`` is
            ``{blank_updated, winner_adopted, fresh_insert}``.

        Boundary contract (v0.4.0):
          - caller MUST pass a fresh ``messages`` list it owns (we
            mutate in place to stamp markers and adopt canonical
            content)
          - autocommit mode is assumed; the repair ``UPDATE`` is
            committed by SQLite before the batch INSERT runs
          - failures are isolated (return ``(-1, {})``); caller decides
            retry vs divert
        """
        if not messages:
            return 0, {"blank_updated": 0, "winner_adopted": 0, "fresh_insert": 0}
        try:
            from ..agent.storage_transcript_repair import (
                resolve_and_repair_transcript_batch,
                sync_flushed_message_markers,
            )
            to_insert = resolve_and_repair_transcript_batch(
                self.conn,
                session_id,
                messages,
                encode_content_fn=lambda x: x,
                decode_content_fn=lambda x: x,
            )
            inserted = self.save_messages(session_id, to_insert)
            # sync markers across the FULL batch (inserted + adopted +
            # in-place updated). Adopted winners are detected by
            # ``_canonical_content`` on the dict.
            batch_rows = [
                {
                    "_row_id": m.get("_row_id"),
                    "timestamp": m.get("timestamp"),
                    "_canonical_content": m.get("_canonical_content"),
                }
                for m in messages
            ]
            sync_flushed_message_markers(messages, batch_rows)
            counts = {
                "blank_updated": sum(
                    1 for m in messages
                    if m.get("_row_id") is not None
                    and m.get("_canonical_content") is None
                    and m not in to_insert
                ),
                "winner_adopted": sum(
                    1 for m in messages if m.get("_canonical_content") is not None
                ),
                "fresh_insert": len(to_insert),
            }
            return inserted, counts
        except Exception as e:  # noqa: BLE001
            kind = classify_persistence_error(e)
            logger.debug("save_messages_with_repair 失败 (%s): %s", kind, e)
            return -1, {"blank_updated": 0, "winner_adopted": 0, "fresh_insert": 0}

    def update_message(self, message_id: int, **fields: Any) -> bool:
        """In-place update of message columns. Returns True if a row changed.

        Allowed columns: content, reasoning, reasoning_content, reasoning_details,
        codex_reasoning_items, codex_message_items, tool_calls, tool_name,
        effect_disposition, token_count, finish_reason, api_content,
        display_kind, display_metadata, display_identity, display_order,
        compressed_summary, _compressed_summary, compacted, active, observed.
        """
        allowed = {
            "content", "reasoning", "reasoning_content", "reasoning_details",
            "codex_reasoning_items", "codex_message_items",
            "tool_calls", "tool_name", "effect_disposition",
            "token_count", "finish_reason", "api_content",
            "display_kind", "display_metadata", "display_identity", "display_order",
            "compressed_summary", "_compressed_summary", "compacted", "active",
            "observed",
        }
        sets: List[str] = []
        values: List[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("tool_calls", "display_metadata") and v is not None and not isinstance(v, str):
                v = json.dumps(v, ensure_ascii=False)
            if k == "display_identity" and isinstance(v, str):
                v = v.encode("utf-8", errors="surrogatepass")
            sets.append(f"{k} = ?")
            values.append(v)
        if not sets:
            return False
        values.append(message_id)
        try:
            cur = self.conn.execute(
                f"UPDATE messages SET {', '.join(sets)} WHERE id = ?",
                values,
            )
            self.conn.commit()
            return cur.rowcount > 0
        except Exception as e:  # noqa: BLE001
            kind = classify_persistence_error(e)
            logger.debug("update_message 失败 (%s): %s", kind, e)
            return False

    def replace_messages(
        self,
        session_id: str,
        cleaned: List[Dict[str, Any]],
        preserve_counters: bool = True,
    ) -> bool:
        """Hard-replace all messages for a session with the supplied list.

        Used by transcript_repair after in-memory merge/drop.
        """
        try:
            self.conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            if cleaned:
                import time as _t
                now = _t.time()
                rows: List[tuple] = []
                for m in cleaned:
                    role = m.get("role", "")
                    content = m.get("content")
                    tool_calls = m.get("tool_calls")
                    if tool_calls is not None and not isinstance(tool_calls, str):
                        tool_calls = json.dumps(tool_calls, ensure_ascii=False)
                    display_metadata = m.get("display_metadata")
                    if display_metadata is not None and not isinstance(display_metadata, str):
                        display_metadata = json.dumps(display_metadata, ensure_ascii=False)
                    display_identity = m.get("display_identity")
                    if isinstance(display_identity, str):
                        display_identity = display_identity.encode("utf-8", errors="surrogatepass")
                    rows.append((
                        session_id, role, content, m.get("tool_call_id"),
                        tool_calls, m.get("tool_name"), m.get("effect_disposition"),
                        m.get("timestamp", now), m.get("token_count", 0),
                        m.get("finish_reason"),
                        m.get("reasoning"), m.get("reasoning_content"),
                        m.get("reasoning_details"), m.get("codex_reasoning_items"),
                        m.get("codex_message_items"),
                        m.get("platform_message_id"), 0,
                        m.get("_compressed_summary", 0),
                        m.get("compressed_summary"),
                        1, m.get("compacted", 0),
                        m.get("api_content"),
                        m.get("display_kind"), display_metadata,
                        display_identity, m.get("display_order"),
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),  # v0.3.4 fix
                    ))
                self.conn.executemany(_INSERT_MESSAGE_SQL, rows)
            self.conn.commit()
            if not preserve_counters and cleaned:
                user_d = sum(1 for m in cleaned if m.get("role") == "user")
                asst_d = sum(1 for m in cleaned if m.get("role") == "assistant")
                tok_d = sum(int(m.get("token_count") or 0) for m in cleaned)
                self.update_session_counters(
                    session_id,
                    message_delta=len(cleaned),
                    user_delta=user_d,
                    assistant_delta=asst_d,
                    tool_delta=0,
                    total_tokens_delta=tok_d,
                )
            return True
        except Exception as e:  # noqa: BLE001
            kind = classify_persistence_error(e)
            logger.debug("replace_messages 失败 (%s): %s", kind, e)
            return False

    def get_messages_by_ids(
        self,
        session_id: str,
        ids: List[int],
    ) -> List[Dict]:
        """Fetch a specific subset of messages for a session, ordered by id."""
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT * FROM messages WHERE session_id = ? AND id IN ({placeholders}) "
            f"ORDER BY id",
            [session_id, *ids],
        ).fetchall()
        return [_row_to_message_dict(r) for r in rows]

    def get_messages_before(
        self,
        session_id: str,
        threshold_ts: float,
        active_only: bool = True,
    ) -> List[Dict]:
        """Messages older than the given epoch seconds (used by history archiver)."""
        af = "AND active = 1" if active_only else ""
        rows = self.conn.execute(
            f"SELECT * FROM messages WHERE session_id = ? AND timestamp < ? {af} "
            f"ORDER BY timestamp ASC",
            (session_id, threshold_ts),
        ).fetchall()
        return [_row_to_message_dict(r) for r in rows]

    def delete_messages_before(
        self,
        session_id: str,
        threshold_ts: float,
        soft: bool = True,
    ) -> int:
        """Delete (soft by default) messages older than the threshold."""
        try:
            if soft:
                cur = self.conn.execute(
                    "UPDATE messages SET active = 0 "
                    "WHERE session_id = ? AND timestamp < ?",
                    (session_id, threshold_ts),
                )
            else:
                cur = self.conn.execute(
                    "DELETE FROM messages WHERE session_id = ? AND timestamp < ?",
                    (session_id, threshold_ts),
                )
            self.conn.commit()
            return cur.rowcount
        except Exception as e:  # noqa: BLE001
            kind = classify_persistence_error(e)
            logger.debug("delete_messages_before 失败 (%s): %s", kind, e)
            return 0

    def get_first_user_message(self, session_id: str) -> Optional[Dict]:
        """First user-role message for a session (used by title generator)."""
        row = self.conn.execute(
            "SELECT * FROM messages WHERE session_id = ? AND role = 'user' "
            "ORDER BY id ASC LIMIT 1",
            (session_id,),
        ).fetchone()
        return _row_to_message_dict(row)

    def get_last_n_messages(self, session_id: str, n: int) -> List[Dict]:
        """The N most recent messages (active only), oldest first within the window."""
        if n <= 0:
            return []
        rows = self.conn.execute(
            "SELECT * FROM messages "
            "WHERE session_id = ? AND active = 1 "
            "ORDER BY id DESC LIMIT ?",
            (session_id, n),
        ).fetchall()
        return [_row_to_message_dict(r) for r in reversed(rows)]

    def mark_messages_compacted(
        self,
        session_id: str,
        message_ids: List[int],
        compressed_summary: str,
    ) -> int:
        """Mark a range of messages as compacted and store a summary.

        Delegates to compression.py's full implementation when that mixin
        is present; otherwise this lightweight fallback is used.
        """
        if not message_ids:
            return 0
        try:
            placeholders = ",".join("?" * len(message_ids))
            cur = self.conn.execute(
                f"UPDATE messages SET compacted = 1, "
                f"_compressed_summary = 1, "
                f"compressed_summary = ? "
                f"WHERE session_id = ? AND id IN ({placeholders})",
                [compressed_summary, session_id, *message_ids],
            )
            self.conn.commit()
            return cur.rowcount
        except Exception as e:  # noqa: BLE001
            kind = classify_persistence_error(e)
            logger.debug("mark_messages_compacted 失败 (%s): %s", kind, e)
            return 0

    def get_compacted_messages(self, session_id: str) -> List[Dict]:
        """Return compacted messages for a session (most recent first by id)."""
        rows = self.conn.execute(
            "SELECT * FROM messages "
            "WHERE session_id = ? AND compacted = 1 "
            "ORDER BY id DESC",
            (session_id,),
        ).fetchall()
        return [_row_to_message_dict(r) for r in rows]

    # ── FTS search ───────────────────────────────────────────
    def search_messages(self, query: str, limit: int = 20) -> List[Dict]:
        """Full-text search across messages.content."""
        rows = self.conn.execute(
            """
            SELECT m.*, s.source, s.model
            FROM messages m
            JOIN sessions s ON m.session_id = s.id
            JOIN messages_fts f ON m.id = f.rowid
            WHERE messages_fts MATCH ?
            ORDER BY m.created_at DESC
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
        return [_row_to_message_dict(r) for r in rows]

    # ── Replay dedup ─────────────────────────────────────────
    def mark_observed(self, message_id: int) -> None:
        """Mark a message as observed (consumed by replay / UI)."""
        self.conn.execute(
            "UPDATE messages SET observed = 1 WHERE id = ?", (message_id,)
        )
        self.conn.commit()

    # ── Soft delete ──────────────────────────────────────────
    def soft_delete_message(self, message_id: int) -> None:
        """Soft-delete (set active=0) instead of removing."""
        self.conn.execute(
            "UPDATE messages SET active = 0 WHERE id = ?", (message_id,)
        )
        self.conn.commit()

    # ── Helper: counter bump for single insert ──────────────
    def _bump_counters_for_message(
        self,
        session_id: str,
        role: str,
        tool_calls: Any,
        token_count: int,
    ) -> None:
        """Increment session counters after save_message."""
        user_d = 1 if role == "user" else 0
        asst_d = 1 if role == "assistant" else 0
        tool_d = 0
        if tool_calls:
            try:
                parsed = json.loads(tool_calls) if isinstance(tool_calls, str) else tool_calls
                tool_d = len(parsed) if isinstance(parsed, list) else 1
            except (json.JSONDecodeError, TypeError):
                tool_d = 1
        self.update_session_counters(
            session_id,
            message_delta=1,
            user_delta=user_d,
            assistant_delta=asst_d,
            tool_delta=tool_d,
            total_tokens_delta=int(token_count or 0),
        )
