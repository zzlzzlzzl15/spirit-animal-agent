"""spirit/cli/storage_commands.py — slash commands exercising the new storage mixins.

v0.4.0 (Hermes-aligned): surface the new storage features (search,
timeline, rewind, vacuum, prune, stats) as CLI slash commands.

Reference: hermes hermes_cli/commands_storage.py.

These are pure functions; the CLI wraps them with the actual agent +
SessionDB at call time.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..storage.session_db import SessionDB

logger = logging.getLogger(__name__)


# Max characters of search-context preview.
_SEARCH_PREVIEW_CHARS = 120


def cmd_search(
    db: SessionDB,
    query: str,
    limit: int = 20,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run a full-text + LIKE search across messages.

    Tries ``SessionSearchMixin.search_messages`` (FTS5) first; falls back to
    ``_search_messages_like`` when the FTS layer is unavailable. Both helpers
    take the same ``session_id`` / ``limit`` kwargs, so the caller never sees
    the MRO shadow (``SessionMessagesMixin.search_messages`` is the simpler
    session-less signature that lives further up the MRO chain).
    """
    if not query.strip():
        return {"ok": False, "error": "empty_query", "hits": []}
    rows: List[Any] = []
    try:
        if hasattr(db, "_search_messages_like"):
            rows = db._search_messages_like(query, session_id=session_id, limit=limit)
        elif hasattr(db, "search_messages_trigram"):
            rows = db.search_messages_trigram(query, session_id=session_id, limit=limit)
        else:
            rows = db.search_messages(query, limit=limit)
    except Exception as exc:
        logger.exception("cmd_search failed")
        return {"ok": False, "error": str(exc), "hits": []}
    hits: List[Dict[str, Any]] = []
    for r in rows:
        content = (r.get("content") or "")[:_SEARCH_PREVIEW_CHARS]
        hits.append(
            {
                "session_id": r.get("session_id"),
                "message_id": r.get("id"),
                "role": r.get("role"),
                "preview": content,
                "timestamp": r.get("timestamp"),
            }
        )
    return {"ok": True, "query": query, "hits": hits, "count": len(hits)}


def cmd_history(
    db: SessionDB,
    session_id: str,
    limit: int = 50,
) -> Dict[str, Any]:
    """Return the timeline for a session."""
    try:
        entries = db.timeline_for_display(session_id, limit=limit)
        summary = db.timeline_summary(session_id)
    except Exception as exc:
        logger.exception("cmd_history failed")
        return {"ok": False, "error": str(exc), "entries": []}
    return {"ok": True, "session_id": session_id, "summary": summary, "entries": entries}


def cmd_undo(
    db: SessionDB,
    session_id: str,
    target_user_message_id: Optional[int] = None,
    target_user_content: Optional[str] = None,
) -> Dict[str, Any]:
    """Rewind the session back to (and including) the target user message.

    Accepts either a numeric ``target_user_message_id`` (when the caller knows
    the row id) or a ``target_user_content`` snippet (when the caller wants the
    most recent user turn whose content matches). Spirit's
    ``SessionDB.rewind_to_user_turn`` / ``SessionDB.rewind_to_user_content``
    encapsulate both paths.
    """
    try:
        if target_user_message_id is not None:
            result = db.rewind_to_user_turn(session_id, target_user_message_id)
        elif target_user_content:
            result = db.rewind_to_user_content(session_id, target_user_content)
        else:
            return {"ok": False, "error": "missing_target"}
    except Exception as exc:
        logger.exception("cmd_undo failed")
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "outcome": getattr(result, "name", str(result)),
        "session_id": session_id,
    }


def cmd_stats(db: SessionDB) -> Dict[str, Any]:
    """Return the structural health snapshot."""
    try:
        stats = db.stats()
        health = db.health_check()
        version = db.current_schema_version()
    except Exception as exc:
        logger.exception("cmd_stats failed")
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "version": version, "stats": stats, "health": health}


def cmd_vacuum(db: SessionDB, target_path: Optional[Path] = None) -> Dict[str, Any]:
    """Run VACUUM (or VACUUM INTO if a target is supplied)."""
    try:
        if target_path is not None:
            result = db.vacuum_into(target_path)
        else:
            result = db.vacuum()
    except Exception as exc:
        logger.exception("cmd_vacuum failed")
        return {"ok": False, "error": str(exc)}
    return {"ok": result.ok, "result": result.to_dict()}


def cmd_prune(
    db: SessionDB,
    older_than_days: int = 30,
    include_orphans: bool = True,
) -> Dict[str, Any]:
    """Prune old inactive sessions + (optionally) orphaned messages."""
    out: Dict[str, Any] = {"ok": True, "operations": []}
    try:
        older_than_s = older_than_days * 86400
        s = db.prune_old_sessions(older_than_s=older_than_s)
        out["operations"].append(s.to_dict())
        if include_orphans:
            o = db.prune_orphaned_messages()
            out["operations"].append(o.to_dict())
    except Exception as exc:
        logger.exception("cmd_prune failed")
        return {"ok": False, "error": str(exc)}
    return out


def cmd_export(db: SessionDB, out_path: Path) -> Dict[str, Any]:
    """Export sessions + messages to a JSON file."""
    try:
        info = db.export_to_json(out_path)
    except Exception as exc:
        logger.exception("cmd_export failed")
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "info": info}


def cmd_health(db: SessionDB) -> Dict[str, Any]:
    """Just the integrity_check / PRAGMA + schema version."""
    try:
        snap = db.schema_snapshot()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "snapshot": snap}
