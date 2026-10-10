"""spirit/storage/registry.py — process-wide shared SessionDB registry.

v0.4.0 (Hermes-aligned): coordinates fd / teardown / generation tokens
across multiple SessionDB instances in the same process.

Reference: hermes_state_registry.py.

Note: Spirit is single-process per session (CLI / TUI / Electron each have
their own SessionDB instance). The registry is here for the cases where
multiple subsystems want to share a single connection (e.g. memory
provider + storage in the same process).
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Per-path teardown barrier: ensures orderly close before another
# SessionDB can take the path.
class _TeardownBarrier:
    """Synchronizes teardown so we don't open a new connection while
    the old one's atexit close is still running.

    Each path can have at most one in-flight teardown; subsequent opens
    wait on the barrier before proceeding.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self._cond = threading.Condition(self._lock)

    def admit(self) -> None:
        """Block until any in-flight teardown for this path is done."""
        with self._cond:
            while self._busy:
                self._cond.wait()

    def begin_teardown(self) -> None:
        """Mark this path as being torn down."""
        with self._cond:
            self._busy = True

    def end_teardown(self) -> None:
        """Mark teardown done; admit waiting openers."""
        with self._cond:
            self._busy = False
            self._cond.notify_all()


class _Generation:
    """A monotonic generation token for a db path.

    Bumped on every force-reopen. Used by cache layers to detect
    invalidation: a stale read with a higher generation means
    "the writer has rotated; refresh".
    """

    def __init__(self, value: int = 0) -> None:
        self.value = value
        self._lock = threading.Lock()

    def bump(self) -> int:
        with self._lock:
            self.value += 1
            return self.value

    def current(self) -> int:
        return self.value


# Process-wide state.
_PATH_BARRIERS: Dict[str, _TeardownBarrier] = {}
_PATH_GENERATIONS: Dict[str, _Generation] = {}
_INSTANCES: "weakref.WeakValueDictionary[str, object]" = weakref.WeakValueDictionary()
# Use RLock so calls that already hold the lock (bump_generation,
# _path_lifecycle_lock_locked callers, etc.) can re-enter via _generation_for.
_REGISTRY_LOCK = threading.RLock()
_TEARDOWN_REGISTERED = False


def _key_for(db_path: Path) -> str:
    try:
        return str(Path(db_path).resolve())
    except Exception:
        return str(db_path)


def _path_lifecycle_lock_locked(db_path: Path) -> _TeardownBarrier:
    """Get-or-create the per-path teardown barrier. Caller holds _REGISTRY_LOCK."""
    key = _key_for(db_path)
    barrier = _PATH_BARRIERS.get(key)
    if barrier is None:
        barrier = _TeardownBarrier()
        _PATH_BARRIERS[key] = barrier
    return barrier


def _generation_for(db_path: Path) -> _Generation:
    with _REGISTRY_LOCK:
        key = _key_for(db_path)
        gen = _PATH_GENERATIONS.get(key)
        if gen is None:
            gen = _Generation()
            _PATH_GENERATIONS[key] = gen
        # Always ensure a barrier exists for this path so describe_registry()
        # can see both barriers and generations consistently.
        if key not in _PATH_BARRIERS:
            _PATH_BARRIERS[key] = _TeardownBarrier()
        return gen


def current_generation(db_path: Path) -> int:
    """Return the current generation token for this path."""
    with _REGISTRY_LOCK:
        return _generation_for(db_path).current()


def bump_generation(db_path: Path) -> int:
    """Bump the generation token; cache layers should invalidate."""
    with _REGISTRY_LOCK:
        return _generation_for(db_path).bump()


@contextmanager
def _path_admit(db_path: Path) -> Iterator[None]:
    """Context manager: admit (wait) before opening; release on exit."""
    key = _key_for(db_path)
    with _REGISTRY_LOCK:
        barrier = _path_lifecycle_lock_locked(db_path)
    barrier.admit()
    try:
        yield
    finally:
        # Nothing to release here — close() handles the teardown barrier.
        pass


def _register_instance(db_path: Path, instance) -> None:
    """Register a SessionDB instance for process-wide coordination."""
    global _TEARDOWN_REGISTERED
    with _REGISTRY_LOCK:
        _INSTANCES[_key_for(db_path)] = instance
    if not _TEARDOWN_REGISTERED:
        atexit.register(_atexit_teardown_all)
        _TEARDOWN_REGISTERED = True


def _unregister_instance(db_path: Path, instance) -> None:
    with _REGISTRY_LOCK:
        # WeakValueDictionary auto-removes dead refs; explicit pop is a hint
        key = _key_for(db_path)
        if _INSTANCES.get(key) is instance:
            try:
                del _INSTANCES[key]
            except KeyError:
                pass


def _teardown_generation(db_path: Path) -> None:
    """Mark a path as being torn down; subsequent opens wait."""
    with _REGISTRY_LOCK:
        barrier = _path_lifecycle_lock_locked(db_path)
    barrier.begin_teardown()
    _admit_teardown_locked(db_path)


def _admit_teardown_locked(db_path: Path) -> None:
    """Wait until the live instance is closed, then mark teardown done.

    Called with the per-path barrier in 'begin_teardown' state.
    """
    key = _key_for(db_path)
    # Wait for the instance to be removed from registry (close() does this).
    deadline = time.monotonic() + 10.0  # 10s upper bound on teardown
    while time.monotonic() < deadline:
        with _REGISTRY_LOCK:
            if key not in _INSTANCES:
                break
        time.sleep(0.01)
    # Bump generation so any readers know to refresh.
    bump_generation(db_path)
    # Release the barrier.
    with _REGISTRY_LOCK:
        barrier = _path_lifecycle_lock_locked(db_path)
    barrier.end_teardown()


def _teardown(db_path: Optional[Path] = None) -> None:
    """Tear down the registered instance for a path, or all paths."""
    if db_path is not None:
        key = _key_for(db_path)
        with _REGISTRY_LOCK:
            inst = _INSTANCES.get(key)
        if inst is not None and hasattr(inst, "close"):
            try:
                inst.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("teardown close failed: %s", e)
        return
    # Tear down all registered paths
    with _REGISTRY_LOCK:
        paths = list(_INSTANCES.keys())
    for key in paths:
        try:
            _teardown(Path(key))
        except Exception:  # noqa: BLE001
            pass


def _atexit_teardown_all() -> None:
    """atexit hook: tear down all instances before interpreter shutdown."""
    try:
        _teardown()
    except Exception as e:  # noqa: BLE001
        logger.debug("atexit teardown failed: %s", e)


def _db_path_of(instance) -> Optional[Path]:
    """Return the db path of a registered SessionDB instance."""
    if hasattr(instance, "db_path"):
        try:
            return Path(instance.db_path)
        except Exception:
            return None
    return None


def _open_session_db(db_path: Path, *, db_factory=None):
    """Open a SessionDB on this path, coordinating with the registry.

    If another process is mid-teardown, this blocks until that finishes.
    The returned instance is auto-registered for atexit teardown.

    Args:
        db_path: path to the SQLite file.
        db_factory: optional callable to construct the SessionDB; defaults
            to ``spirit.storage.SessionDB``.
    """
    from . import SessionDB  # local import to avoid cycle

    factory = db_factory or SessionDB

    with _path_admit(db_path):
        inst = factory(db_path=db_path)
        _register_instance(db_path, inst)
        return inst


def list_open_paths() -> List[str]:
    """Return paths of currently registered SessionDB instances."""
    with _REGISTRY_LOCK:
        return list(_INSTANCES.keys())


def describe_registry() -> dict:
    """Return a snapshot of registry state (for diagnostics)."""
    with _REGISTRY_LOCK:
        return {
            "open_paths": list(_INSTANCES.keys()),
            "barriers": list(_PATH_BARRIERS.keys()),
            "generations": {k: v.current() for k, v in _PATH_GENERATIONS.items()},
        }
