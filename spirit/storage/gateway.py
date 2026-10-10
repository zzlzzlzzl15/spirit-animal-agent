"""spirit/storage/gateway.py — gateway-facing persistence (routing, peers, heartbeats).

v0.4.0 (Hermes-aligned): routing index, hygiene state, generation tokens,
heartbeats, handoffs, orphans.

Reference: hermes_state_gateway.py.

Spirit is local-only for the most part; this module exists for the
telegram / desktop gateway surfaces that DO need cross-process coordination.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


DEFAULT_HEARTBEAT_TTL_S = 90.0  # heartbeats older than this are dead


class SessionGatewayMixin:
    """Gateway-facing SessionDB persistence.

    Mixed into SessionDB via MRO.
    Expects:
        self.conn (sqlite3.Connection)
        self.db_path (Path)
    """

    # ── Heartbeats ────────────────────────────────────────────

    def record_heartbeat(
        self,
        backend_id: str,
        pid: int,
        profile: str = "",
        host: str = "",
    ) -> None:
        """Record or refresh a backend heartbeat."""
        now = time.time()
        try:
            self.conn.execute(
                """INSERT INTO gateway_heartbeats
                       (backend_id, pid, started_at, last_heartbeat, profile, host)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(backend_id) DO UPDATE SET
                     pid = excluded.pid,
                     last_heartbeat = excluded.last_heartbeat,
                     profile = excluded.profile,
                     host = excluded.host""",
                (backend_id, pid, now, now, profile, host),
            )
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("record_heartbeat: %s", e)

    def cleanup_dead_heartbeats(self, ttl_s: float = DEFAULT_HEARTBEAT_TTL_S) -> int:
        """Remove heartbeats older than ttl_s. Returns the number deleted."""
        try:
            cutoff = time.time() - ttl_s
            cur = self.conn.execute(
                "DELETE FROM gateway_heartbeats WHERE last_heartbeat < ?",
                (cutoff,),
            )
            self.conn.commit()
            return cur.rowcount
        except Exception as e:  # noqa: BLE001
            logger.debug("cleanup_dead_heartbeats: %s", e)
            return 0

    def list_heartbeats(self) -> List[Dict[str, Any]]:
        """Return all live heartbeats (best-effort)."""
        try:
            rows = self.conn.execute(
                "SELECT * FROM gateway_heartbeats ORDER BY last_heartbeat DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    # ── Routing index ─────────────────────────────────────────

    def set_routing_entry(
        self,
        scope: str,
        session_key: str,
        entry_json: str,
    ) -> None:
        """Set a routing entry (overwrites)."""
        try:
            self.conn.execute(
                """INSERT INTO gateway_routing (scope, session_key, entry_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(scope, session_key) DO UPDATE SET
                     entry_json = excluded.entry_json,
                     updated_at = excluded.updated_at""",
                (scope, session_key, entry_json, time.time()),
            )
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("set_routing_entry: %s", e)

    def get_routing_entry(self, scope: str, session_key: str) -> Optional[Dict[str, Any]]:
        """Read a routing entry (or None)."""
        try:
            row = self.conn.execute(
                """SELECT entry_json, updated_at FROM gateway_routing
                   WHERE scope = ? AND session_key = ?""",
                (scope, session_key),
            ).fetchone()
            if row is None:
                return None
            import json

            return {
                "entry_json": row["entry_json"],
                "updated_at": row["updated_at"],
                "parsed": json.loads(row["entry_json"]),
            }
        except Exception:  # noqa: BLE001
            return None

    def delete_routing_entry(self, scope: str, session_key: str) -> None:
        try:
            self.conn.execute(
                "DELETE FROM gateway_routing WHERE scope = ? AND session_key = ?",
                (scope, session_key),
            )
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("delete_routing_entry: %s", e)

    # ── Hygiene state ─────────────────────────────────────────

    def bump_hygiene_failure(self, session_key: str) -> int:
        """Increment the failure streak. Returns the new value."""
        try:
            self.conn.execute(
                """INSERT INTO gateway_hygiene_state (session_key, failure_streak)
                   VALUES (?, 1)
                   ON CONFLICT(session_key) DO UPDATE SET
                     failure_streak = failure_streak + 1""",
                (session_key,),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT failure_streak FROM gateway_hygiene_state WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            return int(row["failure_streak"]) if row else 0
        except Exception:  # noqa: BLE001
            return 0

    def clear_hygiene_failures(self, session_key: str) -> None:
        try:
            self.conn.execute(
                "DELETE FROM gateway_hygiene_state WHERE session_key = ?",
                (session_key,),
            )
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("clear_hygiene_failures: %s", e)

    def get_hygiene_streak(self, session_key: str) -> int:
        try:
            row = self.conn.execute(
                "SELECT failure_streak FROM gateway_hygiene_state WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            return int(row["failure_streak"]) if row else 0
        except Exception:  # noqa: BLE001
            return 0

    # ── Generation tokens (cache invalidation) ────────────────

    def bump_conversation_generation(self, source: str, session_key: str) -> int:
        """Bump the generation token for a (source, session_key) pair.

        Cache layers (e.g. FTS) can use this to know when to refresh.
        """
        try:
            self.conn.execute(
                """INSERT INTO conversation_generations (source, session_key, generation)
                   VALUES (?, ?, 1)
                   ON CONFLICT(source, session_key) DO UPDATE SET
                     generation = generation + 1""",
                (source, session_key),
            )
            self.conn.commit()
            row = self.conn.execute(
                """SELECT generation FROM conversation_generations
                   WHERE source = ? AND session_key = ?""",
                (source, session_key),
            ).fetchone()
            return int(row["generation"]) if row else 0
        except Exception:  # noqa: BLE001
            return 0

    def get_conversation_generation(self, source: str, session_key: str) -> int:
        try:
            row = self.conn.execute(
                """SELECT generation FROM conversation_generations
                   WHERE source = ? AND session_key = ?""",
                (source, session_key),
            ).fetchone()
            return int(row["generation"]) if row else 0
        except Exception:  # noqa: BLE001
            return 0

    # ── Handoffs / orphans ────────────────────────────────────

    def record_handoff(
        self,
        session_id: str,
        peer: str,
        lineage_donor: Optional[str] = None,
    ) -> None:
        """Record that a session was handed off to a peer backend.

        Stored in state_meta for now (Hermes uses a dedicated table;
        Spirit keeps things simple).
        """
        try:
            import json

            existing = self.conn.execute(
                "SELECT value FROM state_meta WHERE key = ?",
                (f"handoff:{session_id}",),
            ).fetchone()
            handoffs = []
            if existing is not None:
                try:
                    handoffs = json.loads(existing["value"])
                except (json.JSONDecodeError, TypeError):
                    handoffs = []
            handoffs.append({
                "peer": peer,
                "lineage_donor": lineage_donor,
                "at": time.time(),
            })
            self.conn.execute(
                """INSERT INTO state_meta (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                updated_at = CURRENT_TIMESTAMP""",
                (f"handoff:{session_id}", json.dumps(handoffs, ensure_ascii=False)),
            )
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("record_handoff: %s", e)

    def list_orphans(self) -> List[Dict[str, Any]]:
        """Return sessions with no recent activity (potential orphans).

        Definition: sessions where last_touched_at is older than 24h AND
        no heartbeat references them.
        """
        try:
            cutoff = time.time() - 24 * 3600
            rows = self.conn.execute(
                """SELECT id, source, title, started_at, ended_at, last_touched_at
                   FROM sessions
                   WHERE (last_touched_at IS NULL OR last_touched_at < ?)
                     AND (ended_at IS NULL)
                   ORDER BY started_at DESC
                   LIMIT 100""",
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []
