"""spirit/storage/session_persistence.py — batch flush + persist_session mixin.

v0.3.0 (Hermes-style): extracted from session_db.py.

This is the Spirit equivalent of `agent/session_persistence.py` in Hermes.
It provides:

1. _DB_PERSISTED_MARKER constant — mark dicts as already-flushed to prevent
   duplicate writes when callers re-process the same message buffer.

2. _db_flush_collect(messages) — partition a list of dicts into
   (to_flush, already_flushed) using the marker.

3. _db_flush_write(session_id, messages) — single-transaction batch INSERT.
   Returns True on success, False on failure (caller decides retry).

4. _db_flush_failed(exc) — classify error, log warning, return False.

Spirit deliberately does NOT yet have:
- divert_to_jsonl (v0.4.0)
- CompressionSessionClosedError handling (v0.4.0)
- Multi-process lock holder validation (v0.4.0)

For now, persistence failures are logged at debug level only and the
main loop continues. This is intentional: Spirit's tests catch any
real bugs in add_message path; the "divert" path is for production
resilience that Spirit doesn't yet have.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from .errors import classify_persistence_error
from .messages import SessionMessagesMixin

logger = logging.getLogger(__name__)


# Mark dict as already-persisted to prevent double-flush.
# Hermes uses the same trick. We use a string key (rather than id())
# to be safe against CPython's address reuse and to make the marker
# visible in dict dumps.
_DB_PERSISTED_MARKER = "__spirit_persisted__"


def mark_persisted(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Stamp a message dict as already-flushed. Mutates and returns msg."""
    msg[_DB_PERSISTED_MARKER] = True
    return msg


def is_persisted(msg: Dict[str, Any]) -> bool:
    """Check if a message dict was already flushed."""
    return bool(msg.get(_DB_PERSISTED_MARKER))


def _collect_unflushed(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Partition messages into (to_flush, already_flushed)."""
    to_flush: List[Dict[str, Any]] = []
    already: List[Dict[str, Any]] = []
    for msg in messages:
        if is_persisted(msg):
            already.append(msg)
        else:
            to_flush.append(msg)
    return to_flush


class SessionPersistenceMixin:
    """Batch flush helpers. Mixed into SessionDB via MRO.

    Expects:
        self.conn (sqlite3.Connection)
        self.db_path (Path)
        # From SessionMessagesMixin:
        save_messages(session_id, messages) -> int
    """

    def collect_unflushed(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Public wrapper around _collect_unflushed for agent callers."""
        return _collect_unflushed(messages)

    def persist_session(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        *,
        stamp_after: bool = True,
    ) -> Tuple[bool, int]:
        """Batch-persist a list of messages to the db.

        Hermes-style: single-transaction INSERT, mark dicts as
        persisted after success. Returns (success, count).

        Args:
            session_id: target session
            messages: list of message dicts (mutated if stamp_after=True)
            stamp_after: if True, mutate dicts to mark them persisted

        Returns:
            (success: bool, count: int) — count is rows inserted on
            success, or 0 on failure.
        """
        to_flush = _collect_unflushed(messages)
        if not to_flush:
            return True, 0
        try:
            count = self.save_messages(session_id, to_flush)
            if count < 0:
                logger.debug("persist_session save_messages 返回 -1")
                return False, 0
            if stamp_after:
                for msg in to_flush:
                    mark_persisted(msg)
            return True, count
        except Exception as e:  # noqa: BLE001
            kind = classify_persistence_error(e)
            logger.debug(
                "persist_session 失败 (%s) session=%s msgs=%d: %s",
                kind, session_id[:8] if session_id else "?", len(to_flush), e,
            )
            return False, 0
