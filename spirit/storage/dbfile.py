"""spirit/storage/dbfile.py — connection management for SessionDB.

v0.3.0 (Hermes-style): extracted from session_db.py into a focused module.

Responsibilities:
- Build sqlite3.Connection with correct PRAGMAs (WAL, FK, busy_timeout)
- Ensure parent directory exists with sane permissions
- Provide reusable connection primitive that mixins consume
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default db location: ~/.spirit/state.db (mirrors Hermes)
DEFAULT_DB_PATH = Path.home() / ".spirit" / "state.db"


def open_connection(
    db_path: Optional[Path] = None,
    *,
    row_factory: bool = True,
    timeout: float = 5.0,
) -> sqlite3.Connection:
    """Open sqlite3.Connection with Spirit's standard PRAGMAs.

    Args:
        db_path: path to db file (defaults to ~/.spirit/state.db)
        row_factory: if True, set sqlite3.Row for dict-like access
        timeout: busy timeout in seconds (default 5s)

    Returns:
        A configured sqlite3.Connection. Caller is responsible for close().
    """
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(path),
        check_same_thread=False,
        timeout=timeout,
        isolation_level=None,  # autocommit mode; we manage txns explicitly
    )
    if row_factory:
        conn.row_factory = sqlite3.Row

    # ── PRAGMAs ─────────────────────────────────────────────────
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")  # WAL + NORMAL is safe (Hermes default)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA temp_store=MEMORY")
    # SQLite ≥ 3.36 supports this; older versions ignore it.
    try:
        conn.execute("PRAGMA wal_autocheckpoint=1000")
    except sqlite3.DatabaseError:
        pass

    logger.debug("db 连接已打开: %s (WAL)", path)
    return conn
