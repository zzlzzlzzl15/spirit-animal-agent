"""spirit/storage/lockowners.py — who holds the SQLite write lock on state.db.

v0.4.0 (Hermes-aligned): on Linux, parse /proc/locks to identify the
process holding the state.db write lock. Best-effort on Windows.

Reference: hermes_state_lockowners.py.

The "database is locked for over Ns" failure names the victim but not
the culprit. This module fills in the missing context.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class LockHolder:
    """A process holding a lock on the db file."""

    pid: int
    lock_type: str  # 'FLOCK' / 'OFDLCK' / 'POSIX' / 'MS-DOS' / 'UNKNOWN'
    mode: str  # 'READ' / 'WRITE' / 'UNK'
    start: int
    end: int
    description: str

    def __str__(self) -> str:  # pragma: no cover
        return (
            f"pid={self.pid} {self.lock_type}/{self.mode} "
            f"[{self.start}-{self.end}] {self.description!r}"
        )


def _describe_range(start: int, end: int) -> str:
    if end - start == 0:
        return f"@{start}"
    return f"@{start}+{end - start}"


def parse_proc_locks() -> List[LockHolder]:
    """Parse /proc/locks (POSIX only). Returns empty list on Windows or failure."""
    if os.name == "nt":
        return []
    try:
        text = Path("/proc/locks").read_text(encoding="ascii", errors="ignore")
    except (OSError, FileNotFoundError):
        return []
    holders: List[LockHolder] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            # Format: : POSIX  ADVISORY  READ  12345 00:12:34567 0 100
            # or:      : FLOCK  ADVISORY  WRITE 12345 ...
            # Position: [0]=':' [1]=TYPE [2]=KIND [3]=MODE [4]=PID ...
            lock_type = parts[1]
            kind = parts[2]
            mode = parts[3]
            pid = int(parts[4])
            # 5 and 6 are MAJOR:MINOR:INODE
            start = int(parts[6]) if len(parts) > 6 else 0
            end = int(parts[7]) if len(parts) > 7 else start
            description = " ".join(parts[8:]) if len(parts) > 8 else ""
            holders.append(
                LockHolder(
                    pid=pid,
                    lock_type=lock_type,
                    mode=mode,
                    start=start,
                    end=end,
                    description=description,
                )
            )
        except (ValueError, IndexError):
            continue
    return holders


def _inode_for(db_path: Path) -> Optional[int]:
    try:
        return db_path.stat().st_ino
    except OSError:
        return None


def state_db_write_lock_holders(db_path: Path) -> List[LockHolder]:
    """Return processes currently holding a write lock on db_path.

    Best-effort: matches by inode on Linux. Empty list on Windows.
    """
    inode = _inode_for(db_path)
    if inode is None:
        return []
    all_holders = parse_proc_locks()
    out: List[LockHolder] = []
    for h in all_holders:
        # The description in /proc/locks contains "MAJOR:MINOR:INODE".
        # We don't have a stable cross-platform way to parse it, so we
        # match by pid holding the same db file via /proc/<pid>/fd.
        if _pid_has_db_open(h.pid, db_path):
            if h.mode.upper() in ("WRITE", "W"):
                out.append(h)
    return out


def _pid_has_db_open(pid: int, db_path: Path) -> bool:
    """True if pid has db_path open as one of its fds (POSIX)."""
    if os.name == "nt":
        return False
    try:
        fd_dir = Path(f"/proc/{pid}/fd")
        for entry in os.listdir(fd_dir):
            try:
                target = os.readlink(fd_dir / entry)
            except OSError:
                continue
            try:
                if Path(target).resolve() == db_path.resolve():
                    return True
            except OSError:
                continue
    except OSError:
        return False
    return False


def log_write_lock_holders(db_path: Path) -> None:
    """Log the current write-lock holders for db_path (for diagnostics)."""
    holders = state_db_write_lock_holders(db_path)
    if not holders:
        logger.info("no write-lock holders detected for %s", db_path)
        return
    for h in holders:
        logger.warning("write-lock holder on %s: %s", db_path, h)
