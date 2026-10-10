"""spirit/agent/storage_think_scrubber.py — strip reasoning/thinking blocks.

v0.4.0 (Hermes-aligned): two complementary surfaces share one tag-name
list, so a tag added here is hidden everywhere a change happens.

1. **StorageThinkScrubber.scrub_assistant_message / scrub_pending_assistant**
   Post-hoc cleanup on a fully-formed assistant content string. Used when
   the wire response is buffered and we want to split "what the model
   thought" into ``reasoning_content`` and the visible reply into
   ``content``.

2. **StreamingThinkScrubber.feed(delta)**
   Stateful per-delta scrubber for SSE / chunked responses. A naive
   ``re.sub`` per delta loses an opening ``<think>`` that arrives alone;
   this class holds partial-tag tails at chunk boundaries until they
   resolve. An open tag only starts a block at a block boundary (stream
   start, after a newline, or whitespace-only preceding line), so prose
   *mentioning* ``<think>`` is not over-stripped. Closed pairs are
   always suppressed (intentional — they almost certainly indicate
   leaked reasoning).

THINK_TAG_NAMES = ("think", "thinking", "reasoning", "thought",
"REASONING_SCRATCHPAD")

Reference: hermes agent/think_scrubber.py.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from ..storage.session_db import SessionDB

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Five-name tag taxonomy (the one source of truth)
# ────────────────────────────────────────────────────────────────────


THINK_TAG_NAMES: Tuple[str, ...] = (
    "think",
    "thinking",
    "reasoning",
    "thought",
    "REASONING_SCRATCHPAD",
)
THINK_OPEN_TAGS: Tuple[str, ...] = tuple(
    f"<{name.lower()}>" for name in THINK_TAG_NAMES
)
THINK_CLOSE_TAGS: Tuple[str, ...] = tuple(
    f"</{name.lower()}>" for name in THINK_TAG_NAMES
)


# Compiled post-hoc regexes — used by ``_scrub_think_tags`` (whole-string
# scrub) and the orphan-close stripper on the streaming side.
_OPEN_TAG_GROUP = "(?:" + "|".join(re.escape(t) for t in THINK_OPEN_TAGS) + ")"
_CLOSE_TAG_GROUP = "(?:" + "|".join(re.escape(t) for t in THINK_CLOSE_TAGS) + ")"
_THINK_BLOCK_RE = re.compile(
    _OPEN_TAG_GROUP + r"(.*?)" + _CLOSE_TAG_GROUP,
    re.DOTALL | re.IGNORECASE,
)
_LEADING_THINK_RE = re.compile(
    r"^\s*(?:" + "|".join(re.escape(t) for t in THINK_OPEN_TAGS) + ")",
    re.IGNORECASE,
)
_ORPHAN_CLOSE_RE = re.compile(
    _CLOSE_TAG_GROUP + r"[ \t\n\r]*",
    re.IGNORECASE,
)


# ────────────────────────────────────────────────────────────────────
# Post-hoc helpers (whole-string scrub)
# ────────────────────────────────────────────────────────────────────


def _scrub_think_tags(content: str) -> Tuple[str, Optional[str]]:
    """Strip ``<tag>...</tag>`` blocks for any of the five tag names.

    Returns ``(visible_content, extracted_reasoning)``. ``reasoning`` is
    the joined bodies of every matched block (separated by blank lines),
    or ``None`` when the input had none.
    """
    if not content:
        return "", None
    matches = _THINK_BLOCK_RE.findall(content)
    visible = _THINK_BLOCK_RE.sub("", content).strip()
    reasoning = "\n\n".join(m.strip() for m in matches).strip() or None
    return visible, reasoning


def strip_think_blocks(content: Optional[str]) -> str:
    """Return ``content`` with reasoning blocks removed. Used by the
    title-generator prose fallback (it must not store ``<think>``
    fragments as the session title).

    Public surface: kept importable for any caller that already holds a
    fully-buffered string.
    """
    if not content:
        return ""
    return _THINK_BLOCK_RE.sub("", content)


# ────────────────────────────────────────────────────────────────────
# Streaming scrubber (per-delta, stateful)
# ────────────────────────────────────────────────────────────────────


class StreamingThinkScrubber:
    """Stateful scrubber for streaming reasoning/thinking blocks.

    State:

    - ``_in_block`` — inside an open block; further text is discarded
      until the close tag.
    - ``_buf`` — held-back partial-tag tail (a tag that arrived split
      across two deltas is buffered here until either ``feed`` resolves
      it or ``flush`` releases it as prose).
    - ``_last_emitted_ended_newline`` — True iff the last emission ended
      with ``\\n`` or nothing was emitted yet; gates whether an open
      tag at buffer position 0 sits at a block boundary.

    An open tag starts a block only at a block boundary (stream start /
    after a newline / whitespace-only preceding line), so prose that
    *mentions* ``<think>`` is not suppressed. Closed pairs are always
    suppressed — they almost certainly indicate leaked reasoning.
    """

    _OPEN_TAGS: Tuple[str, ...] = THINK_OPEN_TAGS
    _CLOSE_TAGS: Tuple[str, ...] = THINK_CLOSE_TAGS
    _ALL_TAGS: Tuple[str, ...] = _OPEN_TAGS + _CLOSE_TAGS
    _MAX_TAG_LEN: int = max(len(tag) for tag in _ALL_TAGS)
    _ORPHAN_CLOSE_RE = _ORPHAN_CLOSE_RE

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Reset all state. Call at the top of every new turn."""
        self._in_block: bool = False
        self._buf: str = ""
        self._last_emitted_ended_newline: bool = True

    def feed(self, text: str) -> str:
        """Feed one delta; return the scrubbed visible portion.

        Returns ``""`` when the delta was all reasoning or held back as
        a partial tag. The caller appends the returned string to its
        outbound visible-text buffer; the held-back tail stays in
        ``_buf`` until the next ``feed`` or ``flush``.
        """
        if not text:
            return ""
        buf = self._buf + text
        self._buf = ""
        out: List[str] = []

        while buf:
            if self._in_block:
                close_idx, close_len = self._find_first_tag(buf, self._CLOSE_TAGS)
                if close_idx == -1:
                    # No close yet: hold back a possible partial close-tag
                    # prefix, drop the rest (it would be reasoning anyway).
                    self._hold_partial(buf, self._CLOSE_TAGS)
                    break
                buf = buf[close_idx + close_len:]
                self._in_block = False
                continue

            # Priority 1: closed <tag>X</tag> pair anywhere (even inline
            # pairs are almost certainly leaked reasoning). Priority 2:
            # unterminated open tag at a block boundary (gated so prose
            # mentioning '<think>' isn't over-stripped). Earliest wins.
            pair = self._find_earliest_closed_pair(buf)
            open_idx, open_len = self._find_open_at_boundary(buf, out)
            if pair is not None and (open_idx == -1 or pair[0] <= open_idx):
                self._emit(out, buf[: pair[0]])
                buf = buf[pair[1]:]
                continue
            if open_idx != -1:
                self._emit(out, buf[:open_idx])
                self._in_block = True
                buf = buf[open_idx + open_len:]
                continue

            # No resolvable tag: hold back any partial-tag prefix at the
            # tail so a tag split across deltas isn't missed, then emit
            # the rest.
            self._emit(out, self._hold_partial(buf, self._ALL_TAGS))
            break

        return "".join(out)

    def flush(self) -> str:
        """End-of-stream flush.

        Inside an unterminated block, the held-back content is discarded
        (leaking partial reasoning is worse than a truncated answer).
        Otherwise the tail is emitted verbatim. Always resets the
        boundary flag — intra-turn retries flush then stream again
        without ``reset()``, and a stale False flag made the new
        stream's opening ``<think>`` look mid-line.
        """
        tail = "" if self._in_block else self._buf
        self._buf = ""
        self._in_block = False
        self._last_emitted_ended_newline = True
        return self._strip_orphan_close_tags(tail) if tail else ""

    # ── internal helpers ────────────────────────────────────────

    def _emit(self, out: List[str], text: str) -> None:
        """Append visible prose to *out* (orphan close tags stripped) and
        track the newline flag.
        """
        text = self._strip_orphan_close_tags(text)
        if text:
            out.append(text)
            self._last_emitted_ended_newline = text.endswith("\n")

    def _hold_partial(self, buf: str, tags: Tuple[str, ...]) -> str:
        """Move a trailing partial-tag prefix of *buf* into ``_buf``;
        return the remainder.
        """
        held = self._max_partial_suffix(buf, tags)
        self._buf = buf[-held:] if held else ""
        return buf[:-held] if held else buf

    @staticmethod
    def _find_first_tag(
        buf: str, tags: Tuple[str, ...]
    ) -> Tuple[int, int]:
        """Return ``(earliest_index, tag_length)`` over *tags*
        (case-insensitive), or ``(-1, 0)``.
        """
        buf_lower = buf.lower()
        hits = [
            (idx, len(tag))
            for tag in tags
            if (idx := buf_lower.find(tag)) != -1
        ]
        return min(hits) if hits else (-1, 0)

    def _find_earliest_closed_pair(self, buf: str):
        """``(start_idx, end_idx)`` of the earliest ``<tag>...</tag>`` pair
        (case-insensitive), else ``None``.
        """
        buf_lower = buf.lower()
        pairs: List[Tuple[int, int]] = []
        for open_tag, close_tag in zip(self._OPEN_TAGS, self._CLOSE_TAGS):
            open_idx = buf_lower.find(open_tag)
            close_idx = (
                buf_lower.find(close_tag, open_idx + len(open_tag))
                if open_idx != -1
                else -1
            )
            if close_idx != -1:
                pairs.append((open_idx, close_idx + len(close_tag)))
        return min(pairs) if pairs else None

    def _find_open_at_boundary(
        self, buf: str, already_emitted: List[str]
    ) -> Tuple[int, int]:
        """Return the earliest block-boundary open-tag ``(idx, len)`` or
        ``(-1, 0)``.
        """
        buf_lower = buf.lower()
        hits: List[Tuple[int, int]] = []
        for tag in self._OPEN_TAGS:
            idx = buf_lower.find(tag)
            while idx != -1 and not self._is_block_boundary(
                buf, idx, already_emitted
            ):
                idx = buf_lower.find(tag, idx + 1)
            if idx != -1:
                hits.append((idx, len(tag)))
        return min(hits) if hits else (-1, 0)

    def _is_block_boundary(
        self, buf: str, idx: int, already_emitted: List[str]
    ) -> bool:
        """True iff *idx* is a block boundary: position 0 after a
        newline-terminated (or no) prior emission, or any position whose
        preceding text on the current line is whitespace-only (when no
        newline precedes it in *buf*, the prior emission must also
        have ended with a newline).
        """
        prior_newline = (
            already_emitted[-1].endswith("\n")
            if already_emitted
            else self._last_emitted_ended_newline
        )
        if idx == 0:
            return prior_newline
        preceding = buf[:idx]
        last_nl = preceding.rfind("\n")
        return (
            prior_newline if last_nl == -1 else True
        ) and preceding[last_nl + 1:].strip() == ""

    @classmethod
    def _max_partial_suffix(cls, buf: str, tags: Tuple[str, ...]) -> int:
        """Longest buf-suffix that is a strict prefix of any tag (full
        matches are real tags, handled elsewhere).
        """
        buf_lower = buf.lower()
        for i in range(min(len(buf_lower), cls._MAX_TAG_LEN - 1), 0, -1):
            suffix = buf_lower[-i:]
            if any(
                len(tag) > i and tag.startswith(suffix) for tag in tags
            ):
                return i
        return 0

    @classmethod
    def _strip_orphan_close_tags(cls, text: str) -> str:
        """Remove close tags with no matching open (always noise) plus
        trailing whitespace.
        """
        return cls._ORPHAN_CLOSE_RE.sub("", text) if "</" in text else text


# ────────────────────────────────────────────────────────────────────
# Spirit compatibility class
# ────────────────────────────────────────────────────────────────────


class StorageThinkScrubber:
    """Apply ``<tag>...</tag>`` scrubbing before saving messages.

    Spirit-side entry point used by ``StorageTurnFacade.save_turn``. The
    five tag names live in ``THINK_TAG_NAMES`` so adding one updates
    every surface at once.
    """

    def __init__(self, db: SessionDB):
        self.db = db

    def scrub_assistant_message(
        self,
        session_id: str,
        message_id: int,
        content: str,
    ) -> Dict[str, Any]:
        """Split ``content`` into visible + reasoning, persist both halves."""
        visible, reasoning = _scrub_think_tags(content)
        try:
            self.db.update_message(
                session_id=session_id,
                message_id=message_id,
                content=visible,
                reasoning_content=reasoning or "",
            )
        except Exception:
            logger.exception(
                "scrub_assistant_message failed for %s:%d",
                session_id, message_id,
            )
        return {"visible": visible, "reasoning": reasoning}

    def scrub_pending_assistant(self, session_id: str) -> int:
        """Backfill any assistant row that still has tags in its
        ``content`` (the streaming path landed before scrubbing).
        """
        msgs = self.db.get_messages(session_id)
        updated = 0
        for m in msgs:
            if m.get("role") != "assistant":
                continue
            if m.get("reasoning_content"):
                continue
            content = m.get("content") or ""
            if not _LEADING_THINK_RE.match(content) and not any(
                tag in content for tag in THINK_OPEN_TAGS
            ):
                continue
            self.scrub_assistant_message(
                session_id, m["id"], content,
            )
            updated += 1
        return updated


# ────────────────────────────────────────────────────────────────────
# Public exports
# ────────────────────────────────────────────────────────────────────


__all__ = [
    # Five-name taxonomy
    "THINK_TAG_NAMES",
    "THINK_OPEN_TAGS",
    "THINK_CLOSE_TAGS",
    # Helpers
    "strip_think_blocks",
    "_scrub_think_tags",
    # Stateful streaming scrubber
    "StreamingThinkScrubber",
    # Spirit compatibility class
    "StorageThinkScrubber",
]