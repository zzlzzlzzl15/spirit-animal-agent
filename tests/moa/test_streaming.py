"""MoA 聚合器流式路径测试 — 对标 Hermes ``tests/run_agent/test_moa_streaming.py``。

``MoAChatCompletions.create(stream=True)`` 先跑参考（非流式 fan-out），再返回聚合器的
**流式** token（经 ``_adapt_stream_to_chunks`` 适配成 conversation_loop 期望的 OpenAI
chunk 形状），使 acting model 的输出实时到达用户。``stream=False`` 是完整响应路径
（见 test_moa_loop.py）。

与 Hermes 的差异：Hermes 流式分支把 ``call_llm(stream=True)`` 的**原始 SDK 流**逐字
返回（消费方自己重组）；Spirit transport 产出结构化事件（``{"type":"content"/
"tool_call"/"done"}``），故 facade 用 ``_adapt_stream_to_chunks`` 桥接成 OpenAI chunk
（``chunk.choices[0].delta.{content, tool_calls}`` + ``chunk.usage``）。可 monkeypatch
的 seam 是 ``_call_slot_stream``（非 ``_call_slot``）。
"""

from __future__ import annotations

from spirit.moa.moa_loop import MoAChatCompletions

from .conftest import content_event, done_event, tool_call_event


# ---------------------------------------------------------------------------
# 流式分支路由：参考仍跑，聚合器走 _call_slot_stream
# ---------------------------------------------------------------------------

def test_create_streams_aggregator_when_requested(harness):
    """stream=True：参考仍跑（MoA 未被绕过），聚合器经流式 seam 调用，create() 返回
    适配后的 chunk 迭代器。"""
    facade = MoAChatCompletions("review")
    out = facade.create(
        messages=[{"role": "user", "content": "q"}],
        tools=[{"type": "function"}],
        stream=True,
    )

    # create() 返回一个 chunk 迭代器（从 transport 事件适配而来）。
    chunks = list(out)
    assert chunks, "流式 create() 应产出至少一个 chunk"
    # 参考仍跑（MoA 未被绕过）。
    assert harness.reference_calls
    # 聚合器经流式 seam 调用，恰好一次。
    assert len(harness.stream_calls) == 1
    agg = harness.stream_calls[0]
    assert agg["task"] == "moa_aggregator"
    # 工具仍流向（流式）聚合器——它是 acting model。
    assert agg["tools"] is not None
    # 非流式 _call_slot 未被用于聚合器。
    assert not harness.aggregator_calls


def test_non_stream_does_not_touch_stream_seam(harness):
    """stream=False（默认）走 _call_slot，绝不碰 _call_slot_stream。"""
    facade = MoAChatCompletions("review")
    facade.create(messages=[{"role": "user", "content": "q"}], tools=[])

    assert harness.stream_calls == []
    assert len(harness.aggregator_calls) == 1


def test_stream_aggregator_messages_include_guidance(harness):
    """流式聚合器收到的消息含注入的参考 guidance（与非流式路径一致）。"""
    facade = MoAChatCompletions("review")
    facade.create(messages=[{"role": "user", "content": "q"}], tools=[], stream=True)

    agg_msgs = harness.stream_calls[0]["messages"]
    text = "\n".join(str(m.get("content")) for m in agg_msgs)
    assert "Mixture of Agents reference context" in text
    assert "advice from" in text


# ---------------------------------------------------------------------------
# chunk 适配：content / usage / tool_calls
# ---------------------------------------------------------------------------

def test_stream_chunks_carry_content_and_usage(harness):
    """content 事件 → delta.content；done 事件 → 末 chunk 携带 usage + finish_reason。"""
    facade = MoAChatCompletions("review")
    # 默认假流：content "streamed "、content "answer"、done(1,2)。
    chunks = list(
        facade.create(messages=[{"role": "user", "content": "q"}], tools=[], stream=True)
    )

    text = "".join(
        c.choices[0].delta.content or "" for c in chunks if c.choices[0].delta.content
    )
    assert text == "streamed answer"
    # 末 chunk 携带 usage 与 finish_reason。
    last = chunks[-1]
    assert last.choices[0].finish_reason == "stop"
    assert last.usage.prompt_tokens == 1
    assert last.usage.completion_tokens == 2
    assert last.usage.total_tokens == 3
    # 中间 content chunk 不携带 usage（usage=None）。
    assert chunks[0].usage is None
    assert chunks[0].choices[0].finish_reason is None


def test_stream_tool_calls_adapted(harness):
    """tool_call 事件 → delta.tool_calls，带 index/id/function.{name,arguments}。"""
    harness.set_stream(
        lambda rec: iter([
            tool_call_event("shell", {"cmd": "ls"}, call_id="call_1"),
            done_event(0, 0),
        ])
    )

    facade = MoAChatCompletions("review")
    chunks = list(
        facade.create(messages=[{"role": "user", "content": "q"}], tools=[], stream=True)
    )

    tc_chunks = [c for c in chunks if c.choices[0].delta.tool_calls]
    assert len(tc_chunks) == 1
    tc = tc_chunks[0].choices[0].delta.tool_calls[0]
    assert tc.index == 0
    assert tc.id == "call_1"
    assert tc.function.name == "shell"
    assert "ls" in tc.function.arguments


def test_stream_multiple_tool_calls_get_sequential_index(harness):
    """多个 tool_call 事件获得递增的 index（消费方靠它拼接同一调用的分片）。"""
    harness.set_stream(
        lambda rec: iter([
            tool_call_event("a", {}, call_id="c0"),
            tool_call_event("b", {}, call_id="c1"),
            done_event(0, 0),
        ])
    )

    facade = MoAChatCompletions("review")
    chunks = list(
        facade.create(messages=[{"role": "user", "content": "q"}], tools=[], stream=True)
    )
    indexes = [
        c.choices[0].delta.tool_calls[0].index
        for c in chunks if c.choices[0].delta.tool_calls
    ]
    assert indexes == [0, 1]


def test_stream_content_event_text_preserved_verbatim(harness):
    """自定义 content 事件按序逐字适配（含空字符串边界不丢 chunk 顺序）。"""
    harness.set_stream(
        lambda rec: iter([
            content_event("Hello"),
            content_event(", "),
            content_event("world"),
            done_event(5, 3),
        ])
    )

    facade = MoAChatCompletions("review")
    chunks = list(
        facade.create(messages=[{"role": "user", "content": "q"}], tools=[], stream=True)
    )
    text = "".join(
        c.choices[0].delta.content or "" for c in chunks if c.choices[0].delta.content
    )
    assert text == "Hello, world"
    assert chunks[-1].usage.total_tokens == 8


# ---------------------------------------------------------------------------
# 流式路径的待处理追踪标记
# ---------------------------------------------------------------------------

def test_stream_marks_pending_trace_streamed(harness):
    """流式路径在 create() 时无法内联捕获聚合器输出，故待处理追踪标记 streamed=True、
    output=None，由调用方事后经 consume_and_save_trace 的 fallback 折入。"""
    facade = MoAChatCompletions("review")
    facade.create(messages=[{"role": "user", "content": "q"}], tools=[], stream=True)

    pending = facade._pending_trace
    assert pending is not None
    assert pending["aggregator_streamed"] is True
    assert pending["aggregator_output"] is None
    # 聚合器输入（含 guidance）已记录。
    assert "aggregator_input_messages" in pending


def test_non_stream_captures_aggregator_output_inline(harness):
    """非流式路径在 create() 时内联捕获聚合器输出（streamed=False）。"""
    harness.set_responses(
        lambda rec: None  # 用默认假响应：聚合器返回 "aggregator acted"
    )
    facade = MoAChatCompletions("review")
    facade.create(messages=[{"role": "user", "content": "q"}], tools=[])

    pending = facade._pending_trace
    assert pending is not None
    assert pending["aggregator_streamed"] is False
    assert pending["aggregator_output"] == "aggregator acted"
