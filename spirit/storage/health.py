"""spirit/storage/health.py — profile-level session-storage health monitor.

v0.4.0 (Hermes-aligned): per-process latch + structural corruption tracking.
Reference: hermes_state_health.py.

A structurally corrupt state.db used to show up as an empty or partial
session list, with no signal to the user. We now record corruption
events so the UI can surface them.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Sentinel value used as the value for `state_meta` corruption flag.
STORAGE_CORRUPT = "corrupt"

# Process-wide latch dict: path → corruption info
_HEALTH: Dict[str, Dict[str, Any]] = {}
_HEALTH_LOCK = threading.Lock()


def _key(db_path: Path | str) -> str:
    return str(Path(db_path).resolve())


def is_structural_corruption_error(exc: BaseException) -> bool:
    """True if the exception indicates structural (file-level) corruption.

    Distinct from "transient" (lock) or "schema drift" (missing table) —
    structural corruption means the db file itself is unreadable, not
    just out-of-date.
    """
    msg = str(exc).lower()
    return any(
        marker in msg
        for marker in (
            "database disk image is malformed",
            "file is not a database",
            "database is corrupted",
        )
    )


def mark_storage_corrupt(db_path: Path | str, reason: str) -> None:
    """Record a structural corruption event for this db path."""
    with _HEALTH_LOCK:
        _HEALTH[_key(db_path)] = {
            "state": STORAGE_CORRUPT,
            "reason": reason,
            "at": time.time(),
        }
    logger.error("storage marked corrupt: %s — %s", db_path, reason)


def note_storage_error(db_path: Path | str, exc: BaseException) -> None:
    """Record a non-fatal storage error for diagnostics.

    Does NOT flip the state to 'corrupt' — only records the error. The
    classification is left to the caller.
    """
    key = _key(db_path)
    with _HEALTH_LOCK:
        info = _HEALTH.setdefault(key, {"state": "ok", "errors": []})
        info.setdefault("errors", []).append({
            "type": type(exc).__name__,
            "msg": str(exc)[:500],
            "at": time.time(),
        })
        # Cap at last 20 errors to avoid unbounded growth
        info["errors"] = info["errors"][-20:]


def storage_state(db_path: Path | str) -> Dict[str, Any]:
    """Return current health info for this db path.

    Returns:
        {"state": "ok" | "corrupt", "reason": str|None, "errors": [...], "at": float|None}
    """
    key = _key(db_path)
    with _HEALTH_LOCK:
        return dict(_HEALTH.get(key, {"state": "ok", "reason": None, "errors": []}))


def storage_corrupt_reason(db_path: Path | str) -> str | None:
    """Return the corruption reason, or None if not corrupt."""
    return storage_state(db_path).get("reason")


def reset_storage_state(db_path: Path | str) -> None:
    """Clear health info (e.g. after repair)."""
    key = _key(db_path)
    with _HEALTH_LOCK:
        _HEALTH.pop(key, None)
    logger.info("storage health reset: %s", db_path)


def is_corrupt(db_path: Path | str) -> bool:
    """True if the db has been marked corrupt."""
    return storage_state(db_path).get("state") == STORAGE_CORRUPT
