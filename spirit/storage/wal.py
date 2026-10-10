"""spirit/storage/wal.py — journal-mode + PRAGMA policy for state.db.

v0.4.0 (Hermes-aligned): WAL with fallback + macOS-specific pragmas +
checkpoint barrier + size limit.

Reference: hermes_state_wal.py.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class WalUnsupportedError(RuntimeError):
    """Raised when WAL mode cannot be enabled on the given filesystem."""


# Maximum WAL size before auto-checkpoint (Hermes: 64MB default).
DEFAULT_WAL_SIZE_LIMIT_MB = 64


def _mode_from_row(row) -> str:
    """Read the current journal_mode from a PRAGMA result row."""
    if row is None:
        return ""
    # sqlite3.Row supports key access; row[0] is the mode
    try:
        return str(row[0]).lower()
    except (IndexError, TypeError):
        return ""


def _on_disk_journal_mode(conn: sqlite3.Connection) -> str:
    """Read journal_mode for the on-disk db (not just the connection's view)."""
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        return _mode_from_row(row)
    except sqlite3.DatabaseError as e:
        logger.debug("PRAGMA journal_mode failed: %s", e)
        return ""


def _apply_wal_size_limit(conn: sqlite3.Connection, max_mb: int = DEFAULT_WAL_SIZE_LIMIT_MB) -> None:
    """Set the WAL auto-checkpoint threshold (pages).

    Pages are 4KB. The threshold is in pages, so max_mb * 256 = pages.
    """
    pages = max(1, max_mb) * 256
    try:
        conn.execute(f"PRAGMA wal_autocheckpoint = {pages}")
    except sqlite3.DatabaseError as e:
        logger.debug("wal_autocheckpoint failed: %s", e)


def _darwin_pragma(conn: sqlite3.Connection) -> None:
    """macOS-specific PRAGMAs (darwin has known WAL reset vulnerability)."""
    if sys.platform != "darwin":
        return
    # macOS has a documented issue where the WAL can be reset by a process
    # crash in the middle of a write, causing a future reader to see partial
    # state. Synchronous FULL eliminates the window.
    try:
        conn.execute("PRAGMA synchronous = FULL")
    except sqlite3.DatabaseError as e:
        logger.debug("PRAGMA synchronous FULL (darwin) failed: %s", e)


def _apply_macos_checkpoint_barrier(conn: sqlite3.Connection) -> bool:
    """Insert a TRUNCATE checkpoint barrier after a writer commits on macOS.

    This is a no-op on other platforms. Returns True if a barrier was applied
    (or no-op on non-darwin); False if the checkpoint failed.
    """
    if sys.platform != "darwin":
        return True
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return True
    except sqlite3.DatabaseError as e:
        logger.debug("macos wal_checkpoint barrier failed: %s", e)
        return False


def _enforce_macos_synchronous_full(conn: sqlite3.Connection) -> None:
    """Force synchronous=FULL on macOS (Hermes pattern)."""
    if sys.platform != "darwin":
        return
    try:
        conn.execute("PRAGMA synchronous = FULL")
    except sqlite3.DatabaseError as e:
        logger.debug("enforce_macos_synchronous_full failed: %s", e)


def _apply_wal_companions(conn: sqlite3.Connection) -> None:
    """Apply standard PRAGMA companions to a WAL-mode connection."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
    except sqlite3.DatabaseError as e:
        logger.debug("PRAGMA busy_timeout failed: %s", e)


def apply_wal_with_fallback(
    conn: sqlite3.Connection,
    *,
    max_wal_mb: int = DEFAULT_WAL_SIZE_LIMIT_MB,
) -> str:
    """Enable WAL mode; on failure, fall back to DELETE journal mode.

    Returns:
        The journal mode that ended up active ('wal' on success).

    Raises:
        WalUnsupportedError: if even the fallback to DELETE mode failed.
    """
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError as e:
        logger.warning("WAL enable failed: %s", e)
    mode = _on_disk_journal_mode(conn)
    if mode == "wal":
        _apply_wal_companions(conn)
        _apply_wal_size_limit(conn, max_wal_mb)
        _darwin_pragma(conn)
        _enforce_macos_synchronous_full(conn)
        return "wal"
    # Fallback to DELETE journal mode
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("PRAGMA synchronous = NORMAL")
        logger.info("WAL 不可用,已 fallback 到 DELETE mode (原因: %s)", mode)
        return "delete"
    except sqlite3.DatabaseError as e:
        raise WalUnsupportedError(f"无法启用任何 journal mode: {e}") from e


def apply_database_pragmas(conn: sqlite3.Connection) -> None:
    """Apply the full Spirit PRAGMA set to a connection (idempotent)."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
    except sqlite3.DatabaseError as e:
        logger.debug("busy_timeout pragma failed: %s", e)


# Markers in the WAL that indicate the file is in a known-vulnerable state
# (used by repair / quarantine logic).
_WAL_INCOMPAT_MARKERS = (
    b"wal-reset",
    b"unlink-wal",
    b"mid-frame",
)


def is_sqlite_wal_reset_vulnerable(db_path: Path, *, force_darwin: bool = False) -> bool:
    """True if the db file's WAL is in a state known to be reset-vulnerable
    on the current platform.

    Args:
        db_path: path to the SQLite db file (the WAL is at ``<db_path>-wal``)
        force_darwin: if True, treat as darwin (used for cross-platform tests)

    Note:
        Detection checks the first 1024 bytes of the ``-wal`` file for known
        vuln markers (b"wal-reset", b"unlink-wal", b"mid-frame"). On non-darwin
        platforms without ``force_darwin``, returns False (vuln is darwin-specific).
    """
    if not force_darwin and sys.platform != "darwin":
        return False
    wal_path = db_path.with_suffix(db_path.suffix + "-wal")
    if not wal_path.exists():
        return False
    try:
        head = wal_path.read_bytes()[:1024]
    except OSError:
        return False
    return any(marker in head for marker in _WAL_INCOMPAT_MARKERS)


def quarantine_vulnerable_wal(
    db_path: Path,
    *,
    force_darwin: bool = False,
    quarantine_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """If ``is_sqlite_wal_reset_vulnerable(db_path)`` returns True, rename the
    ``-wal`` file to a timestamped quarantine path so the next open can start
    cleanly without the corrupted WAL.

    Args:
        db_path: path to the SQLite db file
        force_darwin: if True, treat as darwin (used for cross-platform tests)
        quarantine_dir: optional directory to move the WAL file into
                        (default: alongside the db file)

    Returns:
        Dict report:
            - vulnerable: bool — whether vulnerable state was detected
            - quarantined: bool — whether WAL file was actually renamed
            - quarantine_path: Optional[Path] — the renamed path (if any)
            - original_wal: Optional[Path] — original WAL path (if any)
            - reason: str — short reason for logging
    """
    import time as _time

    report: Dict[str, Any] = {
        "vulnerable": False,
        "quarantined": False,
        "quarantine_path": None,
        "original_wal": None,
        "reason": "not_darwin" if not force_darwin and sys.platform != "darwin" else "ok",
    }
    if not force_darwin and sys.platform != "darwin":
        return report

    wal_path = db_path.with_suffix(db_path.suffix + "-wal")
    if not wal_path.exists():
        report["reason"] = "no_wal_file"
        return report

    report["original_wal"] = wal_path
    if not is_sqlite_wal_reset_vulnerable(db_path, force_darwin=force_darwin):
        report["reason"] = "wal_healthy"
        return report

    report["vulnerable"] = True
    target_dir = quarantine_dir or db_path.parent
    ts = int(_time.time() * 1000)
    quarantine_path = target_dir / f"{db_path.name}.quarantine-wal-{ts}"
    try:
        wal_path.rename(quarantine_path)
        report["quarantined"] = True
        report["quarantine_path"] = quarantine_path
        report["reason"] = "renamed"
        logger.warning(
            "wal: quarantined vulnerable WAL: %s → %s",
            wal_path, quarantine_path,
        )
    except OSError as exc:
        report["reason"] = f"rename_failed: {exc}"
        logger.error("wal: quarantine rename failed: %s", exc)
    return report


def safe_wal_open(
    conn: sqlite3.Connection,
    db_path: Path,
    *,
    force_darwin: bool = False,
    quarantine_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Wrap ``is_sqlite_wal_reset_vulnerable`` + ``quarantine_vulnerable_wal`` +
    ``_enforce_macos_synchronous_full`` into a single call suitable for
    ``SessionDB.__init__``.

    Order:
        1. Detect WAL vulnerability (no-op on non-darwin without force_darwin)
        2. If vulnerable, quarantine the WAL file before opening transactions
        3. Re-enforce synchronous=FULL on the connection (defense in depth)

    Returns:
        Dict report with ``vulnerable`` / ``quarantined`` / ``quarantine_path`` /
        ``synchronous_full`` / ``reason`` keys.
    """
    report = quarantine_vulnerable_wal(
        db_path, force_darwin=force_darwin, quarantine_dir=quarantine_dir,
    )
    # Defense in depth: even on non-vulnerable DBs, re-enforce synchronous=FULL
    # on darwin to eliminate the WAL reset window.
    sync_ok = True
    if force_darwin or sys.platform == "darwin":
        try:
            conn.execute("PRAGMA synchronous = FULL")
        except sqlite3.DatabaseError as exc:
            sync_ok = False
            logger.debug("safe_wal_open: PRAGMA synchronous=FULL failed: %s", exc)
    report["synchronous_full"] = sync_ok
    return report
