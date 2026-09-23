"""钩子接线回归测试 — 验证 emit 触发点与 handler 契约真正连通。

背景：钩子系统曾长期处于「只注册、不触发」状态（conversation/tool/lifecycle
事件无任何 emit 调用点，memora_auto_save 未接入启动链）。本测试锁定接线成果：

1. execute_single_tool 触发 before/after_tool_execute（含 memora 消费的 args/result 别名）
2. 工具异常触发 on_tool_error 且不影响结果返回
3. 并发路径 execute_tool_calls_concurrent 同样触发成对事件
4. conversation_loop._emit_hook 在无 hook_manager 时静默跳过
5. agent_init._init_hooks 注册了 memora 自动保存钩子
6. 钩子失败不影响工具执行主流程（异常隔离）
"""

import json
import pytest
from unittest.mock import MagicMock

from spirit.agent.conversation_loop import _emit_hook as loop_emit_hook
from spirit.agent.tool_executor import (
    execute_single_tool,
    execute_tool_calls_concurrent,
)
from spirit.hooks.hook_manager import HookManager, HookEvent
from spirit.hooks.memora_auto_save import on_tool_complete


# ---------------------------------------------------------------------------
# 测试脚手架
# ---------------------------------------------------------------------------

class RecordingAgent:
    """最小假 Agent：仅提供 tool_executor 所需的属性。"""

    def __init__(self, hook_manager=None, invoke_result="ok", invoke_raises=None):
        self.hook_manager = hook_manager
        self._interrupt_requested = False
        self._invoke_result = invoke_result
        self._invoke_raises = invoke_raises
        self.invoke_calls = []

    def invoke_tool(self, tool_name, args):
        self.invoke_calls.append((tool_name, args))
        if self._invoke_raises is not None:
            raise self._invoke_raises
        return self._invoke_result


class RecordingHookManager(HookManager):
    """记录所有 emit 的事件与 kwargs。"""

    def __init__(self):
        super().__init__()
        self.events = []  # [(event, kwargs), ...]
        self.on_all(self._record)

    def _record(self, event, **kwargs):
        self.events.append((event, kwargs))

    def events_of(self, name):
        return [kw for ev, kw in self.events if ev == name]


def make_tool_call(name="write_file", args=None, call_id="tc_1"):
    return {
        "id": call_id,
        "function": {"name": name, "arguments": json.dumps(args or {})},
    }


# ---------------------------------------------------------------------------
# 1. 单工具路径：before/after 成对触发 + kwargs 契约
# ---------------------------------------------------------------------------

class TestSingleToolHookWiring:
    def test_before_and_after_emitted(self):
        hm = RecordingHookManager()
        agent = RecordingAgent(hook_manager=hm, invoke_result="done")

        result, tool_name, _ = execute_single_tool(
            agent, tool_call=make_tool_call(args={"path": "/tmp/a.md"}),
        )

        assert result == "done"
        before = hm.events_of(HookEvent.BEFORE_TOOL_EXECUTE)
        after = hm.events_of(HookEvent.AFTER_TOOL_EXECUTE)
        assert len(before) == 1 and len(after) == 1
        assert before[0]["tool_name"] == "write_file"
        assert before[0]["tool_args"] == {"path": "/tmp/a.md"}

    def test_after_kwargs_contract(self):
        """after_tool_execute 必须同时满足两组 handler 的签名：
        tool_hooks（tool_name/tool_args/tool_result/duration_ms）与
        memora_auto_save（tool_name/args/result）。"""
        hm = RecordingHookManager()
        agent = RecordingAgent(hook_manager=hm, invoke_result="saved")

        execute_single_tool(agent, tool_call=make_tool_call())

        kw = hm.events_of(HookEvent.AFTER_TOOL_EXECUTE)[0]
        for key in ("tool_name", "tool_args", "tool_result", "duration_ms", "args", "result"):
            assert key in kw, f"缺少 kwargs 键: {key}"
        assert kw["tool_result"] == "saved"
        assert kw["args"] == kw["tool_args"]
        assert kw["result"] == kw["tool_result"]

    def test_memora_handler_accepts_emitted_kwargs(self):
        """真实 memora handler 能直接消费 emit 的 kwargs（不触发上传：非目标路径）。"""
        hm = RecordingHookManager()
        hm.register(HookEvent.AFTER_TOOL_EXECUTE, on_tool_complete, priority=200)
        agent = RecordingAgent(hook_manager=hm, invoke_result="ok")

        # 非 SAVABLE 扩展名 → handler 快速过滤，不应抛异常
        execute_single_tool(
            agent, tool_call=make_tool_call(args={"path": "/tmp/x.bin"}),
        )
        assert len(hm.events_of(HookEvent.AFTER_TOOL_EXECUTE)) == 1

    def test_tool_error_emitted_on_exception(self):
        hm = RecordingHookManager()
        boom = RuntimeError("boom")
        agent = RecordingAgent(hook_manager=hm, invoke_raises=boom)

        result, _, _ = execute_single_tool(agent, tool_call=make_tool_call())

        errors = hm.events_of(HookEvent.ON_TOOL_ERROR)
        assert len(errors) == 1
        assert errors[0]["error"] is boom
        # 异常被封装进结果，after 事件仍触发
        assert json.loads(result)["status"] == "error"
        assert len(hm.events_of(HookEvent.AFTER_TOOL_EXECUTE)) == 1

    def test_hook_failure_does_not_break_tool(self):
        """handler 抛异常被 HookManager 隔离，工具结果不受影响。"""
        hm = RecordingHookManager()

        def bad_handler(**kwargs):
            raise ValueError("handler exploded")

        hm.register(HookEvent.BEFORE_TOOL_EXECUTE, bad_handler)
        hm.register(HookEvent.AFTER_TOOL_EXECUTE, bad_handler)
        agent = RecordingAgent(hook_manager=hm, invoke_result="still-ok")

        result, _, _ = execute_single_tool(agent, tool_call=make_tool_call())
        assert result == "still-ok"

    def test_no_hook_manager_is_noop(self):
        agent = RecordingAgent(hook_manager=None, invoke_result="ok")
        result, _, _ = execute_single_tool(agent, tool_call=make_tool_call())
        assert result == "ok"


# ---------------------------------------------------------------------------
# 2. 并发路径：成对事件 + 每工具一次
# ---------------------------------------------------------------------------

class TestConcurrentToolHookWiring:
    def test_pairs_emitted_per_tool(self):
        hm = RecordingHookManager()
        agent = RecordingAgent(hook_manager=hm, invoke_result="r")
        calls = [
            make_tool_call(name="read_file", args={"path": "a"}, call_id="c1"),
            make_tool_call(name="read_file", args={"path": "b"}, call_id="c2"),
        ]
        messages = []

        execute_tool_calls_concurrent(agent, calls, messages)

        before = hm.events_of(HookEvent.BEFORE_TOOL_EXECUTE)
        after = hm.events_of(HookEvent.AFTER_TOOL_EXECUTE)
        assert len(before) == 2 and len(after) == 2
        assert {kw["tool_args"]["path"] for kw in before} == {"a", "b"}
        for kw in after:
            assert kw["tool_result"] == "r"
            assert "duration_ms" in kw and "args" in kw and "result" in kw
        assert len(messages) == 2

    def test_exception_wrapped_still_emits_after(self):
        """并发路径异常被 _run_tool_safe 封装，after 事件仍带错误结果触发。"""
        hm = RecordingHookManager()
        agent = RecordingAgent(hook_manager=hm, invoke_raises=RuntimeError("x"))
        messages = []

        execute_tool_calls_concurrent(agent, [make_tool_call()], messages)

        after = hm.events_of(HookEvent.AFTER_TOOL_EXECUTE)
        assert len(after) == 1
        assert json.loads(after[0]["tool_result"])["status"] == "error"


# ---------------------------------------------------------------------------
# 3. conversation_loop 触发助手
# ---------------------------------------------------------------------------

class TestLoopEmitHelper:
    def test_noop_without_hook_manager(self):
        agent = MagicMock(spec=[])  # 无 hook_manager 属性
        loop_emit_hook(agent, "before_conversation", user_message="hi")  # 不应抛异常

    def test_emits_with_kwargs(self):
        hm = RecordingHookManager()
        agent = MagicMock()
        agent.hook_manager = hm

        loop_emit_hook(agent, "after_conversation", response="done", iterations=3)

        ev = hm.events_of("after_conversation")
        assert ev == [{"response": "done", "iterations": 3}]


# ---------------------------------------------------------------------------
# 4. 启动链接线：_init_hooks 注册 memora 自动保存
# ---------------------------------------------------------------------------

class TestInitHooksWiring:
    def test_auto_save_hook_registered(self):
        from spirit.agent.agent_init import _init_hooks

        class _Stub:
            pass

        agent = _Stub()  # 无 hook_manager → _init_hooks 完整装配
        _init_hooks(agent)

        hm = agent.hook_manager
        handlers = hm.list_handlers(HookEvent.AFTER_TOOL_EXECUTE)
        # memora 的 on_tool_complete + tool_hooks 的 _after_tool_execute 都在
        assert "on_tool_complete" in handlers
        assert len(handlers) >= 2
        # 生命周期/对话钩子也已注册
        assert hm.has_handlers(HookEvent.ON_SESSION_END)
        assert hm.has_handlers(HookEvent.BEFORE_LLM_CALL)

    def test_init_hooks_skips_when_already_wired(self):
        """SpiritAgent.__init__ 已自装配时，_init_hooks 不重复创建。"""
        from spirit.agent.agent_init import _init_hooks

        class _Stub:
            pass

        agent = _Stub()
        existing = HookManager()
        agent.hook_manager = existing
        _init_hooks(agent)
        assert agent.hook_manager is existing
