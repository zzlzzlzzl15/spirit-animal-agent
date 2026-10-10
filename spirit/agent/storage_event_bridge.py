"""spirit/agent/storage_event_bridge.py — bridge event bus ↔ storage.

v0.4.0 (Hermes-aligned): subscribe to progress_notifier events and turn
them into storage actions.

Mapping:
- TASK_START    → lifecycle.on_session_start (acquire holder + touch)
- TASK_COMPLETE → lifecycle.on_session_idle (heartbeat)
- TASK_ABORTED  → lifecycle.on_session_end (release holder + mark inactive)
- "compaction_done" (custom) → observe_compaction (mark messages compacted)

Reference: hermes agent/event_bridge.py.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..progress_notifier import (
    ProgressPhase,
    register_progress_sink,
    unregister_progress_sink,
)
from .storage_compaction_observer import StorageCompactionObserver
from .storage_session_lifecycle import StorageSessionLifecycle
from .storage_turn_facade import StorageTurnFacade
from ..storage.session_db import SessionDB

logger = logging.getLogger(__name__)


# Custom event name for compaction events (consumers post via notify_progress
# with extra={"event_kind": "compaction_done"} or call this module directly).
COMPACTION_EVENT_KIND = "compaction_done"


class StorageEventBridge:
    """Subscribe to ProgressNotifier and persist relevant events."""

    def __init__(
        self,
        db: SessionDB,
        turn_facade: Optional[StorageTurnFacade] = None,
        lifecycle: Optional[StorageSessionLifecycle] = None,
        compaction_observer: Optional[StorageCompactionObserver] = None,
    ):
        self.db = db
        self.turn_facade = turn_facade or StorageTurnFacade(db)
        self.lifecycle = lifecycle or StorageSessionLifecycle(db)
        self.compaction_observer = compaction_observer or StorageCompactionObserver(db)
        self._attached = False
        self._sink = None  # the registered callback

    def attach(self) -> None:
        """Register the dispatch sink with progress_notifier."""
        if self._attached:
            return
        self._sink = self._on_progress_event
        register_progress_sink(self._sink)
        self._attached = True

    def detach(self) -> None:
        if not self._attached:
            return
        try:
            unregister_progress_sink(self._sink)
        except Exception:
            logger.exception("detach failed")
        self._attached = False
        self._sink = None

    # ── dispatch ─────────────────────────────────────────────

    def _on_progress_event(self, event: Dict[str, Any]) -> None:
        try:
            phase = event.get("phase")
            if phase == ProgressPhase.TASK_START.value or phase == "task_start":
                self._handle_session_start(event)
            elif phase == ProgressPhase.TASK_COMPLETE.value or phase == "task_complete":
                self._handle_session_idle(event)
            elif phase == ProgressPhase.TASK_ABORTED.value or phase == "task_aborted":
                self._handle_session_end(event)
            elif phase == COMPACTION_EVENT_KIND:
                self._handle_compaction(event)
        except Exception:
            logger.exception("storage event bridge dispatch failed")

    def _handle_session_start(self, event: Dict[str, Any]) -> None:
        sid = event.get("session_id")
        if not sid:
            return
        try:
            self.lifecycle.on_session_start(sid)
        except Exception:
            logger.exception("session_start dispatch failed")

    def _handle_session_idle(self, event: Dict[str, Any]) -> None:
        sid = event.get("session_id")
        if not sid:
            return
        try:
            self.lifecycle.on_session_idle(sid)
        except Exception:
            logger.exception("session_idle dispatch failed")

    def _handle_session_end(self, event: Dict[str, Any]) -> None:
        sid = event.get("session_id")
        if not sid:
            return
        try:
            self.lifecycle.on_session_end(sid)
        except Exception:
            logger.exception("session_end dispatch failed")

    def _handle_compaction(self, event: Dict[str, Any]) -> None:
        sid = event.get("session_id")
        msg_ids = event.get("message_ids") or []
        summary = event.get("summary") or ""
        strategy = event.get("strategy") or "auto"
        if not sid or not msg_ids:
            return
        try:
            self.compaction_observer.observe_compaction(
                session_id=sid,
                message_ids=list(msg_ids),
                compressed_summary=summary,
                strategy=strategy,
            )
        except Exception:
            logger.exception("compaction dispatch failed")

    # ── convenience: emit compaction event ───────────────────

    def emit_compaction(
        self,
        session_id: str,
        message_ids: list,
        summary: str,
        strategy: str = "auto",
    ) -> None:
        """Direct entry point for context_compressor to mark messages compacted."""
        self._handle_compaction(
            {
                "session_id": session_id,
                "message_ids": message_ids,
                "summary": summary,
                "strategy": strategy,
                "phase": COMPACTION_EVENT_KIND,
            }
        )
