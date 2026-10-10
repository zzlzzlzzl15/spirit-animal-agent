"""spirit/agent/storage_message_sanitization.py — normalize before persistence.

v0.4.0 (Hermes-aligned): the six-subsets message + tool-payload sanitization
suite (pure functions, documented in-place mutation), plus Spirit's own
``StorageMessageSanitization`` policy class that drops oversized / suspicious
messages before they reach the DB.

Six-subsets (per design doc §G.6):
  1. ``sanitize_outbound_kwargs``  — outbound-request chokepoint; surrogate +
     optional non-ASCII strip in-place. Returns True when anything changed.
  2. ``coerce_tool_name``          — coerce a *replayed* tool/function name to
     ``^[A-Za-z0-9_-]{1,64}$`` (deterministic, prompt-cache safe).
  3. ``_sanitize_surrogates``      — replace lone surrogate code points with
     U+FFFD; O(1) skip when text is ASCII.
  4. ``_sanitize_messages``        — apply a string-fix to every message dict
     in-place (content / part text / name / tool_call arguments; deep=True
     also touches tool_call ids, function names, nested reasoning_details).
  5. ``_sanitize_structure``       — apply a string-fix to every str inside a
     nested dict/list payload in-place.
  6. ``_rebalance_json_closers``   — close a JSON prefix's open braces/brackets
     in stack order; ``None`` when text ends inside an unterminated string.
  7. ``_escape_invalid_chars_in_json_strings`` — escape literal control chars
     (0x00-0x1F) inside JSON string values as ``\\uXXXX`` (for llama.cpp-style
     output mixing control chars with other malformations).

The class ``StorageMessageSanitization`` is the Spirit-specific policy layer
above these helpers: drop oversized / suspicious messages, enforce max content
length, normalize whitespace, redact obvious secrets.

Reference: hermes agent/message_sanitization.py.
"""

from __future__ import annotations

import json
import logging
import re
from functools import partial
from typing import Any, Callable, Dict, List, Optional

from ..storage.session_db import SessionDB

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Spirit policy-layer class
# ────────────────────────────────────────────────────────────────────


DEFAULT_MAX_CONTENT_CHARS = 200_000
DEFAULT_MAX_TOOL_CALLS = 64


_SECRET_HINTS = re.compile(
    r"(?i)(sk-[A-Za-z0-9]{20,}|"
    r"ghp_[A-Za-z0-9]{20,}|"
    r"xoxb-[A-Za-z0-9-]{20,}|"
    r"AKIA[0-9A-Z]{16})"
)


def _redact(text: str) -> str:
    return _SECRET_HINTS.sub("[REDACTED]", text)


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text).strip()


class StorageMessageSanitization:
    """Sanitize outgoing messages before save (Spirit policy layer).

    The Spirit-specific class enforces length caps, secret redaction, and
    whitespace normalization. It deliberately stays narrower than the
    Hermes-aligned six-subsets helpers — those live as module-level
    functions below and are invoked by ``conversation_loop`` and the wire
    adapters, not by ``StorageTurnFacade.save_turn``.
    """

    def __init__(
        self,
        db: SessionDB,
        max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    ):
        self.db = db
        self.max_content_chars = max_content_chars
        self.max_tool_calls = max_tool_calls

    def sanitize(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return a sanitized copy of ``message``, or ``None`` to drop it."""
        if not isinstance(message, dict):
            return None
        role = message.get("role", "user")
        content = message.get("content", "")
        if isinstance(content, str):
            content = _normalize_whitespace(_redact(content))
            if len(content) > self.max_content_chars:
                content = content[: self.max_content_chars] + "\n\n[TRUNCATED]"
        tool_calls = message.get("tool_calls") or []
        if isinstance(tool_calls, list) and len(tool_calls) > self.max_tool_calls:
            tool_calls = tool_calls[: self.max_tool_calls]
        if role == "user" and not content and not tool_calls:
            return None
        out = dict(message)
        out["role"] = role
        out["content"] = content
        if tool_calls:
            out["tool_calls"] = tool_calls
        return out

    def sanitize_batch(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [m for m in (self.sanitize(x) for x in messages) if m is not None]


# ────────────────────────────────────────────────────────────────────
# Six-subsets pure helpers (Hermes-aligned)
# ────────────────────────────────────────────────────────────────────


# Lone surrogates are invalid UTF-8 and crash json.dumps in the OpenAI SDK.
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")

# Keys handled explicitly by ``_sanitize_messages``; every OTHER key is swept
# generically. Mirrors Hermes ``_MESSAGE_CORE_KEYS``.
_MESSAGE_CORE_KEYS = frozenset({"content", "name", "tool_calls", "role"})

# OpenAI / Anthropic / Responses all bound ``function.name`` to this pattern;
# one poisoned stored name (``multi_tool_use.parallel``) 400s every later request.
_VALID_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# When a repair rewrites arguments to "{}", the WARNING log is the last
# surviving copy of content that can hold real user data (a truncated
# write_file), so bound it generously.
_FULL_ARGS_LOG_BOUND = 100_000

# JSON opener → closer map used by ``_rebalance_json_closers``.
_JSON_CLOSERS = {"{": "}", "[": "]"}


def _sanitize_surrogates(text: str) -> str:
    """Replace lone surrogate code points with U+FFFD; no-op when none present.

    ``str.isascii`` is an O(1) flag check; surrogates are never ASCII, so the
    regex scan only runs for the (rare) non-ASCII leaf.
    """
    if not isinstance(text, str):
        return str(text)
    if text.isascii():
        return text
    return _SURROGATE_RE.sub("\ufffd", text)


def _strip_non_ascii(text: str) -> str:
    """Drop non-ASCII characters — last resort for ASCII-only system encodings.

    Used by ``sanitize_outbound_kwargs(force_ascii=True)`` after a provider
    error indicating an ASCII-codec rejection.
    """
    if not isinstance(text, str):
        return str(text)
    if text.isascii():
        return text
    return text.encode("ascii", errors="ignore").decode("ascii")


def coerce_tool_name(name: Any, fallback: str = "invalid_tool_call") -> str:
    """Coerce a *replayed* tool/function name to ``^[A-Za-z0-9_-]{1,64}$``.

    Valid names are returned as-is (identity — prompt-cache safe); invalid
    runs collapse to ``_`` and the result is cut at 64; empty/all-invalid
    yields ``fallback``. Deterministic, so the same stored name always
    renders the same bytes.

    Never apply to *live* tool definitions (schema names must match the
    dispatch registry).
    """
    if not isinstance(name, str):
        return fallback
    if _VALID_TOOL_NAME_RE.fullmatch(name):
        return name
    coerced = re.sub(
        r"_+", "_", re.sub(r"[^A-Za-z0-9_-]", "_", name.strip())
    ).strip("_")
    return coerced[:64] or fallback


def _fix_str_field(container: Any, key: Any, fix: Callable[[str], str]) -> bool:
    """Apply ``fix`` to ``container[key]`` if it is a str; True if it changed."""
    value = (
        container.get(key) if isinstance(container, dict) else container[key]
    )
    fixed = fix(value) if isinstance(value, str) else value
    if fixed == value:
        return False
    container[key] = fixed
    return True


def _sanitize_structure(payload: Any, fix: Callable[[str], str]) -> bool:
    """Apply ``fix`` to every str inside nested dict/list ``payload`` in-place.

    Returns True when any string leaf changed.
    """
    found = False
    stack = [payload]
    while stack:
        node = stack.pop()
        items = (
            node.items()
            if isinstance(node, dict)
            else enumerate(node)
            if isinstance(node, list)
            else ()
        )
        for key, value in list(items):
            if isinstance(value, str):
                found |= _fix_str_field(node, key, fix)
            elif isinstance(value, (dict, list)):
                stack.append(value)
    return found


def _sanitize_messages(messages: list, fix: Callable[[str], str], *, deep: bool) -> bool:
    """Apply ``fix`` to the string fields of every message dict in-place.

    Touches ``content`` / part text / ``name`` / tool_call arguments; in
    ``deep=True`` mode also tool_call ``id`` and ``function.name`` plus
    nested non-core top-level fields (e.g. ``reasoning_details`` from
    byte-level models).

    When a live dict is rewritten, its ``__spirit_persisted__`` marker (if
    present) is popped — Spirit's ``SessionPersistenceMixin.flush_persisted``
    uses the marker to skip already-stamped rows, so a sanitized rewrite
    must restamp.
    """
    from ..storage.session_persistence import _DB_PERSISTED_MARKER

    found = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        msg_found = False
        content = msg.get("content")
        parts = (
            [(p, "text") for p in content if isinstance(p, dict)]
            if isinstance(content, list)
            else None
        )
        fields = parts if parts is not None else [(msg, "content")]
        fields.append((msg, "name"))
        tool_calls = msg.get("tool_calls")
        for tc in tool_calls if isinstance(tool_calls, list) else ():
            fn = tc.get("function") if isinstance(tc, dict) else None
            fields += [(tc, "id")] if deep and isinstance(tc, dict) else []
            fields += (
                ([(fn, "name")] if deep else [])
                + [(fn, "arguments")]
                if isinstance(fn, dict)
                else []
            )
        for container, key in fields:
            msg_found |= _fix_str_field(container, key, fix)
        for key, value in [kv for kv in msg.items() if kv[0] not in _MESSAGE_CORE_KEYS]:
            if isinstance(value, str):
                msg_found |= _fix_str_field(msg, key, fix)
            elif deep and isinstance(value, (dict, list)):
                msg_found |= _sanitize_structure(value, fix)
        if msg_found:
            msg.pop(_DB_PERSISTED_MARKER, None)
            found = True
    return found


# In-place sanitizers; each returns True when anything changed.
_sanitize_structure_surrogates = partial(_sanitize_structure, fix=_sanitize_surrogates)
_sanitize_messages_surrogates = partial(
    _sanitize_messages, fix=_sanitize_surrogates, deep=True
)
_sanitize_structure_non_ascii = partial(_sanitize_structure, fix=_strip_non_ascii)
_sanitize_messages_non_ascii = partial(
    _sanitize_messages, fix=_strip_non_ascii, deep=False
)
_sanitize_tools_non_ascii = _sanitize_structure_non_ascii


def _clone_payload_for_ascii(payload: Any) -> Any:
    """Structural clone of ``payload`` (dict / list / primitive) for ASCII strip.

    ``_sanitize_structure`` only rebinds str leaves inside dict/list
    containers; a shallow copy of the outer container protects any caller
    that holds the same reference elsewhere (e.g. ``agent.tools``).
    """
    if isinstance(payload, dict):
        return {k: _clone_payload_for_ascii(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_clone_payload_for_ascii(v) for v in payload]
    return payload


def sanitize_outbound_kwargs(api_kwargs: Dict[str, Any], *, force_ascii: bool = False) -> bool:
    """Outbound-request chokepoint for every built kwargs dict.

    Tool descriptions, extra_body and kwargs strings can carry invalid code
    points that providers reject with a non-retryable 400; one in-place walk
    makes the whole payload ``json.dumps()``-safe. The ASCII strip is opt-in
    via ``force_ascii=True`` (set after an ASCII-codec rejection).

    Returns True when any string leaf was rewritten.
    """
    if not isinstance(api_kwargs, dict):
        return False

    found = False
    # Surrogate repair is deep: tool_call ids, nested reasoning_details.
    messages = api_kwargs.get("messages")
    if isinstance(messages, list):
        found |= _sanitize_messages_surrogates(messages)
    found |= _sanitize_structure_surrogates(api_kwargs)

    if force_ascii:
        # ``tools`` may be a reference shared by the caller; detach before
        # the in-place strip so the caller never sees rewritten bytes.
        tools = api_kwargs.get("tools")
        if tools is not None and isinstance(tools, (dict, list)):
            api_kwargs["tools"] = _clone_payload_for_ascii(tools)
        if isinstance(api_kwargs.get("messages"), list):
            found |= _sanitize_messages_non_ascii(api_kwargs["messages"])
        found |= _sanitize_structure_non_ascii(api_kwargs)

    return found


def _escape_invalid_chars_in_json_strings(raw: str) -> str:
    """Escape literal control chars (0x00-0x1F) inside JSON string values.

    Used as Pass 5 in ``_repair_tool_call_arguments`` for llama.cpp-style
    outputs that mix literal control chars with other malformations.
    """
    if not isinstance(raw, str):
        return str(raw)
    out: List[str] = []
    in_string = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if in_string and ch == "\\" and i + 1 < n:
            out.append(raw[i : i + 2])
            i += 2
            continue
        if ch == '"':
            in_string = not in_string
        out.append(f"\\u{ord(ch):04x}" if in_string and ord(ch) < 0x20 else ch)
        i += 1
    return "".join(out)


def _loads_ok(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except (json.JSONDecodeError, TypeError, ValueError):
        return False


def _rebalance_json_closers(raw: str) -> Optional[str]:
    """Close a JSON prefix's open braces/brackets in stack order.

    Delimiters inside string values (``{"code": "}"}``) are not structure —
    one open brace stays open. A closer that does not match the stack top
    but does match a deeper opener gets the missing inner closers inserted
    BEFORE it: ``{"a": [{"b": 1}}`` → the model dropped the ``]`` and let
    the neighbouring ``}`` close in its place, so the counts balance and
    nothing can be appended.

    Returns ``None`` when the text ends inside an unterminated string —
    that content is unrecoverable and must not be guessed.
    """
    if not isinstance(raw, str):
        return None
    out: List[str] = []
    stack: List[str] = []
    in_string = False
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if in_string:
            if ch == "\\":
                out.append(raw[i : i + 2])
                i += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in _JSON_CLOSERS:
            stack.append(ch)
        elif ch in "}]" and ch in (_JSON_CLOSERS[o] for o in stack):
            while _JSON_CLOSERS[stack[-1]] != ch:
                out.append(_JSON_CLOSERS[stack.pop()])
            stack.pop()
        out.append(ch)
        i += 1
    if in_string:
        return None
    return "".join(out) + "".join(_JSON_CLOSERS[ch] for ch in reversed(stack))


def _repair_tool_call_arguments(raw_args: str, tool_name: str = "?") -> str:
    """Repair malformed tool_call argument JSON (truncation, trailing commas,
    Python ``None``, control chars); ``"{}"`` if unrepairable so the request
    succeeds. Repairs log at WARNING (the only surviving copy of real user
    data when arguments drop to empty).
    """
    raw_stripped = raw_args.strip() if isinstance(raw_args, str) else ""

    if not raw_stripped:
        logger.warning("Sanitized empty tool_call arguments for %s", tool_name)
        return "{}"

    if raw_stripped == "None":
        logger.warning("Sanitized Python-None tool_call arguments for %s", tool_name)
        return "{}"

    # Pass 0: strict=False accepts literal control chars inside strings (the
    # most common local-model case) and re-serialises to wire-valid JSON.
    try:
        reserialised = json.dumps(
            json.loads(raw_stripped, strict=False),
            separators=(",", ":"),
        )
        if reserialised != raw_stripped:
            logger.warning(
                "Repaired unescaped control chars in tool_call arguments for %s",
                tool_name,
            )
        return reserialised
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Passes 2-4: strip trailing commas, close unclosed structures, trim
    # excess closers (bounded). Bracket counting is string-aware.
    fixed = re.sub(r",\s*([}\]])", r"\1", raw_stripped)
    fixed = _rebalance_json_closers(fixed) or fixed
    for _ in range(50):
        if _loads_ok(fixed) or not (
            (fixed.endswith("}") and fixed.count("}") > fixed.count("{"))
            or (fixed.endswith("]") and fixed.count("]") > fixed.count("["))
        ):
            break
        fixed = fixed[:-1]

    if _loads_ok(fixed):
        logger.warning(
            "Repaired malformed tool_call arguments for %s: %s → %s",
            tool_name,
            raw_stripped[:80],
            fixed[:80],
        )
        return fixed

    # Pass 5: escape control chars inside strings, then retry.
    escaped = _escape_invalid_chars_in_json_strings(fixed)
    if escaped != fixed and _loads_ok(escaped):
        logger.warning(
            "Repaired control-char-laced tool_call arguments for %s: %s → %s",
            tool_name,
            raw_stripped[:80],
            escaped[:80],
        )
        return escaped

    logger.warning(
        "Unrepairable tool_call arguments for %s — replaced with empty object (was: %s)",
        tool_name,
        raw_stripped[:_FULL_ARGS_LOG_BOUND],
    )
    return "{}"


# ────────────────────────────────────────────────────────────────────
# Public exports (Hermes-aligned)
# ────────────────────────────────────────────────────────────────────


__all__ = [
    # Spirit policy class (unchanged from previous release)
    "StorageMessageSanitization",
    "DEFAULT_MAX_CONTENT_CHARS",
    "DEFAULT_MAX_TOOL_CALLS",
    # Six-subsets (design doc §G.6)
    "sanitize_outbound_kwargs",
    "coerce_tool_name",
    "_sanitize_surrogates",
    "_sanitize_messages",
    "_sanitize_structure",
    "_rebalance_json_closers",
    "_escape_invalid_chars_in_json_strings",
    # Helpers shared with downstream
    "_strip_non_ascii",
    "_repair_tool_call_arguments",
]