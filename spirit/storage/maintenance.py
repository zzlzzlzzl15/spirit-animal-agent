"""spirit/storage/maintenance.py — background maintenance operations.

v0.4.0 (Hermes-aligned): vacuum, checkpoint, prune, FTS rebuild, stats.

Reference: hermes_state_maintenance.py + hermes_state_stats.py.

These operations are heavy and should normally run from a maintenance
worker (not in-line on the hot path).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Default retention: keep everything unless explicitly pruned.
DEFAULT_PRUNE_OLDER_THAN_S = 90 * 86400  # 90 days


class MaintenanceResult:
    """Result of a single maintenance operation."""

    def __init__(self, op: str, ok: bool, detail: Dict[str, Any], duration_ms: int):
        self.op = op
        self.ok = ok
        self.detail = detail
        self.duration_ms = duration_ms

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op": self.op,
            "ok": self.ok,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
        }


class SessionMaintenanceMixin:
    """Vacuum / checkpoint / prune / FTS rebuild / stats.

    Mixed into SessionDB via MRO.
    Expects:
        self.conn (sqlite3.Connection)
        self.db_path (Path)
    """

    # ── VACUUM ──────────────────────────────────────────────

    def vacuum(self) -> MaintenanceResult:
        t0 = time.time()
        try:
            self.conn.execute("VACUUM")
            return MaintenanceResult("vacuum", True, {}, int((time.time() - t0) * 1000))
        except sqlite3.DatabaseError as exc:
            return MaintenanceResult("vacuum", False, {"error": str(exc)},
                                    int((time.time() - t0) * 1000))

    def vacuum_into(self, target_path: Path) -> MaintenanceResult:
        t0 = time.time()
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.conn.execute("VACUUM INTO ?", (str(target),))
            return MaintenanceResult(
                "vacuum_into", True,
                {"path": str(target), "size": target.stat().st_size},
                int((time.time() - t0) * 1000),
            )
        except sqlite3.DatabaseError as exc:
            return MaintenanceResult(
                "vacuum_into", False, {"error": str(exc)},
                int((time.time() - t0) * 1000),
            )

    # ── Checkpoint ──────────────────────────────────────────

    def checkpoint(self, mode: str = "PASSIVE") -> MaintenanceResult:
        t0 = time.time()
        try:
            self.conn.execute(f"PRAGMA wal_checkpoint({mode})")
            return MaintenanceResult("checkpoint", True, {"mode": mode},
                                    int((time.time() - t0) * 1000))
        except sqlite3.DatabaseError as exc:
            return MaintenanceResult("checkpoint", False, {"error": str(exc)},
                                    int((time.time() - t0) * 1000))

    # ── Prune ───────────────────────────────────────────────

    def prune_old_sessions(self, older_than_s: int = DEFAULT_PRUNE_OLDER_THAN_S) -> MaintenanceResult:
        t0 = time.time()
        threshold = int(time.time()) - older_than_s
        try:
            cur = self.conn.execute(
                "DELETE FROM sessions WHERE updated_at < ? AND active = 0",
                (threshold,),
            )
            deleted = cur.rowcount
            self.conn.commit()
            return MaintenanceResult(
                "prune_old_sessions", True,
                {"deleted": deleted, "threshold": threshold},
                int((time.time() - t0) * 1000),
            )
        except sqlite3.DatabaseError as exc:
            return MaintenanceResult(
                "prune_old_sessions", False, {"error": str(exc)},
                int((time.time() - t0) * 1000),
            )

    def prune_orphaned_messages(self) -> MaintenanceResult:
        t0 = time.time()
        try:
            cur = self.conn.execute(
                "DELETE FROM messages WHERE session_id NOT IN (SELECT id FROM sessions)"
            )
            deleted = cur.rowcount
            self.conn.commit()
            return MaintenanceResult(
                "prune_orphaned_messages", True, {"deleted": deleted},
                int((time.time() - t0) * 1000),
            )
        except sqlite3.DatabaseError as exc:
            return MaintenanceResult(
                "prune_orphaned_messages", False, {"error": str(exc)},
                int((time.time() - t0) * 1000),
            )

    # ── FTS rebuild ─────────────────────────────────────────

    def rebuild_fts(self) -> MaintenanceResult:
        t0 = time.time()
        rebuilt: List[str] = []
        try:
            for fts_name in self._list_fts_tables():
                self.conn.execute(f"INSERT INTO {fts_name}({fts_name}) VALUES('rebuild')")
                rebuilt.append(fts_name)
            self.conn.commit()
            return MaintenanceResult(
                "rebuild_fts", True, {"rebuilt": rebuilt},
                int((time.time() - t0) * 1000),
            )
        except sqlite3.DatabaseError as exc:
            return MaintenanceResult(
                "rebuild_fts", False, {"error": str(exc)},
                int((time.time() - t0) * 1000),
            )

    def _list_fts_tables(self) -> List[str]:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'messages_fts%'"
        ).fetchall()
        return [r[0] for r in rows]

    # ── Stats ───────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        """Return a structural health snapshot (table counts, indexes, sizes)."""
        out: Dict[str, Any] = {"tables": {}, "indexes": [], "fts": []}
        for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall():
            name = r[0]
            if name.startswith("sqlite_"):
                continue
            try:
                count = self.conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            except sqlite3.DatabaseError:
                count = -1
            out["tables"][name] = count
            if name.startswith("messages_fts") or name == "sessions_fts":
                out["fts"].append(name)
        for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        ).fetchall():
            out["indexes"].append(r[0])
        try:
            page_size = self.conn.execute("PRAGMA page_size").fetchone()[0]
            page_count = self.conn.execute("PRAGMA page_count").fetchone()[0]
            out["db_size_bytes"] = page_size * page_count
        except sqlite3.DatabaseError:
            out["db_size_bytes"] = -1
        return out

    def health_check(self) -> Dict[str, Any]:
        """Run PRAGMA integrity_check and report."""
        try:
            row = self.conn.execute("PRAGMA integrity_check").fetchone()
            ok = row and row[0] == "ok"
            return {"ok": ok, "result": row[0] if row else "no_result"}
        except sqlite3.DatabaseError as exc:
            return {"ok": False, "error": str(exc)}
