"""Spirit Agent tool_executor.py 工具执行引擎回归测试（v0.2.4 已交付未测）。

参考 hermes:
    - hermes tool_executor.py (1802 行) 的顺序 + 并发执行

工具调用解析 + 单工具执行 + 顺序/并发执行 + 消息构造 + API 消息准备。
"""

from __future__ import annotations

import json
import os
import time

import pytest

from spirit.agent.tool_executor import (
    _DEFAULT_CONCURRENT_TOOL_TIMEOUT_S,
    _DEFAULT_OVERALL_SOFT_WARN_S,
    _DEFAULT_SINGLE_TOOL_SOFT_WARN_S,
    _DEFAULT_SINGLE_TOOL_TIMEOUT_S,
    _MAX_TOOL_WORKERS,
    _detect_tool_failure,
    _emit_hook,
    _resolve_timeout,
    _run_tool_safe,
    execute_single_tool,
    execute_tool_calls_concurrent,
    execute_tool_calls_sequential,
    make_tool_result_message,
    parse_tool_arguments,
    prepare_api_messages,
    serialize_tool_call,
)


# ─────────────────────────── 常量 ───────────────────────────


class TestConstants:
    """5 个 timeout 默认值常量。"""

    def test_max_tool_workers(self):
        assert _MAX_TOOL_WORKERS == 8

    def test_concurrent_timeout(self):
        assert _DEFAULT_CONCURRENT_TOOL_TIMEOUT_S == 300.0

    def test_single_tool_timeout(self):
        assert _DEFAULT_SINGLE_TOOL_TIMEOUT_S == 60.0

    def test_single_tool_soft_warn(self):
        assert _DEFAULT_SINGLE_TOOL_SOFT_WARN_S == 30.0

    def test_overall_soft_warn(self):
        assert _DEFAULT_OVERALL_SOFT_WARN_S == 90.0


# ─────────────────────────── parse_tool_arguments ───────────────────────────


class TestParseToolArguments:
    """parse_tool_arguments 解析模型生成的工具参数。"""

    def test_valid_json_dict(self):
        args, err = parse_tool_arguments('{"key": "value"}')
        assert args == {"key": "value"}
        assert err is None

    def test_empty_object(self):
        args, err = parse_tool_arguments('{}')
        assert args == {}
        assert err is None

    def test_nested_json(self):
        args, err = parse_tool_arguments('{"a": {"b": 1}}')
        assert args == {"a": {"b": 1}}
        assert err is None

    def test_invalid_json_returns_error(self):
        args, err = parse_tool_arguments('not json')
        assert args == {}
        assert err is not None
        # err 是 JSON 字符串，含 error 字段
        parsed = json.loads(err)
        assert "error" in parsed

    def test_non_dict_json_returns_error(self):
        """JSON 是 list 而非 dict → 错误。"""
        args, err = parse_tool_arguments('[1, 2, 3]')
        assert args == {}
        assert err is not None
        parsed = json.loads(err)
        assert "JSON 对象" in parsed["message"]

    def test_none_input_returns_error(self):
        args, err = parse_tool_arguments(None)
        assert args == {}
        assert err is not None


# ─────────────────────────── make_tool_result_message ───────────────────────────


class TestMakeToolResultMessage:
    """make_tool_result_message 构造 tool 角色消息。"""

    def test_basic_message(self):
        msg = make_tool_result_message("search", "result text", "tc-1")
        assert msg["role"] == "tool"
        assert msg["content"] == "result text"
        assert msg["tool_call_id"] == "tc-1"
        assert msg["name"] == "search"

    def test_with_classified_failure_injects_metadata(self):
        """失败时 classified 元数据注入 content。"""
        from spirit.agent.tool_result_classification import (
            ClassifiedToolResult,
            ToolResultCategory,
        )

        classified = ClassifiedToolResult(
            category=ToolResultCategory.RETRYABLE,
            tool_name="search",
            original_result="Error: timeout",
            retryable=True,
            guidance="retry with shorter timeout",
        )
        msg = make_tool_result_message(
            "search", "Error: timeout", "tc-1", classified=classified,
        )
        # content 含 _classification
        assert "_classification" in msg["content"]
        assert "_retryable" in msg["content"]
        assert "_guidance" in msg["content"]

    def test_classified_success_no_injection(self):
        """成功时 classified 不污染 content。"""
        from spirit.agent.tool_result_classification import (
            ClassifiedToolResult,
            ToolResultCategory,
        )

        classified = ClassifiedToolResult(
            category=ToolResultCategory.SUCCESS,
            tool_name="search",
            original_result="all good",
            retryable=False,
            guidance="",
        )
        msg = make_tool_result_message(
            "search", "all good", "tc-1", classified=classified,
        )
        # content 是原 result
        assert msg["content"] == "all good"

    def test_with_guardrail_decision(self):
        """guardrail_decision 注入 guidance（仅在 action=warn/halt 时）。"""
        from spirit.agent.tool_guardrails import ToolGuardrailDecision

        decision = ToolGuardrailDecision(
            action="warn",
            code="warn_001",
            message="be careful",
            tool_name="search",
            count=1,
        )
        msg = make_tool_result_message(
            "search", "result", "tc-1", guardrail_decision=decision,
        )
        # content 含 message
        assert "be careful" in msg["content"]

    def test_with_guardrail_decision_allow_no_injection(self):
        """guardrail action=allow 时不注入。"""
        from spirit.agent.tool_guardrails import ToolGuardrailDecision

        decision = ToolGuardrailDecision(
            action="allow",
            code="allow",
            message="",
            tool_name="search",
            count=0,
        )
        msg = make_tool_result_message(
            "search", "result", "tc-1", guardrail_decision=decision,
        )
        # content 是原 result（无 guidance 注入）
        assert msg["content"] == "result"


# ─────────────────────────── _detect_tool_failure ───────────────────────────


class TestDetectToolFailure:
    """_detect_tool_failure 简单失败检测。"""

    def test_empty_result_not_failure(self):
        assert _detect_tool_failure("search", "") is False

    def test_none_result_not_failure(self):
        assert _detect_tool_failure("search", None) is False  # type: ignore

    def test_terminal_nonzero_exit_code(self):
        result = json.dumps({"exit_code": 1, "stdout": "err"})
        assert _detect_tool_failure("terminal", result) is True

    def test_terminal_zero_exit_code_not_failure(self):
        result = json.dumps({"exit_code": 0, "stdout": "ok"})
        assert _detect_tool_failure("terminal", result) is False

    def test_terminal_no_exit_code(self):
        """terminal 但无 exit_code 字段 → 走通用检查。"""
        result = json.dumps({"stdout": "normal"})
        assert _detect_tool_failure("terminal", result) is False

    def test_terminal_invalid_json_not_failure(self):
        """terminal JSON 解析失败 → 不视为失败。"""
        assert _detect_tool_failure("terminal", "not json") is False

    def test_generic_tool_with_error_keyword(self):
        assert _detect_tool_failure("search", '{"error": "something"}') is True

    def test_generic_tool_with_failed_keyword(self):
        assert _detect_tool_failure("search", '{"failed": "true"}') is True

    def test_generic_tool_starts_with_error(self):
        assert _detect_tool_failure("search", "Error: foo") is True

    def test_generic_tool_no_error(self):
        assert _detect_tool_failure("search", "all good") is False


# ─────────────────────────── _emit_hook ───────────────────────────


class TestEmitHook:
    """_emit_hook 钩子事件触发。"""

    def test_no_hook_manager_no_error(self):
        class Agent:
            pass

        agent = Agent()
        # 不抛错
        _emit_hook(agent, "test_event", foo="bar")

    def test_hook_manager_called(self):
        captured = []

        class HookManager:
            def emit(self, event, **kwargs):
                captured.append((event, kwargs))

        class Agent:
            hook_manager = HookManager()

        agent = Agent()
        _emit_hook(agent, "test_event", foo="bar")
        assert captured == [("test_event", {"foo": "bar"})]


# ─────────────────────────── Mock Agent ───────────────────────────


class MockAgent:
    """用于 tool_executor 测试的最小 mock SpiritAgent。"""

    def __init__(self, invoke_behavior=None):
        self._invoke_behavior = invoke_behavior or (lambda n, a: f"result for {n}")
        self._interrupt_requested = False
        self.hook_manager = None
        self.invoke_calls = []

    def invoke_tool(self, tool_name, args):
        self.invoke_calls.append((tool_name, args))
        return self._invoke_behavior(tool_name, args)


def make_tool_call(tool_name="search", args='{"q": "x"}', tc_id="tc-1"):
    """构造 OpenAI 风格 tool_call。"""
    return {
        "id": tc_id,
        "type": "function",
        "function": {
            "name": tool_name,
            "arguments": args,
        },
    }


# ─────────────────────────── execute_single_tool ───────────────────────────


class TestExecuteSingleTool:
    """execute_single_tool 单工具执行。"""

    def test_basic_invocation(self):
        agent = MockAgent()
        tc = make_tool_call()
        result, name, decision = execute_single_tool(agent, tool_call=tc)
        assert name == "search"
        assert result == "result for search"
        assert decision is None
        # 验证 invoke_tool 被调用
        assert len(agent.invoke_calls) == 1
        assert agent.invoke_calls[0][0] == "search"

    def test_invalid_arguments_returns_error(self):
        agent = MockAgent()
        tc = make_tool_call(args="not json")
        result, name, decision = execute_single_tool(agent, tool_call=tc)
        # 返回 error JSON
        parsed = json.loads(result)
        assert "error" in parsed
        # 不调用 invoke_tool
        assert agent.invoke_calls == []

    def test_invoke_exception_returns_error(self):
        def boom(n, a):
            raise RuntimeError("tool boom")

        agent = MockAgent(invoke_behavior=boom)
        tc = make_tool_call()
        result, name, decision = execute_single_tool(agent, tool_call=tc)
        parsed = json.loads(result)
        assert "工具执行异常" in parsed["error"]
        assert "RuntimeError" in parsed["error"]

    def test_interrupt_requested_returns_cancelled(self):
        agent = MockAgent()
        agent._interrupt_requested = True
        tc = make_tool_call()
        result, name, decision = execute_single_tool(agent, tool_call=tc)
        parsed = json.loads(result)
        assert parsed["status"] == "cancelled"
        # 不调用 invoke_tool
        assert agent.invoke_calls == []

    def test_guardrail_before_call_blocks(self):
        from spirit.agent.tool_guardrails import ToolGuardrailDecision

        class FakeGuardrails:
            def before_call(self, tool_name, args):
                return ToolGuardrailDecision(
                    action="block",
                    code="block_001",
                    message="blocked by guardrail",
                    tool_name=tool_name,
                    count=1,
                )

            def after_call(self, tool_name, args, result, failed=False):
                return None

        agent = MockAgent()
        tc = make_tool_call()
        result, name, decision = execute_single_tool(
            agent, tool_call=tc, guardrails=FakeGuardrails(),
        )
        # 返回 synthetic result
        assert "blocked" in result or "guardrail" in result.lower()
        # invoke_tool 未被调用
        assert agent.invoke_calls == []

    def test_guardrail_after_call(self):
        """after_call 被调用，failed 参数正确。"""
        from spirit.agent.tool_guardrails import ToolGuardrailDecision

        captured = []

        class FakeGuardrails:
            def before_call(self, tool_name, args):
                return ToolGuardrailDecision(
                    action="allow",
                    code="allow",
                    message="",
                    tool_name=tool_name,
                    count=0,
                )

            def after_call(self, tool_name, args, result, failed=False):
                captured.append((tool_name, result, failed))
                return ToolGuardrailDecision(
                    action="allow",
                    code="allow",
                    message="",
                    tool_name=tool_name,
                    count=0,
                )

        agent = MockAgent()
        tc = make_tool_call()
        execute_single_tool(agent, tool_call=tc, guardrails=FakeGuardrails())
        # after_call 被调用，failed=False（结果正常）
        assert len(captured) == 1
        assert captured[0][0] == "search"
        assert captured[0][2] is False


# ─────────────────────────── execute_tool_calls_sequential ───────────────────────────


class TestExecuteSequential:
    """execute_tool_calls_sequential 顺序执行。"""

    def test_empty_calls(self):
        agent = MockAgent()
        messages = []
        execute_tool_calls_sequential(agent, [], messages)
        assert messages == []

    def test_single_call_appends_message(self):
        agent = MockAgent()
        messages = []
        tc = make_tool_call()
        execute_tool_calls_sequential(agent, [tc], messages)
        assert len(messages) == 1
        assert messages[0]["role"] == "tool"
        assert messages[0]["name"] == "search"
        assert messages[0]["tool_call_id"] == "tc-1"

    def test_multiple_calls_in_order(self):
        agent = MockAgent()
        messages = []
        calls = [
            make_tool_call(tool_name="a", tc_id="tc-1"),
            make_tool_call(tool_name="b", tc_id="tc-2"),
            make_tool_call(tool_name="c", tc_id="tc-3"),
        ]
        execute_tool_calls_sequential(agent, calls, messages)
        # 顺序一致
        assert [m["name"] for m in messages] == ["a", "b", "c"]
        assert [m["tool_call_id"] for m in messages] == ["tc-1", "tc-2", "tc-3"]

    def test_interrupt_appends_cancelled(self):
        agent = MockAgent()
        agent._interrupt_requested = True
        messages = []
        calls = [
            make_tool_call(tc_id="tc-1"),
            make_tool_call(tc_id="tc-2"),
        ]
        execute_tool_calls_sequential(agent, calls, messages)
        # 每条都标注 cancelled
        for m in messages:
            parsed = json.loads(m["content"])
            assert parsed["status"] == "cancelled"


# ─────────────────────────── execute_tool_calls_concurrent ───────────────────────────


class TestExecuteConcurrent:
    """execute_tool_calls_concurrent 并发执行。"""

    def test_empty_calls(self):
        agent = MockAgent()
        messages = []
        execute_tool_calls_concurrent(agent, [], messages)
        assert messages == []

    def test_single_call(self):
        agent = MockAgent()
        messages = []
        tc = make_tool_call()
        execute_tool_calls_concurrent(agent, [tc], messages, timeout_s=10.0)
        assert len(messages) == 1
        assert messages[0]["role"] == "tool"

    def test_multiple_calls_in_order(self):
        """结果按原始 tc 顺序追加（不依赖完成顺序）。"""
        agent = MockAgent(invoke_behavior=lambda n, a: f"result-{n}")
        messages = []
        calls = [
            make_tool_call(tool_name="a", tc_id="tc-1"),
            make_tool_call(tool_name="b", tc_id="tc-2"),
            make_tool_call(tool_name="c", tc_id="tc-3"),
        ]
        execute_tool_calls_concurrent(
            agent, calls, messages, timeout_s=10.0,
        )
        # 顺序按 tc_id 排列
        assert [m["tool_call_id"] for m in messages] == ["tc-1", "tc-2", "tc-3"]

    def test_interrupt_pretags_all(self):
        agent = MockAgent()
        agent._interrupt_requested = True
        messages = []
        calls = [
            make_tool_call(tc_id="tc-1"),
            make_tool_call(tc_id="tc-2"),
        ]
        execute_tool_calls_concurrent(agent, calls, messages, timeout_s=10.0)
        # 每条都标记 cancelled
        for m in messages:
            parsed = json.loads(m["content"])
            assert parsed["status"] == "cancelled"

    def test_blocked_by_guardrail(self):
        """before_call 阻止 → 用 synthetic result。"""
        from spirit.agent.tool_guardrails import ToolGuardrailDecision

        class FakeGuardrails:
            def before_call(self, tool_name, args):
                return ToolGuardrailDecision(
                    action="block",
                    code="block_001",
                    message="blocked",
                    tool_name=tool_name,
                    count=1,
                )

            def after_call(self, tool_name, args, result, failed=False):
                return None

        agent = MockAgent()
        messages = []
        tc = make_tool_call()
        execute_tool_calls_concurrent(
            agent, [tc], messages,
            guardrails=FakeGuardrails(), timeout_s=10.0,
        )
        # 不调用 invoke_tool
        assert agent.invoke_calls == []
        # 但 message 被追加
        assert len(messages) == 1


# ─────────────────────────── _run_tool_safe ───────────────────────────


class TestRunToolSafe:
    """_run_tool_safe 线程安全包装。"""

    def test_successful_invoke(self):
        agent = MockAgent()
        result = _run_tool_safe(agent, "search", {"q": "x"})
        assert result == "result for search"

    def test_exception_returns_error_json(self):
        def boom(n, a):
            raise RuntimeError("tool boom")

        agent = MockAgent(invoke_behavior=boom)
        result = _run_tool_safe(agent, "search", {})
        parsed = json.loads(result)
        assert "工具执行异常" in parsed["error"]
        assert "RuntimeError" in parsed["error"]


# ─────────────────────────── _resolve_timeout ───────────────────────────


class TestResolveTimeout:
    """_resolve_timeout 解析超时（环境变量优先）。"""

    def test_default_value(self, monkeypatch):
        monkeypatch.delenv("SPIRIT_CONCURRENT_TOOL_TIMEOUT_S", raising=False)
        result = _resolve_timeout(100.0)
        assert result == 100.0

    def test_zero_arg_value_passes_through(self, monkeypatch):
        """spirit 现状：参数路径不走 ``<= 0`` 检查（只有 env 路径才检查）。

        ``_resolve_timeout(0)`` 直接 return 0（不是 None）。
        只有 env 变量 <= 0 才返回 None。
        """
        monkeypatch.delenv("SPIRIT_CONCURRENT_TOOL_TIMEOUT_S", raising=False)
        # spirit 现状：arg=0 → return 0
        assert _resolve_timeout(0) == 0
        assert _resolve_timeout(-1.0) == -1.0

    def test_env_variable_overrides(self, monkeypatch):
        monkeypatch.setenv("SPIRIT_CONCURRENT_TOOL_TIMEOUT_S", "60.0")
        result = _resolve_timeout(100.0)
        assert result == 60.0

    def test_env_zero_or_negative_returns_none(self, monkeypatch):
        monkeypatch.setenv("SPIRIT_CONCURRENT_TOOL_TIMEOUT_S", "0")
        assert _resolve_timeout(100.0) is None
        monkeypatch.setenv("SPIRIT_CONCURRENT_TOOL_TIMEOUT_S", "-5")
        assert _resolve_timeout(100.0) is None

    def test_env_invalid_falls_back_to_arg(self, monkeypatch):
        monkeypatch.setenv("SPIRIT_CONCURRENT_TOOL_TIMEOUT_S", "not a number")
        result = _resolve_timeout(100.0)
        # env 解析失败 → 用 arg
        assert result == 100.0

    def test_env_empty_string_falls_back_to_arg(self, monkeypatch):
        monkeypatch.setenv("SPIRIT_CONCURRENT_TOOL_TIMEOUT_S", "   ")
        result = _resolve_timeout(100.0)
        # spirit 用 strip()，空字符串走默认路径
        # spirit line 542: env_val.strip() 是 "" → falsy → 用 arg
        assert result == 100.0


# ─────────────────────────── prepare_api_messages ───────────────────────────


class TestPrepareApiMessages:
    """prepare_api_messages 内部 → OpenAI API 格式。"""

    def test_user_message(self):
        msgs = [{"role": "user", "content": "hi"}]
        api_msgs = prepare_api_messages(msgs)
        assert api_msgs == [{"role": "user", "content": "hi"}]

    def test_assistant_with_tool_calls(self):
        msgs = [
            {
                "role": "assistant",
                "content": "thinking",
                "tool_calls": [
                    {"id": "tc-1", "type": "function", "function": {"name": "search"}},
                ],
            },
        ]
        api_msgs = prepare_api_messages(msgs)
        assert api_msgs[0]["role"] == "assistant"
        assert api_msgs[0]["tool_calls"] is not None
        assert api_msgs[0]["content"] == "thinking"

    def test_assistant_empty_content(self):
        """assistant 无 content → content = None（OpenAI 要求）。"""
        msgs = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "tc-1", "type": "function"}],
            },
        ]
        api_msgs = prepare_api_messages(msgs)
        assert api_msgs[0]["content"] is None

    def test_tool_message_includes_call_id(self):
        msgs = [{"role": "tool", "content": "result", "tool_call_id": "tc-1"}]
        api_msgs = prepare_api_messages(msgs)
        assert api_msgs[0]["role"] == "tool"
        assert api_msgs[0]["content"] == "result"
        assert api_msgs[0]["tool_call_id"] == "tc-1"

    def test_only_copies_basic_fields(self):
        """spirit 现状：prepare_api_messages 只复制 role/content/tool_calls/tool_call_id，
        其他字段（包括 _provider）不复制。
        这是设计分工：provider 元数据剥离由 provider_switch_protection 接管，
        prepare_api_messages 只做格式转换。
        """
        msgs = [{"role": "user", "content": "hi", "_provider": "openai"}]
        api_msgs = prepare_api_messages(msgs)
        # _provider 不在新 dict
        assert "_provider" not in api_msgs[0]
        # 但基础字段保留
        assert api_msgs[0]["role"] == "user"
        assert api_msgs[0]["content"] == "hi"

    def test_empty_messages(self):
        assert prepare_api_messages([]) == []


# ─────────────────────────── serialize_tool_call ───────────────────────────


class TestSerializeToolCall:
    """serialize_tool_call OpenAI SDK tool_call → dict。"""

    def test_basic_serialization(self):
        class MockFunction:
            name = "search"
            arguments = '{"q": "x"}'

        class MockToolCall:
            id = "tc-1"
            function = MockFunction()

        tc = MockToolCall()
        result = serialize_tool_call(tc)
        assert result == {
            "id": "tc-1",
            "type": "function",
            "function": {"name": "search", "arguments": '{"q": "x"}'},
        }


# ─────────────────────────── 端到端集成 ───────────────────────────


class TestEndToEndIntegration:
    """完整工具执行流。"""

    def test_realistic_search_flow(self):
        """真实场景：search → read_file。"""
        agent = MockAgent(invoke_behavior=lambda n, a: f"{n} result")
        messages = []
        calls = [
            make_tool_call(tool_name="search", tc_id="tc-1", args='{"q": "python"}'),
            make_tool_call(tool_name="read_file", tc_id="tc-2", args='{"path": "x.py"}'),
        ]
        execute_tool_calls_sequential(agent, calls, messages)
        # 两条消息
        assert len(messages) == 2
        assert messages[0]["name"] == "search"
        assert messages[1]["name"] == "read_file"
        assert "search result" in messages[0]["content"]
        assert "read_file result" in messages[1]["content"]

    def test_concurrent_search_flow(self):
        agent = MockAgent(invoke_behavior=lambda n, a: f"{n} result")
        messages = []
        calls = [
            make_tool_call(tool_name="a", tc_id="tc-1"),
            make_tool_call(tool_name="b", tc_id="tc-2"),
            make_tool_call(tool_name="c", tc_id="tc-3"),
        ]
        execute_tool_calls_concurrent(
            agent, calls, messages, timeout_s=10.0,
        )
        # 3 条消息，按 tc_id 顺序
        assert [m["tool_call_id"] for m in messages] == ["tc-1", "tc-2", "tc-3"]
        assert [m["name"] for m in messages] == ["a", "b", "c"]

    def test_classified_failure_round_trip(self):
        """execute_single_tool + classify_tool_result + make_tool_result_message 集成。"""
        from spirit.agent.tool_result_classification import classify_tool_result

        def fail(n, a):
            return json.dumps({"error": "network timeout", "status": "timeout"})

        agent = MockAgent(invoke_behavior=fail)
        tc = make_tool_call()
        result, name, decision = execute_single_tool(agent, tool_call=tc)
        classified = classify_tool_result(name, result)
        msg = make_tool_result_message(
            name, result, "tc-1", classified=classified,
        )
        # msg 应含分类 metadata
        assert "_classification" in msg["content"]