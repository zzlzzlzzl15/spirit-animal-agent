"""spirit/agent/storage_turn_facade.py — single entry point for "save this turn".

v0.4.0 (Hermes-aligned): composes the persistence facade, sanitizer,
think-scrubber, and title generator behind one .save_turn() call.

Reference: hermes agent/turn_facade.py.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from .storage_message_sanitization import StorageMessageSanitization
from .storage_session_persistence import StorageSessionPersistence
from .storage_think_scrubber import StorageThinkScrubber
from .storage_title_generator import StorageTitleGenerator
from ..storage.session_db import SessionDB

logger = logging.getLogger(__name__)


LLMCaller = Callable[[str, float, int], str]


class StorageTurnFacade:
    """All-in-one turn persistence: sanitize, write, scrub, title."""

    def __init__(
        self,
        db: SessionDB,
        owner_kind: str = "agent",
        owner_id: str = "main",
        llm_caller: Optional[LLMCaller] = None,
    ):
        self.db = db
        self.persistence = StorageSessionPersistence(db, owner_kind=owner_kind, owner_id=owner_id)
        self.sanitizer = StorageMessageSanitization(db)
        self.scrubber = StorageThinkScrubber(db)
        self.titler = StorageTitleGenerator(db, llm_caller=llm_caller)

    def save_turn(
        self,
        session_id: str,
        user_message: Dict[str, Any],
        assistant_message: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Persist one user→assistant turn."""
        self.persistence.open_session(session_id)
        clean_user = self.sanitizer.sanitize(user_message)
        clean_assistant = self.sanitizer.sanitize(assistant_message)
        if clean_user is None:
            raise ValueError("user message sanitized to None")
        user_id = self.persistence.append_message(session_id, **clean_user)
        assistant_id = None
        if clean_assistant is not None:
            assistant_id = self.persistence.append_message(session_id, **clean_assistant)
            if clean_assistant.get("role") == "assistant":
                self.scrubber.scrub_assistant_message(
                    session_id,
                    assistant_id,
                    clean_assistant.get("content", ""),
                )
        self.titler.maybe_generate_title(session_id)
        return {
            "session_id": session_id,
            "user_message_id": user_id,
            "assistant_message_id": assistant_id,
            "saved_at": int(time.time()),
        }

    # ── v0.4.0 conversation_loop hooks ──────────────────────

    def persist_turn_start(self, session_id: str, user_message: str = "") -> None:
        """Notify the facade a new turn has begun.

        Touches the session row + opens the holder (best-effort).
        Used by conversation_loop.run_conversation().
        """
        try:
            self.persistence.open_session(session_id)
        except Exception:  # noqa: BLE001
            logger.debug("persist_turn_start.open_session 失败", exc_info=True)

    def persist_turn_end(
        self,
        session_id: str,
        end_reason: str = "task_done",
    ) -> None:
        """Notify the facade a turn has ended (best-effort).

        Used by conversation_loop.run_conversation() on every return path.
        """
        try:
            # Mark inactive so the lockowners / holders table reflects
            # the session is idle. close_session remains idempotent.
            if hasattr(self.db, "mark_session_inactive"):
                self.db.mark_session_inactive(session_id, end_reason=end_reason)
        except Exception:  # noqa: BLE001
            logger.debug("persist_turn_end 失败", exc_info=True)
