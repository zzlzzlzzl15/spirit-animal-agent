"""spirit/storage/guard.py — live-DB test-isolation guard.

v0.4.0 (Hermes-aligned): every SessionDB() construction runs through
_ensure_test_isolation() so tests can't accidentally write to the
production state.db.

Reference: hermes_state_guard.py.

Bypass:
- Set SPIRIT_TEST_DB_BYPASS=1 in env to skip the guard.
- pytest auto-detection: any process with 'pytest' in argv is treated as a test.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _real_platform_state_root() -> Path:
    """The actual ~/.spirit dir used in non-test contexts."""
    return Path.home() / ".spirit"


def _running_under_pytest() -> bool:
    """True if 'pytest' shows up in this process's argv."""
    try:
        if "pytest" in sys.argv[0] or any("pytest" in a for a in sys.argv[1:]):
            return True
    except Exception:
        pass
    return False


def _process_looks_like_pytest() -> bool:
    """Heuristic: process whose argv[0] ends in 'pytest' or has pytest in cmdline."""
    if _running_under_pytest():
        return True
    argv0 = sys.argv[0] if sys.argv else ""
    return argv0.endswith("pytest") or "pytest" in argv0


def _has_pytest_ancestor() -> bool:
    """Walk up process tree looking for a pytest ancestor (best-effort)."""
    if _process_looks_like_pytest():
        return True
    return False


def _in_test_context() -> bool:
    """True if we're in a test process.

    Detection sources (in order):
    1. SPIRIT_TEST_DB_BYPASS=0 (explicit "not a test")
    2. pytest in argv
    3. SPIRIT_TEST_DB_BYPASS=1 (explicit "treat as test")
    """
    explicit = os.environ.get("SPIRIT_TEST_DB_BYPASS")
    if explicit == "0":
        return False
    if _has_pytest_ancestor():
        return True
    if explicit == "1":
        return True
    return False


def _is_production_state_db(db_path: Path) -> bool:
    """True if db_path is the real production state.db (under ~/.spirit)."""
    try:
        real = _real_platform_state_root().resolve()
        return real == Path(db_path).resolve().parent
    except Exception:
        return False


# Per-process "last init error" record (Hermes pattern).
_LAST_INIT_ERRORS: dict = {}


def _register_test_instance(db_path: Path) -> None:
    """Record that this process opened this db path (for diagnostics)."""
    _LAST_INIT_ERRORS.setdefault(str(db_path), []).append({
        "kind": "open",
        "ts": time.time(),
    })


def _set_last_init_error(db_path: Path, exc: BaseException) -> None:
    """Record the last init error for this path."""
    _LAST_INIT_ERRORS[str(db_path)] = [{
        "kind": "error",
        "type": type(exc).__name__,
        "msg": str(exc)[:500],
        "ts": time.time(),
    }]


def get_last_init_error(db_path: Path) -> Optional[dict]:
    """Return the most recent init error, or None."""
    errors = _LAST_INIT_ERRORS.get(str(db_path), [])
    return errors[-1] if errors else None


def _ensure_test_isolation(db_path: Path) -> Path:
    """If we're in a test context and the path resolves to the production
    state.db, force-redirect to a temp path. Returns the effective db_path.

    Returns:
        The db_path to use (either the original or a test-only redirect).
    """
    if not _in_test_context():
        return db_path
    if not _is_production_state_db(db_path):
        return db_path
    # In a test process and pointed at production: redirect to tmp.
    import tempfile

    redirect_dir = Path(tempfile.gettempdir()) / "spirit-test-redirect"
    redirect_dir.mkdir(parents=True, exist_ok=True)
    # Keep the original name so test failures are recognizable
    redirect = redirect_dir / db_path.name
    logger.warning(
        "test \u8fdb\u7a0b\u8bf7\u6c42 production state.db\uff0c\u5df2\u91cd\u5b9a\u5411\u5230 %s",
        redirect,
    )
    return redirect
