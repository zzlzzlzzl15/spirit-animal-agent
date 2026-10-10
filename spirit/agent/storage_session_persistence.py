"""spirit/agent/storage_session_persistence.py — turn-level persistence facade.

v0.4.0 (Hermes-aligned): one entry-point for "save this turn's transcript".
Wraps the raw SessionDB calls so conversation_loop doesn't have to know
about WAL retries, JSON serialization, or write batching.

职责分工（Hermes-aligned v0.4.0）：
  - spirit/storage/session_persistence.py
      SessionPersistenceMixin（被 SessionDB MRO 拼入）
      负责：_DB_PERSISTED_MARKER 内禀去重、collect_unflushed、persist_session
      → 单一职责：marker 机制 + 批处理 entry。
  - spirit/agent/storage_session_persistence.py（本文件）
      StorageSessionPersistence facade（被 StorageTurnFacade 组合）
      负责：turn-level lifecycle（open/close_session）、单条 write retry
            with exponential backoff、ephemeral-scaffolding filter
      → 单一职责：lifecycle + retry policy + policy-level 过滤。
  - spirit/agent/storage_turn_facade.py
      StorageTurnFacade high-level facade（被 conversation_loop 调用）
      负责：sanitize + persist + scrub + title 四件套一站式

参考：hermes agent/session_persistence.py。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, List, Optional

from ..storage.session_db import SessionDB
from ..storage.errors import classify_persistence_error

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Ephemeral scaffolding flags (Hermes-aligned §13.2)
# ────────────────────────────────────────────────────────────────────
# Recovery / verification scaffolding the loop pops before appending the
# real response. Persistence must skip them or a resumed session replays
# synthetic turns / breaks prefix-cache reuse.
_EPHEMERAL_SCAFFOLDING_FLAGS = (
    "_empty_recovery_synthetic",
    "_empty_terminal_sentinel",
    "_thinking_prefill",
    "_verification_stop_synthetic",
    "_pre_verify_synthetic",
    "_kanban_stop_synthetic",
    "_dropped_toolcall_nudge",
)


def _is_ephemeral_scaffolding(msg: Any) -> bool:
    """True when ``msg`` is internal recovery scaffolding that must never
    reach the durable transcript.

    Mirrors Hermes agent/session_persistence.py::_is_ephemeral_scaffolding.
    """
    return isinstance(msg, dict) and any(msg.get(flag) for flag in _EPHEMERAL_SCAFFOLDING_FLAGS)


def _filter_ephemeral(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop ephemeral scaffolding messages; preserve original list shape
    for callers that rely on dict identity."""
    return [m for m in messages if not _is_ephemeral_scaffolding(m)]


class StorageSessionPersistence:
    """Thin facade between conversation_loop and SessionDB.

    Boundary contract (v0.4.0):
      - facade adds retry + lifecycle; never reimplements marker logic
      - all batch writes go through ``self.db.persist_session`` so
        ``_DB_PERSISTED_MARKER`` is stamped atomically with the commit
      - ephemeral-scaffolding filtering happens BEFORE marker stamping
        (skipped messages never enter the durable transcript)
    """

    def __init__(self, db: SessionDB, owner_kind: str = "agent", owner_id: str = "main"):
        self.db = db
        self.owner_kind = owner_kind
        self.owner_id = owner_id

    def open_session(
        self,
        session_id: str,
        source: str = "cli",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        existing = self.db.get_session(session_id)
        if existing is None:
            self.db.create_session(
                session_id=session_id,
                source=source,
                metadata=metadata or {},
            )
        self.db.acquire_holder(session_id, self.owner_kind, self.owner_id, force=True)
        return self.db.get_session(session_id) or {}

    def close_session(self, session_id: str) -> None:
        self.db.release_holder(session_id, self.owner_kind, self.owner_id)

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        **kwargs: Any,
    ) -> int:
        for attempt in range(3):
            try:
                return self.db.save_message(
                    session_id=session_id,
                    role=role,
                    content=content,
                    **kwargs,
                )
            except Exception as exc:
                cause = classify_persistence_error(exc)
                if cause in ("lock", "disk_full", "unknown") and attempt < 2:
                    logger.warning(
                        "save_message attempt %d failed (%s): %s; retrying",
                        attempt + 1, cause, exc,
                    )
                    time.sleep(0.05 * (2 ** attempt))
                    continue
                raise

    def append_messages(
        self,
        session_id: str,
        messages: Iterable[Dict[str, Any]],
    ) -> List[int]:
        """Batch write with ephemeral-scaffolding filter + marker stamp.

        Boundary: this method MUST go through ``self.db.persist_session``
        (not ``self.db.save_messages`` directly) so that
        ``_DB_PERSISTED_MARKER`` is stamped atomically with the commit
        and Hermes-style intrinsic dedup works on subsequent calls.
        """
        msgs = list(messages)
        durable = _filter_ephemeral(msgs)
        if len(durable) < len(msgs):
            logger.debug(
                "append_messages: filtered %d ephemeral scaffolding msgs (session=%s)",
                len(msgs) - len(durable), session_id[:8] if session_id else "?",
            )
        if not durable:
            return []
        ok, count = self.db.persist_session(session_id, durable, stamp_after=True)
        if not ok:
            raise RuntimeError(
                f"persist_session failed for session={session_id!r} msgs={len(durable)}"
            )
        # Return the row ids of the persisted rows. Spirit's
        # persist_session returns (success, count); per-row ids aren't
        # surfaced here — callers that need ids must re-query.
        return list(range(count))

    def session_summary(self, session_id: str) -> Dict[str, Any]:
        s = self.db.get_session(session_id) or {}
        t = self.db.timeline_summary(session_id)
        return {**s, **t}

    def last_n_messages(self, session_id: str, n: int) -> List[Dict[str, Any]]:
        return self.db.get_last_n_messages(session_id, n)
