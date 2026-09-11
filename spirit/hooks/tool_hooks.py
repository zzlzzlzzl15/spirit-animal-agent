"""工具钩子 — 工具执行前后的事件处理。"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from spirit.hooks.hook_manager import HookManager, HookEvent

logger = logging.getLogger(__name__)


def register_tool_hooks(hook_manager: HookManager, agent: Any) -> None:
    """注册默认工具钩子处理器。"""

    # ── 工具执行前 ──

    def _before_tool_execute(
        tool_name: str = "",
        tool_args: Dict = None,
        **kwargs,
    ):
        logger.info("工具调用: %s(%s)", tool_name, _truncate_args(tool_args))

    hook_manager.register(HookEvent.BEFORE_TOOL_EXECUTE, _before_tool_execute)

    # ── 工具执行后 ──

    def _after_tool_execute(
        tool_name: str = "",
        tool_result: str = "",
        duration_ms: float = 0,
        **kwargs,
    ):
        result_preview = tool_result[:200] if tool_result else ""
        logger.info(
            "工具完成: %s (%.0fms) -> %s%s",
            tool_name,
            duration_ms,
            result_preview,
            "..." if len(tool_result) > 200 else "",
        )

    hook_manager.register(HookEvent.AFTER_TOOL_EXECUTE, _after_tool_execute)

    # ── 工具错误 ──

    def _on_tool_error(
        tool_name: str = "",
        error: Exception = None,
        **kwargs,
    ):
        logger.warning("工具错误: %s -> %s", tool_name, error)

    hook_manager.register(HookEvent.ON_TOOL_ERROR, _on_tool_error)


def _truncate_args(args: Dict, max_len: int = 100) -> str:
    """截断参数显示。"""
    if not args:
        return ""
    s = str(args)
    if len(s) > max_len:
        return s[:max_len] + "..."
    return s


__all__ = ["register_tool_hooks"]
