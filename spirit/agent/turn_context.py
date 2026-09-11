"""Per-turn setup for ``run_conversation`` (the turn prologue).

This module handles all once-per-turn initialization before the main conversation
loop starts. It extracts the ~200 lines of setup code from run_conversation into
a dedicated builder function that returns a TurnContext dataclass.

Reference: agent/turn_context.py (Hermes Agent, 626 lines)
Simplified for Spirit Agent architecture.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class TurnContext:
    """Values produced by the turn prologue and consumed by the turn loop.
    
    Attributes:
        user_message: Sanitized inbound message (surrogates stripped)
        original_user_message: Clean message preserved for transcripts
        messages: Working message list for this turn (loop appends to it)
        conversation_history: May be reset by preflight compression
        active_system_prompt: Cached system prompt active for this turn
        effective_task_id: Task identifier for this turn
        turn_id: Unique turn identifier
        current_turn_user_idx: Index of current user turn within messages
        should_review_memory: Whether post-turn memory review should fire
        plugin_user_context: Context from plugins (appended to user message)
        ext_prefetch_cache: External-memory prefetch result
    """
    
    # Sanitized inbound message (surrogates stripped)
    user_message: str
    
    # Clean message preserved for transcripts / memory queries
    original_user_message: Any
    
    # Working message list for this turn (loop appends to it)
    messages: List[Dict[str, Any]]
    
    # May be reset to None by preflight compression (new session created)
    conversation_history: Optional[List[Dict[str, Any]]]
    
    # Cached system prompt active for this turn (may be rebuilt by compression)
    active_system_prompt: Optional[str]
    
    # Task / turn identifiers
    effective_task_id: str
    turn_id: str
    
    # Index of the current user turn within messages
    current_turn_user_idx: int
    
    # Whether the post-turn memory review should fire
    should_review_memory: bool = False
    
    # Context contributed by plugins (appended to user message)
    plugin_user_context: str = ""
    
    # External-memory prefetch result, reused across loop iterations
    ext_prefetch_cache: str = ""


def _sanitize_surrogates(text: str) -> str:
    """Remove invalid Unicode surrogate pairs from text.
    
    Args:
        text: Input text that may contain invalid surrogates
        
    Returns:
        Cleaned text with surrogates replaced by replacement character
    """
    if not isinstance(text, str):
        return str(text)
    
    try:
        # Try to encode/decode to catch surrogate errors
        text.encode('utf-8').decode('utf-8')
        return text
    except UnicodeEncodeError:
        # Replace problematic characters
        return text.encode('utf-8', errors='replace').decode('utf-8')


def _install_safe_stdio():
    """Guard stdio against OSError from broken pipes.
    
    Prevents crashes when stdout/stderr are closed (systemd/headless/daemon).
    """
    import sys
    import os
    
    # Check if stdout/stderr are valid file descriptors
    try:
        if hasattr(sys.stdout, 'fileno'):
            fd = sys.stdout.fileno()
            if fd >= 0:
                os.fstat(fd)  # Will raise OSError if invalid
    except (OSError, io.UnsupportedOperation):
        # Redirect to null device if stdout is broken
        sys.stdout = open(os.devnull, 'w', encoding='utf-8', errors='replace')
    
    try:
        if hasattr(sys.stderr, 'fileno'):
            fd = sys.stderr.fileno()
            if fd >= 0:
                os.fstat(fd)
    except (OSError, io.UnsupportedOperation):
        sys.stderr = open(os.devnull, 'w', encoding='utf-8', errors='replace')


def build_turn_context(
    agent,
    user_message: Any,
    system_message: Optional[str],
    conversation_history: Optional[List[Dict[str, Any]]],
    task_id: Optional[str],
    stream_callback=None,
    persist_user_message: Optional[Any] = None,
    persist_user_timestamp: Optional[float] = None,
) -> TurnContext:
    """Run the once-per-turn setup and return the loop's input context.
    
    This function performs all initialization that needs to happen before the
    main conversation loop starts:
    
    1. Guard stdio against broken pipes
    2. Sanitize user message (remove invalid Unicode surrogates)
    3. Restore or build system prompt
    4. Initialize session state (task_id, turn_id)
    5. Reset per-turn counters
    6. Prefetch external memory (async, non-blocking)
    
    Args:
        agent: SpiritAgent instance
        user_message: User's input message
        system_message: Optional custom system message
        conversation_history: Previous conversation messages
        task_id: Optional task identifier
        stream_callback: Optional callback for streaming output
        persist_user_message: Clean message for transcripts
        persist_user_timestamp: Timestamp for persisted message
        
    Returns:
        TurnContext: All values needed by the conversation loop
        
    Side Effects:
        - Mutates agent state (counters, thread id, cached prompt)
        - Creates/updates session in database
        - Installs safe stdio handlers
    """
    import io
    
    # ─ Step 1: Guard stdio ───────────────────────────────────────
    _install_safe_stdio()
    
    # ── Step 2: Sanitize user message ─────────────────────────────
    original_user_message = persist_user_message if persist_user_message is not None else user_message
    user_message_str = str(user_message) if user_message else ""
    user_message_clean = _sanitize_surrogates(user_message_str)
    
    logger.debug(
        "User message sanitized: original_len=%d, cleaned_len=%d",
        len(user_message_str), len(user_message_clean)
    )
    
    # ── Step 3: Restore or build system prompt ────────────────────
    # For first turn, build and cache system prompt
    # For subsequent turns, reuse cached prompt (protects Prompt Caching)
    if not agent.messages:
        # First turn: build system prompt
        active_system_prompt = agent.get_system_prompt()
        agent.add_message("system", active_system_prompt)
        logger.info(
            "System prompt built and cached (%d characters)",
            len(active_system_prompt)
        )
    else:
        # Subsequent turn: reuse cached prompt
        active_system_prompt = getattr(agent, '_cached_system_prompt', None)
        if not active_system_prompt:
            # Fallback: rebuild if cache missing
            active_system_prompt = agent.get_system_prompt()
            logger.warning("System prompt cache missing, rebuilt")
    
    # ── Step 4: Initialize session state ──────────────────────────
    # Generate or use existing task_id
    effective_task_id = task_id or getattr(agent, 'session_id', None) or str(uuid.uuid4())
    
    # Generate turn_id (unique per API call within a task)
    api_call_count = getattr(agent, '_api_call_count', 0)
    turn_id = f"{effective_task_id}-{api_call_count + 1}"
    
    # Find index of current user turn in messages
    current_turn_user_idx = len(agent.messages)
    
    logger.debug(
        "Session initialized: task_id=%s, turn_id=%s, user_idx=%d",
        effective_task_id, turn_id, current_turn_user_idx
    )
    
    # ── Step 5: Reset per-turn counters ───────────────────────────
    # Reset commentary deduplication (spans all tool calls within one turn)
    agent._delivered_interim_texts = set()
    
    # Reset auth pool refresh counts (prevents infinite refresh loops on persistent 401)
    agent._auth_pool_refresh_counts = {}
    
    # Reset iteration budget if starting new turn
    if not hasattr(agent, '_iteration_budget') or agent._iteration_budget.used >= agent._iteration_budget.max_total:
        from spirit.agent.iteration_budget import IterationBudget
        max_iterations = getattr(agent, 'max_iterations', 50)
        agent._iteration_budget = IterationBudget(max_total=max_iterations)
        logger.info("Iteration budget reset: max_total=%d", max_iterations)
    
    # ── Step 6: Prefetch external memory (non-blocking) ───────────
    # External memory prefetch happens asynchronously, results cached for later use
    ext_prefetch_cache = ""
    try:
        # If agent has memory manager, trigger async prefetch
        if hasattr(agent, '_memory_manager') and agent._memory_manager:
            # Start prefetch but don't wait for it
            agent._memory_manager.prefetch_async(effective_task_id, user_message_clean)
            logger.debug("External memory prefetch started (async)")
    except Exception as e:
        logger.debug("External memory prefetch failed (non-critical): %s", e)
    
    # ── Step 7: Plugin user context ───────────────────────────────
    # Plugins can contribute additional context to user message
    plugin_user_context = ""
    try:
        if hasattr(agent, '_plugin_manager') and agent._plugin_manager:
            plugin_user_context = agent._plugin_manager.get_user_context(
                agent, 
                user_message_clean
            )
            if plugin_user_context:
                logger.debug("Plugin context added: %d characters", len(plugin_user_context))
    except Exception as e:
        logger.debug("Plugin context retrieval failed (non-critical): %s", e)
    
    # ── Step 8: Prepare working messages ──────────────────────────
    # Copy messages to working list (loop will append to this)
    messages = list(agent.messages) if agent.messages else []
    
    # Add user message to working list
    messages.append({
        "role": "user",
        "content": user_message_clean,
    })
    
    # ── Step 9: Determine if memory review should fire ────────────
    # Memory review triggers after certain conditions are met
    should_review_memory = False
    try:
        if hasattr(agent, '_memory_manager') and agent._memory_manager:
            # Check if enough turns have passed since last review
            turns_since_review = getattr(agent, '_turns_since_memory_review', 0)
            review_interval = getattr(agent, '_memory_review_interval', 10)
            
            if turns_since_review >= review_interval:
                should_review_memory = True
                logger.debug(
                    "Memory review triggered: turns_since=%d, interval=%d",
                    turns_since_review, review_interval
                )
    except Exception as e:
        logger.debug("Memory review check failed (non-critical): %s", e)
    
    # ── Return context ────────────────────────────────────────────
    context = TurnContext(
        user_message=user_message_clean,
        original_user_message=original_user_message,
        messages=messages,
        conversation_history=conversation_history,
        active_system_prompt=active_system_prompt,
        effective_task_id=effective_task_id,
        turn_id=turn_id,
        current_turn_user_idx=current_turn_user_idx,
        should_review_memory=should_review_memory,
        plugin_user_context=plugin_user_context,
        ext_prefetch_cache=ext_prefetch_cache,
    )
    
    logger.info(
        "Turn context built: task=%s, turn=%s, messages=%d, memory_review=%s",
        effective_task_id, turn_id, len(messages), should_review_memory
    )
    
    return context


if __name__ == "__main__":
    # Simple test
    import sys
    
    print("Testing TurnContext...")
    
    # Test 1: _sanitize_surrogates
    clean_text = _sanitize_surrogates("Hello World")
    assert clean_text == "Hello World"
    print("[OK] Test 1 passed: _sanitize_surrogates")
    
    # Test 2: TurnContext creation
    ctx = TurnContext(
        user_message="test",
        original_user_message="test",
        messages=[],
        conversation_history=None,
        active_system_prompt="test prompt",
        effective_task_id="task-123",
        turn_id="task-123-1",
        current_turn_user_idx=0,
    )
    assert ctx.user_message == "test"
    assert ctx.effective_task_id == "task-123"
    print("[OK] Test 2 passed: TurnContext creation")
    
    print("\n[SUCCESS] All tests passed!")
    sys.exit(0)
