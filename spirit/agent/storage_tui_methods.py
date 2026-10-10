"""spirit/agent/storage_tui_methods.py — TUI 网关 8 个 storage RPC 方法。

设计文档 §13.13：TUI / Electron 桌宠 触点改造 — 文档要求 tui_gateway / electron
三处同接入 storage_search / export / import / rewind 等 6+ 能力。

本模块提供：
- 8 个同步 RPC handler 函数（适配 :class:`spirit.tui.tui_server.TuiGateway`
  的 ``register(method, handler)`` 签名：``(params: dict) -> data``）
- ``register_storage_methods(gateway, *, db_path=None)`` 一键注入 helper
- ``STORAGE_TUI_METHODS`` 命名列表（调试 / 校验 / 文档生成用）

RPC 方法命名（TUI 命名空间，dot-separated 对齐 Hermes）::

    storage.search      — FTS5 + LIKE 全量检索 messages
    storage.history     — 取一个 session 的 timeline + summary
    storage.undo        — rewind session 到指定 user message 前
    storage.stats       — DB 结构 + 健康快照
    storage.health      — PRAGMA integrity_check + schema version
    storage.vacuum      — VACUUM / VACUUM INTO 重建 DB
    storage.prune       — 清理旧 inactive sessions + orphans
    storage.export      — sessions + messages → JSON 快照

响应通过 :class:`TuiGateway` 的 ``_ok_response`` / ``_error_response`` 包装：
``{"type": "response", "id", "method", "ok", "data"|"error", "timestamp"}``。
本模块不重复 envelope，handler 只返回 ``data`` dict（cmd_* 业务结果）即可。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ..cli.storage_commands import (
    cmd_export,
    cmd_health,
    cmd_history,
    cmd_prune,
    cmd_search,
    cmd_stats,
    cmd_undo,
    cmd_vacuum,
)
from ..storage.session_db import SessionDB

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 8 个 RPC handler — 每个都是 (params: dict) -> data dict
# ---------------------------------------------------------------------------
# 关键约定：
#   1. 不 raise — 由 :class:`TuiGateway.handle_message` 在 try/except 里折叠成错误响应。
#   2. 必须字段缺失 / 类型错时返回 ``{"error": <code>, "_ok": False}``，
#      TuiGateway 会把它当成 data 返回（gateway 不解析 data 内部 ok/error，
#      由调用方上层决定；这种"嵌入式 ok/error"让前端可以区分业务失败 vs 协议失败）。
#   3. 不依赖 spirit.api.ws_storage_handlers（envelope 格式不同 — TUI 是 id/method，
#      FastAPI WS 是 request_id/type，不通用）。
# ---------------------------------------------------------------------------


def _resolve_db(db_path: Optional[Path]):
    """打开 SessionDB（可选显式 db_path，否则 default）。"""
    try:
        return SessionDB(db_path=db_path) if db_path else SessionDB()
    except Exception as exc:  # noqa: BLE001
        logger.exception("storage_tui: SessionDB open failed")
        return {"_ok": False, "error": f"db_open_failed: {exc}"}


def _safe(fn: Callable, *args, **kwargs) -> Dict[str, Any]:
    """调一个 cmd_* 函数，吞掉异常返回 ``{"_ok": False, "error": str(exc)}``。"""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.exception("storage_tui RPC failed")
        return {"_ok": False, "error": str(exc)}


def tui_storage_search(params: Dict[str, Any]) -> Dict[str, Any]:
    """storage.search — FTS5 + LIKE 全量检索 messages。

    params:
        query: str（必填）
        limit: int = 20
        session_id: str = None
    """
    query = (params.get("query") or "").strip()
    if not query:
        return {"_ok": False, "error": "empty_query"}
    try:
        limit = int(params.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 200))
    db = _resolve_db(params.get("_db_path"))
    if isinstance(db, dict):
        return db
    try:
        return _safe(cmd_search, db, query=query, limit=limit,
                      session_id=params.get("session_id"))
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


def tui_storage_history(params: Dict[str, Any]) -> Dict[str, Any]:
    """storage.history — 取一个 session 的 timeline + summary。

    params:
        session_id: str（必填）
        limit: int = 50
    """
    session_id = params.get("session_id")
    if not session_id:
        return {"_ok": False, "error": "missing_session_id"}
    try:
        limit = int(params.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 1000))
    db = _resolve_db(params.get("_db_path"))
    if isinstance(db, dict):
        return db
    try:
        return _safe(cmd_history, db, session_id=session_id, limit=limit)
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


def tui_storage_undo(params: Dict[str, Any]) -> Dict[str, Any]:
    """storage.undo — rewind session 到指定 user message 前。

    params:
        session_id: str（必填）
        to_message: int = None
        to_content: str = None
    """
    session_id = params.get("session_id")
    if not session_id:
        return {"_ok": False, "error": "missing_session_id"}
    target_id = params.get("to_message")
    target_content = params.get("to_content")
    if target_id is not None:
        try:
            target_id = int(target_id)
        except (TypeError, ValueError):
            return {"_ok": False, "error": "invalid_to_message"}
    db = _resolve_db(params.get("_db_path"))
    if isinstance(db, dict):
        return db
    try:
        return _safe(cmd_undo, db, session_id=session_id,
                      target_user_message_id=target_id,
                      target_user_content=target_content)
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


def tui_storage_stats(params: Dict[str, Any]) -> Dict[str, Any]:
    """storage.stats — DB 结构 + 健康快照。"""
    db = _resolve_db(params.get("_db_path"))
    if isinstance(db, dict):
        return db
    try:
        return _safe(cmd_stats, db)
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


def tui_storage_health(params: Dict[str, Any]) -> Dict[str, Any]:
    """storage.health — PRAGMA integrity_check + schema version。"""
    db = _resolve_db(params.get("_db_path"))
    if isinstance(db, dict):
        return db
    try:
        return _safe(cmd_health, db)
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


def tui_storage_vacuum(params: Dict[str, Any]) -> Dict[str, Any]:
    """storage.vacuum — VACUUM / VACUUM INTO 重建 DB。

    params:
        target_path: str = None
    """
    raw = params.get("target_path")
    target_path: Optional[Path] = None
    if raw:
        try:
            target_path = Path(raw)
        except Exception as exc:  # noqa: BLE001
            return {"_ok": False, "error": f"invalid_target_path: {exc}"}
    db = _resolve_db(params.get("_db_path"))
    if isinstance(db, dict):
        return db
    try:
        return _safe(cmd_vacuum, db, target_path=target_path)
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


def tui_storage_prune(params: Dict[str, Any]) -> Dict[str, Any]:
    """storage.prune — 清理旧 inactive sessions + orphan messages。

    params:
        older_than_days: int = 90
        include_orphans: bool = True
    """
    try:
        older_than_days = int(params.get("older_than_days") or 90)
    except (TypeError, ValueError):
        older_than_days = 90
    older_than_days = max(1, min(older_than_days, 3650))
    include_orphans = bool(params.get("include_orphans", True))
    db = _resolve_db(params.get("_db_path"))
    if isinstance(db, dict):
        return db
    try:
        return _safe(cmd_prune, db, older_than_days=older_than_days,
                      include_orphans=include_orphans)
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


def tui_storage_export(params: Dict[str, Any]) -> Dict[str, Any]:
    """storage.export — sessions + messages → JSON 快照。

    params:
        output_path: str（必填）
    """
    raw = params.get("output_path")
    if not raw:
        return {"_ok": False, "error": "missing_output_path"}
    try:
        out_path = Path(raw)
    except Exception as exc:  # noqa: BLE001
        return {"_ok": False, "error": f"invalid_output_path: {exc}"}
    db = _resolve_db(params.get("_db_path"))
    if isinstance(db, dict):
        return db
    try:
        return _safe(cmd_export, db, out_path)
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 注册 helper
# ---------------------------------------------------------------------------

# 命名空间常量（method 名字符串 = TUI 客户端 RPC 入口）
STORAGE_TUI_METHODS = (
    "storage.search",
    "storage.history",
    "storage.undo",
    "storage.stats",
    "storage.health",
    "storage.vacuum",
    "storage.prune",
    "storage.export",
)


# method → handler 映射
_HANDLER_BY_METHOD: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "storage.search": tui_storage_search,
    "storage.history": tui_storage_history,
    "storage.undo": tui_storage_undo,
    "storage.stats": tui_storage_stats,
    "storage.health": tui_storage_health,
    "storage.vacuum": tui_storage_vacuum,
    "storage.prune": tui_storage_prune,
    "storage.export": tui_storage_export,
}


def register_storage_methods(gateway) -> None:
    """把 8 个 storage RPC 注入 TuiGateway。

    Args:
        gateway: :class:`spirit.tui.tui_server.TuiGateway` 实例。
                 调用后 ``gateway.list_methods()`` 会多出 8 个 storage.* 项。

    Example:
        >>> from spirit.tui.tui_server import TuiGateway
        >>> gw = TuiGateway()
        >>> register_storage_methods(gw)
        >>> "storage.search" in gw.list_methods()
        True
    """
    for method, handler in _HANDLER_BY_METHOD.items():
        try:
            gateway.register(method, handler)
            logger.debug("TUI storage RPC registered: %s", method)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to register TUI storage RPC: %s", method)


def is_storage_tui_method(method: str) -> bool:
    """判断 method 是否属于 storage.* 命名空间。"""
    return isinstance(method, str) and method in _HANDLER_BY_METHOD
