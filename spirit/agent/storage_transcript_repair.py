"""spirit/agent/storage_transcript_repair.py — repair malformed message sequences.

v0.4.0 (Hermes-aligned): walk messages for a session, fix:
- Adjacent messages with the same role get merged.
- A message whose tool_call_id has no matching tool_calls is dropped.
- An "assistant" message with no content / no tool_calls is dropped.
- An in-flight assistant message carrying ``_row_id`` is reconciled with the
  committed row inside the active write transaction (Hermes-style):
  - blank target row → UPDATE in place (the live bytes ARE the durable copy)
  - non-blank target row (concurrent winner) → adopt canonical content,
    do NOT overwrite
- Watermark compaction may have soft-archived the target row and created
  a clone; the clone is detected by ``(session_id, timestamp)`` and used
  as the in-place update target.
- After commit, ``sync_flushed_message_markers`` stamps
  ``_DB_PERSISTED_MARKER`` and syncs canonical durable fields
  (``_row_id``, ``timestamp``, ``content``) onto the live dicts.

参考：hermes agent/transcript_repair.py。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Callable, Dict, List, Optional

from ..storage.session_db import SessionDB
from ..storage.session_persistence import _DB_PERSISTED_MARKER

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Hermes-aligned helpers (module-level so wire adapters can call them)
# ────────────────────────────────────────────────────────────────────


def is_content_blank(content: Any) -> bool:
    """True when decoded message content is None, whitespace-only, or has no
    visible text parts. Mirrors Hermes
    agent/transcript_repair.py::is_content_blank.
    """
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        return not "".join(
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ).strip()
    return False


def _active_assistant_row(
    conn: sqlite3.Connection,
    session_id: str,
    row_id: int,
) -> Optional[sqlite3.Row]:
    """The active assistant row for ``row_id``, or the active clone a
    watermark compaction made of it.

    Mirrors Hermes agent/transcript_repair.py::_active_assistant_row.
    """
    row = conn.execute(
        "SELECT id, role, active, timestamp, content FROM messages "
        "WHERE id = ? AND session_id = ?",
        (row_id, session_id),
    ).fetchone()
    if row is None or row["role"] != "assistant":
        return None
    if int(row["active"] or 0) == 1:
        return row
    # Watermark compaction soft-archived the concurrent tail and cloned it.
    return conn.execute(
        "SELECT id, role, active, timestamp, content FROM messages "
        "WHERE session_id = ? AND active = 1 AND role = 'assistant' "
        "AND timestamp IS ? AND id != ? "
        "ORDER BY id DESC LIMIT 1",
        (session_id, row["timestamp"], row["id"]),
    ).fetchone()


def resolve_and_repair_transcript_batch(
    conn: sqlite3.Connection,
    session_id: str,
    messages: List[Dict[str, Any]],
    encode_content_fn: Callable[[Any], Any],
    decode_content_fn: Callable[[Any], Any],
) -> List[Dict[str, Any]]:
    """Partition a message batch within an active write transaction.

    An assistant message carrying an existing integer ``_row_id`` targets
    its active SQLite row (or the active clone a watermark compaction
    made of it): a blank row is updated in place; a non-blank one
    (concurrent winner) has its canonical content adopted without
    overwrite. Returns the messages that must be inserted as fresh rows.

    Side effects on the input dicts:
      - ``_row_id`` rewritten to the resolved row id (the clone if any)
      - non-blank winner gets ``_canonical_content`` set; caller drops
        from the INSERT batch and adopts the canonical content
      - blank target rows are updated in place inside the active
        transaction; they are also dropped from the INSERT batch
    """
    inserted_rows: List[Dict[str, Any]] = []
    for msg in messages:
        existing_row_id = msg.get("_row_id") if isinstance(msg, dict) else None
        target_row = None
        if isinstance(existing_row_id, int) and msg.get("role", "unknown") == "assistant":
            target_row = _active_assistant_row(conn, session_id, existing_row_id)
        if target_row is None:
            inserted_rows.append(msg)
            continue
        target_id = int(target_row["id"])
        decoded = decode_content_fn(target_row["content"])
        msg["_row_id"] = target_id
        if is_content_blank(decoded):
            conn.execute(
                "UPDATE messages SET content = ? "
                "WHERE id = ? AND session_id = ? AND active = 1",
                (encode_content_fn(msg.get("content")), target_id, session_id),
            )
            # The blank target was updated in place; do NOT INSERT a new row.
            # The caller will still see this dict via the return value so
            # sync_flushed_message_markers can stamp _DB_PERSISTED_MARKER.
        else:
            # Concurrent winner: adopt, don't overwrite. Drop from INSERT
            # batch entirely; the durable row is the winner's row.
            msg["_canonical_content"] = decoded
    return inserted_rows


def sync_flushed_message_markers(
    batch_msgs: List[Dict[str, Any]],
    batch_rows: List[Dict[str, Any]],
) -> None:
    """Stamp ``_DB_PERSISTED_MARKER`` and sync canonical durable fields
    onto live dicts after commit.

    Mirrors Hermes agent/transcript_repair.py::sync_flushed_message_markers.
    The CONTRACT (per Hermes): the marker asserts the dict's CONTENT is
    durable as written — any in-place mutation that must persist MUST
    pop it.
    """
    for written, row in zip(batch_msgs, batch_rows):
        written[_DB_PERSISTED_MARKER] = True
        if isinstance(row.get("_row_id"), int):
            written["_row_id"] = row["_row_id"]
        if isinstance(row.get("timestamp"), (int, float)):
            written["timestamp"] = row["timestamp"]
        if "_canonical_content" in row:
            written["content"] = row["_canonical_content"]


# ────────────────────────────────────────────────────────────────────
# Spirit-side facade (passive merge + drop + in-flight repair hook)
# ────────────────────────────────────────────────────────────────────


class StorageTranscriptRepair:
    """Repair a session's transcript in-place. Idempotent.

    Two repair paths are exposed:

    1. ``repair_session`` — passive merge/drop for already-committed
       transcripts (adjacent same-role merge, orphan tool-call drop,
       empty assistant drop).

    2. ``repair_in_flight`` — Hermes-style in-flight reconciliation
       inside an active write transaction. Used by ``save_messages`` to
       reconcile an in-memory assistant row that carries ``_row_id``
       with the committed SQLite row (blank → in-place UPDATE;
       non-blank winner → adopt canonical content).
    """

    def __init__(self, db: SessionDB):
        self.db = db

    # ── Passive repair ────────────────────────────────────────
    def repair_session(self, session_id: str, dry_run: bool = False) -> Dict[str, int]:
        """Return counts {merged, dropped_assistant, dropped_orphan_tool}."""
        msgs = self.db.get_messages(session_id)
        if not msgs:
            return {"merged": 0, "dropped_assistant": 0, "dropped_orphan_tool": 0}

        merged = 0
        dropped_assistant = 0
        dropped_orphan_tool = 0

        tool_call_ids: set = set()
        for m in msgs:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        tool_call_ids.add(tc["id"])

        cleaned: List[Dict[str, Any]] = []
        for m in msgs:
            role = m.get("role", "")
            content = m.get("content", "") or ""
            tool_calls = m.get("tool_calls") or []
            tool_call_id = m.get("tool_call_id") or ""
            if role == "tool" and tool_call_id and tool_call_id not in tool_call_ids:
                dropped_orphan_tool += 1
                continue
            if role == "assistant" and not content and not tool_calls:
                dropped_assistant += 1
                continue
            if (
                cleaned
                and cleaned[-1]["role"] == role
                and role in ("user", "assistant")
                and not tool_calls
                and not cleaned[-1].get("tool_calls")
            ):
                cleaned[-1]["content"] = (
                    (cleaned[-1]["content"] or "") + "\n" + content
                ).strip()
                merged += 1
                continue
            cleaned.append(m)

        if not dry_run and (
            merged or dropped_assistant or dropped_orphan_tool
        ):
            try:
                self.db.replace_messages(session_id, cleaned)
            except Exception:
                logger.exception("Failed to persist transcript repair for %s", session_id)

        return {
            "merged": merged,
            "dropped_assistant": dropped_assistant,
            "dropped_orphan_tool": dropped_orphan_tool,
        }

    # ── Hermes-style in-flight repair ───────────────────────
    def repair_in_flight(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
    ) -> Dict[str, int]:
        """Reconcile in-memory assistant rows carrying ``_row_id`` with
        committed SQLite rows inside the active write transaction.

        Side effects on the conn (transaction):
          - blank target rows → UPDATE content in place
          - non-blank winner rows → adopt canonical content (no overwrite)

        Returns counts {blank_updated, winner_adopted, fresh_insert}.
        """
        # Spirit's content column stores plain str (no encode/decode in
        # v0.4.0); both pass-through functions match Hermes's contract.
        return _repair_in_flight_on_conn(
            self.db.conn,
            session_id,
            messages,
            encode_content_fn=lambda x: x,
            decode_content_fn=lambda x: x,
        )


def _repair_in_flight_on_conn(
    conn: sqlite3.Connection,
    session_id: str,
    messages: List[Dict[str, Any]],
    encode_content_fn: Callable[[Any], Any],
    decode_content_fn: Callable[[Any], Any],
) -> Dict[str, int]:
    """Module-level repair hook (mirrors Hermes partition logic)."""
    fresh: List[Dict[str, Any]] = []
    blank_updated = 0
    winner_adopted = 0
    for msg in messages:
        existing_row_id = msg.get("_row_id") if isinstance(msg, dict) else None
        target_row = None
        if isinstance(existing_row_id, int) and msg.get("role", "unknown") == "assistant":
            target_row = _active_assistant_row(conn, session_id, existing_row_id)
        if target_row is None:
            fresh.append(msg)
            continue
        target_id = int(target_row["id"])
        decoded = decode_content_fn(target_row["content"])
        msg["_row_id"] = target_id
        if is_content_blank(decoded):
            conn.execute(
                "UPDATE messages SET content = ? "
                "WHERE id = ? AND session_id = ? AND active = 1",
                (encode_content_fn(msg.get("content")), target_id, session_id),
            )
            blank_updated += 1
            fresh.append(msg)
        else:
            msg["_canonical_content"] = decoded
            winner_adopted += 1
            fresh.append(msg)
    # Mutate the caller's list in place to reflect partition (drop
    # adopted winners from the INSERT batch).
    messages.clear()
    messages.extend(fresh)
    return {
        "blank_updated": blank_updated,
        "winner_adopted": winner_adopted,
        "fresh_insert": sum(
            1 for m in fresh
            if not m.get("_canonical_content") and m.get("_row_id") is None
        ),
    }
