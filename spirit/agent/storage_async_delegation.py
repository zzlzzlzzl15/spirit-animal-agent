"""spirit/agent/storage_async_delegation.py — sub-agent task persistence.

v0.4.0 (Hermes-aligned): when the main agent delegates work to a
sub-agent (planning, RAG, code review), the delegation state lands in
async_delegations.

Reference: hermes agent/async_delegation.py.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..storage.session_db import SessionDB

logger = logging.getLogger(__name__)


class StorageAsyncDelegation:
    """Manage rows in async_delegations."""

    def __init__(self, db: SessionDB):
        self.db = db

    def start_delegation(
        self,
        session_id: str,
        parent_message_id: int,
        kind: str,
        prompt: str,
        agent_name: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        delegation_id = self.db.upsert_delegation(
            session_id=session_id,
            parent_message_id=parent_message_id,
            kind=kind,
            agent_name=agent_name,
            prompt=prompt,
            status="running",
            metadata=metadata or {},
        )
        return delegation_id

    def complete_delegation(
        self,
        delegation_id: int,
        result: Any,
        status: str = "completed",
    ) -> None:
        # update_delegation takes `result=...` (not `result_text=`) and
        # automatically stamps `finished_at` when status reaches a terminal state.
        self.db.update_delegation(
            delegation_id,
            status=status,
            result=str(result) if result is not None else "",
        )

    def fail_delegation(self, delegation_id: int, error: str) -> None:
        self.db.update_delegation(
            delegation_id,
            status="failed",
            error=error[:2000],
        )

    def list_for_session(
        self,
        session_id: str,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self.db.list_delegations(session_id, status=status)
