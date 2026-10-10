"""spirit/storage/search.py — FTS5 search across messages and sessions.

v0.4.0 (Hermes-aligned): full-text + trigram + CJK search via the 5
FTS5 virtual tables created by init_schema.

Reference: hermes_state_search.py.

Spirit uses the standard unicode61 tokenizer for the primary index; the
trigram index handles short-word / fuzzy queries; the CJK index is
contentless and reuses the messages.content column.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SessionSearchMixin:
    """Full-text + LIKE search mixin.

    Mixed into SessionDB via MRO.
    Expects:
        self.conn (sqlite3.Connection)
        self.db_path (Path)
    """

    def _like_params(self, query: str) -> List[str]:
        """Build LIKE params for a multi-word substring search."""
        words = [w for w in re.findall(r"\w+", query) if w]
        return [f"%{w}%" for w in words]

    def _flatten_text(self, text: Any) -> str:
        """Flatten a message content (str | list | None) to a single string."""
        if text is None:
            return ""
        if isinstance(text, str):
            return text
        if isinstance(text, list):
            parts = []
            for p in text:
                if isinstance(p, dict) and p.get("type") == "text":
                    parts.append(p.get("text", ""))
                elif isinstance(p, dict) and p.get("type") in ("image", "image_url"):
                    parts.append("[screenshot]")
            return "\n".join(parts)
        return str(text)

    def _quote_fts_tokens(self, query: str) -> str:
        """Quote each FTS5 token to escape special chars."""
        words = re.findall(r"\w+", query)
        if not words:
            return ""
        return " ".join(f'"{w}"' for w in words)

    # ── FTS search (primary) ──────────────────────────────────

    def search_messages(
        self,
        query: str,
        *,
        session_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Full-text search across messages.

        Falls back to LIKE if the FTS query is malformed.
        """
        if not query or not query.strip():
            return []
        fts_query = self._quote_fts_tokens(query)
        if not fts_query:
            return []
        try:
            if session_id:
                rows = self.conn.execute(
                    """SELECT m.id, m.session_id, m.role, m.content,
                              m.timestamp, m.tool_name, s.title
                       FROM messages m
                       JOIN sessions s ON m.session_id = s.id
                       JOIN messages_fts f ON m.id = f.rowid
                       WHERE messages_fts MATCH ?
                         AND m.session_id = ?
                         AND m.active = 1
                       ORDER BY m.timestamp DESC
                       LIMIT ?""",
                    (fts_query, session_id, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    """SELECT m.id, m.session_id, m.role, m.content,
                              m.timestamp, m.tool_name, s.title
                       FROM messages m
                       JOIN sessions s ON m.session_id = s.id
                       JOIN messages_fts f ON m.id = f.rowid
                       WHERE messages_fts MATCH ?
                         AND m.active = 1
                       ORDER BY m.timestamp DESC
                       LIMIT ?""",
                    (fts_query, limit),
                ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError as e:
            # Malformed FTS query — fall back to LIKE
            logger.debug("FTS query failed, falling back to LIKE: %s", e)
            return self._search_messages_like(query, session_id=session_id, limit=limit)

    def search_messages_trigram(
        self,
        query: str,
        *,
        session_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Trigram search (good for short / partial words)."""
        if not query or not query.strip():
            return []
        # Trigram tokenizer matches as substring by default for queries ≥3 chars
        if len(query) < 3:
            return []
        try:
            if session_id:
                rows = self.conn.execute(
                    """SELECT m.id, m.session_id, m.role, m.content,
                              m.timestamp, s.title
                       FROM messages m
                       JOIN sessions s ON m.session_id = s.id
                       JOIN messages_fts_trigram f ON m.id = f.rowid
                       WHERE messages_fts_trigram MATCH ?
                         AND m.session_id = ?
                         AND m.active = 1
                       ORDER BY m.timestamp DESC
                       LIMIT ?""",
                    (query, session_id, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    """SELECT m.id, m.session_id, m.role, m.content,
                              m.timestamp, s.title
                       FROM messages m
                       JOIN sessions s ON m.session_id = s.id
                       JOIN messages_fts_trigram f ON m.id = f.rowid
                       WHERE messages_fts_trigram MATCH ?
                         AND m.active = 1
                       ORDER BY m.timestamp DESC
                       LIMIT ?""",
                    (query, limit),
                ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError as e:
            logger.debug("trigram FTS failed: %s", e)
            return []

    def search_sessions(
        self,
        query: str,
        *,
        source: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Search across session title + last_preview + notes."""
        if not query or not query.strip():
            return []
        fts_query = self._quote_fts_tokens(query)
        if not fts_query:
            return []
        try:
            if source:
                rows = self.conn.execute(
                    """SELECT s.id, s.source, s.title, s.last_preview, s.notes,
                              s.started_at, s.ended_at
                       FROM sessions s
                       JOIN sessions_fts f ON s.rowid = f.rowid
                       WHERE sessions_fts MATCH ?
                         AND s.source = ?
                       ORDER BY s.started_at DESC
                       LIMIT ?""",
                    (fts_query, source, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    """SELECT s.id, s.source, s.title, s.last_preview, s.notes,
                              s.started_at, s.ended_at
                       FROM sessions s
                       JOIN sessions_fts f ON s.rowid = f.rowid
                       WHERE sessions_fts MATCH ?
                       ORDER BY s.started_at DESC
                       LIMIT ?""",
                    (fts_query, limit),
                ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError as e:
            logger.debug("sessions FTS failed: %s", e)
            return []

    def _search_messages_like(
        self,
        query: str,
        *,
        session_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """LIKE-based fallback search."""
        words = [w for w in re.findall(r"\w+", query) if w]
        if not words:
            return []
        where_parts = ["m.content LIKE ? COLLATE NOCASE" for _ in words]
        where_parts.append("m.active = 1")
        params: List[Any] = [f"%{w}%" for w in words]
        if session_id:
            where_parts.append("m.session_id = ?")
            params.append(session_id)
        params.append(limit)
        sql = f"""
            SELECT m.id, m.session_id, m.role, m.content, m.timestamp, s.title
            FROM messages m
            JOIN sessions s ON m.session_id = s.id
            WHERE {' AND '.join(where_parts)}
            ORDER BY m.timestamp DESC
            LIMIT ?
        """
        try:
            rows = self.conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:  # noqa: BLE001
            logger.debug("LIKE search failed: %s", e)
            return []

    # ── FTS maintenance ───────────────────────────────────────

    def fts_health(self) -> Dict[str, Any]:
        """Return FTS index health (counts of indexed rows)."""
        out: Dict[str, Any] = {}
        for tbl in ("messages_fts", "messages_fts_trigram", "messages_fts_cjk",
                    "sessions_fts", "sessions_fts_trigram"):
            try:
                row = self.conn.execute(f"SELECT COUNT(*) AS n FROM {tbl}").fetchone()
                out[tbl] = int(row["n"]) if row else 0
            except sqlite3.OperationalError:
                out[tbl] = None
        return out

    def mark_fts_stale(self) -> None:
        """Mark the FTS indexes as stale (next search will rebuild)."""
        try:
            self.conn.execute(
                """INSERT INTO state_meta (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                updated_at = CURRENT_TIMESTAMP""",
                ("fts_stale", "1"),
            )
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("mark_fts_stale: %s", e)

    def is_fts_stale(self) -> bool:
        try:
            row = self.conn.execute(
                "SELECT value FROM state_meta WHERE key = ?",
                ("fts_stale",),
            ).fetchone()
            return row is not None and row["value"] == "1"
        except Exception:  # noqa: BLE001
            return False

    def rebuild_fts_index(self) -> int:
        """Rebuild the FTS indexes from scratch.

        Returns the total number of rows reindexed.
        """
        total = 0
        for tbl in ("messages_fts", "messages_fts_trigram",
                    "messages_fts_cjk", "sessions_fts", "sessions_fts_trigram"):
            try:
                # Insert/Update rebuild: delete all then re-insert from source
                if tbl.startswith("messages_fts"):
                    self.conn.execute(f"INSERT INTO {tbl}({tbl}) VALUES('rebuild')")
                else:
                    self.conn.execute(f"INSERT INTO {tbl}({tbl}) VALUES('rebuild')")
                row = self.conn.execute(f"SELECT COUNT(*) AS n FROM {tbl}").fetchone()
                total += int(row["n"]) if row else 0
            except sqlite3.OperationalError as e:
                logger.debug("FTS rebuild %s failed: %s", tbl, e)
        # Clear stale flag
        try:
            self.conn.execute(
                "DELETE FROM state_meta WHERE key = 'fts_stale'",
            )
            self.conn.commit()
        except Exception:
            pass
        return total
