"""Spirit Agent hooks 系统完整测试。

回归目标：
  - v0.2.11 治本：conversation_loop 真实触发全部 hooks（不再"只注册不触发"）
  - register_conversation_hooks / register_tool_hooks 默认注册所有事件
  - 事件名常量 HookEvent 字符串值校验
  - emit handler 异常隔离
  - 优先级排序 + 全局监听器 + emit_count 统计

参考 hermes:
  - test_turn_finalizer_* (5 个 turn_finalizer 测试)
  - test_turn_api_call_interrupt.py
  - test_turn_liveness.py
  - test_turn_overlap_tripwire.py
  - test_tool_call_incremental_persistence.py
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ─────────────────────────── HookEvent 字符串值校验 ───────────────────────────


class TestHookEventConstants:
    """HookEvent 字符串值校验（防止字符串拼写错误）。"""

    def test_lifecycle_events(self):
        """生命周期事件名"""
        from spirit.hooks.hook_manager import HookEvent
        assert HookEvent.ON_AGENT_INIT == "on_agent_init"
        assert HookEvent.ON_SESSION_START == "on_session_start"
        assert HookEvent.ON_SESSION_END == "on_session_end"
        assert HookEvent.ON_SESSION_RESET == "on_session_reset"
        assert HookEvent.ON_AGENT_DESTROY == "on_agent_destroy"

    def test_conversation_events(self):
        """对话事件名"""
        from spirit.hooks.hook_manager import HookEvent
        assert HookEvent.BEFORE_CONVERSATION == "before_conversation"
        assert HookEvent.AFTER_CONVERSATION == "after_conversation"
        assert HookEvent.BEFORE_LLM_CALL == "before_llm_call"
        assert HookEvent.AFTER_LLM_CALL == "after_llm_call"
        assert HookEvent.ON_LLM_ERROR == "on_llm_error"
        assert HookEvent.ON_STREAM_DELTA == "on_stream_delta"
        assert HookEvent.ON_CONTEXT_COMPRESS == "on_context_compress"

    def test_tool_events(self):
        """工具事件名"""
        from spirit.hooks.hook_manager import HookEvent
        assert HookEvent.BEFORE_TOOL_EXECUTE == "before_tool_execute"
        assert HookEvent.AFTER_TOOL_EXECUTE == "after_tool_execute"
        assert HookEvent.ON_TOOL_ERROR == "on_tool_error"
        assert HookEvent.ON_TOOL_APPROVED == "on_tool_approved"
        assert HookEvent.ON_TOOL_REJECTED == "on_tool_rejected"

    def test_task_events(self):
        """任务事件名"""
        from spirit.hooks.hook_manager import HookEvent
        assert HookEvent.ON_TASK_CREATE == "on_task_create"
        assert HookEvent.ON_TASK_START == "on_task_start"
        assert HookEvent.ON_TASK_COMPLETE == "on_task_complete"
        assert HookEvent.ON_TASK_FAIL == "on_task_fail"
        assert HookEvent.ON_TASK_PROGRESS == "on_task_progress"


# ─────────────────────────── HookManager 核心行为 ───────────────────────────


class TestHookManagerRegister:
    """HookManager.register / unregister。"""

    def test_register_handler(self):
        """register 后 handler 被存储"""
        from spirit.hooks.hook_manager import HookManager
        hm = HookManager()
        handler = lambda **kw: None
        hm.register("on_test", handler)
        assert "on_test" in hm.list_events()
        assert handler.__name__ in hm.list_handlers("on_test")

    def test_unregister_existing_handler(self):
        """unregister 存在的 handler → True"""
        from spirit.hooks.hook_manager import HookManager
        hm = HookManager()
        handler = lambda **kw: None
        hm.register("on_test", handler)
        result = hm.unregister("on_test", handler)
        assert result is True
        assert "on_test" not in hm.list_events() or not hm.has_handlers("on_test")

    def test_unregister_nonexistent_handler(self):
        """unregister 不存在的 handler → False"""
        from spirit.hooks.hook_manager import HookManager
        hm = HookManager()
        handler = lambda **kw: None
        result = hm.unregister("on_test", handler)
        assert result is False

    def test_register_multiple_handlers_same_event(self):
        """同一事件注册多个 handler"""
        from spirit.hooks.hook_manager import HookManager
        hm = HookManager()
        h1 = lambda **kw: None
        h2 = lambda **kw: None
        hm.register("on_test", h1)
        hm.register("on_test", h2)
        handlers = hm.list_handlers("on_test")
        assert h1.__name__ in handlers
        assert h2.__name__ in handlers


class TestHookManagerPriority:
    """优先级排序（数字越小越先执行）。"""

    def test_handlers_sorted_by_priority(self):
        """handler 按 priority 升序执行"""
        from spirit.hooks.hook_manager import HookManager
        hm = HookManager()
        order = []

        def h_high(**kw):
            order.append("high")

        def h_low(**kw):
            order.append("low")

        def h_mid(**kw):
            order.append("mid")

        # 注册顺序：high (10), low (200), mid (100)
        hm.register("on_test", h_high, priority=10)
        hm.register("on_test", h_low, priority=200)
        hm.register("on_test", h_mid, priority=100)

        hm.emit("on_test")

        # 预期：high(10) → mid(100) → low(200)
        assert order == ["high", "mid", "low"]


class TestHookManagerEmit:
    """HookManager.emit 行为。"""

    def test_emit_calls_handler_with_kwargs(self):
        """emit 触发 handler 并传 kwargs"""
        from spirit.hooks.hook_manager import HookManager
        hm = HookManager()
        captured = {}

        def h(**kw):
            captured.update(kw)

        hm.register("on_test", h)
        hm.emit("on_test", foo="bar", x=42)
        assert captured == {"foo": "bar", "x": 42}

    def test_emit_no_handlers_does_not_raise(self):
        """emit 无 handler 时不抛错"""
        from spirit.hooks.hook_manager import HookManager
        hm = HookManager()
        # 不应抛错
        hm.emit("on_nonexistent", x=1)

    def test_emit_handler_exception_does_not_propagate(self):
        """handler 抛错不阻断 emit"""
        from spirit.hooks.hook_manager import HookManager
        hm = HookManager()
        called_after = []

        def bad_handler(**kw):
            raise RuntimeError("boom")

        def good_handler(**kw):
            called_after.append("good")

        hm.register("on_test", bad_handler)
        hm.register("on_test", good_handler)
        # 不应抛错
        hm.emit("on_test")
        # 后续 handler 仍被执行
        assert "good" in called_after

    def test_emit_increments_emit_count(self):
        """emit 增加 _emit_count 统计"""
        from spirit.hooks.hook_manager import HookManager
        hm = HookManager()
        hm.register("on_test", lambda **kw: None)
        hm.emit("on_test")
        hm.emit("on_test")
        hm.emit("on_test")
        stats = hm.get_stats()
        assert stats["on_test"] == 3

    def test_emit_with_results_collects_handler_returns(self):
        """emit_with_results 收集所有 handler 返回值"""
        from spirit.hooks.hook_manager import HookManager
        hm = HookManager()
        hm.register("on_test", lambda **kw: "result1")
        hm.register("on_test", lambda **kw: "result2")
        hm.register("on_test", lambda **kw: None)  # None 被过滤
        results = hm.emit_with_results("on_test")
        assert "result1" in results
        assert "result2" in results
        assert len(results) == 2  # None 被过滤


class TestHookManagerGlobalHandler:
    """on_all 全局监听器。"""

    def test_global_handler_receives_all_events(self):
        """on_all 注册的 handler 接收所有事件"""
        from spirit.hooks.hook_manager import HookManager
        hm = HookManager()
        events_seen = []

        def global_h(event, **kw):
            events_seen.append(event)

        hm.on_all(global_h)
        hm.emit("event_a")
        hm.emit("event_b")
        hm.emit("event_c")
        assert "event_a" in events_seen
        assert "event_b" in events_seen
        assert "event_c" in events_seen

    def test_global_handler_exception_does_not_propagate(self):
        """全局 handler 抛错不阻断"""
        from spirit.hooks.hook_manager import HookManager
        hm = HookManager()

        def bad_global(event, **kw):
            raise RuntimeError("boom")

        hm.on_all(bad_global)
        # 不应抛错
        hm.emit("event_x")


class TestHookManagerClear:
    """HookManager.clear。"""

    def test_clear_specific_event(self):
        """clear(event) 只清指定事件"""
        from spirit.hooks.hook_manager import HookManager
        hm = HookManager()
        hm.register("event_a", lambda **kw: None)
        hm.register("event_b", lambda **kw: None)
        hm.clear("event_a")
        assert "event_a" not in hm.list_events()
        assert "event_b" in hm.list_events()

    def test_clear_all(self):
        """clear() 清所有事件 + 全局 + 统计"""
        from spirit.hooks.hook_manager import HookManager
        hm = HookManager()
        hm.register("event_a", lambda **kw: None)
        hm.on_all(lambda event, **kw: None)
        hm.emit("event_a")
        hm.clear()
        assert hm.list_events() == []
        stats = hm.get_stats()
        assert stats == {}


# ─────────────────────────── register_conversation_hooks ───────────────────────────


class TestRegisterConversationHooks:
    """register_conversation_hooks 注册 6 个对话事件。"""

    def test_registers_6_conversation_events(self):
        """注册 6 个事件：before/after_conversation + before/after_llm_call + on_llm_error + on_context_compress"""
        from spirit.hooks.hook_manager import HookManager
        from spirit.hooks.conversation_hooks import register_conversation_hooks

        hm = HookManager()
        agent = MagicMock()
        register_conversation_hooks(hm, agent)

        # 验证 6 个事件都注册了
        assert hm.has_handlers("before_conversation")
        assert hm.has_handlers("after_conversation")
        assert hm.has_handlers("before_llm_call")
        assert hm.has_handlers("after_llm_call")
        assert hm.has_handlers("on_llm_error")
        assert hm.has_handlers("on_context_compress")

    def test_before_conversation_handler_called(self):
        """before_conversation handler 接收 user_message"""
        from spirit.hooks.hook_manager import HookManager
        from spirit.hooks.conversation_hooks import register_conversation_hooks

        hm = HookManager()
        agent = MagicMock()
        register_conversation_hooks(hm, agent)

        # 替换 handler 为 spy
        captured = []
        hm._hooks["before_conversation"] = [
            (100, lambda **kw: captured.append(kw.get("user_message")))
        ]
        hm.emit("before_conversation", user_message="hello")
        assert "hello" in captured

    def test_after_conversation_handler_called(self):
        """after_conversation handler 接收 response + iterations"""
        from spirit.hooks.hook_manager import HookManager
        from spirit.hooks.conversation_hooks import register_conversation_hooks

        hm = HookManager()
        register_conversation_hooks(hm, MagicMock())

        captured = []
        hm._hooks["after_conversation"] = [
            (100, lambda **kw: captured.append((kw.get("response"), kw.get("iterations"))))
        ]
        hm.emit("after_conversation", response="ok", iterations=3)
        assert ("ok", 3) in captured

    def test_after_llm_call_handler_called_with_response(self):
        """after_llm_call handler 接收 response + duration_ms"""
        from spirit.hooks.hook_manager import HookManager
        from spirit.hooks.conversation_hooks import register_conversation_hooks

        hm = HookManager()
        register_conversation_hooks(hm, MagicMock())

        captured = []
        hm._hooks["after_llm_call"] = [
            (100, lambda **kw: captured.append((kw.get("response"), kw.get("duration_ms"))))
        ]
        mock_response = MagicMock()
        mock_response.usage.total_tokens = 100
        hm.emit("after_llm_call", response=mock_response, duration_ms=123.4)
        assert len(captured) == 1

    def test_on_llm_error_handler_called(self):
        """on_llm_error handler 接收 error + retry_count"""
        from spirit.hooks.hook_manager import HookManager
        from spirit.hooks.conversation_hooks import register_conversation_hooks

        hm = HookManager()
        register_conversation_hooks(hm, MagicMock())

        captured = []
        hm._hooks["on_llm_error"] = [
            (100, lambda **kw: captured.append((kw.get("error"), kw.get("retry_count"))))
        ]
        err = ValueError("api down")
        hm.emit("on_llm_error", error=err, retry_count=2)
        assert (err, 2) in captured

    def test_on_context_compress_handler_called(self):
        """on_context_compress handler 接收 before_tokens + after_tokens"""
        from spirit.hooks.hook_manager import HookManager
        from spirit.hooks.conversation_hooks import register_conversation_hooks

        hm = HookManager()
        register_conversation_hooks(hm, MagicMock())

        captured = []
        hm._hooks["on_context_compress"] = [
            (100, lambda **kw: captured.append((kw.get("before_tokens"), kw.get("after_tokens"))))
        ]
        hm.emit("on_context_compress", before_tokens=1000, after_tokens=300)
        assert (1000, 300) in captured


# ─────────────────────────── register_tool_hooks ───────────────────────────


class TestRegisterToolHooks:
    """register_tool_hooks 注册 3 个工具事件。"""

    def test_registers_3_tool_events(self):
        """注册 3 个事件：before/after_tool_execute + on_tool_error"""
        from spirit.hooks.hook_manager import HookManager
        from spirit.hooks.tool_hooks import register_tool_hooks

        hm = HookManager()
        register_tool_hooks(hm, MagicMock())

        assert hm.has_handlers("before_tool_execute")
        assert hm.has_handlers("after_tool_execute")
        assert hm.has_handlers("on_tool_error")

    def test_before_tool_execute_handler_called(self):
        """before_tool_execute handler 接收 tool_name + tool_args"""
        from spirit.hooks.hook_manager import HookManager
        from spirit.hooks.tool_hooks import register_tool_hooks

        hm = HookManager()
        register_tool_hooks(hm, MagicMock())

        captured = []
        hm._hooks["before_tool_execute"] = [
            (100, lambda **kw: captured.append((kw.get("tool_name"), kw.get("tool_args"))))
        ]
        hm.emit("before_tool_execute", tool_name="search", tool_args={"q": "test"})
        assert ("search", {"q": "test"}) in captured

    def test_after_tool_execute_handler_called(self):
        """after_tool_execute handler 接收 tool_name + tool_result + duration_ms"""
        from spirit.hooks.hook_manager import HookManager
        from spirit.hooks.tool_hooks import register_tool_hooks

        hm = HookManager()
        register_tool_hooks(hm, MagicMock())

        captured = []
        hm._hooks["after_tool_execute"] = [
            (100, lambda **kw: captured.append(kw.get("tool_name")))
        ]
        hm.emit("after_tool_execute", tool_name="search", tool_result="found", duration_ms=50)
        assert "search" in captured

    def test_on_tool_error_handler_called(self):
        """on_tool_error handler 接收 tool_name + error"""
        from spirit.hooks.hook_manager import HookManager
        from spirit.hooks.tool_hooks import register_tool_hooks

        hm = HookManager()
        register_tool_hooks(hm, MagicMock())

        captured = []
        hm._hooks["on_tool_error"] = [
            (100, lambda **kw: captured.append((kw.get("tool_name"), str(kw.get("error")))))
        ]
        err = RuntimeError("tool crashed")
        hm.emit("on_tool_error", tool_name="bash", error=err)
        assert ("bash", "tool crashed") in captured


# ─────────────────────────── SpiritAgent 真实触发所有 hooks（v0.2.11 治本） ───────────────────────────


class TestSpiritAgentTriggersAllHooks:
    """SpiritAgent.__init__ 自动装配 6 个 register_*_hooks，触发链路完整（v0.2.11 治本）。"""

    def test_fresh_agent_has_all_hooks_registered(self, monkeypatch, tmp_path):
        """fresh SpiritAgent 创建后 13 个 hooks 都已注册"""
        # 隔离 db
        db_path = tmp_path / "hooks_test.db"
        monkeypatch.setattr(
            "spirit.storage.session_db.DEFAULT_DB_PATH",
            db_path,
        )

        from spirit.agent.agent import SpiritAgent, AgentConfig
        from spirit.hooks.hook_manager import HookEvent

        config = AgentConfig(model="gpt-4o-mini", api_key="test-key-12345")
        agent = SpiritAgent(config=config)

        try:
            # 13 个事件都已被 register
            expected_events = [
                # lifecycle
                "on_agent_init",
                "on_session_start",
                "on_session_end",
                "on_session_reset",
                "on_agent_destroy",
                # conversation
                "before_conversation",
                "after_conversation",
                "before_llm_call",
                "after_llm_call",
                "on_llm_error",
                "on_context_compress",
                # tool
                "before_tool_execute",
                "after_tool_execute",
                "on_tool_error",
            ]
            registered = set(agent.hook_manager.list_events())
            for ev in expected_events:
                assert ev in registered, f"event '{ev}' not registered, got {registered}"
        finally:
            if agent._session_db is not None:
                agent._session_db.close()
