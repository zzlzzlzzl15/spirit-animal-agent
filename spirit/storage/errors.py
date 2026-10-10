"""spirit/storage/errors.py — persistence error classification (Hermes-aligned).

v0.4.0 (Hermes full-alignment): full error taxonomy from hermes_state_errors.py.

Reference (Hermes):
- StateDbCorruptError / StateDbReplacedError → divert to JSONL
- SessionCompressionInProgressError / SessionTurnLeaseLostError → live tip retry
- SQLite "database is locked" → log warning + return False (retry next time)
- CompressionSessionClosedError → live tip retry
- is_disk_full_error → emergency divert
- is_malformed_schema_error → repair
- classify_persistence_error → unifies classification
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)


# ── Error class hierarchy ──────────────────────────────────────────

class StateDbError(Exception):
    """Base class for Spirit storage errors."""


class StateDbCorruptError(StateDbError):
    """Database is corrupt and cannot be recovered — divert to JSONL."""


class StateDbReplacedError(StateDbError):
    """Database file has been replaced underneath us (different inode / generation)."""


class StateDbLockedError(StateDbError):
    """SQLite lock could not be acquired within busy_timeout — caller may retry."""


class CompressionSessionClosedError(StateDbError):
    """Compression window closed mid-flush; caller should retry on the live tip."""


class CompressionSessionBusyError(StateDbError):
    """Another compression attempt holds the lock; caller should back off."""


class SessionCompressionInProgressError(StateDbError):
    """A compression is already in progress for this session."""


class SessionTurnLeaseLostError(StateDbError):
    """Turn lease lost (TTL expired or holder died); caller's turn was preempted."""


class DeletedWalGenerationError(StateDbError):
    """WAL generation was deleted underneath us; must reopen the db file."""


class DiskFullError(StateDbError):
    """SQLite reports disk I/O error; assume disk is full — divert to JSONL."""


# ── String predicates (Hermes pattern: match wrapped RPC stderr) ────

def _sqlite_primary_code(exc: BaseException) -> int | None:
    """Extract the SQLite primary error code from a sqlite3 exception.

    sqlite3 exceptions may be wrapped (e.g. by aiohttp / RPC layers); the
    primary code can sit on .args[0] or on the inner .args.
    """
    cur: BaseException | None = exc
    for _ in range(3):
        if cur is None:
            return None
        if isinstance(cur, sqlite3.Error):
            args = cur.args
            if args and isinstance(args[0], int):
                return args[0]
        cur = cur.__cause__ or cur.__context__
    return None


def is_malformed_db_error(msg_or_exc) -> bool:
    """True if the error string/code indicates a corrupt / malformed db file."""
    if isinstance(msg_or_exc, BaseException):
        code = _sqlite_primary_code(msg_or_exc)
        if code in (11,):  # SQLITE_CORRUPT
            return True
        msg = str(msg_or_exc).lower()
    else:
        msg = str(msg_or_exc).lower()
    return any(
        marker in msg
        for marker in (
            "malformed",
            "corrupt",
            "disk image",
            "database disk image is malformed",
        )
    )


def is_malformed_schema_error(msg_or_exc) -> bool:
    """True if error indicates a missing table / column (schema drift)."""
    if isinstance(msg_or_exc, BaseException):
        msg = str(msg_or_exc).lower()
    else:
        msg = str(msg_or_exc).lower()
    return any(
        marker in msg
        for marker in (
            "no such table",
            "no such column",
            "has no column named",
        )
    )


def is_sqlite_lock_error(msg_or_exc) -> bool:
    """True if SQLite reports the db / a row / a table is locked."""
    code = _sqlite_primary_code(msg_or_exc) if isinstance(msg_or_exc, BaseException) else None
    if code in (5, 6):  # SQLITE_BUSY / SQLITE_LOCKED
        return True
    if isinstance(msg_or_exc, BaseException):
        msg = str(msg_or_exc).lower()
    else:
        msg = str(msg_or_exc).lower()
    return "is locked" in msg


def is_disk_full_error(msg_or_exc) -> bool:
    """True if SQLite reports disk full or I/O error."""
    code = _sqlite_primary_code(msg_or_exc) if isinstance(msg_or_exc, BaseException) else None
    if code in (13, 522, 1544):  # SQLITE_FULL / SQLITE_IOERR_WRITE / SQLITE_IOERR_FSYNC
        return True
    if isinstance(msg_or_exc, BaseException):
        msg = str(msg_or_exc).lower()
    else:
        msg = str(msg_or_exc).lower()
    return any(m in msg for m in ("disk full", "no space", "enospc"))


def is_transient_sqlite_error(msg_or_exc) -> bool:
    """True if the error looks transient (worth retrying)."""
    return is_sqlite_lock_error(msg_or_exc) or is_disk_full_error(msg_or_exc)


def is_fts_scoped_corruption_error(msg_or_exc) -> bool:
    """True if FTS virtual table is corrupt (rest of db may be fine)."""
    if isinstance(msg_or_exc, BaseException):
        msg = str(msg_or_exc).lower()
    else:
        msg = str(msg_or_exc).lower()
    return "fts" in msg and is_malformed_db_error(msg)


def _is_no_more_rows(exc: BaseException) -> bool:
    """True if the exception is sqlite's 'no more rows' (a normal control flow, not error)."""
    return isinstance(exc, StopIteration) or (
        isinstance(exc, sqlite3.Error) and "no more rows" in str(exc).lower()
    )


# ── Classification (Hermes-compatible string buckets) ──────────────

def classify_persistence_error(exc: Exception) -> str:
    """Classify a persistence error into one of:
    - "corrupt"   → divert to JSONL, raise StateDbCorruptError
    - "replaced"  → divert to JSONL, raise StateDbReplacedError
    - "deleted_wal" → reopen db, retry
    - "lock"      → log warning + return False (caller retries)
    - "disk_full" → emergency divert to JSONL
    - "compression_busy" → caller backs off
    - "compression_closed" → caller retries on live tip
    - "schema_drift" → repair or rebuild
    - "fts_corrupt" → rebuild FTS only
    - "unknown"   → log warning + return False (don't crash main loop)

    Returns:
        Classification string. Caller decides recovery strategy.
    """
    # Our own exception types always win over generic sqlite3.Error isinstance
    # checks below — they're more specific.
    if isinstance(exc, SessionCompressionInProgressError):
        return "compression_busy"
    if isinstance(exc, CompressionSessionBusyError):
        return "compression_busy"
    if isinstance(exc, CompressionSessionClosedError):
        return "compression_closed"
    if isinstance(exc, SessionTurnLeaseLostError):
        return "turn_lease_lost"
    if isinstance(exc, DeletedWalGenerationError):
        return "deleted_wal"
    if isinstance(exc, StateDbLockedError):
        logger.debug("db 锁冲突 (下次重试): %s", exc)
        return "lock"
    if isinstance(exc, StateDbReplacedError):
        logger.warning("db 被替换 (尝试新 file): %s", exc)
        return "replaced"
    if isinstance(exc, StateDbCorruptError):
        logger.warning("db 损坏 (尝试重建): %s", exc)
        return "corrupt"

    if isinstance(exc, sqlite3.DatabaseError):
        msg = str(exc).lower()
        if is_malformed_db_error(exc):
            logger.warning("db 损坏 (尝试重建): %s", exc)
            return "corrupt"
        if is_malformed_schema_error(exc):
            logger.warning("db schema 缺失 (尝试重建): %s", exc)
            return "schema_drift"
        if is_fts_scoped_corruption_error(exc):
            logger.warning("FTS 索引损坏 (仅重建 FTS): %s", exc)
            return "fts_corrupt"
        if is_sqlite_lock_error(exc):
            logger.debug("db 锁冲突 (下次重试): %s", exc)
            return "lock"
        if is_disk_full_error(exc):
            logger.warning("磁盘已满 (紧急 divert): %s", exc)
            return "disk_full"
    if isinstance(exc, sqlite3.OperationalError):
        if is_sqlite_lock_error(exc):
            return "lock"
        if is_disk_full_error(exc):
            return "disk_full"
        return "lock"  # treat other OperationalErrors as transient
    if isinstance(exc, sqlite3.IntegrityError):
        # FK violation, UNIQUE conflict — usually caller's bug, not db's
        logger.warning("db 完整性错误 (忽略): %s", exc)
        return "unknown"
    if isinstance(exc, sqlite3.Error):
        # Other SQLite errors: log + classify as unknown
        logger.warning("db SQLite 错误 (忽略): %s: %s", type(exc).__name__, exc)
        return "unknown"
    logger.warning("db 未知错误 (忽略): %s: %s", type(exc).__name__, exc)
    return "unknown"
