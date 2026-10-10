"""spirit/agent/storage_compression_manual.py — manual ``/compress`` core shared by every surface.

v0.4.0 (Hermes-aligned): manual compression is the ONE sanctioned history mutation
(prompt-cache invariant): each surface (CLI, gateway, TUI) parses its own flags and renders its own
text, but the sequence — split for ``here [N]``, estimate, run the compressor, detect a lock-skip,
rejoin the verbatim tail, summarize — lives here so ``--preview`` / ``--aggressive`` and the
lock-skip wording cannot drift per surface again.

The ``compress_now()`` body is intentionally a **flow** over the Spirit-side primitives
(``ContextCompressor.compress``, ``SessionDB.claim_compression_lock``, the
``storage_compression_reply_anchor._ensure_compressed_keeps_last_assistant_reply`` seam guard).
Spirit does not yet have a stand-alone ``agent._compress_context`` facade equivalent — that is
the Phase-3 work — so ``compress_now`` accepts a callable (``compress_fn``) and lets the
caller wire whichever compressor it wants. The CLI surface supplies the Spirit agent's
``compress`` method, the gateway supplies its remote equivalent, etc.

Reference: hermes agent/conversation_compression_manual.py (152 lines).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from spirit.cli.partial_compress import (
    extract_compress_flags,
    parse_partial_compress_args,
    rejoin_compressed_head_and_tail,
    split_history_for_partial_compress,
    summarize_compress_preview,
)


#: Every surface renders the same refusal; hard truncation has no persistence path outside the
#: guarded compression rotation, so ``--aggressive`` is refused rather than mis-parsed as a focus
#: topic.
AGGRESSIVE_UNSUPPORTED = (
    "--aggressive is not supported; use '/compress here [N]' to keep only recent exchanges, "
    "or /undo to drop turns."
)
MIN_MESSAGES = 4


@dataclass
class CompressRequest:
    """Parsed ``/compress`` arguments (``extract_compress_flags`` + ``parse_partial_compress_args``)."""
    preview: bool = False
    aggressive: bool = False
    partial: bool = False
    keep_last: int = 2
    focus_topic: Optional[str] = None


@dataclass
class CompressResult:
    status: str  # "preview" | "compressed" | "lock_skipped" | "nothing_to_do"
    before_messages: List[Dict[str, Any]]
    after_messages: List[Dict[str, Any]]
    before_tokens: int
    after_tokens: int
    request: CompressRequest
    lines: List[str] = field(default_factory=list)  # preview report lines (status == "preview")
    lock_holder: Any = None
    summary: Optional[Dict[str, Any]] = None  # ``summarize_manual_compression`` payload when compressed

    @property
    def removed(self) -> int:
        return len(self.before_messages) - len(self.after_messages)


#: The signature the ``compress_now`` caller supplies for its own compressor. Kept loose so the
#: Spirit ``ContextCompressor.compress`` and a remote-engine wrapper both fit.
#:
#: Args:
#:   * ``head``: the messages to compress (after the partial split).
#:   * ``system_message``: optional prebuilt prompt; ``None`` means rebuild.
#:   * ``approx_tokens``: pre-split estimate, used by ``ContextCompressor.compress`` for budget.
#:   * ``focus_topic``: optional topic to bias the summary toward.
#:   * ``task_id``: compression lineage key (default ``"default"``).
#: Returns ``(compressed, lock_signal)``. ``lock_signal`` is ``True`` / a holder string when the
#: caller skipped the round because another compressor held the lock.
CompressFn = Callable[..., Any]


def parse_compress_args(raw_args: str) -> CompressRequest:
    """One parser for every surface: flags anywhere, then the boundary-aware / focus positional
    forms.

    ``raw_args`` is the full argument string *after* ``/compress`` itself (whitespace-trimmed by
    the caller). The split between flag-stripping (``extract_compress_flags``) and the
    positional/``here`` parser (``parse_partial_compress_args``) keeps each surface honest: any
    new flag only needs to be added in one place.
    """
    rest, preview, aggressive = extract_compress_flags((raw_args or "").strip())
    partial, keep_last, focus_topic = parse_partial_compress_args(rest)
    return CompressRequest(
        preview=preview,
        aggressive=aggressive,
        partial=partial,
        keep_last=keep_last,
        focus_topic=focus_topic or None,
    )


def estimate_request_tokens(
    agent: Any,
    messages: Sequence[Dict[str, Any]],
    *,
    estimate_fn: Optional[Callable[..., int]] = None,
) -> int:
    """Transcript + system prompt + tool schemas: a transcript-only figure understates real
    request pressure and can even appear to grow after a dense handoff summary replaces many
    short turns (#6217).

    The agent's own ``estimate_request_tokens`` / ``_estimate_request_tokens`` method is the
    preferred path — pass the agent, and we look it up by name. ``estimate_fn`` overrides both
    lookups when supplied (testing or a remote-engine wrapper).
    """
    if not messages:
        return 0
    if estimate_fn is not None:
        return int(estimate_fn(list(messages)))
    for attr in ("estimate_request_tokens", "_estimate_request_tokens"):
        fn = getattr(agent, attr, None)
        if callable(fn):
            try:
                return int(
                    fn(
                        list(messages),
                        system_prompt=getattr(agent, "_cached_system_prompt", "") or "",
                        tools=getattr(agent, "tools", None) or None,
                    )
                )
            except TypeError:
                # 2-arg / 1-arg signature variant
                try:
                    return int(fn(list(messages), getattr(agent, "_cached_system_prompt", "") or ""))
                except TypeError:
                    return int(fn(list(messages)))
    # Fallback: rough chars / 3.5 (matches ``compression.chars_per_token`` default).
    total_chars = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            total_chars += len(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, str):
                    total_chars += len(part)
                elif isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        total_chars += len(text)
    return int(total_chars / 3.5)


def compress_now(
    agent: Any,
    history: Sequence[Dict[str, Any]],
    request: CompressRequest,
    *,
    system_message: Any = None,
    task_id: str = "default",
    skip_without_window: bool = False,
    compress_fn: Optional[CompressFn] = None,
    lock_holder: str = "manual",
    finalize_ctx_engine: Optional[Callable[..., None]] = None,
) -> CompressResult:
    """Run one manual compression of ``history`` on ``agent`` and return the outcome; the caller
    installs ``after_messages`` — history is never mutated here.

    ``preview=True`` performs no compression and leaves ``agent`` untouched. A held compression
    lock yields ``lock_skipped`` with the agent's signal cleared and the deferred context-engine
    notification discarded; otherwise the caller must call ``finalize_context_engine_compression_notification``
    once its own history transaction commits (``committed=False`` on failure). ``system_message=None``
    makes the compressor rebuild the prompt; passing the cached prompt duplicated the identity
    block (#15281). ``skip_without_window`` (gateway) answers ``nothing_to_do`` when the local
    compressor sees no summarizable middle; the in-process surfaces leave it off because the
    compressor still does useful work there (phase-1 tool-result prune / blank-echo drop that
    ``ContextCompressor.compress`` commits even when no summary window exists).

    ``compress_fn`` is the agent-specific compressor invocation; defaults to
    ``agent.compress`` / ``agent._compress_context`` (Spirit surfaces wire whichever is present).
    ``lock_holder`` is the holder string for ``SessionDB.claim_compression_lock`` (callers may
    pass a stable id so concurrent surfaces share the lock).
    """
    before = list(history)
    before_tokens = estimate_request_tokens(agent, before)
    head, tail = before, []
    if request.partial:
        head, tail = split_history_for_partial_compress(before, request.keep_last)
        if not tail:  # degenerate split: nothing to keep verbatim → full compression
            head = before
    if request.preview:
        report = summarize_compress_preview(
            before, request.partial, request.keep_last, request.focus_topic, before_tokens,
        )
        return CompressResult(
            "preview", before, before, before_tokens, before_tokens, request, lines=report["lines"],
        )

    compressor = getattr(agent, "context_compressor", None)
    has_content = getattr(compressor, "has_content_to_compress", None)
    if skip_without_window and callable(has_content) and has_content(head) is False:
        return CompressResult(
            "nothing_to_do", before, before, before_tokens, before_tokens, request,
        )

    # An in-place commit archives every durable row under the lease watermark, the kept tail's
    # included, so it must store the tail again itself. A copy keeps its source's row id only
    # while the source is unchanged since it was loaded (marker present): a resume merge rewrote
    # the row and dropped the marker, and its id no longer bounds what the row holds.
    from ..storage.session_persistence import _DB_PERSISTED_MARKER
    tail_rows = []
    for m in tail:
        row = dict(m)
        if not m.get(_DB_PERSISTED_MARKER):
            row.pop("_row_id", None)
        tail_rows.append(row)

    # Try to claim the per-session compression lock when the storage layer is reachable.
    # Done BEFORE resolving the compressor function so a held lock short-circuits without
    # needing the compressor to be present.
    db = getattr(agent, "session_db", None) or getattr(agent, "db", None)
    if db is not None and hasattr(db, "claim_compression_lock"):
        try:
            if not db.claim_compression_lock(getattr(agent, "session_id", "default"), lock_holder):
                # someone else holds it — short-circuit as a lock_skipped.
                return CompressResult(
                    "lock_skipped",
                    before, before, before_tokens, before_tokens, request,
                    lock_holder="another_holder",
                )
        except Exception:  # noqa: BLE001 — lock infra failure must not abort the run
            pass

    # Resolve the compressor function. Spirit's facade exposes ``agent.compress`` /
    # ``agent._compress_context``; the caller may override either.
    if compress_fn is None:
        for attr in ("compress", "_compress_context"):
            fn = getattr(agent, attr, None)
            if callable(fn):
                compress_fn = fn
                break
    if compress_fn is None:
        raise RuntimeError(
            "compress_now: agent has no compress() / _compress_context() method and "
            "compress_fn was not supplied"
        )

    try:
        try:
            compressed = compress_fn(
                head,
                system_message,
                approx_tokens=before_tokens,
                focus_topic=request.focus_topic,
                force=True,
                **({"task_id": task_id} if task_id != "default" else {}),
                **({"verbatim_tail": tail_rows} if tail_rows else {}),
            )
        except Exception:
            if finalize_ctx_engine is not None:
                finalize_ctx_engine(agent, committed=False)
            raise
    finally:
        if db is not None and hasattr(db, "release_compression_lock"):
            try:
                db.release_compression_lock(getattr(agent, "session_id", "default"))
            except Exception:  # noqa: BLE001
                pass

    # The compressor may return either a bare list or a (compressed, lock_signal) tuple.
    lock_signal = False
    if isinstance(compressed, tuple) and len(compressed) == 2:
        compressed, lock_signal = compressed
    if lock_signal is True or isinstance(lock_signal, str):
        if finalize_ctx_engine is not None:
            finalize_ctx_engine(agent, committed=False)
        return CompressResult(
            "lock_skipped",
            before, before, before_tokens, before_tokens, request,
            lock_holder=lock_signal if isinstance(lock_signal, str) else None,
        )

    # Stamped copies mean the in-place commit stored the tail and already returned head + tail.
    # Rotation, a no-op or a rolled-back commit leave them unstamped, and the tail is then only
    # in the caller's dicts.
    if tail and not all(row.get(_DB_PERSISTED_MARKER) is True for row in tail_rows):
        compressed = rejoin_compressed_head_and_tail(compressed, tail)

    after_tokens = estimate_request_tokens(agent, compressed)
    from .storage_manual_compression_feedback import summarize_manual_compression
    summary = summarize_manual_compression(
        before, compressed, before_tokens, after_tokens, compression_state=compressor,
    )
    return CompressResult(
        "compressed", before, list(compressed), before_tokens, after_tokens, request, summary=summary,
    )


def render_compress_result(result: CompressResult, *, prefix: str = "") -> List[str]:
    """Surface-neutral text lines for a result (each surface may add its own icon/prefix)."""
    if result.status == "preview":
        return [f"{prefix}{line}" for line in result.lines]
    if result.status == "lock_skipped":
        from .storage_manual_compression_feedback import describe_compression_lock_skip
        return [
            f"{prefix}{describe_compression_lock_skip(result.lock_holder or True)}"
        ]
    if result.status == "nothing_to_do":
        return [f"{prefix}Nothing to compress yet."]
    summary = result.summary or {}
    return [
        f"{prefix}{line}"
        for line in (
            summary.get("headline"),
            summary.get("token_line"),
            summary.get("note"),
        )
        if line
    ]


__all__ = [
    "AGGRESSIVE_UNSUPPORTED",
    "MIN_MESSAGES",
    "CompressRequest",
    "CompressResult",
    "parse_compress_args",
    "estimate_request_tokens",
    "compress_now",
    "render_compress_result",
]
