"""spirit/agent/storage_message_summarizer.py — summarize compressed messages.

v0.4.0 (Hermes-aligned): given a list of compacted messages, produce a
short summary using an injected LLM caller (with heuristic fallback).

Reference: hermes agent/message_summarizer.py.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, List, Optional

from ..storage.session_db import SessionDB

logger = logging.getLogger(__name__)


LLMCaller = Callable[[str, float, int], str]


def _heuristic_summary(messages: List[Dict[str, Any]], max_chars: int = 280) -> str:
    """Pull the first N chars of each user message until the budget is spent."""
    out: List[str] = []
    remaining = max_chars
    for m in messages:
        if m.get("role") != "user":
            continue
        text = (m.get("content") or "").strip()
        if not text:
            continue
        snippet = text[:remaining]
        out.append(snippet)
        remaining -= len(snippet)
        if remaining <= 0:
            break
    summary = " • ".join(out)
    return re.sub(r"\s+", " ", summary).strip()


class StorageMessageSummarizer:
    """LLM-driven summarization of compacted message groups."""

    def __init__(
        self,
        db: SessionDB,
        llm_caller: Optional[LLMCaller] = None,
        max_chars: int = 280,
    ):
        self.db = db
        self.llm_caller = llm_caller
        self.max_chars = max_chars

    def summarize(self, session_id: str, message_ids: List[int]) -> str:
        msgs = self.db.get_messages_by_ids(session_id, message_ids)
        if not msgs:
            return ""
        if self.llm_caller is not None:
            try:
                transcript = "\n".join(
                    f"[{m.get('role','?')}] {m.get('content','')[:500]}" for m in msgs
                )
                prompt = (
                    "Summarize the following conversation excerpt in "
                    f"{self.max_chars} characters or less:\n\n" + transcript
                )
                out = self.llm_caller(prompt, 0.2, 256).strip()
                if out:
                    return out[: self.max_chars]
            except Exception:
                logger.exception("LLM summarization failed; using heuristic")
        return _heuristic_summary(msgs, self.max_chars)
