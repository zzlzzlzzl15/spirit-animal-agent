"""spirit/storage/rewind.py — carrier-aware user-turn rewind.

v0.4.0 (Hermes-aligned): /undo, /retry implementation shared by CLI / TUI / Electron.

Reference: hermes_state_rewind.py.

Rewind is a persisted-history mutation: it soft-deletes messages back
to the target user turn, so /retry can re-execute from there with the
session lineage intact.
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class RewindTargetUnavailableError(ValueError):
    """Raised when the requested rewind target doesn't exist."""


class RewindOutcome(Enum):
    """Result of a rewind operation."""

    SUCCESS = "success"
    TARGET_NOT_FOUND = "target_not_found"
    NOOP_ALREADY_AT_TARGET = "noop_already_at_target"
    FAILED = "failed"


class SessionRewindMixin:
    """User-turn rewind operations. Mixed into SessionDB via MRO.

    Expects:
        self.conn (sqlite3.Connection)
        self.db_path (Path)
    """

    def _user_indices(self, session_id: str) -> List[Dict[str, Any]]:
        """Return all user-role message rows for a session, ordered by id."""
        rows = self.conn.execute(
            """SELECT id, content, timestamp, tool_call_id, tool_calls
               FROM messages
               WHERE session_id = ? AND role = 'user' AND active = 1
               ORDER BY id""",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _comparison_content(self, msg: Dict[str, Any]) -> str:
        """Return a comparison key for a user message (whitespace-normalized)."""
        content = msg.get("content") or ""
        if isinstance(content, list):
            # multimodal — flatten to text only
            content = "".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        return " ".join(str(content).split()).strip()

    def rewind_to_user_turn(
        self,
        session_id: str,
        target_turn: int | str,
    ) -> RewindOutcome:
        """Soft-delete all messages after the target user turn.

        Args:
            session_id: which session to rewind.
            target_turn: 0-based index of the user turn to keep, OR the
                user message content snippet to match (carried-aware
                resolution).

        Returns:
            RewindOutcome indicating the result.
        """
        users = self._user_indices(session_id)
        if not users:
            return RewindOutcome.TARGET_NOT_FOUND

        # Resolve target
        if isinstance(target_turn, int):
            if target_turn < 0 or target_turn >= len(users):
                return RewindOutcome.TARGET_NOT_FOUND
            target_row = users[target_turn]
        else:
            needle = self._comparison_content({"content": target_turn})
            matches = [u for u in users if self._comparison_content(u) == needle]
            if not matches:
                return RewindOutcome.TARGET_NOT_FOUND
            target_row = matches[-1]

        target_id = int(target_row["id"])

        # Soft-delete everything after the target
        try:
            self.conn.execute(
                """UPDATE messages
                   SET active = 0, compacted = 1
                   WHERE session_id = ? AND id > ?""",
                (session_id, target_id),
            )
            # Update session counters (recompute or decrement)
            self.conn.execute(
                """UPDATE sessions
                   SET message_count = (
                       SELECT COUNT(*) FROM messages
                       WHERE session_id = ? AND active = 1
                   )
                   WHERE id = ?""",
                (session_id, session_id),
            )
            self.conn.commit()
            logger.info("rewound session %s to user turn id=%d", session_id, target_id)
            return RewindOutcome.SUCCESS
        except Exception as e:  # noqa: BLE001
            logger.warning("rewind failed: %s", e)
            self.conn.rollback()
            return RewindOutcome.FAILED

    def rewind_to_user_content(
        self,
        session_id: str,
        content_snippet: str,
    ) -> RewindOutcome:
        """Convenience: rewind to the most recent user turn whose content
        contains content_snippet (case-insensitive substring match)."""
        users = self._user_indices(session_id)
        needle = content_snippet.strip().lower()
        if not needle:
            return RewindOutcome.TARGET_NOT_FOUND
        matches = [
            u for u in users
            if needle in (self._comparison_content(u) or "").lower()
        ]
        if not matches:
            return RewindOutcome.TARGET_NOT_FOUND
        target_row = matches[-1]
        target_id = int(target_row["id"])
        try:
            self.conn.execute(
                """UPDATE messages
                   SET active = 0, compacted = 1
                   WHERE session_id = ? AND id > ?""",
                (session_id, target_id),
            )
            self.conn.execute(
                """UPDATE sessions
                   SET message_count = (
                       SELECT COUNT(*) FROM messages
                       WHERE session_id = ? AND active = 1
                   )
                   WHERE id = ?""",
                (session_id, session_id),
            )
            self.conn.commit()
            return RewindOutcome.SUCCESS
        except Exception as e:  # noqa: BLE001
            logger.warning("rewind_to_user_content failed: %s", e)
            self.conn.rollback()
            return RewindOutcome.FAILED
