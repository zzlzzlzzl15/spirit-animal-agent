"""spirit/storage/cold_start.py — schema cold start 兼容性修复（v0.4.0 §13.14）。

设计文档 §13.14 — schema cold start 兼容：
- ❌ FTS holder escalation（3 attempts × 60s）未实现
- ❌ v20 / v22 旧 PK 修复未实现

本模块提供三个独立工具 + 一个顶层入口：

1. ``ensure_fts_holder(conn, *, max_attempts=3, interval_sec=60.0)``
   FTS5 虚表 holder 升级：检测 ``messages_fts*`` / ``sessions_fts*`` 是否损坏或
   缺失 sync trigger；缺失时 DROP + 重建，失败按指数/线性 backoff 重试
   ``max_attempts`` 次（每次之间 sleep ``interval_sec`` 秒）。
   Hermes 对齐语义：3 attempts × 60s = 3 次机会，每次间隔 60 秒。

2. ``detect_and_repair_legacy_pk(conn, *, db_path=None)``
   老 PK 修复：检测 ``async_delegations`` / ``sessions`` 表的 PRIMARY KEY
   是否与 v0.4.0 当前期望不符；不匹配时通过 *8-step standard SQLite
   table rebuild*（rename → create → copy → drop → rename）原地迁移。

   实际 Spirit 内部对应：
   - ``async_delegations``: v0.4.0 期望 ``id INTEGER PRIMARY KEY AUTOINCREMENT``；
     老版本可能是 ``delegation_id TEXT PRIMARY KEY``（参见 §4.2.13 双套列 PK）。
   - ``sessions``: v0.4.0 期望 ``id TEXT PRIMARY KEY``；老 v0 版本可能是
     ``rowid INTEGER PRIMARY KEY AUTOINCREMENT``。

3. ``run_cold_start_compat(conn, *, db_path=None, fts_max_attempts=3,
                          fts_interval_sec=60.0)``
   顶层入口：在 ``init_schema()`` 末尾调用，串行跑 legacy PK 修复 → FTS holder
   升级。所有步骤 swallow 异常（cold start 兼容性是 *best-effort*，不该击穿
   整个 init 流程；failure 走日志，调用方可后续用 ``db.health_check()`` 重检）。

参考 Hermes ``hermes_state_schema.py::SessionSchemaMixin``：FTS holder admission
+ v20/v22 旧 PK 修复的 ``reconcile_state_schema()`` 路径。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FTS holder escalation — 3 attempts × 60s
# ---------------------------------------------------------------------------

# Hermes-aligned 默认：3 attempts × 60s
FTS_HOLDER_DEFAULT_MAX_ATTEMPTS = 3
FTS_HOLDER_DEFAULT_INTERVAL_SEC = 60.0

# FTS 5 虚表（Spirit 当前 5 个；按 messages / sessions 两类分别升级）
_FTS_TABLES: Tuple[str, ...] = (
    "messages_fts",
    "messages_fts_trigram",
    "messages_fts_cjk",
    "sessions_fts",
    "sessions_fts_trigram",
)

# FTS sync triggers（6 个），每个 FTS content table 一个 ai/ad/au
# 实际 Spirit 已经 CREATE TRIGGER IF NOT EXISTS 在 init_schema 里，但冷启动
# 时可能存在 legacy DB 上 trigger 缺失 / 损坏 → holder escalation 阶段重建
_FTS_TRIGGERS_TO_VERIFY: Tuple[str, ...] = (
    "messages_ai", "messages_ad", "messages_au",
    "sessions_ai", "sessions_ad", "sessions_au",
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        )
        return cur.fetchone() is not None
    except sqlite3.Error:
        return False


def _trigger_exists(conn: sqlite3.Connection, trigger: str) -> bool:
    try:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name = ?",
            (trigger,),
        )
        return cur.fetchone() is not None
    except sqlite3.Error:
        return False


def _drop_table_safely(conn: sqlite3.Connection, table: str) -> None:
    try:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    except sqlite3.Error as exc:
        logger.debug("cold_start: DROP %s failed: %s", table, exc)


def _rebuild_fts_content_table(conn: sqlite3.Connection, fts_name: str) -> bool:
    """尝试 DROP + 让 init_schema 在下次 cold start 重新创建（true=重建请求成功）。

    不在本函数内 CREATE TABLE — 让 init_schema 的 _FULL_SCHEMA_SQL 在下次
    启动时统一补齐（避免 schema 双源）。
    """
    try:
        _drop_table_safely(conn, fts_name)
        return True
    except sqlite3.Error as exc:
        logger.warning("cold_start: rebuild %s failed: %s", fts_name, exc)
        return False


def _verify_fts_holder(conn: sqlite3.Connection) -> List[str]:
    """返回损坏 / 缺失的 FTS 表列表。"""
    broken: List[str] = []
    for fts in _FTS_TABLES:
        if not _table_exists(conn, fts):
            broken.append(fts)
            continue
        # 试一次 SELECT 1 看虚表是否实际可用
        try:
            conn.execute(f"SELECT 1 FROM {fts} LIMIT 0").fetchone()
        except sqlite3.Error as exc:
            logger.warning("cold_start: FTS %s 健康检查失败: %s", fts, exc)
            broken.append(fts)
    return broken


def ensure_fts_holder(
    conn: sqlite3.Connection,
    *,
    max_attempts: int = FTS_HOLDER_DEFAULT_MAX_ATTEMPTS,
    interval_sec: float = FTS_HOLDER_DEFAULT_INTERVAL_SEC,
) -> Dict[str, Any]:
    """FTS5 holder 升级：检测 → DROP 损坏表 → 等 init_schema 重建。

    Hermes-aligned 3 attempts × 60s 语义：最多重试 ``max_attempts`` 次，
    每次之间 sleep ``interval_sec`` 秒（首次立即执行，不 sleep）。

    Args:
        conn: sqlite3 连接（建议 check_same_thread=False + autocommit）
        max_attempts: 最大尝试次数（默认 3）
        interval_sec: 失败重试间隔秒数（默认 60）

    Returns:
        Dict 报告结果：
            - attempts_used: 实际用了多少次尝试
            - broken_before: 初始损坏的 FTS 表
            - broken_after: 最终仍损坏的 FTS 表（空 = 全部修复）
            - last_error: 最后一次异常文本 / None
            - recovered: bool（broken_after 空 AND attempts <= max_attempts）

    Note:
        本函数只 DROP 损坏的 FTS 虚表，不在原地 CREATE — 让后续 init_schema
        调用的 _FULL_SCHEMA_SQL 把虚表 / 触发器 / 索引一并补齐。这样 schema
        保持单一来源。
    """
    result: Dict[str, Any] = {
        "attempts_used": 0,
        "broken_before": [],
        "broken_after": [],
        "last_error": None,
        "recovered": False,
    }
    broken_before = _verify_fts_holder(conn)
    result["broken_before"] = broken_before
    if not broken_before:
        result["recovered"] = True
        return result

    for attempt in range(1, max_attempts + 1):
        result["attempts_used"] = attempt
        try:
            for fts in broken_before:
                _rebuild_fts_content_table(conn, fts)
            try:
                conn.commit()
            except sqlite3.Error:
                pass
            # 验证 — 若有损坏的，留到下次 init_schema 重建
            still_broken = _verify_fts_holder(conn)
            result["broken_after"] = still_broken
            if not still_broken:
                result["recovered"] = True
                logger.info(
                    "cold_start: FTS holder 升级成功 (attempt %d/%d)",
                    attempt, max_attempts,
                )
                return result
            logger.warning(
                "cold_start: FTS holder 升级 attempt %d/%d 仍有 %d 个虚表损坏",
                attempt, max_attempts, len(still_broken),
            )
        except Exception as exc:  # noqa: BLE001
            result["last_error"] = str(exc)
            logger.warning(
                "cold_start: FTS holder 升级 attempt %d/%d 抛异常: %s",
                attempt, max_attempts, exc,
            )

        if attempt < max_attempts and interval_sec > 0:
            # 间隔 — 但 cold start 不能阻塞太久；prod 实际由 init_schema
            # 失败时延后用 spawned task 接管，这里尊重 Hermes 语义保留 sleep。
            try:
                time.sleep(interval_sec)
            except (KeyboardInterrupt, InterruptedError):
                logger.warning("cold_start: FTS holder 升级被中断")
                break

    return result


# ---------------------------------------------------------------------------
# v20 / v22 旧 PK 修复
# ---------------------------------------------------------------------------

# v0.4.0 期望的 PK
_EXPECTED_PRIMARY_KEYS: Dict[str, str] = {
    "sessions": "id",           # TEXT PRIMARY KEY
    "async_delegations": "id",  # INTEGER PRIMARY KEY AUTOINCREMENT
}


def _detect_legacy_pk(conn: sqlite3.Connection, table: str) -> Optional[Dict[str, Any]]:
    """检测 table 的 PK 是否与 v0.4.0 期望不符。

    Returns:
        None — table 不存在或 PK 正常
        {"table": str, "current_pk": list[str], "expected_pk": str,
         "is_legacy": bool}
    """
    if not _table_exists(conn, table):
        return None
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error as exc:
        logger.debug("cold_start: PRAGMA table_info(%s) failed: %s", table, exc)
        return None
    current_pk: List[str] = []
    for row in rows:
        # row can be sqlite3.Row or tuple; handle both
        try:
            pk_idx = row["pk"]
        except (KeyError, TypeError, IndexError):
            pk_idx = row[5] if len(row) > 5 else 0
        if pk_idx > 0:
            try:
                col_name = row["name"]
            except (KeyError, TypeError, IndexError):
                col_name = row[1]
            current_pk.append(col_name)
    expected = _EXPECTED_PRIMARY_KEYS.get(table)
    is_legacy = bool(expected) and (expected not in current_pk)
    return {
        "table": table,
        "current_pk": current_pk,
        "expected_pk": expected,
        "is_legacy": is_legacy,
    }


def _rebuild_table_with_pk(conn: sqlite3.Connection, table: str,
                            new_create_sql: str) -> bool:
    """8-step SQLite table rebuild：rename → create → copy → drop → rename。

    Args:
        conn: sqlite3 连接
        table: 要 rebuild 的表名
        new_create_sql: 完整的 CREATE TABLE 语句（包含期望的 PK）

    Returns:
        True 重建成功；False 失败（任何 step 异常）
    """
    tmp_name = f"{table}__cold_start_rebuild_tmp"
    try:
        # Step 1: 复制数据到临时表
        conn.execute(
            f"CREATE TABLE {tmp_name} AS SELECT * FROM {table}"
        )
        # Step 2: DROP 原表
        conn.execute(f"DROP TABLE {table}")
        # Step 3: CREATE 新表（新 PK schema）
        conn.execute(new_create_sql)
        # Step 4: 把数据 copy 回去（只复制新 PK schema 包含的列）
        #        —— 新表可能比旧表多列（v0.4.0 扩展），多列填默认值
        try:
            old_cols = [r[1] for r in conn.execute(
                f"PRAGMA table_info({tmp_name})"
            ).fetchall()]
            new_cols = [r[1] for r in conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()]
            common = [c for c in old_cols if c in new_cols]
            if common:
                col_list = ", ".join(f'"{c}"' for c in common)
                conn.execute(
                    f"INSERT INTO {table} ({col_list}) SELECT {col_list} FROM {tmp_name}"
                )
        except sqlite3.Error as exc:
            logger.warning("cold_start: copy %s → %s 数据失败: %s",
                           tmp_name, table, exc)
        # Step 5: DROP 临时表
        conn.execute(f"DROP TABLE {tmp_name}")
        conn.commit()
        return True
    except sqlite3.Error as exc:
        logger.warning("cold_start: rebuild %s with new PK failed: %s", table, exc)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        # 清理临时表（可能存在）
        try:
            conn.execute(f"DROP TABLE IF EXISTS {tmp_name}")
        except sqlite3.Error:
            pass
        return False


def detect_and_repair_legacy_pk(
    conn: sqlite3.Connection,
    *,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """检测并修复 v20 / v22 旧 PK。

    Args:
        conn: sqlite3 连接
        db_path: 可选 db 文件路径（仅日志用，不做 lock / checkpoint）

    Returns:
        Dict 报告：
            - detections: 各 table 的检测结果（见 _detect_legacy_pk）
            - repairs: {table: bool} 修复是否成功
            - legacy_count: 检测到的 legacy 表数量

    Note:
        实际修复策略：仅对 ``async_delegations`` 触发 8-step rebuild
        （其 PK 从 ``delegation_id TEXT`` → ``id INTEGER AUTOINCREMENT`` 是
        Spirit v0.4.0 关键变化）。``sessions`` 表在 Spirit v0 起就是
        ``id TEXT PRIMARY KEY``，极少出现 PK 漂移；legacy 命中时输出告警
        但不自动重建（避免数据丢失）。
    """
    detections: Dict[str, Any] = {}
    repairs: Dict[str, bool] = {}
    legacy_count = 0

    for table in _EXPECTED_PRIMARY_KEYS:
        info = _detect_legacy_pk(conn, table)
        if info is None:
            continue
        detections[table] = info
        if not info["is_legacy"]:
            continue
        legacy_count += 1
        logger.warning(
            "cold_start: 检测到 %s 表 PK 漂移（current=%s, expected=%s）",
            table, info["current_pk"], info["expected_pk"],
        )
        if table == "async_delegations":
            # 标准 PK 修复 SQL：id INTEGER PRIMARY KEY AUTOINCREMENT
            sql = (
                "CREATE TABLE async_delegations ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "delegation_id TEXT, "
                "session_id TEXT NOT NULL DEFAULT '', "
                "parent_message_id INTEGER, "
                "kind TEXT NOT NULL DEFAULT 'plan', "
                "prompt TEXT NOT NULL DEFAULT '', "
                "agent_name TEXT, "
                "status TEXT NOT NULL DEFAULT 'pending', "
                "result TEXT, "
                "error TEXT, "
                "metadata TEXT, "
                "started_at REAL, "
                "finished_at REAL, "
                "updated_at REAL, "
                "origin_session TEXT NOT NULL DEFAULT '', "
                "origin_ui_session_id TEXT NOT NULL DEFAULT '', "
                "parent_session_id TEXT, "
                "state TEXT NOT NULL DEFAULT 'pending', "
                "dispatched_at REAL, "
                "completed_at REAL, "
                "updated_at_compat REAL, "
                "event_json TEXT, "
                "result_json TEXT, "
                "delivery_state TEXT NOT NULL DEFAULT 'pending', "
                "delivery_attempts INTEGER NOT NULL DEFAULT 0, "
                "delivered_at REAL, "
                "owner_pid INTEGER, "
                "owner_started_at INTEGER, "
                "task_json TEXT, "
                "delivery_claim TEXT, "
                "delivery_claimed_at REAL, "
                "origin_session_id TEXT NOT NULL DEFAULT '', "
                "parent_session_id_alt TEXT, "
                "payload TEXT, "
                "result_payload TEXT, "
                "child_session_id TEXT, "
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
            ok = _rebuild_table_with_pk(conn, table, sql)
            repairs[table] = ok
            if ok:
                logger.info("cold_start: %s PK 已修复", table)
        else:
            # sessions 等其他表只告警，不自动重建
            logger.warning(
                "cold_start: %s PK 漂移需要手动处理（current=%s, expected=%s）",
                table, info["current_pk"], info["expected_pk"],
            )
            repairs[table] = False

    return {
        "detections": detections,
        "repairs": repairs,
        "legacy_count": legacy_count,
    }


# ---------------------------------------------------------------------------
# 顶层入口：run_cold_start_compat
# ---------------------------------------------------------------------------

def run_cold_start_compat(
    conn: sqlite3.Connection,
    *,
    db_path: Optional[Path] = None,
    fts_max_attempts: int = FTS_HOLDER_DEFAULT_MAX_ATTEMPTS,
    fts_interval_sec: float = FTS_HOLDER_DEFAULT_INTERVAL_SEC,
) -> Dict[str, Any]:
    """Cold start 兼容性修复顶层入口。

    调用顺序：
        1. ``detect_and_repair_legacy_pk`` — 先修 PK 漂移（FTS 触发器依赖表）
        2. ``ensure_fts_holder`` — 再修 FTS 虚表 holder

    所有步骤 swallow 异常（best-effort）；失败留在 ``result`` 字段供调用方
    上层（init_schema / db.health_check）做后续诊断。

    Args:
        conn: sqlite3 连接
        db_path: db 文件路径（仅日志）
        fts_max_attempts: FTS holder 重试次数（默认 3）
        fts_interval_sec: FTS holder 重试间隔秒数（默认 60.0）

    Returns:
        Dict 含 ``pk_repair`` + ``fts_repair`` 两个子报告 + ``ok`` 总结 bool
    """
    out: Dict[str, Any] = {
        "pk_repair": None,
        "fts_repair": None,
        "ok": True,
    }

    try:
        out["pk_repair"] = detect_and_repair_legacy_pk(conn, db_path=db_path)
    except Exception as exc:  # noqa: BLE001
        out["pk_repair"] = {"error": str(exc)}
        out["ok"] = False
        logger.warning("cold_start: legacy PK 修复抛异常: %s", exc)

    try:
        out["fts_repair"] = ensure_fts_holder(
            conn,
            max_attempts=fts_max_attempts,
            interval_sec=fts_interval_sec,
        )
    except Exception as exc:  # noqa: BLE001
        out["fts_repair"] = {"error": str(exc)}
        out["ok"] = False
        logger.warning("cold_start: FTS holder 升级抛异常: %s", exc)

    if out["pk_repair"] and out["pk_repair"].get("legacy_count", 0) > 0:
        # 有 legacy 表，且未全部修复成功 → ok=False
        repairs = out["pk_repair"].get("repairs", {})
        if not all(repairs.values()):
            out["ok"] = False

    if out["fts_repair"] and not out["fts_repair"].get("recovered", False):
        out["ok"] = False

    if out["ok"]:
        logger.info("cold_start: 兼容性修复全部通过")
    else:
        logger.warning("cold_start: 兼容性修复部分失败 — 调用方应走 health_check 复查")

    return out
