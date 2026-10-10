"""spirit/storage/compression.py — compression lineage, locks, turn leases.

v0.4.0 (Hermes-aligned): per-session compression lock + per-conversation
turn lease, with TTL-based auto-reclaim.

Reference: hermes_state_compression.py.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Default TTL: 60s for compression lock, 30s for turn lease.
DEFAULT_COMPRESSION_LOCK_TTL = 60.0
DEFAULT_TURN_LEASE_TTL = 30.0


class SessionCompressionMixin:
    """Compression lineage + cooldown/streak counters + locks + turn leases.

    Mixed into SessionDB via MRO.
    Expects:
        self.conn (sqlite3.Connection)
        self.db_path (Path)
    """

    # ── Compression lock ──────────────────────────────────────

    def claim_compression_lock(
        self,
        session_id: str,
        holder: str,
        *,
        ttl: float = DEFAULT_COMPRESSION_LOCK_TTL,
    ) -> bool:
        """Try to claim a compression lock for session_id.

        Returns True if claimed, False if another holder has it.
        Stale locks (TTL expired or holder dead) are reclaimed.
        """
        now = time.time()
        expires = now + ttl
        # Try INSERT OR REPLACE only if no live holder
        try:
            existing = self.conn.execute(
                "SELECT holder, acquired_at, expires_at FROM compression_locks WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if existing is not None:
                if existing["expires_at"] > now and existing["holder"] != holder:
                    # Live other holder
                    return False
                # Stale or self — claim
            self.conn.execute(
                """INSERT INTO compression_locks (session_id, holder, acquired_at, expires_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     holder = excluded.holder,
                     acquired_at = excluded.acquired_at,
                     expires_at = excluded.expires_at""",
                (session_id, holder, now, expires),
            )
            self.conn.commit()
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("claim_compression_lock failed: %s", e)
            return False

    def release_compression_lock(self, session_id: str) -> None:
        """Release the compression lock for session_id (no-op if not held)."""
        try:
            self.conn.execute(
                "DELETE FROM compression_locks WHERE session_id = ?",
                (session_id,),
            )
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("release_compression_lock: %s", e)

    def refresh_compression_lock(
        self,
        session_id: str,
        holder: str,
        *,
        ttl: float = DEFAULT_COMPRESSION_LOCK_TTL,
    ) -> bool:
        """Extend the lock TTL (only if still held by us)."""
        try:
            now = time.time()
            cur = self.conn.execute(
                "SELECT holder FROM compression_locks WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if cur is None or cur["holder"] != holder:
                return False
            self.conn.execute(
                "UPDATE compression_locks SET expires_at = ? WHERE session_id = ?",
                (now + ttl, session_id),
            )
            self.conn.commit()
            return True
        except Exception as e:  # noqa: BLE001
            logger.debug("refresh_compression_lock: %s", e)
            return False

    def is_compression_in_progress(self, session_id: str) -> bool:
        """True if a live (non-expired) compression lock exists."""
        try:
            row = self.conn.execute(
                "SELECT expires_at FROM compression_locks WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return False
            return row["expires_at"] > time.time()
        except Exception:  # noqa: BLE001
            return False

    # ── Turn lease ────────────────────────────────────────────

    def claim_turn_lease(
        self,
        conversation_id: str,
        holder: str,
        *,
        ttl: float = DEFAULT_TURN_LEASE_TTL,
    ) -> bool:
        """Try to claim a turn lease for conversation_id."""
        now = time.time()
        expires = now + ttl
        try:
            existing = self.conn.execute(
                "SELECT holder, expires_at FROM session_turn_leases WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if existing is not None:
                if existing["expires_at"] > now and existing["holder"] != holder:
                    return False
            self.conn.execute(
                """INSERT INTO session_turn_leases (conversation_id, holder, acquired_at, expires_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(conversation_id) DO UPDATE SET
                     holder = excluded.holder,
                     acquired_at = excluded.acquired_at,
                     expires_at = excluded.expires_at""",
                (conversation_id, holder, now, expires),
            )
            self.conn.commit()
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("claim_turn_lease failed: %s", e)
            return False

    def release_turn_lease(self, conversation_id: str) -> None:
        try:
            self.conn.execute(
                "DELETE FROM session_turn_leases WHERE conversation_id = ?",
                (conversation_id,),
            )
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("release_turn_lease: %s", e)

    # ── Cooldown / streak ─────────────────────────────────────

    def cooldown_session(self, session_id: str, duration_s: float) -> None:
        """Set a compression cooldown on a session.

        Stored in sessions.compacted (1) + last_compressed_at (timestamp).
        """
        try:
            self.conn.execute(
                """UPDATE sessions
                   SET compacted = 1, last_compressed_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (session_id,),
            )
            # Stash duration in state_meta for later checks
            self.conn.execute(
                """INSERT INTO state_meta (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                updated_at = CURRENT_TIMESTAMP""",
                (f"cooldown:{session_id}", str(time.time() + duration_s)),
            )
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("cooldown_session: %s", e)

    def is_in_cooldown(self, session_id: str) -> bool:
        """True if the session is in a compression cooldown window."""
        try:
            row = self.conn.execute(
                "SELECT value FROM state_meta WHERE key = ?",
                (f"cooldown:{session_id}",),
            ).fetchone()
            if row is None:
                return False
            return float(row["value"]) > time.time()
        except Exception:  # noqa: BLE001
            return False

    # ── Compressor attempt tracking ───────────────────────────

    def mark_compressor_attempt(self, session_id: str, attempt_id: str) -> None:
        """Record that a compressor attempt with this attempt_id is in flight."""
        try:
            self.conn.execute(
                """INSERT INTO state_meta (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                updated_at = CURRENT_TIMESTAMP""",
                (f"compressor_attempt:{session_id}", attempt_id),
            )
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("mark_compressor_attempt: %s", e)

    def compressor_attempt_is_current(
        self, session_id: str, attempt_id: str
    ) -> bool:
        """True if attempt_id matches the currently-marked attempt."""
        try:
            row = self.conn.execute(
                "SELECT value FROM state_meta WHERE key = ?",
                (f"compressor_attempt:{session_id}",),
            ).fetchone()
            if row is None:
                return True  # no marker → first attempt is current
            return row["value"] == attempt_id
        except Exception:  # noqa: BLE001
            return True

    def clear_compressor_attempt(self, session_id: str) -> None:
        try:
            self.conn.execute(
                "DELETE FROM state_meta WHERE key = ?",
                (f"compressor_attempt:{session_id}",),
            )
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("clear_compressor_attempt: %s", e)

    # ── Compacted-message bookkeeping (storage_compaction_observer) ──

    def mark_messages_compacted(
        self,
        session_id: str,
        message_ids: List[int],
        compressed_summary: str = "",
        strategy: str = "auto",
    ) -> int:
        """Mark a range of messages as compacted and record the summary.

        Returns the number of rows actually updated.
        """
        if not message_ids:
            return 0
        try:
            placeholders = ",".join("?" * len(message_ids))
            cur = self.conn.execute(
                f"UPDATE messages SET compacted = 1, _compressed_summary = 1, "
                f"compressed_summary = ? "
                f"WHERE session_id = ? AND id IN ({placeholders})",
                [compressed_summary, session_id, *message_ids],
            )
            self.conn.commit()
            count = cur.rowcount
            # Record the compaction event in state_meta for observability.
            try:
                self.conn.execute(
                    "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (f"compaction_event:{session_id}",
                     f"{count}|{strategy}|{len(compressed_summary)}"),
                )
                self.conn.commit()
            except Exception:
                pass
            return count
        except Exception as e:  # noqa: BLE001
            logger.debug("mark_messages_compacted 失败: %s", e)
            return 0

    def get_compacted_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Return messages with compacted=1, newest first by id."""
        rows = self.conn.execute(
            "SELECT * FROM messages "
            "WHERE session_id = ? AND compacted = 1 "
            "ORDER BY id DESC",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]
