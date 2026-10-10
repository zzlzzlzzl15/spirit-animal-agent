"""spirit/agent/storage_session_lifecycle.py — handle session start/end events.

v0.4.0 (Hermes-aligned): wraps open/close/idle events, fires the
matching storage actions (acquire_holder / release_holder / heartbeat).

Reference: hermes agent/session_lifecycle.py.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from ..storage.session_db import SessionDB

logger = logging.getLogger(__name__)


DEFAULT_HEARTBEAT_INTERVAL_S = 10.0


class StorageSessionLifecycle:
    """Per-session lifecycle bookkeeping."""

    def __init__(
        self,
        db: SessionDB,
        owner_kind: str = "agent",
        owner_id: str = "main",
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    ):
        self.db = db
        self.owner_kind = owner_kind
        self.owner_id = owner_id
        self.heartbeat_interval = heartbeat_interval
        self._last_heartbeat: Dict[str, float] = {}
        self._lock = threading.Lock()

    def on_session_start(
        self,
        session_id: str,
        source: str = "cli",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if self.db.get_session(session_id) is None:
            self.db.create_session(
                session_id=session_id,
                source=source,
                metadata=metadata or {},
            )
        self.db.acquire_holder(session_id, self.owner_kind, self.owner_id, force=True)
        self.db.touch_session(session_id)
        with self._lock:
            self._last_heartbeat[session_id] = time.time()
        return {"session_id": session_id, "status": "started"}

    def on_session_idle(self, session_id: str) -> None:
        now = time.time()
        with self._lock:
            last = self._last_heartbeat.get(session_id, 0.0)
        if now - last < self.heartbeat_interval:
            return
        try:
            self.db.refresh_holder(session_id, self.owner_kind, self.owner_id)
            self.db.touch_session(session_id)
            with self._lock:
                self._last_heartbeat[session_id] = now
        except Exception:
            logger.exception("heartbeat refresh failed for %s", session_id)

    def on_session_end(self, session_id: str) -> None:
        try:
            self.db.release_holder(session_id, self.owner_kind, self.owner_id)
        except Exception:
            logger.exception("release_holder failed for %s", session_id)
        with self._lock:
            self._last_heartbeat.pop(session_id, None)
        try:
            self.db.mark_session_inactive(session_id)
        except Exception:
            logger.exception("mark_session_inactive failed for %s", session_id)
