"""spirit/agent/storage_history_archiver.py — archive old messages to JSONL.

v0.4.0 (Hermes-aligned): periodically stream messages older than a
threshold out of the live DB and into a per-session JSONL archive.

Reference: hermes agent/history_archiver.py.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..storage.session_db import SessionDB

logger = logging.getLogger(__name__)


DEFAULT_ARCHIVE_DIR = "archives"
DEFAULT_ARCHIVE_OLDER_THAN_S = 30 * 86400  # 30 days


class StorageHistoryArchiver:
    """Stream cold messages out of state.db into JSONL side files."""

    def __init__(
        self,
        db: SessionDB,
        archive_dir: Path,
        older_than_s: int = DEFAULT_ARCHIVE_OLDER_THAN_S,
    ):
        self.db = db
        self.archive_dir = Path(archive_dir)
        self.older_than_s = older_than_s

    def archive_session(
        self,
        session_id: str,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Archive one session's cold messages. Returns a summary dict."""
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        threshold = int(time.time()) - self.older_than_s
        msgs = self.db.get_messages_before(session_id, threshold)
        if not msgs:
            return {"archived": 0, "path": None}

        archive_path = self.archive_dir / f"{session_id}.jsonl"
        with archive_path.open("a", encoding="utf-8") as fp:
            for m in msgs:
                fp.write(json.dumps(m, ensure_ascii=False, default=str) + "\n")
        if not dry_run:
            try:
                self.db.delete_messages_before(session_id, threshold)
            except Exception:
                logger.exception(
                    "Failed to delete archived messages for %s; archive kept",
                    session_id,
                )
        return {"archived": len(msgs), "path": str(archive_path)}

    def archive_all(self, dry_run: bool = False) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for s in self.db.list_sessions():
            out.append(self.archive_session(s["id"], dry_run=dry_run))
        return out
