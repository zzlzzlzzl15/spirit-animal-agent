"""spirit/storage/readpool.py — read-connection budgeting for SessionDB WAL reads.

v0.4.0 (Hermes-aligned): per-file + process-wide permit ceiling +
descriptor-headroom gate.

Reference: hermes_state_readpool.py.

Spirit is much simpler than Hermes: typically only 1-2 read connections
per process. The gate exists so that a misbehaving caller cannot exhaust
file descriptors.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Process-wide defaults (Hermes values).
READ_POOL_MAX = 4
FD_SOFT_LIMIT_FALLBACK = 256  # when psutil / ulimit unavailable
FD_HEADROOM_MIN = 32  # reserve at least this many fds for other use


# Targets to count toward fd budget (best-effort).
def _proc_fd_targets() -> int:
    """Count of fd-like resources held by this process (best-effort).

    On POSIX, walks /proc/self/fd. On Windows, returns -1 (caller should
    fall back to the soft limit).
    """
    if os.name != "nt":
        try:
            return len(os.listdir(f"/proc/{os.getpid()}/fd"))
        except OSError:
            return -1
    # Windows: psutil or fall back
    try:
        import psutil  # type: ignore

        p = psutil.Process(os.getpid())
        return len(p.open_files()) + 50  # heuristic offset for sockets etc.
    except ImportError:
        return -1


def _open_fd_count() -> int:
    """Return current fd count, or -1 if unknown."""
    return _proc_fd_targets()


def _fd_soft_limit() -> int:
    """Return the soft fd limit (POSIX: RLIMIT_NOFILE; Windows: fallback)."""
    if os.name == "nt":
        return FD_SOFT_LIMIT_FALLBACK
    try:
        import resource

        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        return soft
    except (ImportError, OSError, ValueError):
        return FD_SOFT_LIMIT_FALLBACK


def _fd_headroom_ok() -> bool:
    """True if we have at least FD_HEADROOM_MIN fds available."""
    soft = _fd_soft_limit()
    used = _open_fd_count()
    if used < 0:
        # Unknown — be optimistic but cap by soft limit
        return True
    return (soft - used) >= FD_HEADROOM_MIN


def _reclaim_idle_read_conn_anywhere() -> int:
    """Best-effort: try to close any idle read connections in the budget.

    Returns the number of connections reclaimed.
    """
    reclaimed = 0
    with _BUDGETS_LOCK:
        for budget in list(_PATH_BUDGETS.values()):
            r = budget.reclaim_idle()
            reclaimed += r
    return reclaimed


def _read_budget_key(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


class _PathReadBudget:
    """Per-file read-connection budget for a single db path."""

    def __init__(self, max_permits: int = READ_POOL_MAX):
        self.max_permits = max_permits
        self.in_use = 0
        self.idle: Dict[str, sqlite3.Connection] = {}
        self._lock = threading.Lock()

    def acquire(self, db_path: Path) -> sqlite3.Connection:
        """Acquire a read connection. Reuses idle, opens new if budget allows."""
        with self._lock:
            if self.idle:
                uri = next(iter(self.idle))
                conn = self.idle.pop(uri)
                self.in_use += 1
                return conn
            if self.in_use >= self.max_permits:
                # Block by reclaiming the oldest idle
                if self.idle:
                    uri = next(iter(self.idle))
                    conn = self.idle.pop(uri)
                    try:
                        conn.close()
                    except Exception:
                        pass
                    self.max_permits -= 1
                else:
                    raise RuntimeError(
                        f"read pool exhausted (in_use={self.in_use}, max={self.max_permits})"
                    )
        if not _fd_headroom_ok():
            raise RuntimeError("fd headroom exhausted; cannot open new read connection")
        # Open outside the lock
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro",
            uri=True,
            check_same_thread=False,
            timeout=5.0,
        )
        conn.row_factory = sqlite3.Row
        with self._lock:
            self.in_use += 1
        return conn

    def release(self, conn: sqlite3.Connection) -> None:
        """Return a read connection to the idle pool."""
        with self._lock:
            self.in_use -= 1
            uri = str(id(conn))
            self.idle[uri] = conn

    def reclaim_idle(self) -> int:
        """Close idle connections; returns count closed."""
        with self._lock:
            n = len(self.idle)
            for conn in self.idle.values():
                try:
                    conn.close()
                except Exception:
                    pass
            self.idle.clear()
            return n


_PATH_BUDGETS: Dict[str, _PathReadBudget] = {}
_BUDGETS_LOCK = threading.Lock()


def _read_budget_for(path: Path) -> _PathReadBudget:
    """Get-or-create the per-path read budget."""
    key = _read_budget_key(path)
    with _BUDGETS_LOCK:
        budget = _PATH_BUDGETS.get(key)
        if budget is None:
            budget = _PathReadBudget()
            _PATH_BUDGETS[key] = budget
        return budget


def acquire_read_conn(db_path: Path) -> sqlite3.Connection:
    """Open a read-only connection to db_path, gated by the per-path budget."""
    return _read_budget_for(db_path).acquire(db_path)


def release_read_conn(db_path: Path, conn: sqlite3.Connection) -> None:
    """Return a read connection to the budget's idle pool."""
    _read_budget_for(db_path).release(conn)
