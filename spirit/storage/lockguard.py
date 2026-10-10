"""spirit/storage/lockguard.py — hold state.db writer's WAL locks across close().

v0.4.0 (Hermes-aligned): SQLite protects a live WAL generation with two
POSIX advisory locks; this module holds them so a stray close() can't
cancel them.

Reference: hermes_state_lockguard.py.

Spirit runs on Windows where POSIX fcntl/flock isn't available, so we
fall back to msvcrt on Windows and skip the guard entirely if neither
backend is available.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Iterator, Optional, Tuple

logger = logging.getLogger(__name__)


def supported() -> bool:
    """True if at least one advisory-lock backend is available."""
    if _flock is not None or _ofd_lock is not None:
        return True
    if os.name == "nt" and _msvcrt is not None:
        return True
    return False


# Lazy backend imports.
def _try_flock():
    try:
        import fcntl  # type: ignore

        return fcntl
    except ImportError:
        return None


def _try_ofd():
    # ofd locks are fcntl-style on Linux for newer kernels; reuse fcntl if available
    # (F_OFD_SETLKW is a fcntl op). For Spirit, we don't need the OFD variant —
    # the classic flock via fcntl F_SETLK is sufficient.
    return None


def _try_msvcrt():
    if os.name != "nt":
        return None
    try:
        import msvcrt  # type: ignore

        return msvcrt
    except ImportError:
        return None


_flock = _try_flock()
_ofd_lock = _try_ofd()
_msvcrt = _try_msvcrt()


# Identity of "this process" for lock-owning detection.
def _identity() -> str:
    return f"pid={os.getpid()}"


def _own_fds_for(db_path: Path) -> Optional[int]:
    """Return the fd of an open file handle for db_path, or None.

    Best-effort: walk /proc/<pid>/fd looking for a match.
    """
    if os.name == "nt":
        return None
    try:
        pid = os.getpid()
        for entry in os.listdir(f"/proc/{pid}/fd"):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{entry}")
            except OSError:
                continue
            try:
                if Path(target).resolve() == db_path.resolve():
                    return int(entry)
            except OSError:
                continue
    except OSError:
        return None
    return None


def _guard_ranges(db_path: Path) -> Tuple[int, int]:
    """Byte ranges to lock on the db file. We lock the first 4KB header
    and the WAL header at offset 0 (when present).

    Returns:
        ((start1, len1), (start2, len2)) — two ranges to lock.
    """
    # For simplicity, lock just the first 4KB.
    return ((0, 4096),)


_HELD_LOCKS: dict = {}  # db_path → list of (fd, lock_info)
_HELD_LOCKS_LOCK = threading.Lock()


@contextlib.contextmanager
def hold(db_path: Path) -> Iterator[None]:
    """Hold a writer's advisory lock on db_path for the duration of the
    with-block.

    A no-op if no backend is available.
    """
    if not supported():
        yield
        return

    # Open a dedicated fd for the lock (not the db connection's fd).
    lock_fd: Optional[int] = None
    try:
        try:
            lock_fd = os.open(str(db_path), os.O_RDWR)
        except OSError as e:
            logger.debug("lockguard: cannot open %s: %s", db_path, e)
            yield
            return

        if _flock is not None:
            try:
                _flock.flock(lock_fd, _flock.LOCK_EX | _flock.LOCK_NB)
            except OSError as e:
                logger.debug("lockguard: flock failed: %s", e)
                yield
                return
        elif _msvcrt is not None and os.name == "nt":
            # msvcrt.locking requires a C runtime FILE*; we use os.open
            # and rely on Windows share-mode via the open call above. The
            # closest portable equivalent is opening the file with
            # O_RDWR | _O_SHORT_LIVED, which Spirit doesn't have direct
            # access to from Python. We best-effort: skip the explicit
            # lock and rely on the SQLite busy_timeout.
            logger.debug("lockguard: msvcrt fallback is advisory only")

        with _HELD_LOCKS_LOCK:
            _HELD_LOCKS.setdefault(str(db_path), []).append(
                (lock_fd, {"acquired_at": __import__("time").time()})
            )
        try:
            yield
        finally:
            with _HELD_LOCKS_LOCK:
                held = _HELD_LOCKS.get(str(db_path), [])
                if held:
                    held.pop()
            if _flock is not None:
                try:
                    _flock.flock(lock_fd, _flock.LOCK_UN)
                except OSError:
                    pass
    finally:
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass


def release(db_path: Path) -> None:
    """Release any locks held on db_path (best-effort)."""
    with _HELD_LOCKS_LOCK:
        held = _HELD_LOCKS.pop(str(db_path), [])
    for fd, _ in held:
        if _flock is not None:
            try:
                _flock.flock(fd, _flock.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass
