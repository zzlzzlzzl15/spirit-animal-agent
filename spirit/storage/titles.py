"""spirit/storage/titles.py — Auto session title generation.

v0.3.0 (Hermes-style): simplified version of hermes_state_titles.py.

Hermes has full LLM-based title generation with prompt tuning. Spirit's
v0.3.0 ships a simpler approach:

1. Heuristic fallback (always works, no LLM call):
   - Take the first user message
   - Truncate to 30 chars
   - Strip newlines

2. Optional LLM hook (set in SpiritAgent config):
   - If `agent.title_generator` is provided, call it with the user msg
   - It returns a ≤30 char title
   - If it fails, fall back to heuristic

The actual title generation is triggered by `agent.add_message()` when
it sees the first user message. This module only provides the helpers;
the trigger lives in spirit/agent/agent.py.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# Title length cap (consistent across heuristic + LLM)
MAX_TITLE_LEN = 30


def heuristic_title(user_msg: str, max_len: int = MAX_TITLE_LEN) -> str:
    """Generate a heuristic title from the first user message.

    Rules:
    - Strip leading whitespace
    - Collapse internal whitespace
    - Remove common prefixes like "请帮我" / "帮我"
    - Truncate to max_len (with … suffix if truncated)
    """
    if not user_msg:
        return ""
    s = user_msg.strip()
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s)
    # Strip common Chinese imperative prefixes
    s = re.sub(r"^(请帮我|帮我|请|你好[,，]\s*|请问我可以\s*)", "", s)
    # Truncate
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


async def generate_title_async(
    user_msg: str,
    *,
    llm_call: Optional[Callable[[str], str]] = None,
    max_len: int = MAX_TITLE_LEN,
) -> str:
    """Async title generator.

    Args:
        user_msg: first user message in the session
        llm_call: optional LLM function (str) -> str. Used to generate
                  a smarter title. None → use heuristic.
        max_len: max title length

    Returns:
        Generated title (≤ max_len chars). Always non-empty for non-empty input.
    """
    if not user_msg:
        return ""
    if llm_call is None:
        return heuristic_title(user_msg, max_len)
    try:
        prompt = (
            f"为以下用户消息生成一个简洁的中文标题（≤{max_len}字），"
            f"直接输出标题文本，不要任何解释或前缀：\n\n{user_msg[:500]}"
        )
        result = llm_call(prompt).strip()
        # Strip quotes / prefixes LLM might add
        _quote_chars = "「」" + chr(34) + chr(39) + "`" + " "
        result = result.strip(_quote_chars)
        # Take first line if multi-line
        result = result.split("\n")[0].strip()
        if len(result) > max_len:
            result = result[: max_len - 1] + "…"
        if not result:
            return heuristic_title(user_msg, max_len)
        return result
    except Exception as e:  # noqa: BLE001
        logger.debug("LLM title 生成失败, 用启发式 fallback: %s", e)
        return heuristic_title(user_msg, max_len)


def generate_title_sync(
    user_msg: str,
    *,
    llm_call: Optional[Callable[[str], str]] = None,
    max_len: int = MAX_TITLE_LEN,
) -> str:
    """Synchronous version (no asyncio) for simpler callers.

    Spirit's main agent loop is sync, so this is the primary entry point.
    """
    if not user_msg:
        return ""
    if llm_call is None:
        return heuristic_title(user_msg, max_len)
    try:
        prompt = (
            f"为以下用户消息生成一个简洁的中文标题（≤{max_len}字），"
            f"直接输出标题文本，不要任何解释或前缀：\n\n{user_msg[:500]}"
        )
        result = llm_call(prompt).strip()
        _quote_chars2 = "「」" + chr(34) + chr(39) + "`" + " "
        result = result.strip(_quote_chars2)
        result = result.split("\n")[0].strip()
        if len(result) > max_len:
            result = result[: max_len - 1] + "…"
        if not result:
            return heuristic_title(user_msg, max_len)
        return result
    except Exception as e:  # noqa: BLE001
        logger.debug("LLM title 生成失败, fallback: %s", e)
        return heuristic_title(user_msg, max_len)


def maybe_update_title(
    db,
    session_id: str,
    user_msg: str,
    *,
    llm_call: Optional[Callable[[str], str]] = None,
) -> str:
    """Idempotent title setter: only sets if title is currently NULL.

    Called by agent.add_message() on first user message.

    Args:
        db: SessionDB instance (mixin must provide set_session_title, get_session)
        session_id: target session
        user_msg: first user message
        llm_call: optional LLM function

    Returns:
        The new title (or existing one if already set).
    """
    existing = db.get_session(session_id) or {}
    if existing.get("title"):
        # Title already set (user-set or auto-set on first msg). Don't overwrite.
        return existing["title"]
    title = generate_title_sync(user_msg, llm_call=llm_call)
    if title:
        db.set_session_title(session_id, title, source="auto")
    return title
