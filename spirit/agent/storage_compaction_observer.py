"""spirit/agent/storage_compaction_observer.py — observer hooks for compaction.

v0.4.0 (Hermes-aligned): when the context_compressor marks messages as
compacted, this observer records the event so the UI can show it and
search can exclude compacted bodies.

Reference: hermes agent/compaction_observer.py.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..storage.session_db import SessionDB

logger = logging.getLogger(__name__)


class StorageCompactionObserver:
    """Bridge between context_compressor and the storage layer."""

    def __init__(self, db: SessionDB):
        self.db = db

    def observe_compaction(
        self,
        session_id: str,
        message_ids: List[int],
        compressed_summary: str,
        strategy: str = "auto",
    ) -> Dict[str, Any]:
        """Mark the given messages as compacted and persist a summary."""
        if not message_ids:
            return {"updated": 0}
        try:
            self.db.mark_messages_compacted(
                session_id=session_id,
                message_ids=message_ids,
                compressed_summary=compressed_summary,
                strategy=strategy,
            )
            return {"updated": len(message_ids), "strategy": strategy}
        except Exception:
            logger.exception(
                "observe_compaction failed for session=%s ids=%s",
                session_id, message_ids,
            )
            return {"updated": 0, "error": "persist_failed"}

    def list_compacted(self, session_id: str) -> List[Dict[str, Any]]:
        return self.db.get_compacted_messages(session_id)

    def last_compression(self, session_id: str) -> Optional[Dict[str, Any]]:
        msgs = self.list_compacted(session_id)
        if not msgs:
            return None
        msgs.sort(key=lambda m: m.get("timestamp", 0), reverse=True)
        return msgs[0]
