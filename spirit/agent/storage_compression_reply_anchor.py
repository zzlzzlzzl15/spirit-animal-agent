"""spirit/agent/storage_compression_reply_anchor.py — keep the just-delivered assistant reply live.

v0.4.0 (Hermes-aligned): sibling of ``storage_compression_manual.py`` (the facade). The facade
late-imports this module at its single call site in ``compress_now``; this module must never
import the facade at module level (import cycle). Local helpers (``_message_text`` /
``_is_real_user_message`` / ``_fresh_compaction_message_copy`` / ``is_compaction_summary_message``)
are intentionally defined here — Spirit does not yet have a stand-alone ``message_content`` module
(§G.8) so we don't reach across files for these.

Reference: hermes agent/conversation_compression_reply_anchor.py (252 lines).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, FrozenSet, List, Optional

from ..storage.session_persistence import _DB_PERSISTED_MARKER

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Local helpers — kept inside this module so the file is importable
# in isolation (no circular import with the facade).
# ────────────────────────────────────────────────────────────────────


def _message_text(message: Dict[str, Any]) -> str:
    """Flatten a message's visible text. Accepts a string ``content`` or a parts list (OpenAI /
    Anthropic / Responses shapes — text / image / audio / tool-call parts).

    Str content returns ``content``; a list returns the joined text of every ``text`` /
    ``input_text`` / ``output_text`` part (with type-fallback for untyped parts). Non-text parts
    contribute ``""``. Multimodal rows whose ``content`` is neither a ``str`` nor a ``list`` fall
    back to ``str(content)`` so something visible always reaches the twin check.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: List[str] = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
                continue
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in ("text", "input_text", "output_text"):
                out.append(str(part.get("text") or ""))
            elif "text" in part:  # untyped / partial-shape fallback
                out.append(str(part.get("text") or ""))
        return "".join(out)
    if content is None:
        return ""
    return str(content)


def _is_real_user_message(message: Any) -> bool:
    """True iff *message* is a real user turn (not a tool result, not a compaction summary, not a
    user-role scaffolding row such as a todo snapshot or handoff).
    """
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    if message.get("tool_call_id"):
        return False
    if message.get("tool_calls"):
        return False
    if is_compaction_summary_message(message):
        return False
    return True


def is_compaction_summary_message(message: Any) -> bool:
    """True iff *message* is a compaction summary row (any of the marker keys Spirit engines stamp).

    The marker keys track Hermes's ``_COMPACTION_SUMMARY_PREFIXES`` ("[Old conversation summary]" /
    "[Earlier context summary]" / "[Context summary]") plus Spirit's own compaction prefix — any
    row whose ``content`` starts with one of these prefixes is treated as a summary, not as a real
    user/assistant turn.
    """
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if not isinstance(content, str):
        return False
    prefixes = (
        "[Old conversation summary]",
        "[Earlier context summary]",
        "[Context summary]",
        "[上下文压缩 — 仅供参考]",
    )
    return any(content.startswith(p) for p in prefixes)


def _fresh_compaction_message_copy(message: Dict[str, Any]) -> Dict[str, Any]:
    """Return a shallow copy of *message* with the persistence marker cleared.

    The marker stamps that the row was already flushed to disk; a compaction's re-INSERT rewrites
    row ids, and only an *unmarked* copy can be safely re-stamped. Stripping the marker here keeps
    the post-commit contract simple (#98450): callers re-stamp after the commit lands.
    """
    copy = dict(message)
    copy.pop(_DB_PERSISTED_MARKER, None)
    return copy


def _coalesce_tool_call_id(call_or_message: Dict[str, Any]) -> Optional[str]:
    """Pairing id of a tool-call dict or a tool row.

    Hermes's ``coalesce_tool_call_id`` keys by ``call_id`` before ``id`` because Codex rows carry
    ``id=fc_…`` AND ``call_id=call_…`` and results pair on the latter. Spirit mirrors this.
    """
    if not isinstance(call_or_message, dict):
        return None
    for key in ("call_id", "id"):
        val = call_or_message.get(key)
        if isinstance(val, str) and val:
            return val
    return None


# ────────────────────────────────────────────────────────────────────
# Public surface — keep the just-delivered assistant reply live (#118900)
# ────────────────────────────────────────────────────────────────────


def _ensure_compressed_keeps_last_assistant_reply(
    original_messages: List[Dict[str, Any]],
    compressed: List[Dict[str, Any]],
    *,
    session_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Keep the latest visible assistant reply live across compaction (#118900).

    A reply that just finished streaming is the row the user is reading; when an
    engine's fold drops it into the summary region, the commit archives its row
    (active=0) and surfaces that render it from the active set drop it on the
    next refresh — while the content sits intact on disk. The built-in
    compressor keeps this row in the tail, but plugin engines implement their
    own ``compress()`` without that guard, so it is enforced here, next to
    ``_ensure_compressed_has_user_turn``, where every engine and every path
    (threshold preflight, engine maintenance, manual /compress; in-place and
    rotation) routes through.

    Runs FIRST at the commit boundary — before the todo-snapshot fold and the
    user-turn anchor — so the follower rows still read as the engine returned
    them and the later passes see the reply as the tail they place around
    (``[summary, reply, U_new]`` / ``[..., reply, TODO-row]``). Placement is
    chronological (see ``_reply_insertion_index``); when the only slot would
    sit assistant-adjacent to a non-twin row the reinsertion is skipped, never
    forced: strict alternation beats recovery. Empty (reasoning-only) and
    tool-call rows are out of scope: the former is a different bug family, the
    latter must keep its atomic tool group.

    Returns the exact dict placed into ``compressed`` (the caller names it as a
    carried row at commit), or ``None`` when nothing was reinserted — every
    skip that is not "the reply is already there" is logged here with its
    reason.
    """
    reply, reply_text = None, ""
    for message in reversed(original_messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if message.get("tool_calls") or is_compaction_summary_message(message):
            continue
        reply_text = _message_text(message).strip()
        if not reply_text:
            continue
        reply = message
        break
    if reply is None:
        return None

    def _is_reply_twin(message: Any) -> bool:
        if (
            not isinstance(message, dict)
            or message.get("role") != "assistant"
            or message.get("tool_calls")
        ):
            return False
        return _same_visible_content(message, reply, reply_text)

    if any(_is_reply_twin(message) for message in compressed):
        return None
    anchor = _fresh_compaction_message_copy(reply)
    # Identity scan, not list.index(): duplicate rows with identical content
    # exist in the wild, and == would resolve to the older twin (#118900).
    reply_pos = next(i for i, m in enumerate(original_messages) if m is reply)
    index = _reply_insertion_index(
        original_messages[reply_pos + 1:],
        compressed,
        reused_ids=_reused_tool_call_ids(original_messages),
    )
    if index is None:
        logger.warning(
            "Compression: engine folded away the just-delivered assistant reply and the "
            "surviving tool rounds reuse tool-call ids, so its slot cannot be located; "
            "not reinserting it (session=%s).",
            session_id or "none",
        )
        return None
    # Either neighbour already being a non-tool assistant twin means the reply is
    # effectively present at this seam; inserting would create assistant;assistant.
    neighbours = [compressed[i] for i in (index - 1, index) if 0 <= i < len(compressed)]
    if any(_is_reply_twin(message) for message in neighbours):
        return None
    # A non-twin assistant neighbour (an older kept reply, the engine's own paraphrase of
    # this one, or a tool-call row) means the slot cannot take the row without
    # assistant;assistant adjacency, and sliding past it would only move the seam while
    # putting the newest reply before an older one. Strict alternation is the invariant
    # every provider path relies on; the reply stays readable on disk, so skip the
    # recovery rather than break the shape.
    if any(isinstance(m, dict) and m.get("role") == "assistant" for m in neighbours):
        logger.warning(
            "Compression: engine folded away the just-delivered assistant reply and left a "
            "non-matching assistant row at its slot; not reinserting it to keep strict "
            "role alternation (session=%s).",
            session_id or "none",
        )
        return None
    # The built-in compressor deliberately lets a reply that alone dominates the window fold
    # (``_ensure_last_assistant_message_in_tail`` yields to ``head_end + 1``) so compaction can
    # make progress; forcing it back would make the candidate no smaller than the input. Mirror
    # the commit-site no-growth guard: if reinserting would put the candidate between
    # ``rough_out`` (inclusive) and ``rough_out + anchor`` (exclusive), skip the recovery.
    rough_in = _estimate_messages_tokens_rough(original_messages)
    rough_out = _estimate_messages_tokens_rough(compressed)
    if rough_out < rough_in < rough_out + _estimate_messages_tokens_rough([anchor]):
        logger.info(
            "Compression: not reinserting the just-delivered assistant reply; it would stop "
            "this compaction from shrinking the transcript (session=%s).",
            session_id or "none",
        )
        return None
    # Post-commit contract (#98450, mirrors _insert_real_user_anchor._place):
    # archive_and_compact durably writes every dict in `compressed` as the new
    # active set, so stamp the copy or the next flush re-INSERTs it as a duplicate.
    anchor[_DB_PERSISTED_MARKER] = True
    compressed.insert(index, anchor)
    return anchor


def _same_visible_content(left: Dict[str, Any], right: Dict[str, Any], right_text: str) -> bool:
    """Visible-content equality: normalized text when either side has any, else raw ``content``.

    A conforming engine may hand a row back whitespace-stripped or with parts-list content
    re-rendered as a string; raw ``==`` would treat that as a different row (the twin check
    would insert a duplicate, the follower scan would miss its slot). ``left`` is the
    per-candidate row and is flattened here; ``right_text`` is the caller's row flattened once
    per scan rather than once per candidate.
    """
    left_text = _message_text(left).strip()
    if left_text or right_text:
        return left_text == right_text
    return left.get("content") == right.get("content")


def _tool_call_ids(message: Dict[str, Any]) -> FrozenSet[str]:
    """Pairing ids of an assistant row's tool calls, keyed the way the pre-call sanitizer keys
    them (``call_id`` before ``id``, composite ``call|item`` split).
    """
    return frozenset(
        filter(
            None,
            (_coalesce_tool_call_id(tc) for tc in (message.get("tool_calls") or ())),
        )
    )


def _tool_result_id(message: Dict[str, Any]) -> str:
    """The ``tool_call_id`` of a ``tool`` row under the same normalisation as ``_tool_call_ids``."""
    cid = _coalesce_tool_call_id({"id": message.get("tool_call_id")})
    return cid or ""


def _reused_tool_call_ids(messages: List[Dict[str, Any]]) -> FrozenSet[str]:
    """Tool-call ids that more than one assistant row in ``messages`` uses.

    ``tool_call_id`` is not unique in practice: llama.cpp emits one constant id for every call
    it ever returns, and other providers reuse ``call_0`` per turn. Such ids cannot locate a
    specific row.
    """
    seen: Dict[str, int] = {}
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "assistant":
            for call_id in _tool_call_ids(message):
                seen[call_id] = seen.get(call_id, 0) + 1
    return frozenset(call_id for call_id, count in seen.items() if count > 1)


def _reply_insertion_index(
    followers: List[Dict[str, Any]],
    compressed: List[Dict[str, Any]],
    *,
    reused_ids: FrozenSet[str] = frozenset(),
) -> Optional[int]:
    """Chronologically correct slot for the dropped reply inside ``compressed``.

    ``followers`` are the ORIGINAL rows after the reply (the next user turn, its tool rounds, ...).
    The slot is just before the surviving row that originally followed it: the LAST match in
    ``compressed`` on role + normalized text (a first-match scan binds to an older identical
    "ok"/"thanks" twin and resurfaces the reply mid-history; raw ``==`` misses a follower the
    engine re-rendered from a parts list). When no follower survives but the original had a real
    user turn after the reply, the reply must still never land after a trailing REAL user row —
    that is the turn the loop is about to answer, and an ``assistant`` tail would present the old
    reply as its answer — so it goes right before that row instead. No followers at all
    (idle/maintenance compaction right after the reply) → the reply is genuinely the tail.

    Tool rounds whose ids the transcript reuses (``reused_ids``) are not usable anchors: the id
    would bind to whichever round happens to sit last and put the reply mid-chain. Such followers
    are passed over in favour of the remaining real followers; when none locates the slot and a
    reused-id round survives in ``compressed``, the placement is unknown and ``None`` is returned
    (skip).
    """
    def _row_ids(message: Dict[str, Any]) -> FrozenSet[str]:
        return _tool_call_ids(message) | frozenset(
            filter(None, (_tool_result_id(message),))
        )

    followers = [f for f in followers if isinstance(f, dict)]
    ambiguous = False
    for follower in followers:
        follower_role = follower.get("role")
        follower_text = _message_text(follower).strip()
        follower_call_ids = _tool_call_ids(follower)
        if _row_ids(follower) & reused_ids:
            ambiguous = ambiguous or any(
                isinstance(m, dict) and _row_ids(m) & reused_ids for m in compressed
            )
            continue
        for pos in range(len(compressed) - 1, -1, -1):
            message = compressed[pos]
            if not isinstance(message, dict) or message.get("role") != follower_role:
                continue
            message_call_ids = _tool_call_ids(message)
            if follower_call_ids or message_call_ids:
                if message_call_ids == follower_call_ids:
                    return pos
                continue
            if follower_role == "tool" and (
                _tool_result_id(follower) or _tool_result_id(message)
            ):
                if _tool_result_id(message) == _tool_result_id(follower):
                    return pos
                continue
            if _same_visible_content(message, follower, follower_text):
                return pos
    if ambiguous:
        return None
    if (
        followers
        and any(_is_real_user_message(f) for f in followers)
        and compressed
        and _is_real_user_message(compressed[-1])
    ):
        return len(compressed) - 1
    return len(compressed)


def _estimate_messages_tokens_rough(messages: List[Dict[str, Any]]) -> int:
    """Local rough estimate (chars / 3.5) for the in-module no-growth check.

    The real ``estimate_messages_tokens_rough`` lives in ``model_metadata``; importing it here
    would force this module to reach across files for a value that only matters as a coarse
    no-growth comparison. The CLI default ``1 token ≈ 3.5 chars`` matches the central config
    (``compression.chars_per_token``), so this stays accurate enough for the boundary check.
    """
    if not messages:
        return 0
    total_chars = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, str):
                    total_chars += len(part)
                elif isinstance(part, dict):
                    total_chars += len(_message_text(part))
        # tool_calls text is included indirectly via the structured content above.
    return int(total_chars / 3.5)


__all__ = [
    "_ensure_compressed_keeps_last_assistant_reply",
    "_same_visible_content",
    "_tool_call_ids",
    "_tool_result_id",
    "_reused_tool_call_ids",
    "_reply_insertion_index",
    "_message_text",
    "_is_real_user_message",
    "is_compaction_summary_message",
    "_fresh_compaction_message_copy",
    "_coalesce_tool_call_id",
]
