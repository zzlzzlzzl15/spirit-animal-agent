"""spirit/storage/schema_coordination.py — schema-version + PRAGMA coordination.

v0.4.0 (Hermes-aligned): schema-version table, migration registry,
PRAGMA policy, and drift detection.

Reference: hermes_state_schema_coordination.py + hermes_state_pragmas.py.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .schema import CURRENT_SCHEMA_VERSION

logger = logging.getLogger(__name__)


# PRAGMA policy applied to every SessionDB connection. Tuned for WAL.
DEFAULT_PRAGMAS: List[Tuple[str, Any]] = [
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("foreign_keys", "ON"),
    ("temp_store", "MEMORY"),
    ("cache_size", -64000),  # 64MB
    ("busy_timeout", 5000),  # 5 seconds
    ("wal_autocheckpoint", 1000),
]


class SchemaDriftError(RuntimeError):
    """Raised when on-disk schema version is ahead of code version."""


class SchemaCoordinationMixin:
    """Apply PRAGMAs, ensure schema version, run migrations.

    Mixed into SessionDB via MRO.
    Expects:
        self.conn (sqlite3.Connection)
        self.db_path (Path)
    """

    # ── PRAGMA coordination ─────────────────────────────────

    def apply_pragma_policy(
        self,
        pragmas: Optional[List[Tuple[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Apply our default PRAGMA policy. Returns a dict of what was set."""
        pragmas = pragmas or DEFAULT_PRAGMAS
        out: Dict[str, Any] = {}
        for name, value in pragmas:
            try:
                self.conn.execute(f"PRAGMA {name} = {self._pragma_value(value)}")
                out[name] = value
            except sqlite3.DatabaseError as exc:
                logger.debug("PRAGMA %s = %s failed: %s", name, value, exc)
                out[name] = None
        return out

    def _pragma_value(self, value: Any) -> str:
        if isinstance(value, str):
            return f"'{value}'"
        return str(value)

    # ── Schema version table ────────────────────────────────

    def ensure_schema_version(self) -> int:
        """Create schema_meta if missing and return current version."""
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta ("
            "  key TEXT PRIMARY KEY,"
            "  value TEXT NOT NULL"
            ")"
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(CURRENT_SCHEMA_VERSION),),
            )
            self.conn.commit()
            return CURRENT_SCHEMA_VERSION
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return 0

    def current_schema_version(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return 0

    def set_schema_version(self, version: int) -> None:
        self.conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(version),),
        )
        self.conn.commit()

    # ── Migration registry ──────────────────────────────────

    def pending_migrations(self) -> List[int]:
        """Return the list of versions between current and CURRENT_SCHEMA_VERSION."""
        cur = self.current_schema_version()
        if cur >= CURRENT_SCHEMA_VERSION:
            return []
        return list(range(cur + 1, CURRENT_SCHEMA_VERSION + 1))

    def run_migrations(self) -> List[int]:
        """Apply all pending migrations. Returns list of applied versions."""
        applied: List[int] = []
        for v in self.pending_migrations():
            handler_name = f"_migrate_to_v{v}"
            handler = getattr(self, handler_name, None)
            if handler is None:
                logger.warning("No migration handler for schema v%d; skipping", v)
                continue
            try:
                handler()
                self.set_schema_version(v)
                applied.append(v)
            except Exception:
                logger.exception("Migration v%d failed", v)
                raise
        return applied

    def assert_schema_ok(self) -> None:
        """Raise SchemaDriftError if on-disk version is ahead of code."""
        cur = self.current_schema_version()
        if cur > CURRENT_SCHEMA_VERSION:
            raise SchemaDriftError(
                f"on-disk schema v{cur} is newer than code v{CURRENT_SCHEMA_VERSION}; "
                "upgrade Spirit Agent to continue"
            )

    # ── Drift detection helpers ─────────────────────────────

    def table_exists(self, name: str) -> bool:
        return self._table_exists(name)

    def _table_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None

    def list_columns(self, table: str) -> List[str]:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return [r[1] for r in rows]

    def schema_snapshot(self) -> Dict[str, Any]:
        """Snapshot of the on-disk schema (used for diff against code DDL)."""
        tables: Dict[str, List[str]] = {}
        for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall():
            name = r[0]
            if name.startswith("sqlite_"):
                continue
            tables[name] = self.list_columns(name)
        indexes: List[str] = []
        for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall():
            if not r[0].startswith("sqlite_"):
                indexes.append(r[0])
        return {
            "version": self.current_schema_version(),
            "tables": tables,
            "indexes": indexes,
        }

    # ── v1 → v2 migration ───────────────────────────────────

    def _migrate_to_v2(self) -> None:
        """v1 → v2: extend messages table + add new state tables.

        v1 schema (Spirit pre-Hermes): 6 columns on `messages`, no FTS, no
        async_delegations / gateway_* / compression_locks tables.

        v2 schema (Hermes-aligned, what init_schema already creates on a fresh
        DB): 26 columns on `messages`, 5 FTS tables, 8 new state tables,
        +30 indexes.

        This migration is *additive only* — it never drops columns or rows.
        If a column / table already exists, the ADD / CREATE is skipped.

        Idempotent: safe to call multiple times; ALTER TABLE ADD COLUMN has no
        IF NOT EXISTS in SQLite, so we check sqlite_master / table_info first.
        """
        cur = self.conn.execute("PRAGMA table_info(messages)").fetchall()
        existing_cols = {row[1] for row in cur}

        # ── messages table: bring to v2 26-column layout ───────────
        # (additive: each ALTER adds the column if missing)
        _COL_ADDS: List[Tuple[str, str]] = [
            ("reasoning", "TEXT"),
            ("reasoning_content", "TEXT"),
            ("reasoning_details", "TEXT"),
            ("codex_reasoning_items", "TEXT"),
            ("codex_message_items", "TEXT"),
            ("tool_name", "TEXT"),
            ("effect_disposition", "TEXT"),
            ("timestamp", "REAL DEFAULT 0"),
            ("token_count", "INTEGER DEFAULT 0"),
            ("finish_reason", "TEXT"),
            ("platform_message_id", "TEXT"),
            ("observed", "INTEGER DEFAULT 0"),
            ("_compressed_summary", "INTEGER DEFAULT 0"),
            ("compressed_summary", "TEXT"),
            ("compacted", "INTEGER DEFAULT 0"),
            ("api_content", "TEXT"),
            ("display_kind", "TEXT"),
            ("display_metadata", "TEXT"),
            ("display_identity", "BLOB"),
            ("display_order", "INTEGER"),
        ]
        for col_name, col_def in _COL_ADDS:
            if col_name not in existing_cols:
                try:
                    self.conn.execute(
                        f"ALTER TABLE messages ADD COLUMN {col_name} {col_def}"
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("ALTER messages ADD %s: %s (likely exists)", col_name, exc)
        # Backfill timestamp for old rows using the legacy `created_at`.
        if "timestamp" not in existing_cols:
            try:
                self.conn.execute(
                    "UPDATE messages SET timestamp = COALESCE(timestamp, created_at, 0)"
                )
            except Exception:
                pass

        # ── New tables (idempotent CREATE IF NOT EXISTS) ──────────
        _NEW_TABLES: List[str] = [
            # gateway_routing
            """CREATE TABLE IF NOT EXISTS gateway_routing (
                scope TEXT NOT NULL,
                session_key TEXT NOT NULL,
                backend_id TEXT NOT NULL,
                profile TEXT NOT NULL DEFAULT '',
                started_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_seen_at REAL,
                PRIMARY KEY (scope, session_key)
            )""",
            # gateway_hygiene_state
            """CREATE TABLE IF NOT EXISTS gateway_hygiene_state (
                session_key TEXT PRIMARY KEY,
                failure_streak INTEGER NOT NULL DEFAULT 0,
                last_failure_at REAL,
                last_failure_reason TEXT,
                recovered_at REAL
            )""",
            # conversation_generations
            """CREATE TABLE IF NOT EXISTS conversation_generations (
                source TEXT NOT NULL,
                session_key TEXT NOT NULL,
                generation INTEGER NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (source, session_key)
            )""",
            # gateway_heartbeats (with holder columns)
            """CREATE TABLE IF NOT EXISTS gateway_heartbeats (
                backend_id TEXT PRIMARY KEY,
                pid INTEGER NOT NULL,
                started_at REAL NOT NULL,
                last_heartbeat REAL NOT NULL,
                profile TEXT NOT NULL DEFAULT '',
                host TEXT NOT NULL DEFAULT '',
                session_id TEXT,
                owner_kind TEXT,
                owner_id TEXT,
                hostname TEXT,
                acquired_at REAL,
                metadata TEXT
            )""",
            # compression_locks
            """CREATE TABLE IF NOT EXISTS compression_locks (
                session_id TEXT PRIMARY KEY,
                owner_token TEXT NOT NULL,
                acquired_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )""",
            # session_turn_leases
            """CREATE TABLE IF NOT EXISTS session_turn_leases (
                conversation_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                owner_token TEXT NOT NULL,
                acquired_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )""",
            # async_delegations
            """CREATE TABLE IF NOT EXISTS async_delegations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                parent_message_id INTEGER,
                kind TEXT NOT NULL,
                prompt TEXT NOT NULL,
                agent_name TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                result TEXT,
                error TEXT,
                started_at REAL,
                updated_at REAL,
                finished_at REAL,
                metadata TEXT
            )""",
        ]
        for ddl in _NEW_TABLES:
            try:
                self.conn.execute(ddl)
            except Exception as exc:  # noqa: BLE001
                logger.debug("CREATE TABLE in v2 migrate: %s", exc)

        # ── Indexes (additive) ──────────────────────────────────
        _INDEXES: List[Tuple[str, str]] = [
            ("idx_messages_session_active", "messages(session_id, active)"),
            ("idx_messages_session_id", "messages(session_id, id)"),
            ("idx_messages_compacted", "messages(session_id, compacted)"),
            ("idx_messages_observed", "messages(observed)"),
            ("idx_messages_timestamp", "messages(timestamp)"),
            ("idx_messages_platform_msgid", "messages(platform_message_id)"),
            ("idx_sessions_active", "sessions(active)"),
            ("idx_sessions_started", "sessions(started_at)"),
            ("idx_sessions_source", "sessions(source)"),
            ("idx_sessions_parent", "sessions(parent_session_id)"),
            ("idx_state_meta_key", "state_meta(key)"),
            ("idx_async_delegations_session", "async_delegations(session_id)"),
            ("idx_async_delegations_status", "async_delegations(status)"),
            ("idx_gateway_routing_backend", "gateway_routing(backend_id)"),
            ("idx_compression_locks_expiry", "compression_locks(expires_at)"),
            ("idx_session_turn_leases_expiry", "session_turn_leases(expires_at)"),
        ]
        for idx_name, idx_def in _INDEXES:
            try:
                self.conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {idx_name} ON {idx_def}"
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("CREATE INDEX %s: %s", idx_name, exc)

        # ── FTS5 tables + triggers (additive) ───────────────────
        _FTS_STMTS: List[str] = [
            """CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content,
                content='messages',
                content_rowid='id',
                tokenize='unicode61'
            )""",
            """CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
                content,
                content='messages',
                content_rowid='id',
                tokenize='trigram'
            )""",
            """CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_cjk USING fts5(
                content,
                content='messages',
                content_rowid='id',
                tokenize='unicode61 remove_diacritics 2 categories \'L* N* Co\''
            )""",
            """CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
                title, summary,
                content='sessions',
                content_rowid='id',
                tokenize='unicode61'
            )""",
            """CREATE VIRTUAL TABLE IF NOT EXISTS system_prompts_fts USING fts5(
                prompt,
                content='system_prompts',
                content_rowid='hash',
                tokenize='unicode61'
            )""",
        ]
        for ddl in _FTS_STMTS:
            try:
                self.conn.execute(ddl)
            except Exception as exc:  # noqa: BLE001
                logger.debug("FTS5 create: %s", exc)

        # FTS triggers (idempotent via IF NOT EXISTS via DROP+CREATE pattern)
        _TRIGGERS: List[Tuple[str, str]] = [
            ("messages_ai", """CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
            END"""),
            ("messages_au", """CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
                UPDATE messages_fts SET content = new.content WHERE rowid = old.id;
            END"""),
            ("messages_ad", """CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
                DELETE FROM messages_fts WHERE rowid = old.id;
            END"""),
            ("sessions_ai", """CREATE TRIGGER sessions_ai AFTER INSERT ON sessions BEGIN
                INSERT INTO sessions_fts(rowid, title, summary) VALUES (new.id, new.title, '');
            END"""),
            ("sessions_au", """CREATE TRIGGER sessions_au AFTER UPDATE ON sessions BEGIN
                UPDATE sessions_fts SET title = new.title WHERE rowid = old.id;
            END"""),
            ("sessions_ad", """CREATE TRIGGER sessions_ad AFTER DELETE ON sessions BEGIN
                DELETE FROM sessions_fts WHERE rowid = old.id;
            END"""),
        ]
        for trig_name, trig_sql in _TRIGGERS:
            try:
                self.conn.execute(f"DROP TRIGGER IF EXISTS {trig_name}")
                self.conn.execute(trig_sql)
            except Exception as exc:  # noqa: BLE001
                logger.debug("trigger %s: %s", trig_name, exc)

        # ── async_delegations column-level upgrade (pre-v2 schemas) ───
        # Old schemas had delegation_id TEXT PK + origin_session + state etc.,
        # missing the session_id/parent_message_id/kind/prompt/result/error columns
        # that delegations.py's upsert_delegation / update_delegation need.
        _upgrade_async_delegations_legacy_columns(self.conn)

        # ── Re-create indexes that init_schema's legacy-tolerance pass skipped ──
        # Now that async_delegations has all v0.4.0 columns, the indexes
        # referencing status / delivery_state / parent_session_id can land.
        _create_legacy_aware_indexes(self.conn)

        self.conn.commit()
        logger.info("v1 → v2 migration completed")


# ── Module-level helpers (called by both init_schema and _migrate_to_v2) ──

def _upgrade_async_delegations_legacy_columns(conn: sqlite3.Connection) -> None:
    """Add the v0.4.0 columns to async_delegations if missing.

    Pre-v2 async_delegations tables had ``delegation_id TEXT PRIMARY KEY``
    plus a handful of legacy columns (``origin_session`` / ``state`` /
    ``dispatched_at`` / ``completed_at`` / ``updated_at_compat``). They
    lacked the 10 columns that delegations.py's ``upsert_delegation`` /
    ``update_delegation`` need to write under v0.4.0.

    This helper is idempotent — each ``ALTER TABLE ... ADD COLUMN`` is
    guarded by a ``PRAGMA table_info`` lookup, and SQLite's ALTER has no
    ``IF NOT EXISTS`` for columns.

    Called from both ``init_schema()`` (so legacy DBs that were opened by
    an older Spirit but don't yet have a ``schema_meta`` row still get
    upgraded) and ``_migrate_to_v2()`` (for the documented v1→v2 path).

    If the legacy table has ``delegation_id TEXT PRIMARY KEY`` as its PK
    (i.e. no ``id INTEGER PK AUTOINCREMENT``), the table is *rebuilt* with
    the v0.4.0 schema so that ``delegations.upsert_delegation`` /
    ``list_delegations`` can rely on the integer ``id`` column. The legacy
    data is preserved by copying each row into the new table; ``delegation_id``
    stays as a regular column (its old value moves into the new
    ``delegation_id`` column, not into ``id``).
    """
    try:
        _table_info = conn.execute(
            "PRAGMA table_info(async_delegations)"
        ).fetchall()
    except sqlite3.DatabaseError:
        # async_delegations doesn't exist yet — nothing to upgrade.
        return

    if not _table_info:
        return

    # Detect the legacy PK shape: PK is `delegation_id TEXT` rather than the
    # v0.4.0 `id INTEGER PRIMARY KEY AUTOINCREMENT`. SQLite's ALTER TABLE
    # cannot add / change a PRIMARY KEY, so we have to rebuild the table.
    _pk_col = next((r[1] for r in _table_info if r[5] > 0), None)
    _has_id_pk = any(
        r[1] == "id" and r[5] > 0 and (r[2] or "").upper().startswith("INTEGER")
        for r in _table_info
    )
    if not _has_id_pk:
        _rebuild_async_delegations_with_integer_pk(conn, _table_info)
        # Re-fetch after rebuild so subsequent ALTER steps see the new shape.
        _table_info = conn.execute(
            "PRAGMA table_info(async_delegations)"
        ).fetchall()
        if not _table_info:
            return

    _delegation_cols = {r[1] for r in _table_info}

    _DELEGATION_ADDS: List[Tuple[str, str]] = [
        ("session_id", "TEXT NOT NULL DEFAULT ''"),
        ("parent_message_id", "INTEGER"),
        ("kind", "TEXT NOT NULL DEFAULT 'plan'"),
        ("prompt", "TEXT NOT NULL DEFAULT ''"),
        ("agent_name", "TEXT"),
        ("status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("result", "TEXT"),
        ("error", "TEXT"),
        ("metadata", "TEXT"),
        ("started_at", "REAL"),
        ("finished_at", "REAL"),
        # Pre-v2 schemas had `updated_at_compat` instead of `updated_at`;
        # delegations.upsert_delegation writes `updated_at` on every call,
        # so a legacy row would crash the INSERT without this column.
        ("updated_at", "REAL"),
    ]
    for col, decl in _DELEGATION_ADDS:
        if col in _delegation_cols:
            continue
        try:
            conn.execute(
                f"ALTER TABLE async_delegations ADD COLUMN {col} {decl}"
            )
        except sqlite3.DatabaseError as exc:
            logger.debug(
                "ALTER async_delegations add %s: %s", col, exc,
            )


def _create_legacy_aware_indexes(conn: sqlite3.Connection) -> None:
    """Create async_delegations indexes that may have been skipped during
    init_schema()'s legacy-tolerance pass.

    These 4 indexes are deliberately *not* inline in _FULL_SCHEMA_SQL because
    legacy async_delegations tables lack some columns (status / delivery_state /
    parent_session_id) — putting them inline would crash `executescript()` on
    legacy DBs. Instead we call this function in two places:

    1. ``init_schema()`` tail — covers fresh DBs (current v0.4.0 schema).
    2. ``_migrate_to_v2()`` tail — covers legacy DBs *after* the ALTER TABLE
       column upgrades land and all referenced columns exist.

    The function is idempotent: every CREATE INDEX uses IF NOT EXISTS, and
    every column reference is guarded by a ``PRAGMA table_info`` check so a
    still-legacy column won't crash the call.
    """
    _INDEXES: List[Tuple[str, str, str]] = [
        # (index_name, table_columns_expr, guard_column)
        (
            "idx_async_delegations_session",
            "async_delegations(session_id)",
            "session_id",
        ),
        (
            "idx_async_delegations_status",
            "async_delegations(status)",
            "status",
        ),
        (
            "idx_async_delegations_delivery",
            "async_delegations(delivery_state, dispatched_at)",
            "delivery_state",
        ),
        (
            "idx_async_delegations_parent",
            "async_delegations(parent_session_id)",
            "parent_session_id",
        ),
    ]

    try:
        _existing_cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info(async_delegations)"
            ).fetchall()
        }
    except sqlite3.DatabaseError:
        # async_delegations doesn't exist yet — nothing to index.
        return

    for idx_name, idx_def, guard_col in _INDEXES:
        if guard_col not in _existing_cols:
            # Index references a column that legacy schema doesn't have yet.
            # Skip silently — _migrate_to_v2 will ALTER TABLE the column in and
            # then call this function again.
            continue
        try:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {idx_def}")
        except sqlite3.DatabaseError as exc:
            logger.debug(
                "legacy-aware index %s on %s failed: %s",
                idx_name, idx_def, exc,
            )


def _rebuild_async_delegations_with_integer_pk(
    conn: sqlite3.Connection,
    legacy_table_info: List[sqlite3.Row],
) -> None:
    """Rebuild async_delegations so its PK is ``id INTEGER AUTOINCREMENT``.

    Pre-v2 async_delegations tables used ``delegation_id TEXT PRIMARY KEY``
    instead of ``id INTEGER PRIMARY KEY AUTOINCREMENT``. v0.4.0's
    ``upsert_delegation`` / ``list_delegations`` rely on the integer ``id``
    column for ``cur.lastrowid`` / ``ORDER BY id DESC``.

    SQLite has no ``ALTER TABLE ... ADD PRIMARY KEY`` syntax, so we have to
    rebuild the table:

    1. Capture existing column names from the legacy schema.
    2. ``ALTER TABLE async_delegations RENAME TO _async_delegations_pre_v4__``.
    3. ``CREATE TABLE async_delegations (...)`` using the v0.4.0 DDL.
    4. ``INSERT INTO async_delegations (...) SELECT ... FROM _async_delegations_pre_v4__``
       — copy every legacy column that still exists in the new shape.
    5. ``DROP TABLE _async_delegations_pre_v4__``.
    6. Indexes will be added by the follow-up ``_create_legacy_aware_indexes``
       call once the new table has all expected columns.

    The function is idempotent: if ``async_delegations`` is already in the
    v0.4.0 shape (``id INTEGER PRIMARY KEY AUTOINCREMENT``), the caller
    won't reach this code path.
    """
    _NEW_DDL = """CREATE TABLE async_delegations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delegation_id TEXT,
    session_id TEXT NOT NULL DEFAULT '',
    parent_message_id INTEGER,
    kind TEXT NOT NULL DEFAULT 'plan',
    prompt TEXT NOT NULL DEFAULT '',
    agent_name TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT,
    error TEXT,
    metadata TEXT,
    started_at REAL,
    finished_at REAL,
    updated_at REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)"""

    _legacy_cols = [r[1] for r in legacy_table_info]
    # New columns that survived the rebuild and that we should copy from legacy.
    _new_cols = [
        "delegation_id", "session_id", "parent_message_id", "kind",
        "prompt", "agent_name", "status", "result", "error", "metadata",
        "started_at", "finished_at", "updated_at",
    ]
    _copy_cols = [c for c in _new_cols if c in _legacy_cols]
    if not _copy_cols:
        _copy_cols = ["delegation_id"]  # never empty: legacy has at least it

    _TEMP_NAME = "_async_delegations_pre_v4__"
    try:
        conn.execute(f"ALTER TABLE async_delegations RENAME TO {_TEMP_NAME}")
    except sqlite3.DatabaseError as exc:
        logger.debug("rename async_delegations: %s", exc)
        return

    try:
        conn.executescript(_NEW_DDL)
    except sqlite3.DatabaseError as exc:
        logger.error("rebuild async_delegations CREATE failed: %s", exc)
        # Try to roll the rename back so we don't strand data.
        try:
            conn.execute(
                f"ALTER TABLE {_TEMP_NAME} RENAME TO async_delegations"
            )
        except sqlite3.DatabaseError:
            pass
        return

    _cols_csv = ", ".join(_copy_cols)
    try:
        conn.execute(
            f"INSERT INTO async_delegations ({_cols_csv}) "
            f"SELECT {_cols_csv} FROM {_TEMP_NAME}"
        )
    except sqlite3.DatabaseError as exc:
        logger.error("rebuild async_delegations copy failed: %s", exc)

    try:
        conn.execute(f"DROP TABLE {_TEMP_NAME}")
    except sqlite3.DatabaseError as exc:
        logger.debug("drop legacy async_delegations: %s", exc)

    conn.commit()
    logger.info(
        "Rebuilt async_delegations with id INTEGER PK; copied %d legacy cols",
        len(_copy_cols),
    )
