"""tests/hooks/test_hook_manager.py — 钩子系统测试。"""

import pytest
import time
from spirit.hooks.hook_manager import HookManager, HookEvent


class TestHookManager:
    """HookManager 核心测试。"""

    def test_register_and_emit(self, hook_manager):
        """测试注册和触发。"""
        results = []
        hook_manager.register("test_event", lambda **kw: results.append("called"))
        hook_manager.emit("test_event")
        assert results == ["called"]

    def test_emit_with_kwargs(self, hook_manager):
        """测试带参数触发。"""
        captured = {}
        def handler(**kw):
            captured.update(kw)
        hook_manager.register("test", handler)
        hook_manager.emit("test", name="spirit", value=42)
        assert captured["name"] == "spirit"
        assert captured["value"] == 42

    def test_priority_ordering(self, hook_manager):
        """测试优先级排序。"""
        order = []
        hook_manager.register("test", lambda **kw: order.append("low"), priority=200)
        hook_manager.register("test", lambda **kw: order.append("high"), priority=10)
        hook_manager.register("test", lambda **kw: order.append("mid"), priority=100)
        hook_manager.emit("test")
        assert order == ["high", "mid", "low"]

    def test_multiple_handlers(self, hook_manager):
        """测试多处理器。"""
        results = []
        hook_manager.register("test", lambda **kw: results.append("a"))
        hook_manager.register("test", lambda **kw: results.append("b"))
        hook_manager.register("test", lambda **kw: results.append("c"))
        hook_manager.emit("test")
        assert len(results) == 3

    def test_exception_isolation(self, hook_manager):
        """测试异常隔离 — 一个 handler 失败不影响其他。"""
        results = []
        def bad_handler(**kw):
            raise ValueError("boom!")
        hook_manager.register("test", bad_handler)
        hook_manager.register("test", lambda **kw: results.append("ok"))
        hook_manager.emit("test")  # 不应抛异常
        assert results == ["ok"]

    def test_unregister(self, hook_manager):
        """测试取消注册。"""
        results = []
        handler = lambda **kw: results.append("called")
        hook_manager.register("test", handler)
        assert hook_manager.unregister("test", handler) is True
        hook_manager.emit("test")
        assert results == []

    def test_unregister_nonexistent(self, hook_manager):
        """测试取消不存在的 handler。"""
        assert hook_manager.unregister("test", lambda: None) is False

    def test_global_handler(self, hook_manager):
        """测试全局监听器。"""
        events = []
        hook_manager.on_all(lambda event, **kw: events.append(event))
        hook_manager.emit("event_a")
        hook_manager.emit("event_b")
        assert events == ["event_a", "event_b"]

    def test_has_handlers(self, hook_manager):
        """测试 has_handlers。"""
        assert hook_manager.has_handlers("nonexistent") is False
        hook_manager.register("test", lambda **kw: None)
        assert hook_manager.has_handlers("test") is True

    def test_list_events(self, hook_manager):
        """测试列出事件。"""
        hook_manager.register("event_a", lambda **kw: None)
        hook_manager.register("event_b", lambda **kw: None)
        events = hook_manager.list_events()
        assert "event_a" in events
        assert "event_b" in events

    def test_list_handlers(self, hook_manager):
        """测试列出处理器。"""
        def my_handler(**kw): pass
        hook_manager.register("test", my_handler)
        handlers = hook_manager.list_handlers("test")
        assert "my_handler" in handlers

    def test_emit_count(self, hook_manager):
        """测试触发计数。"""
        hook_manager.register("test", lambda **kw: None)
        hook_manager.emit("test")
        hook_manager.emit("test")
        stats = hook_manager.get_stats()
        assert stats["test"] == 2

    def test_clear_specific_event(self, hook_manager):
        """测试清除特定事件。"""
        hook_manager.register("a", lambda **kw: None)
        hook_manager.register("b", lambda **kw: None)
        hook_manager.clear("a")
        assert hook_manager.has_handlers("a") is False
        assert hook_manager.has_handlers("b") is True

    def test_clear_all(self, hook_manager):
        """测试清除所有。"""
        hook_manager.register("a", lambda **kw: None)
        hook_manager.on_all(lambda **kw: None)
        hook_manager.clear()
        assert hook_manager.list_events() == []

    def test_emit_with_results(self, hook_manager):
        """测试收集返回值。"""
        hook_manager.register("test", lambda **kw: "result_1")
        hook_manager.register("test", lambda **kw: "result_2")
        results = hook_manager.emit_with_results("test")
        assert "result_1" in results
        assert "result_2" in results


class TestHookEvent:
    """HookEvent 常量测试。"""

    def test_event_constants_exist(self):
        """测试预定义事件常量。"""
        assert HookEvent.ON_AGENT_INIT == "on_agent_init"
        assert HookEvent.BEFORE_LLM_CALL == "before_llm_call"
        assert HookEvent.BEFORE_TOOL_EXECUTE == "before_tool_execute"
        assert HookEvent.ON_TASK_CREATE == "on_task_create"
        assert HookEvent.ON_SESSION_START == "on_session_start"
