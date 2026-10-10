"""spirit/agent/storage_title_generator.py — auto-title a session.

v0.4.0 (Hermes-aligned): two-stage title pipeline, both off the critical
path of the assistant turn.

1. **Stage 1 — instant derived title** (``apply_instant_title``)
   Synchronous, runs BEFORE the model call on the critical path. Cannot
   fail (heuristic only, no LLM). Persisted with ``title_source="derived"``
   authority. The first thing a user sees in the session list.

2. **Stage 2 — LLM upgrade** (``_auto_title_session``)
   Daemon thread spawned via ``start_title_upgrade``. Cheap model call
   (temperature 0.2, max_tokens 64, JSON-constrained via prompt).
   Persisted with ``title_source="llm"`` authority. Replaces the derived
   title on success, leaves it on failure.

Provenance order ``derived < llm < user`` is enforced by
``_persist_session_title``: a session that already carries an
``llm``/``user`` title is never overwritten by a lower authority.

Module API (matches design doc §G.5):
  - ``wait_for_title_upgrades(timeout=10.0)``   — bounded join of in-flight
    upgrade threads; never raises; called at process shutdown.
  - ``start_title_upgrade(thread)``             — start a (deferred) thread,
    register in the WeakSet so ``wait_for_title_upgrades`` can join it.
  - ``strip_control_wrappers(text)``            — drop Codex-style
    ``<command-message>``/``<system-reminder>`` etc. from a nominal user
    turn so titling continues on the prose the user actually typed.
  - ``derive_title(user_message)``              — synchronous, no-model
    title from the first meaningful line.
  - ``generate_title(user_message)``            — synchronous LLM call
    returning a JSON-constrained title (or None on rejection).
  - ``apply_instant_title(db, session_id, user_message)``
  - ``maybe_auto_title(db, session_id, user_message)``  — entry point:
    Stage 1 inline + Stage 2 spawn.

Reference: hermes agent/title_generator.py.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import weakref
from contextlib import suppress
from typing import Any, Callable, Dict, List, Optional

from ..storage.session_db import SessionDB

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Public type aliases
# ────────────────────────────────────────────────────────────────────


LLMCaller = Callable[[str, float, int], str]
# LLMCaller signature mirrors Spirit's existing convention:
#   (prompt: str, temperature: float, max_tokens: int) -> str

# (title, source) -> None  (``source`` is the persisted provenance:
# "derived" or "llm"). Consumers paying a per-title remote rename
# (Discord thread, Telegram topic) should act on ``llm`` only.
TitleCallback = Callable[[str, str], None]

# (task_name, exception) -> None — surfaces silent drops so NULL titles
# don't accumulate as hidden regressions.
FailureCallback = Callable[[str, BaseException], None]


# ────────────────────────────────────────────────────────────────────
# In-flight upgrade threads
# ────────────────────────────────────────────────────────────────────


# In-flight stage-2 upgrade threads. The WeakSet auto-evicts threads that
# the GC has collected; ``wait_for_title_upgrades`` joins whatever's left
# at process exit.
_UPGRADE_THREADS: "weakref.WeakSet[threading.Thread]" = weakref.WeakSet()


def wait_for_title_upgrades(timeout: float = 10.0) -> None:
    """Bounded join of the auto-title threads still running; never raises.

    Process shutdown hook: a long-lived LLM call (especially a self-hosted
    one) can outlive ``SessionDB.close()`` and bill the wrong session if
    the daemon thread is GCed before the auxiliary ledger write lands.
    """
    deadline = time.monotonic() + timeout
    for thread in list(_UPGRADE_THREADS):
        remaining = max(0.0, deadline - time.monotonic())
        thread.join(remaining)


def start_title_upgrade(upgrade: Optional[threading.Thread]) -> None:
    """Start a (deferred) upgrade thread; joinable via wait_for_title_upgrades."""
    if upgrade is None or upgrade.ident is not None:
        return
    _UPGRADE_THREADS.add(upgrade)
    upgrade.start()


# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────


# Title budget handed to the model (Claude Code / OpenClaw converged on 1000).
MAX_TITLE_INPUT_CHARS = 1000
# Cap on the instant derived title.
MAX_DERIVED_TITLE_CHARS = 48
# Output budget for the title call: room for a fenced/prefixed JSON reply
# and for a reasoning model that thinks despite the thinking-disabled request.
TITLE_MAX_TOKENS = 64
# Upper bound on accepted title word count. Titling is a 3-7 word task;
# longer output is a model that answered instead of titling — rejected, not
# truncated (the half-answer would be persisted as a session title).
_MAX_TITLE_WORDS = 12
# Cap on a persisted title (sessions.title column is bounded elsewhere).
MAX_TITLE_CHARS = 80

# Examples shown to the model in the prompt — the echo-guard set rejects
# outputs that parrot them back verbatim (most visibly "Fix login button
# on mobile" naming sessions that have nothing to do with a login button).
# "Friendly greeting" is deliberately NOT rejected: the prompt instructs
# the model to produce it for bare greetings, so it is a legitimate output.
_PROMPT_GOOD_EXAMPLES = (
    "Fix login button on mobile",
    "Postgres connection pool exhaustion",
    "Friendly greeting",
)
_PROMPT_VAGUE_EXAMPLE = "Code changes"

_EXAMPLE_ECHO_REJECT = frozenset(
    t.lower() for t in _PROMPT_GOOD_EXAMPLES if t != "Friendly greeting"
) | {_PROMPT_VAGUE_EXAMPLE.lower()}

# "Friendly greeting" is what the prompt asks for when the opener has no
# topic yet. It must NOT settle the session as ``llm`` authority — it is a
# placeholder, like the instant derived title. The next substantive turn
# upgrades it.
_PROVISIONAL_GREETING_TITLE = "friendly greeting"


# Control-tag wrappers around machine-authored content inside a nominal
# "user" message (Codex CLI's RECOGNIZED_CONTROL_WRAPPERS). Stripped so
# titling continues on what remains.
_CONTROL_WRAPPERS = tuple(
    (f"<{tag}>", f"</{tag}>")
    for tag in (
        "command-message", "command-name", "command-args",
        "local-command-caveat", "local-command-stderr", "local-command-stdout",
        "task-notification", "system-reminder",
        "ide_opened_file", "ide_selection",
    )
)

# Hermes' own machine-authored openers: a compaction handoff or resumed
# session must not be titled after them.
_MACHINE_PREFIXES = (
    "[CONTEXT COMPACTION",
    "[System note:",
    "[System:",
    "[SYSTEM]",
    "[System: The active model for this chat has changed to ",
)


# ────────────────────────────────────────────────────────────────────
# Provenance order (derived < llm < user)
# ────────────────────────────────────────────────────────────────────


_TITLE_SOURCE_ORDER = {"derived": 0, "llm": 1, "user": 2}


def _source_rank(source: Optional[str]) -> int:
    """Numeric rank for the provenance string. Unknown strings rank -1
    (lower than everything known), so a future ``human`` or ``manual``
    authority added later still beats the current ones only if the rank
    table is updated. ``None`` ranks as 0 (derived).
    """
    if source is None:
        return 0
    return _TITLE_SOURCE_ORDER.get(source.lower(), -1)


# ────────────────────────────────────────────────────────────────────
# Control-wrapper stripping
# ────────────────────────────────────────────────────────────────────


def strip_control_wrappers(text: str) -> str:
    """Remove leading control wrappers (nested too) so a slash-command turn
    reduces to the prose the user typed.

    Bounded: each pass must remove a wrapper or we stop — protects against
    pathological inputs whose wrappers never close.
    """
    current = (text or "").strip()
    for _ in range(len(_CONTROL_WRAPPERS) * 2):
        stripped = _strip_one_wrapper(current)
        if stripped == current:
            break
        current = stripped
    return current


def _strip_one_wrapper(text: str) -> str:
    lowered = text.lower()
    for open_tag, close_tag in _CONTROL_WRAPPERS:
        if not lowered.startswith(open_tag):
            continue
        end = lowered.find(close_tag)
        if end == -1:
            # Unterminated wrapper: drop the opening tag, keep the body.
            return text[len(open_tag):].strip()
        # Prefer trailing prose; otherwise the wrapper body is all we have.
        return (
            text[end + len(close_tag):].strip()
            or text[len(open_tag):end].strip()
        ).strip()
    return text


def _summarize_user_message(user_message: str) -> str:
    """Text worth titling: strip control wrappers so a slash-command turn
    reduces to the prose the user typed.

    Spirit does not have a skill_commands layer (that's a Hermes feature),
    so the only transformation here is wrapper stripping.
    """
    if not user_message:
        return ""
    return strip_control_wrappers(user_message)


def is_titleable_user_message(user_message: Any) -> bool:
    """False for machine-authored openers and turns that reduce to nothing
    once wrappers are stripped.
    """
    if not isinstance(user_message, str) or not user_message.strip():
        return False
    if user_message.lstrip().startswith(_MACHINE_PREFIXES):
        return False
    return bool(_summarize_user_message(user_message).strip())


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


# ────────────────────────────────────────────────────────────────────
# Stage 1: instant derived title (synchronous, cannot fail)
# ────────────────────────────────────────────────────────────────────


def derive_title(user_message: str) -> Optional[str]:
    """Instant title: first meaningful line trimmed to a word boundary.
    No model call, never fails.
    """
    text = _summarize_user_message(user_message)
    if not text:
        return None
    line = " ".join(_first_line(text).split())
    if len(line) > MAX_DERIVED_TITLE_CHARS:
        cut = line[:MAX_DERIVED_TITLE_CHARS]
        space = cut.rfind(" ")
        line = (
            cut[:space] if space > MAX_DERIVED_TITLE_CHARS // 2 else cut
        ).rstrip(" ,.;:—-") + "…"
    return line or None


# ────────────────────────────────────────────────────────────────────
# Stage 2: LLM upgrade (daemon thread)
# ────────────────────────────────────────────────────────────────────


_TITLE_PROMPT_TEMPLATE = (
    "You name chat sessions. Given the user's opening message, write a "
    "title that lets them find this conversation again in a list.\n\n"
    "Rules:\n"
    "- 3 to 7 words, sentence case (capitalize only the first word and proper nouns).\n"
    "- Name what the user wants DONE, not that they asked a question.\n"
    "- Keep technical terms, filenames, numbers, and error codes exact.\n"
    "- Drop filler words: the, this, my, a, an.\n"
    "- No trailing punctuation, no quotes, no tool names, no 'Title:' prefix.\n"
    "- Never answer the message. Name it.\n"
    "- Always produce something, even for a bare greeting.\n\n"
    + "".join(f'Good: {{"title": "{t}"}}\n' for t in _PROMPT_GOOD_EXAMPLES)
    + f'Too vague: {{"title": "{_PROMPT_VAGUE_EXAMPLE}"}}\n'
    "Too long: {\"title\": \"Investigate and fix the issue where the login "
    "button does not respond on mobile devices\"}\n\n"
    'Reply with JSON only: {"title": "..."}'
)


def _strip_title_prefix(text: str) -> str:
    return text[6:].strip() if text.lower().startswith("title:") else text


def _extract_json_title(raw: str) -> Optional[str]:
    """Title from a ``{"title": ...}`` payload — strict parse, then a loose
    ``"title": "..."`` scan; None when absent.
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("title"), str):
            return parsed["title"].strip()
    except (ValueError, TypeError):
        pass
    match = re.search(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    if match:
        with suppress(ValueError):
            return json.loads(f'"{match.group(1)}"').strip()
        return match.group(1).strip()
    return None


def _extract_title_text(content: str) -> str:
    """Strict JSON, then a loose JSON scan, then first-line prose.

    A truncated structured payload (e.g. ``{"title`` with no closer) is
    dropped rather than handed to the prose fallback: the fragment would
    otherwise be persisted as the session title.
    """
    if not content:
        return ""
    raw = content.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()
    title = _extract_json_title(raw)
    if title is not None:
        return title
    # Truncated structured output: refuse the fallback.
    if raw.startswith(('{"', '["', "[{")) or (
        raw.startswith("```") and raw.count("```") % 2 == 1
    ):
        return ""
    return _strip_title_prefix(_first_line(raw)).strip("\"'").strip()


def _clean_title(text: str) -> Optional[str]:
    """Normalize a model-produced title, or None when nothing usable remains."""
    title = (
        _strip_title_prefix(
            " ".join((text or "").split()).strip("\"'").strip()
        ).rstrip(".!,;:")
    )
    if len(title) > MAX_TITLE_CHARS:
        title = title[: MAX_TITLE_CHARS - 3].rstrip() + "..."
    return title or None


def _is_provisional_greeting_title(title: str) -> bool:
    """The prompt's greeting placeholder (also "friendly greeting in chat"
    and quoted/bracketed variants).
    """
    normalized = re.sub(
        r"^[\W_]+|[\W_]+$", "", title.strip(), flags=re.UNICODE
    ).lower()
    return normalized in (
        _PROVISIONAL_GREETING_TITLE,
        _PROVISIONAL_GREETING_TITLE + " in chat",
    )


def _is_prompt_example_echo(title: str) -> bool:
    """True when *title* is one of the prompt's own example titles.

    Comparison is case-insensitive after stripping any leading/trailing run
    of non-letter/non-digit characters, so bracket/quote wrappers cannot
    bypass the guard.
    """
    normalized = re.sub(
        r"^[\W_]+|[\W_]+$", "", title.strip(), flags=re.UNICODE
    ).lower()
    return normalized in _EXAMPLE_ECHO_REJECT


def _safe_callback(
    callback: Optional[Callable], args: tuple, log_fmt: str, label: str
) -> None:
    try:
        if callback is not None:
            callback(*args)
    except Exception:
        logger.debug(log_fmt, label, exc_info=True)


def _notify_title(
    title_callback: Optional[TitleCallback],
    title: str,
    source: str,
    label: str,
) -> None:
    _safe_callback(
        title_callback, (title, source), "%s title callback failed", label
    )


def _report_failure(
    failure_callback: Optional[FailureCallback],
    exc: BaseException,
    label: str,
) -> None:
    _safe_callback(
        failure_callback, ("title generation", exc), "%s failure_callback raised", label
    )


def generate_title(
    user_message: str,
    *,
    llm_caller: Optional[LLMCaller] = None,
    failure_callback: Optional[FailureCallback] = None,
    title_preview: Optional[str] = None,
) -> Optional[str]:
    """Title from the opening message alone. Calls ``llm_caller`` if provided.

    Spirit's LLMCaller takes ``(prompt, temperature, max_tokens)`` and
    returns a string. The prompt constrains the model to a JSON
    ``{"title": ...}`` reply; the response is parsed with a strict JSON
    first, then a loose regex scan, then a prose fallback that is
    rejected for truncated structured outputs.

    Returns None when the model answers instead of titling
    (``> _MAX_TITLE_WORDS`` words), parrots a prompt example, or fails
    outright — caller should fall back to ``derive_title``.
    """
    user_snippet = _summarize_user_message(user_message)
    if isinstance(title_preview, str) and title_preview.strip():
        # The titler sees the message AFTER @-reference expansion (Hermes);
        # Spirit does not have @-refs, so we just append the preview.
        user_snippet = user_snippet + "\n\nPasted content:\n" + title_preview[:MAX_TITLE_INPUT_CHARS]
    user_snippet = user_snippet[:MAX_TITLE_INPUT_CHARS]
    if not user_snippet.strip():
        return None

    if llm_caller is None:
        # No LLM configured: drop straight to the derived fallback path.
        return None

    prompt = _TITLE_PROMPT_TEMPLATE
    try:
        out = llm_caller(prompt, 0.2, TITLE_MAX_TOKENS)
    except Exception as e:
        logger.warning("Title generation failed: %s", e)
        logger.debug("Title generation traceback", exc_info=True)
        _report_failure(failure_callback, e, "Title generation")
        return None

    title = _clean_title(_extract_title_text(out or ""))

    # Reject on word-count blow-up (the model answered instead of titling).
    if title is not None and len(title.split()) > _MAX_TITLE_WORDS:
        logger.debug(
            "Rejecting answer-shaped title output (%d words > %d)",
            len(title.split()),
            _MAX_TITLE_WORDS,
        )
        return None

    # Reject on prompt-example echo.
    if title is not None and _is_prompt_example_echo(title):
        logger.debug("Rejecting prompt-example echo title: %r", title)
        return None

    return title


# ────────────────────────────────────────────────────────────────────
# Storage glue: provenance-aware persistence
# ────────────────────────────────────────────────────────────────────


def _persist_session_title(
    db: SessionDB,
    session_id: str,
    title: Optional[str],
    *,
    source: str,
) -> Optional[str]:
    """Persist *title* at *source* authority. Returns the persisted title,
    or None when a higher authority held the row.

    Spirit's ``sessions.title`` and ``sessions.title_source`` columns make
    this a one-row UPDATE with a WHERE clause that pre-checks the
    existing source's rank; the whole statement runs inside a single
    transaction so a concurrent ``/title`` cannot lose to a background
    upgrade.
    """
    if not title:
        return None
    rank = _source_rank(source)
    # UPDATE that only writes when the existing rank is <= ours (so a
    # derived/llm never overwrites a user title). `rowcount` tells us
    # whether we won the race.
    try:
        cur = db.conn.execute(
            "UPDATE sessions SET title = ?, title_source = ?, "
            "title_updated_at = ? "
            "WHERE id = ? AND ("
            "  title IS NULL OR title = '' OR "
            "  COALESCE(title_source, '') = '' OR "
            "  ? >= ("
            "    CASE COALESCE(title_source, '')"
            "      WHEN 'derived' THEN 0"
            "      WHEN 'llm' THEN 1"
            "      WHEN 'user' THEN 2"
            "      ELSE -1"
            "    END)"
            ")",
            (title[:MAX_TITLE_CHARS], source, time.time(), session_id, rank),
        )
        db.conn.commit()
    except Exception as e:
        logger.debug("set_auto_title UPDATE failed for %s: %s", session_id, e)
        return None
    if cur.rowcount == 0:
        logger.debug(
            "Skipping %s title: a higher-authority title already holds session %s",
            source,
            session_id,
        )
        return None
    return title[:MAX_TITLE_CHARS]


def _has_upgraded_title(db: SessionDB, session_id: str) -> bool:
    """True when the session already carries an ``llm``/``user`` title.

    Returns True when the check fails (conservative — we'd rather skip a
    legitimate upgrade than accidentally overwrite a manual title).
    """
    try:
        row = db.conn.execute(
            "SELECT title_source FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    except Exception:
        return True
    if row is None:
        return False
    return _source_rank(row[0]) >= _source_rank("llm")


def _session_is_untitled(db: SessionDB, session_id: str) -> bool:
    """No title of any provenance; False when it can't tell."""
    try:
        row = db.conn.execute(
            "SELECT title FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    except Exception:
        logger.debug("Untitled check failed for %s", session_id, exc_info=True)
        return False
    return row is not None and not str(row[0] or "").strip()


# ────────────────────────────────────────────────────────────────────
# Stage 1 entry point
# ────────────────────────────────────────────────────────────────────


def apply_instant_title(
    db: SessionDB,
    session_id: str,
    user_message: str,
    title_callback: Optional[TitleCallback] = None,
    title_preview: Optional[str] = None,
) -> Optional[str]:
    """Write the derived title inline. Returns it, or None (no usable text,
    or a higher-authority title exists). Never raises.

    Runs synchronously on the critical path. Cannot fail — the worst case
    is a None return, and the upgrade thread that follows will fill in the
    model-generated title or its own derived fallback.
    """
    if db is None or not session_id:
        return None
    try:
        if not is_titleable_user_message(user_message):
            return None
        title = derive_title(user_message)
        if title_preview and isinstance(title_preview, str):
            # Prepend a tiny preview slice so a paste-only opener gets a
            # meaningful derived title (matches Hermes' build_title_input).
            preview_slice = title_preview.strip()[:MAX_DERIVED_TITLE_CHARS]
            if preview_slice and not title.startswith(preview_slice):
                title = preview_slice
        persisted = _persist_session_title(
            db, session_id, title, source="derived"
        )
        if persisted:
            _notify_title(title_callback, persisted, "derived", "Instant-title")
        return persisted
    except Exception:
        logger.debug("Instant title failed", exc_info=True)
        return None


# ────────────────────────────────────────────────────────────────────
# Stage 2 daemon-thread target
# ────────────────────────────────────────────────────────────────────


def _auto_title_session(
    db: SessionDB,
    session_id: str,
    user_message: str,
    *,
    llm_caller: Optional[LLMCaller] = None,
    failure_callback: Optional[FailureCallback] = None,
    title_callback: Optional[TitleCallback] = None,
    title_preview: Optional[str] = None,
) -> None:
    """Daemon thread body. Skips sessions already carrying an
    ``llm``/``user`` title. Never lets an exception escape (the threading
    excepthook would spray a traceback into the terminal).
    """
    try:
        if db is None or not session_id or _has_upgraded_title(db, session_id):
            return
        title = generate_title(
            user_message,
            llm_caller=llm_caller,
            failure_callback=failure_callback,
            title_preview=title_preview,
        )
        source = "llm"
        if title and _is_provisional_greeting_title(title):
            source = "derived"
        if not title:
            # The inline attempt declined collisions; off the critical
            # path the lineage scan is affordable — re-derive.
            title, source = derive_title(user_message), "derived"
        if not title:
            return
        persisted = _persist_session_title(
            db, session_id, title, source=source
        )
        if persisted is not None:
            logger.debug("Auto-generated session title: %s", persisted)
            _notify_title(title_callback, persisted, source, "Auto-title")
    except Exception as e:
        logger.warning(
            "Auto-title failed (harmless; if this started after an update, "
            "restart the running Spirit process): %s",
            e,
        )
        logger.debug("Auto-title traceback", exc_info=True)
        _report_failure(failure_callback, e, "Auto-title")


# ────────────────────────────────────────────────────────────────────
# Entry point: instant inline + daemon upgrade
# ────────────────────────────────────────────────────────────────────


def maybe_auto_title(
    db: SessionDB,
    session_id: str,
    user_message: str,
    *,
    llm_caller: Optional[LLMCaller] = None,
    failure_callback: Optional[FailureCallback] = None,
    title_callback: Optional[TitleCallback] = None,
    title_preview: Optional[str] = None,
) -> Optional[threading.Thread]:
    """Instant inline title, then a daemon-thread upgrade.

    Returns the upgrade thread (already started). Past the opening turn,
    skip when the session already carries an ``llm``/``user`` title —
    a ``derived`` placeholder is still upgradable.
    """
    if db is None or not session_id or not user_message:
        return None
    if not is_titleable_user_message(user_message):
        return None

    # Stage 1: instant derived (critical path, cannot fail).
    apply_instant_title(
        db, session_id, user_message,
        title_callback=title_callback, title_preview=title_preview,
    )

    # Stage 2: spawn daemon thread (off critical path).
    thread = threading.Thread(
        target=_auto_title_session,
        name="auto-title",
        args=(db, session_id, user_message),
        kwargs={
            "llm_caller": llm_caller,
            "failure_callback": failure_callback,
            "title_callback": title_callback,
            "title_preview": title_preview,
        },
        daemon=True,
    )
    start_title_upgrade(thread)
    return thread


# ────────────────────────────────────────────────────────────────────
# Spirit compatibility class (preserved from previous release)
# ────────────────────────────────────────────────────────────────────


_HEURISTIC_STOPWORDS = {
    "the", "a", "an", "please", "can", "you", "could", "i", "we", "to",
    "this", "that", "is", "of", "for", "in", "on", "with", "and", "or",
    "do", "make", "give", "me", "my",
}


def _legacy_heuristic_title(text: str, max_len: int = 40) -> str:
    """Backwards-compat: simple sentence-cased heuristic for callers that
    only want a synchronous, no-LLM title without the full pipeline.
    """
    text = text.strip()
    if not text:
        return "New session"
    first = re.split(r"[.!?\n]", text, maxsplit=1)[0].strip()
    if len(first) > max_len:
        first = first[: max_len - 1].rstrip() + "…"
    return first or "New session"


class StorageTitleGenerator:
    """Generate and persist session titles (Spirit compatibility class).

    Backwards-compatible entry point: ``maybe_generate_title`` calls the
    full two-stage pipeline (``maybe_auto_title``) under the hood. New
    callers should prefer the module-level ``maybe_auto_title`` directly.
    """

    def __init__(
        self,
        db: SessionDB,
        llm_caller: Optional[LLMCaller] = None,
    ):
        self.db = db
        self.llm_caller = llm_caller

    def maybe_generate_title(
        self,
        session_id: str,
        max_chars: int = MAX_TITLE_INPUT_CHARS,
    ) -> Optional[str]:
        """Synchronous wrapper: writes the derived title inline and returns
        it. The daemon upgrade thread (Stage 2) still runs and replaces the
        derived title when the LLM call succeeds.

        For new code prefer ``maybe_auto_title`` which returns the upgrade
        thread and exposes the title_callback hook for surfaces that want
        to react to the upgrade landing.
        """
        session = self.db.get_session(session_id)
        if not session:
            return None
        if session.get("title") and session.get("title_source") in ("llm", "user"):
            # Already upgraded; respect the higher authority.
            return session["title"]

        first_user = self.db.get_first_user_message(session_id)
        if not first_user or not first_user.get("content"):
            return None
        seed = (first_user["content"] or "")[:max_chars]

        # Stage 1 inline.
        derived = apply_instant_title(self.db, session_id, seed)

        # Stage 2 (off critical path — daemon thread).
        maybe_auto_title(
            self.db,
            session_id,
            seed,
            llm_caller=self.llm_caller,
        )

        if derived:
            return derived
        # No derived title (machine-authored opener etc.) — caller can
        # re-poll after the upgrade thread settles.
        return session.get("title")


# ────────────────────────────────────────────────────────────────────
# Public exports
# ────────────────────────────────────────────────────────────────────


__all__ = [
    # Stage 1
    "derive_title",
    "apply_instant_title",
    # Stage 2
    "generate_title",
    # Entry point
    "maybe_auto_title",
    # Thread management
    "wait_for_title_upgrades",
    "start_title_upgrade",
    # Helpers
    "strip_control_wrappers",
    "is_titleable_user_message",
    "_persist_session_title",
    "_has_upgraded_title",
    "_summarize_user_message",
    # Backwards-compat
    "StorageTitleGenerator",
    "MAX_TITLE_INPUT_CHARS",
    "MAX_DERIVED_TITLE_CHARS",
    "MAX_TITLE_CHARS",
]