"""后台异步任务委托系统 — Spirit Agent。

移植自 Hermes tools/async_delegation.py（936 行）。

核心功能：
- 守护线程池执行后台子任务
- SQLite 持久化委托记录（崩溃恢复）
- 完成队列（drain 机制）
- 跨进程交付声明（claim/release/complete）
- 孤儿委托恢复（owner process 退出后标记为 unknown）
"""

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

SPIRIT_HOME = Path(os.environ.get("SPIRIT_HOME", "~/.spirit")).expanduser()

# ============================================================================
# 守护线程池
# ============================================================================


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """所有工作线程设为 daemon 的线程池。

    主进程崩溃时 OS 直接回收守护线程，不留僵尸。
    不注册 atexit，避免退出时被阻塞等待。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 将已有线程标记为 daemon
        for t in self._threads:
            t.daemon = True

    def _adjust_thread_count(self):
        super()._adjust_thread_count()
        for t in self._threads:
            t.daemon = True


# ============================================================================
# 模块级状态
# ============================================================================

_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_executor_max_workers: int = 0

_records_lock = threading.Lock()
_records: Dict[str, Dict[str, Any]] = {}

_DEFAULT_MAX_ASYNC_CHILDREN = 3
_MAX_RETAINED_COMPLETED = 50
_DURABLE_RETENTION_SECONDS = 7 * 24 * 60 * 60  # 7 天
_MAX_DURABLE_PENDING = 1000
_DB_LOCK = threading.Lock()


def _db_path() -> Path:
    return SPIRIT_HOME / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS async_delegations (
            delegation_id TEXT PRIMARY KEY,
            origin_session TEXT NOT NULL,
            origin_ui_session_id TEXT NOT NULL DEFAULT '',
            parent_session_id TEXT,
            state TEXT NOT NULL,
            dispatched_at REAL NOT NULL,
            completed_at REAL,
            updated_at REAL NOT NULL,
            event_json TEXT,
            result_json TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at REAL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            task_json TEXT,
            delivery_claim TEXT,
            delivery_claimed_at REAL
        )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(async_delegations)")}
    for name, sql_type in (
        ("owner_pid", "INTEGER"),
        ("owner_started_at", "INTEGER"),
        ("task_json", "TEXT"),
        ("delivery_claim", "TEXT"),
        ("delivery_claimed_at", "REAL"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE async_delegations ADD COLUMN {name} {sql_type}")
    return conn


# ============================================================================
# 持久化操作
# ============================================================================


def _persist_dispatch(record: Dict[str, Any]) -> None:
    """持久化委托记录到 SQLite。"""
    now = time.time()
    task_payload = {
        key: record.get(key)
        for key in ("goal", "goals", "context", "toolsets", "role", "model", "is_batch")
        if key in record
    }
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id,
                parent_session_id, state, dispatched_at, updated_at,
                delivery_state, delivery_attempts, owner_pid,
                owner_started_at, task_json)
               VALUES (?, ?, ?, ?, 'running', ?, ?, 'pending', 0, ?, ?, ?)""",
            (record["delegation_id"], record.get("session_key", ""),
             record.get("origin_ui_session_id", ""), record.get("parent_session_id"),
             record["dispatched_at"], now, os.getpid(),
             None, json.dumps(task_payload)),
        )
    _prune_durable_records()


def _persist_completion(event: Dict[str, Any], result: Dict[str, Any]) -> None:
    """持久化完成记录。"""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """UPDATE async_delegations SET state=?, completed_at=?, updated_at=?,
               event_json=?, result_json=?, delivery_state='pending'
               WHERE delegation_id=?""",
            (event.get("status", "completed"), event.get("completed_at", now), now,
             json.dumps(event), json.dumps(result), event["delegation_id"]),
        )


def _delete_durable_delegation(delegation_id: str) -> None:
    with _DB_LOCK, _connect() as conn:
        conn.execute("DELETE FROM async_delegations WHERE delegation_id=?",
                     (delegation_id,))


def _prune_durable_records() -> None:
    """限制持久化记录数量。"""
    now = time.time()
    cutoff = now - _DURABLE_RETENTION_SECONDS
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            "DELETE FROM async_delegations WHERE delivery_state='delivered' AND updated_at < ?",
            (cutoff,),
        )
        terminal_count = conn.execute(
            "SELECT COUNT(*) FROM async_delegations WHERE state NOT IN ('running','finalizing')"
        ).fetchone()[0]
        excess = max(0, terminal_count - _MAX_RETAINED_COMPLETED)
        if excess:
            conn.execute(
                """DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('running','finalizing')
                     ORDER BY CASE delivery_state WHEN 'delivered' THEN 0 ELSE 1 END,
                              updated_at ASC LIMIT ?
                   )""",
                (excess,),
            )
        pending_count = conn.execute(
            """SELECT COUNT(*) FROM async_delegations
               WHERE state NOT IN ('running','finalizing') AND delivery_state='pending'"""
        ).fetchone()[0]
        overflow = max(0, pending_count - _MAX_DURABLE_PENDING)
        if overflow:
            conn.execute(
                """DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('running','finalizing') AND delivery_state='pending'
                     ORDER BY updated_at ASC LIMIT ?
                   )""",
                (overflow,),
            )


# ============================================================================
# 交付声明（跨进程安全）
# ============================================================================


def claim_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """声明一个待交付完成的交付权（跨进程安全）。"""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT delivery_state FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        if row is None:
            return True
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_claim=?, delivery_claimed_at=?,
                      delivery_attempts=delivery_attempts+1, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND (delivery_claim IS NULL OR delivery_claimed_at < ?)""",
            (claim_id, now, now, delegation_id, now - 300),
        )
        return cur.rowcount == 1


def release_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """释放失败的交付声明。"""
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_claim=NULL,
                      delivery_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (time.time(), delegation_id, claim_id),
        )
        return cur.rowcount == 1


def complete_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """确认交付完成。"""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered',
                      delivered_at=?, updated_at=?, delivery_claim=NULL,
                      delivery_claimed_at=NULL
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (now, now, delegation_id, claim_id),
        )
        return cur.rowcount == 1


def mark_completion_delivered(delegation_id: str) -> bool:
    """原子标记完成交付。"""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered', delivered_at=?, updated_at=?
               WHERE delegation_id=? AND delivery_state!='delivered'""",
            (now, now, delegation_id),
        )
        return cur.rowcount == 1


def claim_event_delivery(evt: Dict[str, Any], consumer: str) -> Optional[str]:
    """声明事件交付权。"""
    if evt.get("type") != "async_delegation":
        return ""
    delegation_id = str(evt.get("delegation_id") or "")
    if not delegation_id:
        return ""
    claim_id = f"{consumer}:{os.getpid()}:{uuid.uuid4().hex}"
    return claim_id if claim_completion_delivery(delegation_id, claim_id) else None


def complete_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        complete_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)


def release_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        release_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)


# ============================================================================
# 孤儿恢复
# ============================================================================


def recover_abandoned_delegations() -> int:
    """将 owner 进程已退出的委托标记为 unknown。"""
    now = time.time()
    recovered = 0
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT delegation_id, origin_session, origin_ui_session_id,
                      parent_session_id, dispatched_at, owner_pid,
                      owner_started_at, task_json
               FROM async_delegations WHERE state IN ('running','finalizing')"""
        ).fetchall()
        for row in rows:
            delegation_id, session_key, origin_ui, parent_id, dispatched_at, pid, started, task_json = row
            live = False
            if pid:
                try:
                    os.kill(int(pid), 0)
                    live = True
                except (OSError, ProcessLookupError):
                    live = False
            if live:
                continue
            task = json.loads(task_json or "{}")
            event = {
                "type": "async_delegation", "delegation_id": delegation_id,
                "session_key": session_key, "origin_ui_session_id": origin_ui,
                "parent_session_id": parent_id, "goal": task.get("goal", ""),
                "status": "unknown", "summary": None,
                "error": "Delegation host exited before recording a result.",
                "dispatched_at": dispatched_at, "completed_at": now,
            }
            result = {"status": "unknown", "summary": None, "error": event["error"]}
            conn.execute(
                """UPDATE async_delegations SET state='unknown', completed_at=?,
                   updated_at=?, event_json=?, result_json=?, delivery_state='pending'
                   WHERE delegation_id=?""",
                (now, now, json.dumps(event), json.dumps(result), delegation_id),
            )
            recovered += 1
    return recovered


def restore_undelivered_completions(target_queue) -> int:
    """恢复未交付的完成记录到目标队列。"""
    recover_abandoned_delegations()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT delegation_id, event_json FROM async_delegations
               WHERE state != 'running' AND delivery_state='pending' AND event_json IS NOT NULL
               ORDER BY completed_at, delegation_id"""
        ).fetchall()
        for _delegation_id, payload in rows:
            evt = json.loads(payload)
            if isinstance(evt, dict):
                evt["restored"] = True
            target_queue.put(evt)
    return len(rows)


# ============================================================================
# 查询
# ============================================================================


def get_durable_delegation(delegation_id: str) -> Optional[Dict[str, Any]]:
    """查询持久化委托记录。"""
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT origin_session, state, dispatched_at, completed_at,
                      result_json, delivery_state, delivery_attempts
               FROM async_delegations WHERE delegation_id=?""", (delegation_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "delegation_id": delegation_id, "origin_session": row[0], "state": row[1],
        "dispatched_at": row[2], "completed_at": row[3],
        "result": json.loads(row[4]) if row[4] else None,
        "delivery_state": row[5], "delivery_attempts": row[6],
    }


# ============================================================================
# 执行器管理
# ============================================================================


def _get_executor(max_workers: int) -> ThreadPoolExecutor:
    """懒创建/扩容共享守护执行器。"""
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is None or max_workers > _executor_max_workers:
            if _executor is not None:
                _executor.shutdown(wait=False)
            _executor = DaemonThreadPoolExecutor(max_workers=max_workers)
            _executor_max_workers = max_workers
        return _executor


# ============================================================================
# 委托分发
# ============================================================================


def dispatch_async_delegation(
    goal: str,
    *,
    session_key: str = "",
    parent_session_id: str = "",
    context: str = "",
    toolsets: Optional[List[str]] = None,
    role: str = "",
    model: str = "",
    is_batch: bool = False,
    callback: Optional[Callable] = None,
    max_workers: int = _DEFAULT_MAX_ASYNC_CHILDREN,
) -> str:
    """分发一个后台异步委托。

    Args:
        goal: 任务目标描述
        session_key: 来源会话键
        parent_session_id: 父会话 ID
        context: 额外上下文
        toolsets: 可用工具集
        role: 角色提示
        model: 模型选择
        is_batch: 是否批量
        callback: 完成回调
        max_workers: 最大并发子任务数

    Returns:
        delegation_id: 委托唯一标识
    """
    delegation_id = uuid.uuid4().hex[:12]
    now = time.time()

    record = {
        "delegation_id": delegation_id,
        "session_key": session_key,
        "origin_ui_session_id": "",
        "parent_session_id": parent_session_id,
        "goal": goal,
        "context": context,
        "toolsets": toolsets,
        "role": role,
        "model": model,
        "is_batch": is_batch,
        "dispatched_at": now,
    }

    # 持久化
    _persist_dispatch(record)

    # 内存记录
    with _records_lock:
        _records[delegation_id] = {
            **record,
            "state": "running",
            "future": None,
            "callback": callback,
        }

    # 提交到执行器
    executor = _get_executor(max_workers)

    def _run():
        try:
            # 实际执行逻辑 — 这里调用 agent 运行子任务
            result = _execute_delegation(goal, context, toolsets, role, model)
            event = {
                "type": "async_delegation",
                "delegation_id": delegation_id,
                "session_key": session_key,
                "status": "completed",
                "summary": result.get("summary", ""),
                "completed_at": time.time(),
            }
            _persist_completion(event, result)
            with _records_lock:
                if delegation_id in _records:
                    _records[delegation_id]["state"] = "completed"
                    _records[delegation_id]["result"] = result
            if callback:
                try:
                    callback(event, result)
                except Exception as exc:
                    logger.debug("Delegation callback failed: %s", exc)
            return result
        except Exception as exc:
            error_result = {"status": "error", "error": str(exc), "summary": None}
            event = {
                "type": "async_delegation",
                "delegation_id": delegation_id,
                "session_key": session_key,
                "status": "error",
                "error": str(exc),
                "completed_at": time.time(),
            }
            _persist_completion(event, error_result)
            with _records_lock:
                if delegation_id in _records:
                    _records[delegation_id]["state"] = "error"
                    _records[delegation_id]["result"] = error_result
            return error_result

    future = executor.submit(_run)
    with _records_lock:
        if delegation_id in _records:
            _records[delegation_id]["future"] = future

    return delegation_id


def _execute_delegation(
    goal: str, context: str, toolsets: Optional[List[str]],
    role: str, model: str,
) -> Dict[str, Any]:
    """执行委托任务的实际逻辑。

    在 Spirit Agent 中，这会创建一个新的 agent 实例来执行子任务。
    这里提供框架实现，实际 LLM 调用需要接入 agent 核心。
    """
    return {
        "status": "completed",
        "summary": f"Completed: {goal}",
        "details": f"Delegation for goal '{goal}' executed with context length {len(context)}.",
    }


# ============================================================================
# 查询内存记录
# ============================================================================


def list_async_delegations() -> List[Dict[str, Any]]:
    """列出所有委托记录。"""
    with _records_lock:
        return [
            {
                "delegation_id": did,
                "goal": rec.get("goal", ""),
                "state": rec.get("state", "unknown"),
                "dispatched_at": rec.get("dispatched_at", 0),
            }
            for did, rec in _records.items()
        ]


def get_async_delegation(delegation_id: str) -> Optional[Dict[str, Any]]:
    """获取指定委托的状态。"""
    with _records_lock:
        rec = _records.get(delegation_id)
    if rec:
        return {
            "delegation_id": delegation_id,
            "goal": rec.get("goal", ""),
            "state": rec.get("state", "unknown"),
            "result": rec.get("result"),
        }
    # 回退到持久化存储
    return get_durable_delegation(delegation_id)
