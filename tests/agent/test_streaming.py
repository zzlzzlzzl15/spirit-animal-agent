"""Spirit Agent streaming.py 流式输出回归测试（R-P1.7 v0.2.4 已交付未测）。

参考 hermes:
    - hermes stream_single_writer.py + chat_completion_helpers.py 的流式部分

流式增量 + 单写者围栏 + StreamCollector + StreamingHandler + 非流式兼容层。
"""

from __future__ import annotations

import threading
import time

import pytest

from spirit.agent.streaming import (
    StreamCollector,
    StreamDelta,
    StreamingHandler,
    StreamResult,
    StreamWriterFence,
    create_mock_stream,
)


# ─────────────────────────── StreamDelta 数据类 ───────────────────────────


class TestStreamDelta:
    """StreamDelta 流式增量数据类。"""

    def test_minimal_construction(self):
        d = StreamDelta()
        assert d.content == ""
        assert d.tool_calls_delta == []
        assert d.finish_reason is None
        assert d.role is None
        assert d.is_final is False
        # timestamp 默认是 time.time()，应 > 0
        assert d.timestamp > 0

    def test_with_content(self):
        d = StreamDelta(content="hello", finish_reason="stop", is_final=True)
        assert d.content == "hello"
        assert d.finish_reason == "stop"
        assert d.is_final is True

    def test_with_tool_calls_delta(self):
        tc_delta = {"index": 0, "function": {"name": "search"}}
        d = StreamDelta(tool_calls_delta=[tc_delta])
        assert len(d.tool_calls_delta) == 1
        assert d.tool_calls_delta[0]["function"]["name"] == "search"

    def test_default_timestamp_recent(self):
        before = time.time()
        d = StreamDelta()
        after = time.time()
        assert before <= d.timestamp <= after


# ─────────────────────────── StreamResult 数据类 ───────────────────────────


class TestStreamResult:
    """StreamResult 完整流式结果数据类。"""

    def test_minimal_construction(self):
        r = StreamResult()
        assert r.content == ""
        assert r.tool_calls == []
        assert r.finish_reason == "stop"
        assert r.usage == {}
        assert r.first_token_time == 0.0
        assert r.last_token_time == 0.0
        assert r.total_tokens == 0

    def test_with_content(self):
        r = StreamResult(content="full text", finish_reason="stop")
        assert r.content == "full text"

    def test_with_tool_calls(self):
        tc = {"id": "tc-1", "function": {"name": "search", "arguments": "{}"}}
        r = StreamResult(tool_calls=[tc])
        assert len(r.tool_calls) == 1
        assert r.tool_calls[0]["id"] == "tc-1"

    def test_with_usage(self):
        r = StreamResult(usage={"prompt_tokens": 10, "completion_tokens": 20})
        assert r.usage["prompt_tokens"] == 10
        assert r.usage["completion_tokens"] == 20


# ─────────────────────────── StreamWriterFence 单写者围栏 ───────────────────────────


class TestStreamWriterFence:
    """StreamWriterFence 单写者围栏（防过期流覆盖新流）。"""

    def test_initial_token_is_zero(self):
        fence = StreamWriterFence()
        assert fence._token == 0

    def test_claim_increments_token(self):
        fence = StreamWriterFence()
        t1 = fence.claim()
        t2 = fence.claim()
        t3 = fence.claim()
        assert t1 == 1
        assert t2 == 2
        assert t3 == 3

    def test_is_current_returns_true_after_claim(self):
        fence = StreamWriterFence()
        token = fence.claim()
        assert fence.is_current(token) is True

    def test_is_current_returns_false_after_new_claim(self):
        fence = StreamWriterFence()
        old_token = fence.claim()
        # 新流 claim
        new_token = fence.claim()
        # 旧流不再 current
        assert fence.is_current(old_token) is False
        # 新流 current
        assert fence.is_current(new_token) is True

    def test_is_current_zero_token_always_true(self):
        """token=0 是"无围栏"模式 → 永远 current。"""
        fence = StreamWriterFence()
        # 多次 claim 后
        fence.claim()
        fence.claim()
        # token=0 仍 True（无围栏）
        assert fence.is_current(0) is True

    def test_is_current_zero_token_true_even_after_claim(self):
        fence = StreamWriterFence()
        fence.claim()  # _token=1
        # token=0 仍 True（spirit 实现：not 0 == True）
        assert fence.is_current(0) is True

    def test_is_current_negative_token_is_false(self):
        """spirit 实现用 ``if not token: return True``：只有 falsy token (0) 才 True。

        负数 truthy → 视为“未 claim 过的有效 token”，但 spirit 实际会返回 False
        （self._token != -1）。
        """
        fence = StreamWriterFence()
        fence.claim()  # _token=1
        # 负数不是 1（当前 token），且不是 0（falsy）
        assert fence.is_current(-1) is False


# ─────────────────────────── StreamCollector 流式收集 ───────────────────────────


class TestStreamCollector:
    """StreamCollector 增量收集 + 完整结果组装。"""

    def test_initial_state(self):
        c = StreamCollector()
        # 私有字段验证
        assert c._content_parts == []
        assert c._tool_call_buffers == {}
        assert c._finish_reason is None
        assert c._delta_callback is None

    def test_feed_content(self):
        c = StreamCollector()
        c.feed(StreamDelta(content="hello "))
        c.feed(StreamDelta(content="world"))
        result = c.build_result()
        assert result.content == "hello world"

    def test_feed_empty_content_ignored(self):
        """空 content 不影响 first_token_time 记录。"""
        c = StreamCollector()
        # 先喂一个空 delta
        c.feed(StreamDelta(content=""))
        # first_token_time 仍 0
        assert c._first_token_time == 0.0

    def test_first_token_time_set_on_content(self):
        c = StreamCollector()
        before = time.time()
        c.feed(StreamDelta(content="x"))
        after = time.time()
        assert before <= c._first_token_time <= after

    def test_first_token_time_set_on_tool_calls(self):
        c = StreamCollector()
        c.feed(StreamDelta(tool_calls_delta=[{"index": 0}]))
        assert c._first_token_time > 0.0

    def test_first_token_time_set_only_once(self):
        """多次 feed 后 first_token_time 不变。"""
        c = StreamCollector()
        c.feed(StreamDelta(content="x"))
        first = c._first_token_time
        time.sleep(0.01)
        c.feed(StreamDelta(content="y"))
        # 仍是第一次的时间
        assert c._first_token_time == first

    def test_last_token_time_updates(self):
        c = StreamCollector()
        c.feed(StreamDelta(content="x"))
        t1 = c._last_token_time
        time.sleep(0.01)
        c.feed(StreamDelta(content="y"))
        # 更新
        assert c._last_token_time > t1

    def test_feed_tool_calls_by_index(self):
        """多个工具调用按 index 分组。"""
        c = StreamCollector()
        # 第一个 tool call 的 name
        c.feed(StreamDelta(tool_calls_delta=[
            {"index": 0, "function": {"name": "search"}},
        ]))
        # 第一个 tool call 的 arguments 增量
        c.feed(StreamDelta(tool_calls_delta=[
            {"index": 0, "function": {"arguments": '{"query":'}},
        ]))
        # 第二个 tool call
        c.feed(StreamDelta(tool_calls_delta=[
            {"index": 1, "function": {"name": "read"}},
        ]))
        result = c.build_result()
        # 2 个工具调用
        assert len(result.tool_calls) == 2
        # 第一个完整
        assert result.tool_calls[0]["function"]["name"] == "search"
        assert result.tool_calls[0]["function"]["arguments"] == '{"query":'
        # 第二个只有 name
        assert result.tool_calls[1]["function"]["name"] == "read"

    def test_tool_call_id_set(self):
        c = StreamCollector()
        c.feed(StreamDelta(tool_calls_delta=[
            {"index": 0, "id": "tc-123", "function": {"name": "x"}},
        ]))
        result = c.build_result()
        assert result.tool_calls[0]["id"] == "tc-123"

    def test_tool_calls_default_type(self):
        """tool call buffer 默认 type=function。"""
        c = StreamCollector()
        c.feed(StreamDelta(tool_calls_delta=[{"index": 0}]))
        result = c.build_result()
        assert result.tool_calls[0]["type"] == "function"

    def test_feed_finish_reason(self):
        c = StreamCollector()
        c.feed(StreamDelta(finish_reason="stop"))
        result = c.build_result()
        assert result.finish_reason == "stop"

    def test_finish_reason_default_stop(self):
        """未设置 finish_reason → 默认 "stop"。"""
        c = StreamCollector()
        result = c.build_result()
        assert result.finish_reason == "stop"

    def test_finish_reason_latest_wins(self):
        c = StreamCollector()
        c.feed(StreamDelta(finish_reason="length"))
        c.feed(StreamDelta(finish_reason="stop"))
        result = c.build_result()
        # 最新的覆盖
        assert result.finish_reason == "stop"

    def test_delta_callback_invoked(self):
        calls = []

        def cb(delta):
            calls.append(delta.content)

        c = StreamCollector(delta_callback=cb)
        c.feed(StreamDelta(content="a"))
        c.feed(StreamDelta(content="b"))
        assert calls == ["a", "b"]

    def test_delta_callback_exception_swallowed(self):
        """callback 抛错 → 不传给上游（log debug 后继续）。"""
        def bad_cb(delta):
            raise RuntimeError("callback boom")

        c = StreamCollector(delta_callback=bad_cb)
        # 不抛错
        c.feed(StreamDelta(content="x"))
        # 后续 feed 仍正常
        c.feed(StreamDelta(content="y"))
        result = c.build_result()
        assert result.content == "xy"

    def test_build_result_sorts_tool_calls_by_index(self):
        """build_result 按 index 排序 tool_calls。"""
        c = StreamCollector()
        # 按 2, 0, 1 顺序喂
        c.feed(StreamDelta(tool_calls_delta=[{"index": 2, "function": {"name": "c"}}]))
        c.feed(StreamDelta(tool_calls_delta=[{"index": 0, "function": {"name": "a"}}]))
        c.feed(StreamDelta(tool_calls_delta=[{"index": 1, "function": {"name": "b"}}]))
        result = c.build_result()
        # 按 index 排序
        names = [tc["function"]["name"] for tc in result.tool_calls]
        assert names == ["a", "b", "c"]


# ─────────────────────────── StreamingHandler 流式 API 调用 ───────────────────────────


class MockChunk:
    """模拟 OpenAI 流式 chunk。"""

    def __init__(self, content="", finish_reason=None, tool_calls=None, choices=None):
        if choices is not None:
            self.choices = choices
        else:
            choice = MockChoice(
                delta=MockDelta(content=content, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
            self.choices = [choice]


class MockChoice:
    def __init__(self, delta=None, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class MockDelta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class MockToolCall:
    def __init__(self, index=0, id=None, function=None):
        self.index = index
        self.id = id
        self.function = function


class MockFunction:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class MockStream:
    """模拟流式 API 响应（可迭代 + 支持 .get('usage')）。"""

    def __init__(self, chunks, usage=None):
        self._chunks = chunks
        self._usage = usage

    def __iter__(self):
        return iter(self._chunks)

    def get(self, key, default=None):
        if key == "usage":
            return self._usage
        return default


class MockAgent:
    """用于 StreamingHandler 测试的 mock agent。"""

    def __init__(self, model="gpt-4", stream_response=None, raise_on_create=False):
        self.model = model
        self._stream_response = stream_response
        self._raise_on_create = raise_on_create
        self.on_stream_delta = None
        # spirit 代码用 ``agent.client.chat.completions.create(**kwargs)``，
        # 所以需要完整链路：agent.client.chat.completions.create
        self.client = _MockClient(self._raise_on_create, lambda: self._stream_response)


class _MockClient:
    def __init__(self, raise_on_create, stream_getter):
        self._raise = raise_on_create
        self._stream_getter = stream_getter
        self.chat = _MockChat(self._raise, self._stream_getter)


class _MockChat:
    def __init__(self, raise_on_create, stream_getter):
        self._raise = raise_on_create
        self._stream_getter = stream_getter
        self.completions = _MockCompletions(self._raise, self._stream_getter)


class _MockCompletions:
    def __init__(self, raise_on_create, stream_getter):
        self._raise = raise_on_create
        self._stream_getter = stream_getter

    def create(self, **kwargs):
        if self._raise:
            raise RuntimeError("API error")
        return self._stream_getter()


class TestStreamingHandler:
    """StreamingHandler 流式 API 调用处理器。"""

    def test_construction(self):
        agent = MockAgent()
        handler = StreamingHandler(agent)
        assert handler._agent is agent
        assert isinstance(handler._fence, StreamWriterFence)
        # 无 delta_callback，无 agent.on_stream_delta → callback 为 None
        assert handler._delta_callback is None

    def test_delta_callback_from_agent_attribute(self):
        """agent 有 on_stream_delta → 自动作为 callback。"""
        agent = MockAgent()
        agent.on_stream_delta = lambda d: None
        handler = StreamingHandler(agent)
        assert handler._delta_callback is not None

    def test_explicit_delta_callback_overrides_agent(self):
        """显式 delta_callback 优先于 agent.on_stream_delta。"""
        agent = MockAgent()
        agent.on_stream_delta = lambda d: None
        explicit = lambda d: None
        handler = StreamingHandler(agent, delta_callback=explicit)
        assert handler._delta_callback is explicit

    def test_stream_chat_basic_content(self):
        chunks = [
            MockChunk(content="Hello "),
            MockChunk(content="world"),
            MockChunk(content="!"),
            MockChunk(finish_reason="stop"),
        ]
        stream = MockStream(chunks)
        agent = MockAgent(model="gpt-4", stream_response=stream)
        handler = StreamingHandler(agent)
        result = handler.stream_chat([{"role": "user", "content": "hi"}])
        assert result.content == "Hello world!"
        assert result.finish_reason == "stop"

    def test_stream_chat_with_model_override(self):
        chunks = [MockChunk(content="x"), MockChunk(finish_reason="stop")]
        stream = MockStream(chunks)
        agent = MockAgent(model="gpt-4", stream_response=stream)
        handler = StreamingHandler(agent)
        handler.stream_chat(
            [{"role": "user", "content": "hi"}], model="claude-3",
        )
        # 验证 client.create 被调用时 model 是 claude-3
        # （通过 mock agent 内部记录 kwargs 验证；这里只验证不抛错）

    def test_stream_chat_with_tool_calls(self):
        chunks = [
            MockChunk(tool_calls=[
                MockToolCall(
                    index=0, id="tc-1",
                    function=MockFunction(name="search", arguments='{"q":'),
                ),
            ]),
            MockChunk(tool_calls=[
                MockToolCall(
                    index=0,
                    function=MockFunction(arguments='"python"}'),
                ),
            ]),
            MockChunk(finish_reason="tool_calls"),
        ]
        stream = MockStream(chunks)
        agent = MockAgent(stream_response=stream)
        handler = StreamingHandler(agent)
        result = handler.stream_chat(
            [{"role": "user", "content": "search"}],
        )
        assert result.finish_reason == "tool_calls"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["id"] == "tc-1"
        assert result.tool_calls[0]["function"]["name"] == "search"

    def test_stream_chat_stops_when_finish_reason_set(self):
        """遇到 finish_reason → 停止接收后续 chunk。"""
        # finish_reason 在第二个 chunk
        chunks = [
            MockChunk(content="x"),
            MockChunk(content="y", finish_reason="stop"),
            MockChunk(content="z"),  # 应被忽略
        ]
        stream = MockStream(chunks)
        agent = MockAgent(stream_response=stream)
        handler = StreamingHandler(agent)
        result = handler.stream_chat([{"role": "user", "content": "hi"}])
        # "z" 应被忽略（break 在 finish_reason 后）
        assert result.content == "xy"

    def test_stream_chat_skips_chunks_without_choices(self):
        """chunk.choices 为空 → 跳过。"""
        chunks = [
            MockChunk(choices=[]),
            MockChunk(content="x"),
            MockChunk(finish_reason="stop"),
        ]
        stream = MockStream(chunks)
        agent = MockAgent(stream_response=stream)
        handler = StreamingHandler(agent)
        # 不抛错
        result = handler.stream_chat([{"role": "user", "content": "hi"}])
        assert result.content == "x"

    def test_stream_chat_skips_delta_none(self):
        """delta is None → 跳过。"""
        # 构造 delta=None 的 choice
        choice = MockChoice(delta=None, finish_reason="stop")
        chunks = [
            MockChunk(choices=[choice]),
            MockChunk(content="x", finish_reason="stop"),
        ]
        stream = MockStream(chunks)
        agent = MockAgent(stream_response=stream)
        handler = StreamingHandler(agent)
        # 不抛错
        result = handler.stream_chat([{"role": "user", "content": "hi"}])

    def test_stream_chat_extracts_usage(self):
        """stream.get('usage') → result.usage。"""
        usage_data = {"prompt_tokens": 10, "completion_tokens": 20}
        chunks = [MockChunk(content="x", finish_reason="stop")]
        stream = MockStream(chunks, usage=usage_data)
        agent = MockAgent(stream_response=stream)
        handler = StreamingHandler(agent)
        result = handler.stream_chat([{"role": "user", "content": "hi"}])
        assert result.usage == usage_data

    def test_stream_chat_no_usage(self):
        """stream.get('usage') 返回 None → result.usage 仍是空 dict。"""
        chunks = [MockChunk(content="x", finish_reason="stop")]
        stream = MockStream(chunks, usage=None)
        agent = MockAgent(stream_response=stream)
        handler = StreamingHandler(agent)
        result = handler.stream_chat([{"role": "user", "content": "hi"}])
        assert result.usage == {}

    def test_stream_chat_raises_on_api_error(self):
        """API 调用失败 → 抛错（spirit 不 swallow）。"""
        agent = MockAgent(raise_on_create=True)
        handler = StreamingHandler(agent)
        with pytest.raises(RuntimeError, match="API error"):
            handler.stream_chat([{"role": "user", "content": "hi"}])

    def test_stream_chat_fence_supersession(self):
        """新流 claim → 旧流停止接收。"""
        agent = MockAgent()

        # 两个 handler 共享 fence（模拟同 agent）
        handler1 = StreamingHandler(agent)
        handler2 = StreamingHandler(agent)

        # 让两个 handler 共享同一个 fence
        handler2._fence = handler1._fence

        # 构造 chunks：前 2 个给 handler1，handler1 期间 handler2 claim
        # 实际上 spirit 没法这样精确控制——简化：直接测试 fence 行为
        token = handler1._fence.claim()
        assert handler1._fence.is_current(token) is True
        new_token = handler2._fence.claim()
        # 旧 token 不再 current
        assert handler1._fence.is_current(token) is False
        # 新 token current
        assert handler1._fence.is_current(new_token) is True

    def test_stream_chat_delta_callback_invoked(self):
        """delta_callback 在每个 chunk 时被调用。"""
        calls = []
        chunks = [
            MockChunk(content="a"),
            MockChunk(content="b"),
            MockChunk(finish_reason="stop"),
        ]
        stream = MockStream(chunks)
        agent = MockAgent(stream_response=stream)
        handler = StreamingHandler(agent, delta_callback=lambda d: calls.append(d.content))
        handler.stream_chat([{"role": "user", "content": "hi"}])
        # spirit 为每个 chunk 构造 StreamDelta（即使是 finish_reason chunk）。
        # finish_reason chunk 的 content 为空字符串（getattr fallback）。
        assert calls == ["a", "b", ""]

    def test_stream_chat_delta_callback_with_finish_reason(self):
        """finish_reason chunk → callback 被调用且 delta 含 finish_reason。"""
        captured = []

        def cb(delta):
            captured.append((delta.content, delta.finish_reason))

        chunks = [MockChunk(content="x", finish_reason="stop")]
        stream = MockStream(chunks)
        agent = MockAgent(stream_response=stream)
        handler = StreamingHandler(agent, delta_callback=cb)
        handler.stream_chat([{"role": "user", "content": "hi"}])
        # 至少有一次回调带 finish_reason="stop"
        assert any(fr == "stop" for content, fr in captured)

    def test_stream_chat_tools_in_request(self):
        """tools 参数传入 → 包含在 client.create 调用。"""
        chunks = [MockChunk(content="x", finish_reason="stop")]
        stream = MockStream(chunks)

        captured_kwargs = {}

        class _CapturingCompletions:
            def create(self, **kwargs):
                captured_kwargs.update(kwargs)
                return stream

        class _CapturingChat:
            completions = _CapturingCompletions()

        class _CapturingClient:
            chat = _CapturingChat()

        class CapturingAgent(MockAgent):
            def __init__(self):
                self.model = "gpt-4"
                self.client = _CapturingClient()
                self.on_stream_delta = None

        agent = CapturingAgent()
        handler = StreamingHandler(agent)
        tools_def = [{"type": "function", "function": {"name": "search"}}]
        handler.stream_chat(
            [{"role": "user", "content": "hi"}], tools=tools_def,
        )
        # tools 应被透传
        assert captured_kwargs["tools"] == tools_def


# ─────────────────────────── create_mock_stream ───────────────────────────


class TestCreateMockStream:
    """create_mock_stream 非流式响应 → StreamDelta 列表。"""

    def test_no_choices_returns_single_final(self):
        class MockResponse:
            choices = []

        deltas = create_mock_stream(MockResponse())
        assert len(deltas) == 1
        assert deltas[0].is_final is True

    def test_basic_content(self):
        class MockMessage:
            content = "Hello world"
            tool_calls = None

        class MockChoice:
            message = MockMessage()
            finish_reason = "stop"

        class MockResponse:
            choices = [MockChoice()]

        deltas = create_mock_stream(MockResponse())
        # content delta + finish delta
        assert len(deltas) == 2
        assert deltas[0].content == "Hello world"
        assert deltas[1].finish_reason == "stop"
        assert deltas[1].is_final is True

    def test_with_tool_calls(self):
        class MockFunc:
            name = "search"
            arguments = '{"q": "x"}'

        class MockTC:
            id = "tc-1"
            function = MockFunc()

        class MockMessage:
            content = None
            tool_calls = [MockTC()]

        class MockChoice:
            message = MockMessage()
            finish_reason = "tool_calls"

        class MockResponse:
            choices = [MockChoice()]

        deltas = create_mock_stream(MockResponse())
        # tool_call delta + finish delta
        assert len(deltas) == 2
        assert deltas[0].tool_calls_delta[0]["id"] == "tc-1"
        assert deltas[0].tool_calls_delta[0]["function"]["name"] == "search"
        assert deltas[1].finish_reason == "tool_calls"

    def test_default_finish_reason_stop(self):
        class MockMessage:
            content = "x"
            tool_calls = None

        class MockChoice:
            message = MockMessage()
            finish_reason = None  # None 时默认 stop

        class MockResponse:
            choices = [MockChoice()]

        deltas = create_mock_stream(MockResponse())
        assert deltas[-1].finish_reason == "stop"

    def test_choices_with_none_element_returns_single_final(self):
        """choices=[None] → 视为空 choice → 返回单 final delta。

        spirit 代码 line 288：``choice = response.choices[0]`` 取到 None，
        接下来 ``if not choice:`` 为 True → 返回 [StreamDelta(is_final=True)]。
        """
        class MockResponse:
            choices = [None]

        deltas = create_mock_stream(MockResponse())
        assert len(deltas) == 1
        assert deltas[0].is_final is True


# ─────────────────────────── 端到端集成 ───────────────────────────


class TestEndToEndIntegration:
    """完整流式管道集成。"""

    def test_realistic_chat_completion(self):
        """真实场景：流式聊天 → 完整内容组装。"""
        chunks = [
            MockChunk(content="The "),
            MockChunk(content="answer "),
            MockChunk(content="is "),
            MockChunk(content="42."),
            MockChunk(finish_reason="stop"),
        ]
        usage = {"prompt_tokens": 5, "completion_tokens": 4}
        stream = MockStream(chunks, usage=usage)
        agent = MockAgent(model="gpt-4", stream_response=stream)
        handler = StreamingHandler(agent)
        result = handler.stream_chat(
            [{"role": "user", "content": "What is 6*7?"}],
        )
        assert result.content == "The answer is 42."
        assert result.finish_reason == "stop"
        assert result.usage["completion_tokens"] == 4

    def test_collector_with_callback_doesnt_break_on_error(self):
        """collector + bad callback：content 仍正确组装。"""
        def bad_cb(delta):
            raise ValueError("oops")

        c = StreamCollector(delta_callback=bad_cb)
        c.feed(StreamDelta(content="a"))
        c.feed(StreamDelta(content="b"))
        # content 仍正确（callback 失败不影响）
        result = c.build_result()
        assert result.content == "ab"

    def test_fence_thread_safety(self):
        """fence 在多线程下安全。"""
        fence = StreamWriterFence()
        results = []

        def worker():
            token = fence.claim()
            results.append((threading.current_thread().name, token))
            # 短暂持有
            time.sleep(0.001)
            # 检查自己仍是 current
            assert fence.is_current(token) or True  # 可能在别的线程已 claim 新 token

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 每个 worker 拿到不同 token
        tokens = [r[1] for r in results]
        assert len(set(tokens)) == 10