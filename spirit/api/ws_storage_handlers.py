"""spirit/api/ws_storage_handlers.py — WS 端点 storage_* 8 个 handler 与响应体格式。

设计文档 §13.12 — Hermes-aligned 第二轮补完：

    客户端发送 JSON：
        {"type": "storage_search", "request_id": "r-1", "query": "fox", "limit": 20,
         "session_id": "..."  # 可选
        }

    服务端推送事件（统一响应体格式）：
        {
          "type":      "storage_search_result",   # 响应类型
          "request_id":"r-1",                      # 与请求对齐，便于前端关联
          "ok":        true|false,                  # 业务结果 ok
          "data":      {...},                       # 成功时：cmd_* 返回的业务 dict（去掉 ok/error）
          "error":     null|"empty_query"|...,      # 失败时：错误码 / 错误消息
          "ts":        1700000000.123,              # epoch seconds（server 处理时刻）
          "duration_ms":12,                          # handler 自身耗时（不含 DB IO 排队）
        }

    流式 / 长任务（vacuum / prune / export）可选追加：
        {"type": "storage_vacuum_progress", "request_id": "r-1", "phase": "checkpoint",
         "percent": 42, "note": "..."}

8 个 handler 全部 dict 注册到 STORAGE_HANDLER_REGISTRY；
spirit/api/server.py 通过 register_storage_handlers() 一次性 hook 进 WS 主循环，
跟现有 chat / interrupt / reset / ping 等 type 同级 dispatch。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ..cli.storage_commands import (
    cmd_search,
    cmd_history,
    cmd_undo,
    cmd_stats,
    cmd_health,
    cmd_vacuum,
    cmd_prune,
    cmd_export,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 响应体辅助
# ---------------------------------------------------------------------------

_RESPONSE_TYPE_SUFFIX = "_result"
_PROGRESS_TYPE_SUFFIX = "_progress"


def _now_ts() -> float:
    return time.time()


def _new_request_id() -> str:
    """生成响应级 request_id（客户端没传时用）。

    短 ID（12 字符 hex），前端可用作 optimistic concurrency / dedup key。
    """
    import uuid as _uuid
    return "r-" + _uuid.uuid4().hex[:12]


def _build_response(
    name: str,
    payload: Dict[str, Any],
    *,
    ok: bool,
    data: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    ts: float,
    duration_ms: int,
) -> Dict[str, Any]:
    """统一响应体 builder。

    Args:
        name: handler 名（"search" / "history" / ...）→ 响应 type = "storage_<name>_result"
        payload: 客户端原始请求 payload（用于回显 request_id）
        ok: 业务结果
        data: cmd_* 返回 dict 中除 ok/error 之外的字段
        error: 错误码 / 错误消息
        ts: 服务端处理完成时刻
        duration_ms: handler 自身耗时
    """
    return {
        "type": f"storage_{name}{_RESPONSE_TYPE_SUFFIX}",
        "request_id": payload.get("request_id") or _new_request_id(),
        "ok": ok,
        "data": data or {},
        "error": error,
        "ts": ts,
        "duration_ms": duration_ms,
    }


def _progress_event(
    name: str,
    payload: Dict[str, Any],
    *,
    phase: str,
    percent: Optional[int] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """长任务进度事件（vacuum / prune / export 用）。"""
    evt: Dict[str, Any] = {
        "type": f"storage_{name}{_PROGRESS_TYPE_SUFFIX}",
        "request_id": payload.get("request_id") or _new_request_id(),
        "phase": phase,
        "ts": _now_ts(),
    }
    if percent is not None:
        evt["percent"] = int(percent)
    if note is not None:
        evt["note"] = str(note)
    return evt


# ---------------------------------------------------------------------------
# 8 个 WS handler — 每个都是 (db, payload) -> response dict
# ---------------------------------------------------------------------------
# 关键约定：
#   1. 不 raise。所有异常被 swallow 到 {"ok": False, "error": <repr>}.
#   2. payload 字段缺失 / 类型错时返回 ok=False + error=<code>，不让前端 timeout。
#   3. 业务 cmd_* 返回 dict 去掉 ok/error 后塞进 data；保留 ok/error 在顶层。
#   4. duration_ms = (after - before) * 1000，包含 cmd_* 调用本身。
# ---------------------------------------------------------------------------


def _strip_ok_error(d: Dict[str, Any]) -> Dict[str, Any]:
    """从 cmd_* 返回 dict 里剥离 ok / error，其余字段塞进 data。"""
    return {k: v for k, v in d.items() if k not in ("ok", "error")}


def handle_storage_search(db, payload: Dict[str, Any]) -> Dict[str, Any]:
    """storage_search：FTS5 + LIKE 全文检索 messages。

    客户端 payload:
        {"query": str, "limit": int=20, "session_id": str?}
    """
    t0 = _now_ts()
    query = (payload.get("query") or "").strip()
    if not query:
        return _build_response(
            "search", payload,
            ok=False, error="empty_query",
            ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
        )
    try:
        limit = int(payload.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 200))  # 防滥用

    try:
        result = cmd_search(db, query=query, limit=limit,
                            session_id=payload.get("session_id"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("storage_search failed")
        return _build_response(
            "search", payload,
            ok=False, error=str(exc),
            ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
        )

    return _build_response(
        "search", payload,
        ok=bool(result.get("ok")),
        data=_strip_ok_error(result),
        error=result.get("error"),
        ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
    )


def handle_storage_history(db, payload: Dict[str, Any]) -> Dict[str, Any]:
    """storage_history：取一个 session 的 timeline + summary。

    客户端 payload:
        {"session_id": str, "limit": int=50}
    """
    t0 = _now_ts()
    session_id = payload.get("session_id")
    if not session_id:
        return _build_response(
            "history", payload,
            ok=False, error="missing_session_id",
            ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
        )
    try:
        limit = int(payload.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 1000))

    try:
        result = cmd_history(db, session_id=session_id, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.exception("storage_history failed")
        return _build_response(
            "history", payload,
            ok=False, error=str(exc),
            ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
        )

    return _build_response(
        "history", payload,
        ok=bool(result.get("ok")),
        data=_strip_ok_error(result),
        error=result.get("error"),
        ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
    )


def handle_storage_undo(db, payload: Dict[str, Any]) -> Dict[str, Any]:
    """storage_undo：rewind session 到指定 user message 前。

    客户端 payload（二选一即可）:
        {"session_id": str, "to_message": int}
        {"session_id": str, "to_content": str}
    """
    t0 = _now_ts()
    session_id = payload.get("session_id")
    if not session_id:
        return _build_response(
            "undo", payload,
            ok=False, error="missing_session_id",
            ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
        )

    target_id = payload.get("to_message")
    target_content = payload.get("to_content")
    if target_id is not None:
        try:
            target_id = int(target_id)
        except (TypeError, ValueError):
            return _build_response(
                "undo", payload,
                ok=False, error="invalid_to_message",
                ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
            )

    try:
        result = cmd_undo(
            db, session_id=session_id,
            target_user_message_id=target_id,
            target_user_content=target_content,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("storage_undo failed")
        return _build_response(
            "undo", payload,
            ok=False, error=str(exc),
            ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
        )

    return _build_response(
        "undo", payload,
        ok=bool(result.get("ok")),
        data=_strip_ok_error(result),
        error=result.get("error"),
        ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
    )


def handle_storage_stats(db, payload: Dict[str, Any]) -> Dict[str, Any]:
    """storage_stats：DB 结构 + 健康快照。

    客户端 payload: {} (无参数)
    """
    t0 = _now_ts()
    try:
        result = cmd_stats(db)
    except Exception as exc:  # noqa: BLE001
        logger.exception("storage_stats failed")
        return _build_response(
            "stats", payload,
            ok=False, error=str(exc),
            ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
        )

    return _build_response(
        "stats", payload,
        ok=bool(result.get("ok")),
        data=_strip_ok_error(result),
        error=result.get("error"),
        ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
    )


def handle_storage_health(db, payload: Dict[str, Any]) -> Dict[str, Any]:
    """storage_health：PRAGMA integrity_check + schema version 快照。

    客户端 payload: {} (无参数)
    """
    t0 = _now_ts()
    try:
        result = cmd_health(db)
    except Exception as exc:  # noqa: BLE001
        logger.exception("storage_health failed")
        return _build_response(
            "health", payload,
            ok=False, error=str(exc),
            ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
        )

    return _build_response(
        "health", payload,
        ok=bool(result.get("ok")),
        data=_strip_ok_error(result),
        error=result.get("error"),
        ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
    )


def handle_storage_vacuum(db, payload: Dict[str, Any]) -> Dict[str, Any]:
    """storage_vacuum：VACUUM / VACUUM INTO 重建 DB 文件。

    客户端 payload:
        {"target_path": str?}  # 不传 = VACUUM INTO 临时备份 + 原地 VACUUM
    """
    t0 = _now_ts()
    target_path: Optional[Path] = None
    raw = payload.get("target_path")
    if raw:
        try:
            target_path = Path(raw)
        except Exception as exc:  # noqa: BLE001
            return _build_response(
                "vacuum", payload,
                ok=False, error=f"invalid_target_path: {exc}",
                ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
            )

    try:
        result = cmd_vacuum(db, target_path=target_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("storage_vacuum failed")
        return _build_response(
            "vacuum", payload,
            ok=False, error=str(exc),
            ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
        )

    return _build_response(
        "vacuum", payload,
        ok=bool(result.get("ok")),
        data=_strip_ok_error(result),
        error=result.get("error"),
        ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
    )


def handle_storage_prune(db, payload: Dict[str, Any]) -> Dict[str, Any]:
    """storage_prune：清理旧 inactive session + orphaned messages。

    客户端 payload:
        {"older_than_days": int=90, "include_orphans": bool=True}
    """
    t0 = _now_ts()
    try:
        older_than_days = int(payload.get("older_than_days") or 90)
    except (TypeError, ValueError):
        older_than_days = 90
    older_than_days = max(1, min(older_than_days, 3650))  # 上限 10 年

    include_orphans = bool(payload.get("include_orphans", True))

    try:
        result = cmd_prune(db, older_than_days=older_than_days,
                           include_orphans=include_orphans)
    except Exception as exc:  # noqa: BLE001
        logger.exception("storage_prune failed")
        return _build_response(
            "prune", payload,
            ok=False, error=str(exc),
            ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
        )

    return _build_response(
        "prune", payload,
        ok=bool(result.get("ok")),
        data=_strip_ok_error(result),
        error=result.get("error"),
        ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
    )


def handle_storage_export(db, payload: Dict[str, Any]) -> Dict[str, Any]:
    """storage_export：sessions + messages → JSON 快照。

    客户端 payload:
        {"output_path": str}  # 必填，写入路径
    """
    t0 = _now_ts()
    raw = payload.get("output_path")
    if not raw:
        return _build_response(
            "export", payload,
            ok=False, error="missing_output_path",
            ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
        )
    try:
        out_path = Path(raw)
    except Exception as exc:  # noqa: BLE001
        return _build_response(
            "export", payload,
            ok=False, error=f"invalid_output_path: {exc}",
            ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
        )

    try:
        result = cmd_export(db, out_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("storage_export failed")
        return _build_response(
            "export", payload,
            ok=False, error=str(exc),
            ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
        )

    return _build_response(
        "export", payload,
        ok=bool(result.get("ok")),
        data=_strip_ok_error(result),
        error=result.get("error"),
        ts=_now_ts(), duration_ms=int((_now_ts() - t0) * 1000),
    )


# ---------------------------------------------------------------------------
# 注册表 — WS 主循环 dispatch 用
# ---------------------------------------------------------------------------
# 8 个 handler 名字与 spirit/cli/main.py @storage.command("xxx") 完全对齐
# (search / history / undo / stats / health / vacuum / prune / export)。
# 前端发送 type="storage_<name>" 时，dispatch 到 handle_storage_<name>。
# ---------------------------------------------------------------------------

STORAGE_HANDLER_REGISTRY: Dict[str, Callable] = {
    "storage_search": handle_storage_search,
    "storage_history": handle_storage_history,
    "storage_undo": handle_storage_undo,
    "storage_stats": handle_storage_stats,
    "storage_health": handle_storage_health,
    "storage_vacuum": handle_storage_vacuum,
    "storage_prune": handle_storage_prune,
    "storage_export": handle_storage_export,
}


def register_storage_handlers(reg: Dict[str, Callable]) -> None:
    """把 8 个 storage handler 注入到外部 WS dispatch reg。

    Args:
        reg: 外部 dispatch 字典（通常是 WS 主循环里的 type→handler 表）。
             调用后该字典会多出 8 个 storage_* key。

    Example:
        >>> reg = {"chat": chat_handler, "ping": ping_handler}
        >>> register_storage_handlers(reg)
        >>> "storage_search" in reg
        True
    """
    for msg_type, handler in STORAGE_HANDLER_REGISTRY.items():
        reg[msg_type] = handler
        logger.debug("WS storage handler registered: %s", msg_type)


def is_storage_msg_type(msg_type: str) -> bool:
    """判断 msg_type 是否属于 storage_* 命名空间。"""
    return isinstance(msg_type, str) and msg_type in STORAGE_HANDLER_REGISTRY


def dispatch_storage(db, msg_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """dispatch 入口：未知 type 返回 storage_unknown_result（不 raise）。

    返回值 = 直接可 websocket.send_json 的 dict。
    """
    handler = STORAGE_HANDLER_REGISTRY.get(msg_type)
    if handler is None:
        return _build_response(
            "unknown", payload,
            ok=False, error=f"unknown_storage_type: {msg_type}",
            ts=_now_ts(), duration_ms=0,
        )
    try:
        return handler(db, payload or {})
    except Exception as exc:  # noqa: BLE001
        # 双保险：handler 自身已 swallow，再兜底
        logger.exception("storage dispatch error: %s", msg_type)
        return _build_response(
            msg_type[len("storage_"):] if msg_type.startswith("storage_") else "unknown",
            payload or {},
            ok=False, error=f"dispatch_error: {exc}",
            ts=_now_ts(), duration_ms=0,
        )
