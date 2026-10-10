"""spirit/storage/delegations.py — async sub-agent delegation persistence (Hermes-aligned).

v0.4.0: persists rows in the `async_delegations` table.

The async_delegations table is created by schema.py; this mixin adds the
CRUD API the storage_async_delegation middleware expects.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SessionDelegationsMixin:
    """Persistence for sub-agent delegations.

    Mixed into SessionDB via MRO.
    Expects:
        self.conn (sqlite3.Connection)
    """

    # ── Writes ───────────────────────────────────────────────

    def upsert_delegation(
        self,
        session_id: str,
        parent_message_id: Optional[int],
        kind: str,
        prompt: str,
        agent_name: str = "",
        status: str = "pending",
        metadata: Optional[Dict[str, Any]] = None,
        delegation_id: Optional[int] = None,
    ) -> int:
        """Insert or update a delegation row. Returns the row id.

        If delegation_id is provided, UPDATEs the matching row; otherwise
        INSERTs and returns the new id.
        """
        now = time.time()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        try:
            if delegation_id is not None:
                self.conn.execute(
                    "UPDATE async_delegations SET "
                    "  session_id = ?, parent_message_id = ?, kind = ?, "
                    "  prompt = ?, agent_name = ?, status = ?, metadata = ?, "
                    "  updated_at = ? "
                    "WHERE id = ?",
                    (session_id, parent_message_id, kind, prompt,
                     agent_name, status, meta_json, now, delegation_id),
                )
                self.conn.commit()
                return delegation_id
            cur = self.conn.execute(
                "INSERT INTO async_delegations ("
                "  session_id, parent_message_id, kind, prompt, agent_name, "
                "  status, started_at, updated_at, metadata"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, parent_message_id, kind, prompt, agent_name,
                 status, now, now, meta_json),
            )
            self.conn.commit()
            return cur.lastrowid
        except Exception as e:  # noqa: BLE001
            logger.debug("upsert_delegation 失败: %s", e)
            return -1

    def update_delegation(
        self,
        delegation_id: int,
        status: Optional[str] = None,
        result: Optional[str] = None,
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Partial update of a delegation row.

        status transitions: pending → running → completed / failed / cancelled
        """
        if delegation_id is None:
            return False
        sets: List[str] = []
        values: List[Any] = []
        if status is not None:
            sets.append("status = ?")
            values.append(status)
        if result is not None:
            sets.append("result = ?")
            values.append(result)
        if error is not None:
            sets.append("error = ?")
            values.append(error)
        if metadata is not None:
            sets.append("metadata = ?")
            values.append(json.dumps(metadata, ensure_ascii=False))
        sets.append("updated_at = ?")
        values.append(time.time())
        if status in ("completed", "failed", "cancelled"):
            sets.append("finished_at = ?")
            values.append(time.time())
        values.append(delegation_id)
        try:
            cur = self.conn.execute(
                f"UPDATE async_delegations SET {', '.join(sets)} WHERE id = ?",
                values,
            )
            self.conn.commit()
            return cur.rowcount > 0
        except Exception as e:  # noqa: BLE001
            logger.debug("update_delegation 失败: %s", e)
            return False

    # ── Reads ────────────────────────────────────────────────

    def list_delegations(
        self,
        session_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict]:
        """List delegations, newest first. Optional filters by session_id / status."""
        clauses: List[str] = []
        values: List[Any] = []
        if session_id is not None:
            clauses.append("session_id = ?")
            values.append(session_id)
        if status is not None:
            clauses.append("status = ?")
            values.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        values.append(limit)
        try:
            rows = self.conn.execute(
                f"SELECT * FROM async_delegations {where} "
                f"ORDER BY id DESC LIMIT ?",
                values,
            ).fetchall()
        except Exception as e:  # noqa: BLE001
            logger.debug("list_delegations 失败: %s", e)
            return []
        return [dict(r) for r in rows]

    def get_delegation(self, delegation_id: int) -> Optional[Dict]:
        try:
            row = self.conn.execute(
                "SELECT * FROM async_delegations WHERE id = ?",
                (delegation_id,),
            ).fetchone()
        except Exception as e:  # noqa: BLE001
            logger.debug("get_delegation 失败: %s", e)
            return None
        return dict(row) if row else None