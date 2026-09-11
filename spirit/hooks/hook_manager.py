"""HookManager — 统一钩子管理器。

设计原则（借鉴 Hermes 防御性编程）：
1. 钩子失败不影响主流程（异常隔离）
2. 每个 handler 独立 try-except
3. 支持优先级排序（数字越小越先执行）
4. 支持全局监听器（捕获所有事件）
5. 支持异步 handler

事件命名约定：
  - before_*  : 事件发生前
  - after_*   : 事件发生后
  - on_*      : 事件发生时 / 状态变更
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class HookManager:
    """统一钩子管理器 — Spirit Agent 的事件总线核心。"""

    def __init__(self):
        # {event_name: [(priority, handler), ...]}
        self._hooks: Dict[str, List[Tuple[int, Callable]]] = {}
        # 全局监听器（监听所有事件）
        self._global_handlers: List[Callable] = []
        # 事件统计
        self._emit_count: Dict[str, int] = {}
        # 是否启用调试日志
        self._debug = False

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register(
        self,
        event: str,
        handler: Callable,
        priority: int = 100,
    ) -> None:
        """注册钩子处理器。

        Args:
            event: 事件名称（如 "on_task_create"）
            handler: 处理函数（同步或异步）
            priority: 优先级（数字越小越先执行，默认 100）
        """
        self._hooks.setdefault(event, [])
        self._hooks[event].append((priority, handler))
        # 按优先级排序
        self._hooks[event].sort(key=lambda x: x[0])
        if self._debug:
            logger.debug(
                "注册钩子: %s -> %s (priority=%d)",
                event, handler.__name__, priority,
            )

    def unregister(self, event: str, handler: Callable) -> bool:
        """取消注册钩子处理器。

        Returns:
            是否成功移除
        """
        handlers = self._hooks.get(event, [])
        for i, (_, h) in enumerate(handlers):
            if h is handler:
                handlers.pop(i)
                return True
        return False

    def on_all(self, handler: Callable) -> None:
        """注册全局监听器（监听所有事件）。"""
        self._global_handlers.append(handler)

    # ------------------------------------------------------------------
    # 触发
    # ------------------------------------------------------------------

    def emit(self, event: str, **kwargs: Any) -> None:
        """同步触发钩子事件。

        防御性设计：
        - 每个 handler 独立 try-except
        - 一个 handler 失败不影响其他
        - 记录 debug 日志但不中断
        """
        self._emit_count[event] = self._emit_count.get(event, 0) + 1

        # 特定事件处理器
        for priority, handler in self._hooks.get(event, []):
            try:
                if asyncio.iscoroutinefunction(handler):
                    # 异步 handler — 尝试在现有事件循环中调度
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(handler(**kwargs))
                    except RuntimeError:
                        # 没有运行中的事件循环，同步执行
                        handler(**kwargs)
                else:
                    handler(**kwargs)
            except Exception as e:
                logger.debug(
                    "钩子 %s.%s 失败: %s",
                    event, handler.__name__, e,
                )

        # 全局监听器
        for handler in self._global_handlers:
            try:
                handler(event, **kwargs)
            except Exception as e:
                logger.debug("全局钩子 %s 失败: %s", handler.__name__, e)

    async def emit_async(self, event: str, **kwargs: Any) -> None:
        """异步触发钩子事件（等待所有异步 handler 完成）。"""
        self._emit_count[event] = self._emit_count.get(event, 0) + 1
        tasks = []

        for priority, handler in self._hooks.get(event, []):
            try:
                if asyncio.iscoroutinefunction(handler):
                    tasks.append(handler(**kwargs))
                else:
                    handler(**kwargs)
            except Exception as e:
                logger.debug("钩子 %s.%s 失败: %s", event, handler.__name__, e)

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.debug("异步钩子失败: %s", result)

        # 全局监听器
        for handler in self._global_handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event, **kwargs)
                else:
                    handler(event, **kwargs)
            except Exception as e:
                logger.debug("全局异步钩子失败: %s", e)

    def emit_with_results(self, event: str, **kwargs: Any) -> List[Any]:
        """触发钩子并收集所有 handler 的返回值。

        用于需要聚合结果的场景（如审批流程）。
        """
        results = []
        for priority, handler in self._hooks.get(event, []):
            try:
                result = handler(**kwargs)
                if result is not None:
                    results.append(result)
            except Exception as e:
                logger.debug("钩子 %s.%s 失败: %s", event, handler.__name__, e)
        return results

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def has_handlers(self, event: str) -> bool:
        """检查事件是否有注册的处理器。"""
        return bool(self._hooks.get(event))

    def list_events(self) -> List[str]:
        """列出所有已注册的事件名称。"""
        return list(self._hooks.keys())

    def list_handlers(self, event: str) -> List[str]:
        """列出事件的所有处理器名称。"""
        return [h.__name__ for _, h in self._hooks.get(event, [])]

    def get_stats(self) -> Dict[str, int]:
        """获取事件触发统计。"""
        return dict(self._emit_count)

    # ------------------------------------------------------------------
    # 管理
    # ------------------------------------------------------------------

    def clear(self, event: str = None) -> None:
        """清除钩子。

        Args:
            event: 指定事件名则只清该事件；None 则清除所有
        """
        if event:
            self._hooks.pop(event, None)
        else:
            self._hooks.clear()
            self._global_handlers.clear()
            self._emit_count.clear()

    def set_debug(self, enabled: bool) -> None:
        """启用/禁用调试日志。"""
        self._debug = enabled


# ---------------------------------------------------------------------------
# 预定义事件名称常量
# ---------------------------------------------------------------------------

class HookEvent:
    """预定义钩子事件名称 — 避免字符串拼写错误。"""

    # ── 生命周期 ──
    ON_AGENT_INIT = "on_agent_init"
    ON_SESSION_START = "on_session_start"
    ON_SESSION_END = "on_session_end"
    ON_SESSION_RESET = "on_session_reset"
    ON_AGENT_DESTROY = "on_agent_destroy"

    # ── 对话 ──
    BEFORE_LLM_CALL = "before_llm_call"
    AFTER_LLM_CALL = "after_llm_call"
    ON_LLM_ERROR = "on_llm_error"
    ON_STREAM_DELTA = "on_stream_delta"
    ON_CONTEXT_COMPRESS = "on_context_compress"
    BEFORE_CONVERSATION = "before_conversation"
    AFTER_CONVERSATION = "after_conversation"

    # ── 工具 ──
    BEFORE_TOOL_EXECUTE = "before_tool_execute"
    AFTER_TOOL_EXECUTE = "after_tool_execute"
    ON_TOOL_ERROR = "on_tool_error"
    ON_TOOL_APPROVED = "on_tool_approved"
    ON_TOOL_REJECTED = "on_tool_rejected"

    # ── 任务 ──
    ON_TASK_CREATE = "on_task_create"
    ON_TASK_START = "on_task_start"
    ON_TASK_COMPLETE = "on_task_complete"
    ON_TASK_FAIL = "on_task_fail"
    ON_TASK_PROGRESS = "on_task_progress"
    ON_TASK_CHECK = "on_task_check"           # 任务预检事件
    ON_TASK_DECOMPOSE = "on_task_decompose"   # 任务分解事件
    ON_SUBTASK_CREATE = "on_subtask_create"
    ON_SUBTASK_COMPLETE = "on_subtask_complete"
    ON_SUBTASK_FAIL = "on_subtask_fail"

    # ── 目标 ──
    ON_GOAL_SET = "on_goal_set"
    ON_GOAL_COMPLETE = "on_goal_complete"

    # ── 通知 ──
    ON_STATUS = "on_status"
    ON_ERROR = "on_error"
    ON_WARNING = "on_warning"


__all__ = ["HookManager", "HookEvent"]
