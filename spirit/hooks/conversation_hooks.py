"""对话钩子 — LLM 调用和对话循环的事件处理。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from spirit.hooks.hook_manager import HookManager, HookEvent

logger = logging.getLogger(__name__)


def register_conversation_hooks(hook_manager: HookManager, agent: Any) -> None:
    """注册默认对话钩子处理器。"""

    # ── LLM 调用前 ──

    def _before_llm_call(
        messages: List[Dict] = None,
        tools: List[Dict] = None,
        **kwargs,
    ):
        msg_count = len(messages) if messages else 0
        tool_count = len(tools) if tools else 0
        logger.debug("LLM 调用: %d 消息, %d 工具", msg_count, tool_count)

    hook_manager.register(HookEvent.BEFORE_LLM_CALL, _before_llm_call)

    # ── LLM 调用后 ──

    def _after_llm_call(
        response: Any = None,
        duration_ms: float = 0,
        **kwargs,
    ):
        # 提取 token 使用量
        tokens = ""
        if response and hasattr(response, "usage") and response.usage:
            tokens = f", tokens={response.usage.total_tokens}"
        logger.debug("LLM 响应: %.0fms%s", duration_ms, tokens)

    hook_manager.register(HookEvent.AFTER_LLM_CALL, _after_llm_call)

    # ── LLM 错误 ──

    def _on_llm_error(
        error: Exception = None,
        retry_count: int = 0,
        **kwargs,
    ):
        logger.warning("LLM 错误 (#%d): %s", retry_count, error)

    hook_manager.register(HookEvent.ON_LLM_ERROR, _on_llm_error)

    # ── 上下文压缩 ──

    def _on_context_compress(
        before_tokens: int = 0,
        after_tokens: int = 0,
        **kwargs,
    ):
        reduction = before_tokens - after_tokens
        logger.info(
            "上下文压缩: %d -> %d tokens (减少 %d)",
            before_tokens, after_tokens, reduction,
        )

    hook_manager.register(HookEvent.ON_CONTEXT_COMPRESS, _on_context_compress)

    # ── 对话开始 ──

    def _before_conversation(
        user_message: str = "",
        **kwargs,
    ):
        logger.debug("对话开始: %s", user_message[:50])

    hook_manager.register(HookEvent.BEFORE_CONVERSATION, _before_conversation)

    # ── 对话结束 ──

    def _after_conversation(
        response: str = "",
        iterations: int = 0,
        **kwargs,
    ):
        logger.debug("对话结束: %d 轮迭代", iterations)

    hook_manager.register(HookEvent.AFTER_CONVERSATION, _after_conversation)


__all__ = ["register_conversation_hooks"]
