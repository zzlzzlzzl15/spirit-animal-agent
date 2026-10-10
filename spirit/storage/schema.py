"""spirit/storage/schema.py — Spirit storage schema (Hermes-aligned, v0.4.0).

v0.4.0 (Hermes full-alignment):
- Sessions: 38 → 59 columns (added billing, git, title provenance, lifecycle flags)
- Messages: 23 → 26 columns (added display_identity BLOB, display_order, codex_message_items)
- 13 tables total (added: gateway_routing, gateway_hygiene_state, conversation_generations,
  gateway_heartbeats, compression_locks, session_turn_leases, async_delegations)
- 30+ indexes
- 5 FTS5 virtual tables (messages_fts + 3 tokenizer variants + sessions_fts + trigram)
- 6 FTS sync triggers

Schema design follows hermes_state_common.py philosophy:
- WAL mode, single writer, concurrent readers
- Soft delete via `active` + `compacted` overlay
- generation tokens for cache invalidation
- session source tags ('cli', 'telegram', 'gateway', 'tui', 'cron', 'acp')
- parent_session_id chain for compression / branch / resume lineage
- first reason wins via `ended_at IS NULL` guard
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Current schema version. Bump on incompatible changes.
CURRENT_SCHEMA_VERSION = 2  # bumped for v0.4.0 13-table layout

# ── Sessions table (59 columns) ─────────────────────────────────────
# Mirrors Hermes hermes_state_common.py sessions table fully.
_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'cli',
    user_id TEXT,
    session_key TEXT,
    chat_id TEXT,
    chat_type TEXT,
    thread_id TEXT,
    display_name TEXT,
    origin_json TEXT,
    expiry_finalized INTEGER DEFAULT 0,

    -- model & config
    model TEXT NOT NULL DEFAULT '',
    model_config TEXT,
    system_prompt TEXT,
    system_prompt_hash TEXT,

    -- lineage
    parent_session_id TEXT REFERENCES sessions(id),
    predecessor_session_id TEXT,
    branch_label TEXT,
    fork_of TEXT,

    -- timing
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP,
    end_reason TEXT,
    last_touched_at REAL,

    -- counters (denormalized for cheap O(1) reads)
    message_count INTEGER DEFAULT 0,
    user_message_count INTEGER DEFAULT 0,
    assistant_message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,

    -- token cost (USD cents)
    cost_cents INTEGER DEFAULT 0,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,

    -- workspace + git context
    cwd TEXT,
    git_branch TEXT,
    git_repo_root TEXT,
    git_metadata_generation INTEGER NOT NULL DEFAULT 0,

    -- billing
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,

    -- title (auto / user / llm provenance)
    title TEXT,
    title_source TEXT,
    title_updated_at REAL,
    last_preview TEXT,

    -- lifecycle flags
    pinned INTEGER DEFAULT 0,
    hidden INTEGER DEFAULT 0,
    read INTEGER DEFAULT 0,
    is_favorite INTEGER DEFAULT 0,
    is_archived INTEGER DEFAULT 0,
    archived INTEGER DEFAULT 0,

    -- compaction / soft delete
    compact_summary TEXT,
    compacted INTEGER NOT NULL DEFAULT 0,
    compression_count INTEGER DEFAULT 0,
    last_compressed_at TIMESTAMP,
    active INTEGER NOT NULL DEFAULT 1,

    -- turn lease
    turn_lease_holder TEXT,
    turn_lease_expires_at REAL,

    -- context engine
    context_tokens_estimate INTEGER DEFAULT 0,

    -- metadata
    metadata TEXT DEFAULT '{}',
    tags TEXT DEFAULT '[]',
    source_metadata_json TEXT,
    notes TEXT,
    workspace_key TEXT,
    fingerprint TEXT,

    -- debugging
    trace_id TEXT,
    parent_trace_id TEXT
);
"""

# ── Messages table (26 columns) ─────────────────────────────────────
# Aligned with Hermes hermes_state_messages.py _INSERT_MESSAGE_SQL.
_MESSAGES_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,

    -- core
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,

    -- tool metadata
    tool_calls TEXT,
    tool_name TEXT,
    effect_disposition TEXT,

    -- stats
    timestamp REAL,
    token_count INTEGER DEFAULT 0,
    finish_reason TEXT,

    -- reasoning (Hermes-style: 5 dedicated columns for thinking blocks)
    reasoning TEXT,
    reasoning_content TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT,
    codex_message_items TEXT,

    -- replay dedup
    platform_message_id TEXT,
    observed INTEGER DEFAULT 0,

    -- compression marker
    _compressed_summary INTEGER NOT NULL DEFAULT 0,
    compressed_summary TEXT,

    -- soft delete overlay
    active INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0,

    -- raw bytes that hit the API
    api_content TEXT,

    -- display control (4 columns for UI)
    display_kind TEXT,
    display_metadata TEXT,
    display_identity BLOB,
    display_order INTEGER,

    -- audit timestamp
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# ── system_prompts (dedupe by content hash) ─────────────────────────
_SYSTEM_PROMPTS_SQL = """
CREATE TABLE IF NOT EXISTS system_prompts (
    hash TEXT PRIMARY KEY,
    prompt TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# ── session_model_usage (per-session per-model token aggregation) ──
# 18 columns matching Hermes
_SESSION_MODEL_USAGE_SQL = """
CREATE TABLE IF NOT EXISTS session_model_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    billing_provider TEXT NOT NULL DEFAULT '',
    billing_base_url TEXT NOT NULL DEFAULT '',
    billing_mode TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL DEFAULT '',
    api_call_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    actual_cost_usd REAL NOT NULL DEFAULT 0,
    cost_status TEXT,
    cost_source TEXT,
    first_seen REAL,
    last_seen REAL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(session_id, model, billing_provider, billing_mode, task)
);
"""

# ── state_meta (Spirit-specific KV) ────────────────────────────────
_STATE_META_SQL = """
CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# ── schema_version (migration tracking) ─────────────────────────────
_SCHEMA_VERSION_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
"""

# ── gateway_routing (cross-surface routing index) ──────────────────
_GATEWAY_ROUTING_SQL = """
CREATE TABLE IF NOT EXISTS gateway_routing (
    scope TEXT NOT NULL DEFAULT '',
    session_key TEXT NOT NULL,
    entry_json TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (scope, session_key)
);
"""

# ── gateway_hygiene_state (failure streak per session_key) ─────────
_GATEWAY_HYGIENE_SQL = """
CREATE TABLE IF NOT EXISTS gateway_hygiene_state (
    session_key TEXT PRIMARY KEY,
    failure_streak INTEGER NOT NULL DEFAULT 0
);
"""

# ── conversation_generations (cache invalidation generation token) ─
_CONVERSATION_GENERATIONS_SQL = """
CREATE TABLE IF NOT EXISTS conversation_generations (
    source TEXT NOT NULL,
    session_key TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, session_key)
);
"""

# ── gateway_heartbeats (liveness from gateway backends) ─────────────
_GATEWAY_HEARTBEATS_SQL = """
CREATE TABLE IF NOT EXISTS gateway_heartbeats (
    backend_id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    started_at REAL NOT NULL,
    last_heartbeat REAL NOT NULL,
    profile TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT '',
    -- v0.4.0 addition: per-session write-lease columns (for SessionHoldersMixin).
    -- NULL backend_id rows act as session-scoped holders; non-NULL act as
    -- process-level liveness beacons.
    session_id TEXT,
    owner_kind TEXT,
    owner_id TEXT,
    hostname TEXT,
    acquired_at REAL,
    metadata TEXT
);
"""

# ── compression_locks (per-session compression lease) ───────────────
_COMPRESSION_LOCKS_SQL = """
CREATE TABLE IF NOT EXISTS compression_locks (
    session_id TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
"""

# ── session_turn_leases (per-conversation turn lease) ──────────────
_SESSION_TURN_LEASES_SQL = """
CREATE TABLE IF NOT EXISTS session_turn_leases (
    conversation_id TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
"""

# ── async_delegations (delegation queue, Hermes-aligned 20 cols) ───
_ASYNC_DELEGATIONS_SQL = """
CREATE TABLE IF NOT EXISTS async_delegations (
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
    -- v0.4.0 backwards-compat with old v2 rows
    origin_session TEXT NOT NULL DEFAULT '',
    origin_ui_session_id TEXT NOT NULL DEFAULT '',
    parent_session_id TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    dispatched_at REAL,
    completed_at REAL,
    updated_at_compat REAL,
    event_json TEXT,
    result_json TEXT,
    delivery_state TEXT NOT NULL DEFAULT 'pending',
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    delivered_at REAL,
    owner_pid INTEGER,
    owner_started_at INTEGER,
    task_json TEXT,
    delivery_claim TEXT,
    delivery_claimed_at REAL,
    origin_session_id TEXT NOT NULL DEFAULT '',
    parent_session_id_alt TEXT,
    payload TEXT,
    result_payload TEXT,
    child_session_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- async_delegations indexes are created lazily by
-- SchemaCoordinationMixin._create_legacy_aware_indexes() after
-- _migrate_to_v2() has ALTER TABLE'd any missing columns in for legacy dbs.
"""

# ── FTS5 virtual tables (5 tables) ─────────────────────────────────
# messages: 3 tokenizers (unicode61 default, trigram, cjk_unicode61)
# sessions: 2 tokenizers (unicode61, trigram)
_MESSAGES_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
"""

_MESSAGES_FTS_TRIGRAM_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
    content,
    content='messages',
    content_rowid='id',
    tokenize='trigram'
);
"""

_MESSAGES_FTS_CJK_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_cjk USING fts5(
    content,
    content='messages',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
"""

_SESSIONS_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    title,
    last_preview,
    notes,
    content='sessions',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);
"""

_SESSIONS_FTS_TRIGRAM_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts_trigram USING fts5(
    title,
    last_preview,
    content='sessions',
    content_rowid='rowid',
    tokenize='trigram'
);
"""

# ── FTS sync triggers (6) ──────────────────────────────────────────
_FTS_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content)
    VALUES (new.id, COALESCE(new.content, ''));
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES('delete', old.id, COALESCE(old.content, ''));
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES('delete', old.id, COALESCE(old.content, ''));
    INSERT INTO messages_fts(rowid, content)
    VALUES (new.id, COALESCE(new.content, ''));
END;

CREATE TRIGGER IF NOT EXISTS sessions_ai AFTER INSERT ON sessions BEGIN
    INSERT INTO sessions_fts(rowid, title, last_preview, notes)
    VALUES (new.rowid, COALESCE(new.title, ''), COALESCE(new.last_preview, ''), COALESCE(new.notes, ''));
END;

CREATE TRIGGER IF NOT EXISTS sessions_ad AFTER DELETE ON sessions BEGIN
    INSERT INTO sessions_fts(sessions_fts, rowid, title, last_preview, notes)
    VALUES('delete', old.rowid, COALESCE(old.title, ''), COALESCE(old.last_preview, ''), COALESCE(old.notes, ''));
END;

CREATE TRIGGER IF NOT EXISTS sessions_au AFTER UPDATE ON sessions BEGIN
    INSERT INTO sessions_fts(sessions_fts, rowid, title, last_preview, notes)
    VALUES('delete', old.rowid, COALESCE(old.title, ''), COALESCE(old.last_preview, ''), COALESCE(old.notes, ''));
    INSERT INTO sessions_fts(rowid, title, last_preview, notes)
    VALUES (new.rowid, COALESCE(new.title, ''), COALESCE(new.last_preview, ''), COALESCE(new.notes, ''));
END;
"""

# ── Indexes (30+ indexes for Hermes parity) ────────────────────────
_INDEXES_SQL = """
-- Messages indexes
CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_session_id
    ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_session_role
    ON messages(session_id, role);
CREATE INDEX IF NOT EXISTS idx_messages_active
    ON messages(session_id, active);
CREATE INDEX IF NOT EXISTS idx_messages_compressed
    ON messages(session_id, _compressed_summary);
CREATE INDEX IF NOT EXISTS idx_messages_platform_id
    ON messages(platform_message_id);
CREATE INDEX IF NOT EXISTS idx_messages_assistant_calls
    ON messages(session_id, timestamp)
    WHERE role = 'assistant' AND tool_calls IS NOT NULL;

-- Sessions indexes
CREATE INDEX IF NOT EXISTS idx_sessions_source
    ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_source_id
    ON sessions(source, id);
CREATE INDEX IF NOT EXISTS idx_sessions_parent
    ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started
    ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_source_started
    ON sessions(source, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_title_unique
    ON sessions(title) WHERE title IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_active
    ON sessions(active, ended_at);
CREATE INDEX IF NOT EXISTS idx_sessions_archived
    ON sessions(archived);
CREATE INDEX IF NOT EXISTS idx_sessions_workspace
    ON sessions(workspace_key);
CREATE INDEX IF NOT EXISTS idx_sessions_user
    ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_pinned
    ON sessions(pinned);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry
    ON sessions(expiry_finalized, ended_at);
CREATE INDEX IF NOT EXISTS idx_sessions_chat
    ON sessions(chat_id, thread_id);
CREATE INDEX IF NOT EXISTS idx_sessions_fingerprint
    ON sessions(fingerprint);

-- session_model_usage indexes
CREATE INDEX IF NOT EXISTS idx_session_model_usage
    ON session_model_usage(session_id);
CREATE INDEX IF NOT EXISTS idx_session_model_usage_model
    ON session_model_usage(model);

-- compression_locks / turn_leases expire indexes
CREATE INDEX IF NOT EXISTS idx_compression_locks_expires
    ON compression_locks(expires_at);
CREATE INDEX IF NOT EXISTS idx_session_turn_leases_expires
    ON session_turn_leases(expires_at);

-- async_delegations indexes are created lazily by
-- SchemaCoordinationMixin._create_legacy_aware_indexes() after
-- async_delegations gains v0.4.0 columns.

-- gateway_heartbeats cleanup
CREATE INDEX IF NOT EXISTS idx_gateway_heartbeats_heartbeat
    ON gateway_heartbeats(last_heartbeat);

-- conversation_generations
CREATE INDEX IF NOT EXISTS idx_conversation_generations
    ON conversation_generations(source);
"""

# Aggregate schema (order matters — FK dependencies)
_FULL_SCHEMA_SQL = "\n".join(
    [
        _SESSIONS_SQL,
        _SYSTEM_PROMPTS_SQL,
        _MESSAGES_SQL,
        _SESSION_MODEL_USAGE_SQL,
        _STATE_META_SQL,
        _SCHEMA_VERSION_SQL,
        _GATEWAY_ROUTING_SQL,
        _GATEWAY_HYGIENE_SQL,
        _CONVERSATION_GENERATIONS_SQL,
        _GATEWAY_HEARTBEATS_SQL,
        _COMPRESSION_LOCKS_SQL,
        _SESSION_TURN_LEASES_SQL,
        _ASYNC_DELEGATIONS_SQL,
        # FTS
        _MESSAGES_FTS_SQL,
        _MESSAGES_FTS_TRIGRAM_SQL,
        _MESSAGES_FTS_CJK_SQL,
        _SESSIONS_FTS_SQL,
        _SESSIONS_FTS_TRIGRAM_SQL,
        _FTS_TRIGGERS_SQL,
        # Indexes
        _INDEXES_SQL,
    ]
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Check if a table exists (case-insensitive, SQLite is case-insensitive)."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone() is not None


def _detect_legacy_schema(conn: sqlite3.Connection) -> bool:
    """Detect if db has the old (v0 / v0.3.0) Spirit schema.

    v0.3.0: 7 tables including system_prompts, async_delegations
    v0.4.0: 13 tables + 5 FTS + more

    Returns True if db has the legacy schema and should be backed up + rebuilt.
    """
    if not _table_exists(conn, "messages"):
        return False
    cur = conn.execute("PRAGMA table_info(messages)")
    cols = {row[1] for row in cur.fetchall()}
    # v0 schema: 6 columns (no reasoning)
    # v0.3.0 schema: 23+ columns (has reasoning, but not codex_message_items or display_identity BLOB)
    # v0.4.0: 26 columns (has codex_message_items, display_identity BLOB, display_order)
    if "reasoning" not in cols:
        return True
    if "codex_message_items" not in cols or "display_identity" not in cols:
        return True  # v0.3.0 → v0.4.0
    return False


def _backup_db(db_path: Path) -> str | None:
    """Backup db file before rebuild. Returns backup filename or None."""
    if not db_path.exists():
        return None
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_suffix(db_path.suffix + f".pre-v{CURRENT_SCHEMA_VERSION}.backup-{ts}")
    try:
        try:
            ck_conn = sqlite3.connect(str(db_path), timeout=1.0)
            ck_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            ck_conn.close()
        except Exception:
            pass
        backup.write_bytes(db_path.read_bytes())
        for suffix in ("-shm", "-wal"):
            sibling = db_path.with_suffix(db_path.suffix + suffix)
            if sibling.exists():
                try:
                    sibling.unlink()
                except Exception:
                    pass
        logger.info("已备份旧 db: %s", backup.name)
        return backup.name
    except Exception as e:  # noqa: BLE001
        logger.warning("备份旧 db 失败 (非致命): %s", e)
        return None


def init_schema(conn: sqlite3.Connection, db_path: Path | None = None) -> bool:
    """Initialize the v0.4.0 schema (13 tables + 5 FTS + 30+ indexes + 6 triggers).

    Strategy:
    1. If db doesn't exist or has no tables → CREATE all
    2. If db has legacy v0 / v0.3.0 schema → backup + DROP all + CREATE new
    3. If db already has v0.4.0 schema → ensure current (no-op)
    4. Always write schema_version row

    Returns:
        True if db was rebuilt (legacy → new), False otherwise.
    """
    conn.row_factory = sqlite3.Row
    has_messages = _table_exists(conn, "messages")
    rebuilt = False

    if has_messages and _detect_legacy_schema(conn):
        logger.warning("检测到旧版 schema — 备份并重建到 v%d", CURRENT_SCHEMA_VERSION)
        if db_path:
            _backup_db(db_path)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        for (table,) in cur.fetchall():
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
        for (trigger,) in cur.fetchall():
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        rebuilt = True

    # Execute the schema DDL. async_delegations indexes are deliberately
    # *not* in the inline DDL: they are created lazily by
    # SchemaCoordinationMixin._create_legacy_aware_indexes() after
    # _migrate_to_v2() ALTER TABLEs any v0.4.0 columns missing from
    # legacy async_delegations tables. We also call it here at init_schema
    # tail so fresh DBs (where _migrate_to_v2 is a no-op) get the same indexes.
    conn.executescript(_FULL_SCHEMA_SQL)
    conn.commit()

    # Lazy import to avoid the schema ↔ schema_coordination cycle.
    from .schema_coordination import (
        _create_legacy_aware_indexes,
        _upgrade_async_delegations_legacy_columns,
    )

    # async_delegations column-level upgrade (legacy column gaps + indexes)
    # must run on every cold start, not just on the migration path: a legacy
    # DB opened here would otherwise leave async_delegations half-upgraded
    # because ``run_migrations()`` would short-circuit on schema_version=2.
    _upgrade_async_delegations_legacy_columns(conn)
    _create_legacy_aware_indexes(conn)

    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
        (CURRENT_SCHEMA_VERSION,),
    )
    conn.commit()
    logger.info("schema 已初始化 (v%d, rebuilt=%s)", CURRENT_SCHEMA_VERSION, rebuilt)

    # ── v0.4.0 §13.14 cold start compat: FTS holder + legacy PK 修复 ──
    # 走 best-effort 路径（任意步骤失败不回滚 init_schema 主体）：
    # - legacy PK 漂移（async_delegations）8-step rebuild
    # - FTS 虚表 holder escalation（3 attempts × 60s 默认）
    # 调用方可在后续 db.health_check() 复查 ok=False 的情况。
    try:
        from .cold_start import run_cold_start_compat
        compat_report = run_cold_start_compat(conn, db_path=db_path)
        if not compat_report.get("ok", False):
            logger.warning("cold_start: 部分修复未通过，详见 compat_report")
    except Exception as exc:  # noqa: BLE001
        logger.warning("cold_start: compat hook 抛异常 (非致命): %s", exc)

    return rebuilt
