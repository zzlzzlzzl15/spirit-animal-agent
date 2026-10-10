"""spirit/storage/__init__.py — Spirit storage public API.

v0.4.0 (Hermes-aligned): SessionDB is composed via MRO of 15+ focused
mixins covering the full Hermes feature surface.

Public exports:
- SessionDB: main entry point. Mixin of (in MRO order):
    SchemaCoordinationMixin     — PRAGMA policy + schema version
    SessionPersistenceMixin    — batch flush + persistence marker
    SessionSessionsMixin       — session CRUD + counters
    SessionMessagesMixin       — message append/batch/search
    SessionUsageMixin          — token aggregation
    StateMetaMixin             — KV store
    SessionRewindMixin         — /undo, /retry
    SessionCompressionMixin    — compression locks + turn leases
    SessionGatewayMixin        — gateway routing / heartbeats / handoffs
    SessionTelegramMixin       — telegram topic-mode names
    SessionSearchMixin         — FTS5 + LIKE search
    SessionPortabilityMixin    — export / import / cross-platform
    SessionMaintenanceMixin    — vacuum / checkpoint / prune / FTS rebuild
    SessionTimelineMixin       — chronological timeline view
    ProfileRepairMixin         — corruption recovery
    SessionHoldersMixin        — owner attribution / write leases
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .compression import SessionCompressionMixin
from .dbfile import DEFAULT_DB_PATH, open_connection
from .delegations import SessionDelegationsMixin
from .errors import (
    DiskFullError,
    SessionCompressionInProgressError,
    SessionTurnLeaseLostError,
    StateDbCorruptError,
    StateDbError,
    StateDbLockedError,
    StateDbReplacedError,
    classify_persistence_error,
    is_malformed_db_error,
)
from .gateway import SessionGatewayMixin
from .guard import (
    _ensure_test_isolation,
    _is_production_state_db,
    _register_test_instance,
    _running_under_pytest,
    get_last_init_error,
)
from .health import (
    STORAGE_CORRUPT,
    is_corrupt,
    is_structural_corruption_error,
    mark_storage_corrupt,
    note_storage_error,
    reset_storage_state,
    storage_corrupt_reason,
    storage_state,
)
from .holders import DEFAULT_HOLDER_TTL_S, Holder, SessionHoldersMixin
from .maintenance import MaintenanceResult, SessionMaintenanceMixin
from .messages import SessionMessagesMixin
from .portability import (
    SessionExportError,
    SessionImportError,
    SessionPortabilityMixin,
)
from .profile_repair import ProfileRepairMixin, ProfileRepairResult
from .registry import (
    _Generation,
    _TeardownBarrier,
    _open_session_db,
    _register_instance,
    _unregister_instance,
    bump_generation,
    current_generation,
    describe_registry,
    list_open_paths,
)
from .rewind import (
    RewindOutcome,
    RewindTargetUnavailableError,
    SessionRewindMixin,
)
from .schema import CURRENT_SCHEMA_VERSION, _FULL_SCHEMA_SQL, init_schema
from .schema_coordination import (
    DEFAULT_PRAGMAS,
    SchemaCoordinationMixin,
    SchemaDriftError,
)
from .search import SessionSearchMixin
from .session_persistence import (
    SessionPersistenceMixin,
    _DB_PERSISTED_MARKER,
    is_persisted,
    mark_persisted,
)
from .sessions import SessionSessionsMixin
from .state_meta import StateMetaMixin
from .telegram import SessionTelegramTopicsMixin, _normalize_telegram_topic_profile_name as normalize_telegram_topic_profile_name
# Module-level alias so the SessionDB MRO below can reference the legacy name.
SessionTelegramMixin = SessionTelegramTopicsMixin
from .timeline import SessionTimelineMixin, TimelineEntry
from .titles import (
    MAX_TITLE_LEN,
    generate_title_sync,
    heuristic_title,
    maybe_update_title,
)
from .usage import SessionUsageMixin
from .user_copy import StorageFailure, describe_storage_failure
from .wal import (
    DEFAULT_WAL_SIZE_LIMIT_MB,
    WalUnsupportedError,
    apply_wal_with_fallback,
    is_sqlite_wal_reset_vulnerable,
    quarantine_vulnerable_wal,
    safe_wal_open,
)

logger = logging.getLogger(__name__)


__all__ = [
    # Main entry
    "SessionDB",
    # Schema / connection
    "DEFAULT_DB_PATH",
    "CURRENT_SCHEMA_VERSION",
    "_FULL_SCHEMA_SQL",
    "DEFAULT_PRAGMAS",
    "MAX_TITLE_LEN",
    "WalUnsupportedError",
    "DEFAULT_WAL_SIZE_LIMIT_MB",
    # Persistence marker
    "_DB_PERSISTED_MARKER",
    "mark_persisted",
    "is_persisted",
    # Errors
    "StateDbError",
    "StateDbCorruptError",
    "StateDbLockedError",
    "StateDbReplacedError",
    "DiskFullError",
    "SessionCompressionInProgressError",
    "SessionTurnLeaseLostError",
    "SchemaDriftError",
    "SessionExportError",
    "SessionImportError",
    "RewindTargetUnavailableError",
    "classify_persistence_error",
    "is_malformed_db_error",
    # Health
    "STORAGE_CORRUPT",
    "is_corrupt",
    "is_structural_corruption_error",
    "mark_storage_corrupt",
    "note_storage_error",
    "reset_storage_state",
    "storage_corrupt_reason",
    "storage_state",
    # User copy
    "StorageFailure",
    "describe_storage_failure",
    # Titles
    "generate_title_sync",
    "heuristic_title",
    "maybe_update_title",
    # Telegram
    "normalize_telegram_topic_profile_name",
    # Holders
    "Holder",
    "DEFAULT_HOLDER_TTL_S",
    # Maintenance
    "MaintenanceResult",
    # Timeline
    "TimelineEntry",
    # Profile repair
    "ProfileRepairResult",
    # Rewind
    "RewindOutcome",
    # Registry / guards
    "_Generation",
    "_TeardownBarrier",
    "_open_session_db",
    "_register_instance",
    "_unregister_instance",
    "bump_generation",
    "current_generation",
    "describe_registry",
    "list_open_paths",
    "_ensure_test_isolation",
    "_is_production_state_db",
    "_running_under_pytest",
    "get_last_init_error",
    # WAL
    "apply_wal_with_fallback",
    "is_sqlite_wal_reset_vulnerable",
    "quarantine_vulnerable_wal",
    "safe_wal_open",
]


class SessionDB(
    SchemaCoordinationMixin,
    SessionPersistenceMixin,
    SessionSessionsMixin,
    SessionMessagesMixin,
    SessionUsageMixin,
    StateMetaMixin,
    SessionRewindMixin,
    SessionCompressionMixin,
    SessionGatewayMixin,
    SessionTelegramTopicsMixin,
    SessionSearchMixin,
    SessionPortabilityMixin,
    SessionMaintenanceMixin,
    SessionTimelineMixin,
    ProfileRepairMixin,
    SessionHoldersMixin,
    SessionDelegationsMixin,
):
    """SQLite-backed session/message storage, Hermes-aligned (v0.4.0).

    Composition: this class inherits from 16 focused mixins, each handling
    one concern. The MRO order matters — earlier mixins can call methods
    on later mixins (e.g. SchemaCoordination must come first so it can
    coordinate PRAGMA before persistence opens transactions).

    Args:
        db_path: path to SQLite file (default ~/.spirit/state.db)
    """

    def __init__(self, db_path: Optional[Path] = None):
        # ── Resolve db_path via monkeypatch-aware lookup ─────────
        dbfile_mod = sys.modules["spirit.storage.dbfile"]
        session_db_mod = sys.modules.get("spirit.storage.session_db") or dbfile_mod
        effective_path = (
            db_path
            or session_db_mod.DEFAULT_DB_PATH
            or dbfile_mod.DEFAULT_DB_PATH
        )
        self.db_path = Path(effective_path)

        # ── Test isolation guard (must come BEFORE open_connection) ──
        # If a test process silently tries to open the production state.db,
        # transparently redirect to <tmpdir>/spirit-test-redirect/<name>. The
        # guard is a no-op when:
        #   - not in test context (regular CLI / gateway / Electron)
        #   - db_path is not under ~/.spirit (e.g. tmp_path fixtures)
        #   - SPIRIT_TEST_DB_BYPASS=1 explicitly bypasses
        try:
            self.db_path = _ensure_test_isolation(self.db_path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("_ensure_test_isolation failed; using original path: %s", exc)
        _register_test_instance(self.db_path)

        # ── Open connection ────────────────────────────────
        try:
            self.conn = open_connection(self.db_path, row_factory=True)
        except Exception as exc:
            from .guard import _set_last_init_error

            _set_last_init_error(self.db_path, exc)
            raise

        # ── macOS WAL reset barrier — PRE-init_schema (P3-2 §13.15) ─────
        # 关键顺序：必须在 init_schema 之前 quarantine vulnerable WAL，
        # 否则 init_schema 触发 SQLite WAL write 会覆盖 vulnerable marker。
        try:
            wal_report = safe_wal_open(self.conn, self.db_path)
            if wal_report.get("vulnerable"):
                logger.warning(
                    "SessionDB: macOS WAL vulnerable; quarantined=%s path=%s",
                    wal_report.get("quarantined"),
                    wal_report.get("quarantine_path"),
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("safe_wal_open failed; continuing: %s", exc)

        # ── Init schema ──────────────────────────────
        try:
            self._rebuilt = init_schema(self.conn, db_path=self.db_path)
        except Exception as exc:
            from .guard import _set_last_init_error

            _set_last_init_error(self.db_path, exc)
            raise

        # ── PRAGMA coordination (WAL, synchronous, FK, cache) ───
        self.apply_pragma_policy()

        # ── WAL mode with fallback ───────────────────────────────
        try:
            apply_wal_with_fallback(self.conn)
        except Exception as exc:  # noqa: BLE001
            logger.warning("WAL setup failed; continuing with default journal: %s", exc)

        # ── Schema version + migration ───────────────────────────
        try:
            self.ensure_schema_version()
            self.assert_schema_ok()
            self.run_migrations()
        except SchemaDriftError:
            logger.exception("schema drift; refusing to use this DB")
            raise

        # ── Reclaim stale holders from a previous process ────────
        try:
            reclaimed = self.reclaim_stale_holders()
            if reclaimed:
                logger.info("Reclaimed %d stale holders on open", reclaimed)
        except Exception:  # noqa: BLE001
            logger.debug("reclaim_stale_holders failed; ignoring", exc_info=True)

        # ── Process-wide registry: register for shared coordination ──
        # After this point, _open_session_db() coordination, atexit teardown,
        # and generation tokens all see this instance. If another instance was
        # previously open on the same path, _register_instance will replace it;
        # callers should use _open_session_db() instead of SessionDB() directly
        # to honour the teardown barrier (bumping generation).
        try:
            _register_instance(self.db_path, self)
        except Exception as exc:  # noqa: BLE001
            logger.debug("registry register failed; ignoring: %s", exc)

        logger.debug(
            "SessionDB ready: %s (schema v%d)",
            self.db_path, CURRENT_SCHEMA_VERSION,
        )

    # ── Convenience passthroughs ────────────────────────────────
    def list_recent_sessions(self, limit: int = 5, source: str = None):
        return self.list_sessions(limit=limit, source=source)

    def close(self) -> None:
        # ── macOS WAL checkpoint barrier (P3-2 §13.15) ───────────
        # 在 writer close 时强制 TRUNCATE checkpoint，把 WAL 缩到 0 字节，
        # 避免 macOS 进程崩溃造成 WAL reset 漏洞窗口。
        if self.conn is not None:
            try:
                from .wal import _apply_macos_checkpoint_barrier
                _apply_macos_checkpoint_barrier(self.conn)
            except Exception:  # noqa: BLE001
                logger.debug("macos wal_checkpoint barrier skipped on close", exc_info=True)

        # ── Unregister + bump generation (so any cache layers refresh) ──
        try:
            _unregister_instance(self.db_path, self)
        except Exception:  # noqa: BLE001
            pass
        try:
            bump_generation(self.db_path)
        except Exception:  # noqa: BLE001
            pass

        # ── Close underlying connection ────────────────────
        if self.conn:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
            self.conn = None
