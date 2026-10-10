"""spirit/storage/telegram.py — telegram DM topic mode (per-topic profile names).

v0.4.0 (Hermes-aligned): normalize and persist topic-mode profile names
for telegram DMs. Used by the telegram gateway (not yet in Spirit) but
the schema is in place for forward-compat.

Reference: hermes_state_telegram.py.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Default: the user can keep their main profile, or get a per-topic one.
_MAX_TOPIC_NAME_LEN = 64
_INVALID_NAME_CHARS = re.compile(r"[^\w\- ]+")


def _normalize_telegram_topic_profile_name(name: Optional[str]) -> str:
    """Sanitize a topic profile name.

    - Strip control characters
    - Replace forbidden chars with '-'
    - Collapse runs of '-'
    - Trim to MAX_TOPIC_NAME_LEN
    """
    if not name:
        return ""
    cleaned = _INVALID_NAME_CHARS.sub("-", str(name).strip())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned[:_MAX_TOPIC_NAME_LEN]


class SessionTelegramTopicsMixin:
    """Telegram DM topic mode: per-(chat_id, topic_id) profile names.

    Mixed into SessionDB via MRO.
    Expects:
        self.conn (sqlite3.Connection)
        self.db_path (Path)
    """

    def set_topic_profile_name(
        self, chat_id: int | str, topic_id: int | str, name: str
    ) -> str:
        """Set the per-topic profile name. Returns the normalized name."""
        normalized = _normalize_telegram_topic_profile_name(name)
        try:
            # We don't have a dedicated table; use state_meta with a
            # composite key.
            key = f"telegram_topic:{chat_id}:{topic_id}"
            self.conn.execute(
                """INSERT INTO state_meta (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                updated_at = CURRENT_TIMESTAMP""",
                (key, normalized),
            )
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("set_topic_profile_name: %s", e)
        return normalized

    def get_topic_profile_name(
        self, chat_id: int | str, topic_id: int | str
    ) -> Optional[str]:
        """Get the per-topic profile name (or None)."""
        try:
            key = f"telegram_topic:{chat_id}:{topic_id}"
            row = self.conn.execute(
                "SELECT value FROM state_meta WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            return row["value"] or None
        except Exception:  # noqa: BLE001
            return None

    def list_topic_profiles(self, chat_id: int | str) -> List[Dict[str, Any]]:
        """Return all topic profiles for a given chat_id."""
        try:
            prefix = f"telegram_topic:{chat_id}:"
            rows = self.conn.execute(
                "SELECT key, value FROM state_meta WHERE key LIKE ?",
                (f"{prefix}%",),
            ).fetchall()
            out = []
            for row in rows:
                tail = row["key"][len(prefix):]
                out.append({
                    "topic_id": tail,
                    "profile_name": row["value"],
                })
            return out
        except Exception:  # noqa: BLE001
            return []

    def delete_topic_profile(self, chat_id: int | str, topic_id: int | str) -> None:
        try:
            key = f"telegram_topic:{chat_id}:{topic_id}"
            self.conn.execute(
                "DELETE FROM state_meta WHERE key = ?",
                (key,),
            )
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("delete_topic_profile: %s", e)
