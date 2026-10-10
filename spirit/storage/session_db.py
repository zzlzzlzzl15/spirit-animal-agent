"""Backward-compatible facade for the Spirit storage package.

v0.3.0 (Hermes-style migration): `SessionDB` and related utilities
have moved to `spirit/storage/` (a multi-module package). This file
is kept as a thin shim so existing imports continue to work:

    from spirit.storage.session_db import SessionDB  # still works
    from spirit.storage import SessionDB             # also works

The old monolithic `session_db.py` (381 lines) has been split into
focused modules:

- schema.py            — full Hermes-style schema (7 tables)
- dbfile.py            — connection management + PRAGMAs
- sessions.py          — SessionSessionsMixin (CRUD + title + counters)
- messages.py          — SessionMessagesMixin (append / batch / search)
- usage.py             — SessionUsageMixin (token aggregation)
- state_meta.py        — StateMetaMixin (KV store)
- session_persistence.py — SessionPersistenceMixin (batch flush + marker)
- titles.py            — auto session title generation helpers
- errors.py            — error types + classify_persistence_error
"""

from __future__ import annotations

# Re-export everything from the new package location.
from . import (  # noqa: F401  (re-export)
    DEFAULT_DB_PATH,
    CURRENT_SCHEMA_VERSION,
    MAX_TITLE_LEN,
    SessionDB,
    _DB_PERSISTED_MARKER,
    generate_title_sync,
    heuristic_title,
    is_persisted,
    mark_persisted,
    maybe_update_title,
)
from .errors import (  # noqa: F401
    StateDbCorruptError,
    StateDbError,
    StateDbLockedError,
    StateDbReplacedError,
    classify_persistence_error,
)

__all__ = [
    "SessionDB",
    "DEFAULT_DB_PATH",
    "CURRENT_SCHEMA_VERSION",
    "MAX_TITLE_LEN",
    "_DB_PERSISTED_MARKER",
    "StateDbError",
    "StateDbCorruptError",
    "StateDbLockedError",
    "StateDbReplacedError",
    "classify_persistence_error",
    "mark_persisted",
    "is_persisted",
    "generate_title_sync",
    "heuristic_title",
    "maybe_update_title",
]
