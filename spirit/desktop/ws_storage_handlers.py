"""spirit/desktop/ws_storage_handlers.py — WebSocket command handlers for storage features.

v0.4.0 (Hermes-aligned): expose search / history / undo / stats / vacuum /
prune / export / health as WebSocket commands the Electron desktop pet
can call.

Reference: hermes web/handlers_storage.py.

Hand-to-hand registration pattern: the existing ws_server.py uses a
``reg("cmd_name", handler)`` registry. We expose the handlers in this
module so they can be wired in via a one-liner.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from ..storage.session_db import SessionDB
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

logger = logging.getLogger(__name__)


# Type alias matching the ws_server handler signature.
WSHandler = Callable[[Dict[str, Any], Any], Awaitable[Dict[str, Any]]]


def _get_db(data: Dict[str, Any]) -> Optional[SessionDB]:
    """Extract the SessionDB from a WS request. Falls back to a default one."""
    db = data.get("_session_db")
    if isinstance(db, SessionDB):
        return db
    try:
        return SessionDB()
    except Exception:
        logger.exception("Could not acquire SessionDB")
        return None


async def ws_search(data: Dict[str, Any], client: Any) -> Dict[str, Any]:
    db = _get_db(data)
    if db is None:
        return {"ok": False, "error": "no_db"}
    query = (data.get("query") or "").strip()
    limit = int(data.get("limit") or 20)
    sid = data.get("session_id")
    return cmd_search(db, query=query, limit=limit, session_id=sid)


async def ws_history(data: Dict[str, Any], client: Any) -> Dict[str, Any]:
    db = _get_db(data)
    if db is None:
        return {"ok": False, "error": "no_db"}
    sid = data.get("session_id")
    limit = int(data.get("limit") or 50)
    if not sid:
        return {"ok": False, "error": "missing_session_id"}
    return cmd_history(db, session_id=sid, limit=limit)


async def ws_undo(data: Dict[str, Any], client: Any) -> Dict[str, Any]:
    db = _get_db(data)
    if db is None:
        return {"ok": False, "error": "no_db"}
    sid = data.get("session_id")
    target_id = data.get("target_message_id")
    if not sid:
        return {"ok": False, "error": "missing_session_id"}
    return cmd_undo(db, session_id=sid, target_user_message_id=target_id)


async def ws_stats(data: Dict[str, Any], client: Any) -> Dict[str, Any]:
    db = _get_db(data)
    if db is None:
        return {"ok": False, "error": "no_db"}
    return cmd_stats(db)


async def ws_vacuum(data: Dict[str, Any], client: Any) -> Dict[str, Any]:
    db = _get_db(data)
    if db is None:
        return {"ok": False, "error": "no_db"}
    target = data.get("target_path")
    return cmd_vacuum(db, target_path=Path(target) if target else None)


async def ws_prune(data: Dict[str, Any], client: Any) -> Dict[str, Any]:
    db = _get_db(data)
    if db is None:
        return {"ok": False, "error": "no_db"}
    days = int(data.get("older_than_days") or 30)
    include_orphans = bool(data.get("include_orphans", True))
    return cmd_prune(db, older_than_days=days, include_orphans=include_orphans)


async def ws_export(data: Dict[str, Any], client: Any) -> Dict[str, Any]:
    db = _get_db(data)
    if db is None:
        return {"ok": False, "error": "no_db"}
    out_path = data.get("out_path")
    if not out_path:
        return {"ok": False, "error": "missing_out_path"}
    return cmd_export(db, out_path=Path(out_path))


async def ws_health(data: Dict[str, Any], client: Any) -> Dict[str, Any]:
    db = _get_db(data)
    if db is None:
        return {"ok": False, "error": "no_db"}
    return cmd_health(db)


def all_storage_handlers() -> Dict[str, WSHandler]:
    """Return the dict of WS handler name → async fn. Pass to ws_server.reg()."""
    return {
        "storage_search": ws_search,
        "storage_history": ws_history,
        "storage_undo": ws_undo,
        "storage_stats": ws_stats,
        "storage_vacuum": ws_vacuum,
        "storage_prune": ws_prune,
        "storage_export": ws_export,
        "storage_health": ws_health,
    }


def register_storage_handlers(reg: Callable[[str, WSHandler], None]) -> None:
    """Wire all storage_* handlers into a ws_server `reg` registry.

    Usage from ws_server.py:
        from .ws_storage_handlers import register_storage_handlers
        register_storage_handlers(reg)
    """
    for name, handler in all_storage_handlers().items():
        try:
            reg(name, handler)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to register storage handler %s", name)
