"""spirit/storage/profile_repair.py — corruption-aware profile recovery.

v0.4.0 (Hermes-aligned): when state.db reports structural corruption, try
to reconstruct what we can from the still-readable parts of the file.

Reference: hermes_state_profile_repair.py.

This is a *last-resort* helper called from health.latch_corrupt().
It does NOT replace a real sqlite3 backup; it only salvages the
bytes / rows that sqlite can still read.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class ProfileRepairResult:
    """Outcome of a profile repair attempt."""

    def __init__(
        self,
        ok: bool,
        recovered_sessions: int,
        recovered_messages: int,
        notes: List[str],
    ):
        self.ok = ok
        self.recovered_sessions = recovered_sessions
        self.recovered_messages = recovered_messages
        self.notes = notes

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "recovered_sessions": self.recovered_sessions,
            "recovered_messages": self.recovered_messages,
            "notes": list(self.notes),
        }


class ProfileRepairMixin:
    """Recover rows from a partially corrupt state.db.

    Mixed into SessionDB via MRO.
    Expects:
        self.conn (sqlite3.Connection)
        self.db_path (Path)
    """

    def try_repair(self) -> ProfileRepairResult:
        """Best-effort repair.

        Strategy:
        1. PRAGMA integrity_check; if OK, no-op.
        2. If corrupt, dump everything we can read into a fresh DB.
        3. Replace self.db_path with the repaired copy.

        Returns a ProfileRepairResult describing what was salvaged.
        """
        notes: List[str] = []
        try:
            integrity = self.conn.execute("PRAGMA integrity_check").fetchone()
            if integrity and integrity[0] == "ok":
                return ProfileRepairResult(True, 0, 0, ["already_healthy"])
            notes.append("integrity_failed: %s" % (integrity[0] if integrity else "no_result"))
        except sqlite3.DatabaseError as exc:
            notes.append("integrity_threw: %s" % exc)

        recovered_s, recovered_m = 0, 0
        salvage_path = self.db_path.with_suffix(".salvage.sqlite")
        if salvage_path.exists():
            salvage_path.unlink()

        salvage = sqlite3.connect(str(salvage_path))
        try:
            salvage.execute("PRAGMA journal_mode=WAL")
            salvage.execute("PRAGMA synchronous=NORMAL")
            from .schema import SCHEMA_SQL
            salvage.executescript(SCHEMA_SQL)
            salvage.commit()
        except Exception:
            logger.exception("Could not init salvage schema")
            salvage.close()
            return ProfileRepairResult(False, 0, 0, notes + ["salvage_schema_failed"])

        for table in ("sessions", "messages", "usage", "async_delegations",
                      "conversation_generations", "gateway_routing"):
            try:
                cur = self.conn.execute(f"SELECT * FROM {table}")
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                placeholders = ",".join("?" for _ in cols)
                for row in rows:
                    try:
                        salvage.execute(
                            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
                            row,
                        )
                        if table == "sessions":
                            recovered_s += 1
                        elif table == "messages":
                            recovered_m += 1
                    except sqlite3.DatabaseError:
                        continue
                salvage.commit()
            except sqlite3.DatabaseError as exc:
                notes.append(f"table_{table}_unreadable: {exc}")

        salvage.close()
        notes.append("salvaged_to: %s" % str(salvage_path))
        return ProfileRepairResult(
            ok=recovered_s + recovered_m > 0,
            recovered_sessions=recovered_s,
            recovered_messages=recovered_m,
            notes=notes,
        )

    def replace_with_salvage(self, salvage_path: Path) -> bool:
        """Atomically swap self.db_path with the salvage file."""
        salvage = Path(salvage_path)
        if not salvage.exists():
            return False
        try:
            self.conn.close()
        except Exception:
            pass
        backup = self.db_path.with_suffix(".corrupt.bak")
        if backup.exists():
            backup.unlink()
        self.db_path.rename(backup)
        salvage.rename(self.db_path)
        return True
