"""spirit/storage/state_meta.py — KV store for Spirit-specific persistence.

v0.3.0 (Hermes-style): state_meta table is Spirit-specific (goal
persistence, Ralph Loop state, /resume state). Hermes uses similar
KV pattern in hermes_state.py state_meta table.

API:
- get_meta(key) -> str | None
- set_meta(key, value)
- delete_meta(key)
- list_keys(prefix="") -> list[str]
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)


class StateMetaMixin:
    """KV state metadata. Mixed into SessionDB via MRO.

    Expects:
        self.conn (sqlite3.Connection)
    """

    def get_meta(self, key: str) -> str | None:
        """Read state_meta value by key. None if absent."""
        row = self.conn.execute(
            "SELECT value FROM state_meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        """Upsert (insert or replace) state_meta value."""
        self.conn.execute(
            """
            INSERT INTO state_meta (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, value),
        )
        self.conn.commit()

    def delete_meta(self, key: str) -> None:
        """Delete state_meta entry. No-op if absent."""
        self.conn.execute("DELETE FROM state_meta WHERE key = ?", (key,))
        self.conn.commit()

    def list_meta_keys(self, prefix: str = "") -> list[str]:
        """List all state_meta keys, optionally filtered by prefix."""
        if prefix:
            rows = self.conn.execute(
                "SELECT key FROM state_meta WHERE key LIKE ?", (f"{prefix}%",)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT key FROM state_meta").fetchall()
        return [r["key"] for r in rows]
