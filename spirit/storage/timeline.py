"""spirit/storage/timeline.py — user-facing session timeline view.

v0.4.0 (Hermes-aligned): ordered summary of messages + tool calls + token
usage suitable for TUI / Web / Electron rendering.

Reference: hermes_state_timeline.py.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class TimelineEntry:
    """One row in the timeline."""

    timestamp: int
    kind: str  # 'message' / 'tool' / 'compression' / 'note'
    role: str = ""
    summary: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    message_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "kind": self.kind,
            "role": self.role,
            "summary": self.summary,
            "metadata": self.metadata,
            "message_id": self.message_id,
        }


class SessionTimelineMixin:
    """Build a chronological timeline for a session.

    Mixed into SessionDB via MRO.
    Expects:
        self.conn (sqlite3.Connection)
    """

    def build_timeline(
        self,
        session_id: str,
        limit: int = 200,
        include_compactions: bool = True,
    ) -> List[TimelineEntry]:
        """Return up to `limit` TimelineEntry rows for `session_id`."""
        entries: List[TimelineEntry] = []
        try:
            rows = self.conn.execute(
                "SELECT id, role, content, tool_name, finish_reason, timestamp, "
                "       token_count, observed, compacted, compressed_summary "
                "FROM messages WHERE session_id = ? "
                "ORDER BY timestamp ASC, id ASC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        except Exception:
            logger.exception("Failed to read messages for timeline")
            return entries

        for row in rows:
            mid, role, content, tool_name, finish_reason, ts, tokens, observed, compacted, summary = row
            text = (content or "") if isinstance(content, str) else str(content or "")
            entry = TimelineEntry(
                timestamp=int(ts or 0),
                kind="message",
                role=role or "",
                summary=text[:240],
                message_id=int(mid),
                metadata={
                    "tokens": tokens or 0,
                    "tool": tool_name or "",
                    "finish": finish_reason or "",
                    "observed": bool(observed),
                    "compacted": bool(compacted),
                },
            )
            entries.append(entry)
            if include_compactions and compacted and summary:
                entries.append(
                    TimelineEntry(
                        timestamp=int(ts or 0) + 1,
                        kind="compression",
                        summary=("compressed: " + (summary[:200] if summary else "")),
                        metadata={"parent_message_id": int(mid)},
                    )
                )
        return entries

    def timeline_summary(self, session_id: str) -> Dict[str, Any]:
        """Aggregate counts (no full body)."""
        try:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n, MIN(timestamp) AS first_ts, MAX(timestamp) AS last_ts "
                "FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            n = row["n"] if row else 0
            first_ts = row["first_ts"] if row else None
            last_ts = row["last_ts"] if row else None
            tokens_row = self.conn.execute(
                "SELECT COALESCE(SUM(token_count), 0) AS total_tokens "
                "FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            total_tokens = tokens_row["total_tokens"] if tokens_row else 0
        except Exception:
            logger.exception("timeline_summary failed")
            n, first_ts, last_ts, total_tokens = 0, None, None, 0
        return {
            "session_id": session_id,
            "message_count": n,
            "first_at": int(first_ts) if first_ts else None,
            "last_at": int(last_ts) if last_ts else None,
            "total_tokens": int(total_tokens or 0),
            "span_seconds": (
                int(last_ts - first_ts) if (first_ts and last_ts) else 0
            ),
        }

    def timeline_for_display(self, session_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Convenience for UI: list of dicts."""
        return [e.to_dict() for e in self.build_timeline(session_id, limit=limit)]
