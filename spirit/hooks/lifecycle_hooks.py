"""生命周期钩子 — Agent 和会话的生命周期事件。"""

from __future__ import annotations

import logging
from typing import Any, Optional

from spirit.hooks.hook_manager import HookManager, HookEvent

logger = logging.getLogger(__name__)


def register_lifecycle_hooks(hook_manager: HookManager, agent: Any) -> None:
    """注册默认生命周期钩子处理器。"""

    # ── Agent 初始化 ──

    def _on_agent_init(**kwargs):
        logger.info("Agent 初始化完成: model=%s", agent.model)

    hook_manager.register(HookEvent.ON_AGENT_INIT, _on_agent_init)

    # ── 会话开始 ──

    def _on_session_start(session_id: str = "", **kwargs):
        logger.info("会话开始: %s", session_id[:8] if session_id else "new")
        # 如果有 session_db，加载历史消息
        session_db = getattr(agent, "_session_db", None)
        if session_db and session_id:
            try:
                history = session_db.get_messages(session_id)
                if history:
                    for msg in history:
                        agent.add_message(msg["role"], msg["content"])
                    logger.info("加载 %d 条历史消息", len(history))
            except Exception as e:
                logger.debug("加载历史消息失败: %s", e)

    hook_manager.register(HookEvent.ON_SESSION_START, _on_session_start)

    # ── 会话结束 ──

    def _on_session_end(session_id: str = "", **kwargs):
        logger.info("会话结束: %s", session_id[:8] if session_id else "current")
        # 保存会话状态
        session_db = getattr(agent, "_session_db", None)
        if session_db and session_id:
            try:
                session_db.save_messages(session_id, agent.messages)
            except Exception as e:
                logger.debug("保存会话失败: %s", e)

    hook_manager.register(HookEvent.ON_SESSION_END, _on_session_end)

    # ── 会话重置 ──

    def _on_session_reset(**kwargs):
        logger.info("会话重置")
        # 重置压缩器状态
        compressor = getattr(agent, "_context_compressor", None)
        if compressor:
            try:
                from spirit.agent.context_compressor import SessionLifecycleHooks
                hooks = SessionLifecycleHooks(compressor)
                hooks.on_session_reset()
            except Exception as e:
                logger.debug("重置压缩器失败: %s", e)

    hook_manager.register(HookEvent.ON_SESSION_RESET, _on_session_reset)

    # ── Agent 销毁 ──

    def _on_agent_destroy(**kwargs):
        logger.info("Agent 销毁 — 清理资源")
        # 保存最终状态
        session_db = getattr(agent, "_session_db", None)
        if session_db:
            try:
                session_db.save_messages(agent.session_id, agent.messages)
            except Exception:
                pass

    hook_manager.register(HookEvent.ON_AGENT_DESTROY, _on_agent_destroy)


__all__ = ["register_lifecycle_hooks"]
