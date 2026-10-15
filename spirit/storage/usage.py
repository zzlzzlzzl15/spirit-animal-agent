"""spirit/storage/usage.py — Per-session per-model token aggregation.

v0.3.0 (Hermes-style): extracted session_model_usage CRUD.

The session_model_usage table is the canonical source for "how many
tokens did session X spend on model Y". This is more granular than
sessions.total_tokens (which is across all models). Use it when
displaying per-model cost breakdown.

API:
- record_usage(session_id, model, input_tokens, output_tokens)
- get_session_usage(session_id) -> dict[model, {input, output, total}]
- get_top_sessions_by_tokens(limit=10) -> list[(session_id, model, total)]
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Dict, List

logger = logging.getLogger(__name__)


_RECORD_USAGE_SQL = """
INSERT INTO session_model_usage (session_id, model, input_tokens, output_tokens, updated_at)
VALUES (?, ?, ?, ?, datetime('now', 'localtime'))
ON CONFLICT(session_id, model) DO UPDATE SET
    input_tokens = input_tokens + excluded.input_tokens,
    output_tokens = output_tokens + excluded.output_tokens,
    updated_at = datetime('now', 'localtime')
"""

_GET_SESSION_USAGE_SQL = """
SELECT model, input_tokens, output_tokens, updated_at
FROM session_model_usage
WHERE session_id = ?
ORDER BY (input_tokens + output_tokens) DESC
"""

_GET_TOP_SESSIONS_SQL = """
SELECT session_id, model,
       (input_tokens + output_tokens) AS total_tokens,
       input_tokens, output_tokens
FROM session_model_usage
ORDER BY total_tokens DESC
LIMIT ?
"""


class SessionUsageMixin:
    """Token usage aggregation. Mixed into SessionDB via MRO.

    Expects:
        self.conn (sqlite3.Connection)
    """

    def record_usage(
        self,
        session_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Record (or accumulate) token usage for (session_id, model).

        Idempotent — calling twice with same args doubles the count, by design.
        """
        try:
            self.conn.execute(
                _RECORD_USAGE_SQL,
                (session_id, model, int(input_tokens), int(output_tokens)),
            )
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            logger.debug("record_usage 失败 (非致命): %s", e)

    def get_session_usage(self, session_id: str) -> Dict[str, Dict[str, int]]:
        """Return {model: {input_tokens, output_tokens, total_tokens}} for a session."""
        rows = self.conn.execute(
            _GET_SESSION_USAGE_SQL, (session_id,)
        ).fetchall()
        out: Dict[str, Dict[str, int]] = {}
        for row in rows:
            model = row["model"]
            inp = int(row["input_tokens"] or 0)
            out_tokens = int(row["output_tokens"] or 0)
            out[model] = {
                "input_tokens": inp,
                "output_tokens": out_tokens,
                "total_tokens": inp + out_tokens,
                "updated_at": row["updated_at"],
            }
        return out

    def get_top_sessions_by_tokens(self, limit: int = 10) -> List[Dict]:
        """Return top-N (session_id, model) pairs by total token usage."""
        rows = self.conn.execute(_GET_TOP_SESSIONS_SQL, (limit,)).fetchall()
        return [
            {
                "session_id": row["session_id"],
                "model": row["model"],
                "input_tokens": int(row["input_tokens"] or 0),
                "output_tokens": int(row["output_tokens"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
            }
            for row in rows
        ]
