"""spirit/storage/holders.py — owner / holder attribution for sessions.

v0.4.0 (Hermes-aligned): who created / last touched a session; which
process / thread / agent is currently "holding" a session for writing.

Reference: hermes_state_holders.py.

Used by:
- The CLI / TUI / Electron to show "session is in use elsewhere".
- The async delegation router to enforce one-writer-per-session.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


DEFAULT_HOLDER_TTL_S = 30.0


@dataclass
class Holder:
    """A writer claim on a session."""

    session_id: str
    owner_kind: str  # 'cli' / 'tui' / 'electron' / 'agent' / 'gateway'
    owner_id: str
    hostname: str
    pid: int
    acquired_at: int
    last_heartbeat: int
    metadata: Dict[str, Any]

    def is_alive(self, ttl_s: float = DEFAULT_HOLDER_TTL_S, now: Optional[int] = None) -> bool:
        if now is None:
            now = int(time.time())
        return (now - self.last_heartbeat) <= ttl_s


class SessionHoldersMixin:
    """Owner attribution + write-lease enforcement.

    Mixed into SessionDB via MRO.
    Expects:
        self.conn (sqlite3.Connection)
    """

    def acquire_holder(
        self,
        session_id: str,
        owner_kind: str,
        owner_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> Optional[Holder]:
        """Try to acquire the writer-lease for `session_id`.

        Returns the Holder on success, None if another live holder exists.
        """
        now = int(time.time())
        existing = self.list_holders(session_id, alive_only=True, now=now)
        if existing and not force:
            return None
        hostname = self._hostname()
        pid = os.getpid()
        meta = metadata or {}
        backend_id = f"{session_id}|{owner_kind}|{owner_id}"
        try:
            self.conn.execute(
                "INSERT INTO gateway_heartbeats (backend_id, pid, started_at, "
                "                              last_heartbeat, profile, host, "
                "                              session_id, owner_kind, owner_id, "
                "                              hostname, acquired_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(backend_id) DO UPDATE SET "
                "  last_heartbeat = excluded.last_heartbeat, "
                "  acquired_at = excluded.acquired_at, "
                "  metadata = excluded.metadata",
                (backend_id, pid, now, now, "", hostname,
                 session_id, owner_kind, owner_id, hostname, now, _json(meta)),
            )
            self.conn.commit()
        except Exception:
            logger.exception("acquire_holder failed")
            return None
        return Holder(
            session_id=session_id,
            owner_kind=owner_kind,
            owner_id=owner_id,
            hostname=hostname,
            pid=pid,
            acquired_at=now,
            last_heartbeat=now,
            metadata=meta,
        )

    def refresh_holder(
        self,
        session_id: str,
        owner_kind: str,
        owner_id: str,
    ) -> bool:
        now = int(time.time())
        cur = self.conn.execute(
            "UPDATE gateway_heartbeats SET last_heartbeat = ? "
            "WHERE session_id = ? AND owner_kind = ? AND owner_id = ?",
            (now, session_id, owner_kind, owner_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def release_holder(
        self,
        session_id: str,
        owner_kind: str,
        owner_id: str,
    ) -> bool:
        cur = self.conn.execute(
            "DELETE FROM gateway_heartbeats "
            "WHERE session_id = ? AND owner_kind = ? AND owner_id = ?",
            (session_id, owner_kind, owner_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def list_holders(
        self,
        session_id: str,
        alive_only: bool = True,
        now: Optional[int] = None,
        ttl_s: float = DEFAULT_HOLDER_TTL_S,
    ) -> List[Holder]:
        rows = self.conn.execute(
            "SELECT session_id, owner_kind, owner_id, hostname, pid, "
            "       acquired_at, last_heartbeat, metadata "
            "FROM gateway_heartbeats WHERE session_id = ? "
            "ORDER BY last_heartbeat DESC",
            (session_id,),
        ).fetchall()
        out: List[Holder] = []
        if now is None:
            now = int(time.time())
        for r in rows:
            try:
                meta = _unjson(r["metadata"]) if r["metadata"] else {}
            except Exception:
                meta = {}
            h = Holder(
                session_id=r["session_id"],
                owner_kind=r["owner_kind"],
                owner_id=r["owner_id"],
                hostname=r["hostname"],
                pid=r["pid"],
                acquired_at=int(r["acquired_at"] or 0),
                last_heartbeat=int(r["last_heartbeat"] or 0),
                metadata=meta,
            )
            if alive_only and not h.is_alive(ttl_s=ttl_s, now=now):
                continue
            out.append(h)
        return out

    def reclaim_stale_holders(self, ttl_s: float = DEFAULT_HOLDER_TTL_S) -> int:
        """Delete holders whose last_heartbeat is older than ttl_s."""
        cutoff = int(time.time()) - ttl_s
        cur = self.conn.execute(
            "DELETE FROM gateway_heartbeats WHERE last_heartbeat < ?",
            (cutoff,),
        )
        self.conn.commit()
        return cur.rowcount

    def _hostname(self) -> str:
        try:
            import socket
            return socket.gethostname()
        except Exception:
            return "unknown"


def _json(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return "{}"


def _unjson(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return {}
