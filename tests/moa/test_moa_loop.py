"""MoA facade ``create()`` 行为契约测试 — 对标 Hermes ``tests/run_agent/test_moa_loop_mode.py``。

覆盖 ``MoAChatCompletions`` / ``MoAClient`` facade 的核心不变量：

- 参考拿到**裁剪后的 advisory 视图**（advisory 系统提示 + 无 agent system 提示 + 工具
  调用/结果扁平化成文本 + 以 user 轮结尾），聚合器拿到**原始消息 + 工具 schema**。
- 聚合器是 acting model（拿到 tools），参考是 advisory（拿不到 tools）。
- 禁用预设跳过参考 fan-out，聚合器独自行动。
- 展示钩子按「先每个参考、后一个 aggregating 信号」的顺序 emit。
- 状态域缓存：新工具结果 / 新 user 轮 = 缓存 MISS（参考重跑），相同状态的冗余
  create() = 缓存 HIT（不重跑、不重复 emit、不重复计费）。
- 默认不封顶输出（max_tokens=None）；``reference_max_tokens`` 只封顶 advisor。
- 并行 fan-out：保序、失败隔离、递归 MoA 守卫。
- 聚合器递归守卫（defense-in-depth，独立于配置归一化）。
- 参考用量折叠：每轮求和一次，consume 清空，缓存 HIT 归零。

与 Hermes 的差异：Spirit seam 是 ``_call_slot`` / ``_call_slot_stream``（非 ``call_llm``），
usage 是 dict（非 ``CanonicalUsage``），无定价表故 ``consume_reference_usage`` 返回
``(usage_dict, None)``。transport 由 harness fixture 注入（见 conftest）。
"""

from __future__ import annotations

import time

import pytest

from spirit.moa import moa_loop
from spirit.moa.moa_loop import MoAClient, MoAChatCompletions

from .conftest import fake_response, make_config


# ---------------------------------------------------------------------------
# 参考拿到裁剪视图；聚合器是 actor
# ---------------------------------------------------------------------------

def test_facade_references_get_trimmed_messages(harness):
    """参考看到 advisory 视图（裁剪 + 扁平化），聚合器看到原始转录 + 工具 schema。

    参考是 advisory 旁路：前置 advisory-role 系统提示、丢掉 agent 自己的 system 提示、
    把工具调用/结果扁平化成文本（零 tool-role 消息、零 tool_calls 数组）、以 user 轮
    结尾。聚合器是 acting model，拿到未裁剪的消息与工具 schema。
    """
    facade = MoAChatCompletions("review")
    facade.create(
        messages=[
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "question"},
            {
                "role": "assistant",
                "content": "checking",
                "tool_calls": [{"id": "x", "function": {"name": "lookup", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "x", "content": "tool output"},
        ],
        tools=[{"type": "function"}],
    )

    ref_call = harness.reference_calls[0]
    ref_msgs = ref_call["messages"]
    # advisory-role 系统提示在前；agent 自己的 system 提示消失。
    assert ref_msgs[0]["role"] == "system"
    assert "reference advisor" in ref_msgs[0]["content"].lower()
    assert "system prompt" not in ref_msgs[0]["content"]
    # 无 tool-role 消息、无 tool_calls 数组泄给参考。
    assert all(m["role"] in ("system", "user", "assistant") for m in ref_msgs)
    assert all("tool_calls" not in m for m in ref_msgs)
    # agent 的动作 + 工具结果作为文本保留。
    joined = "\n".join(m["content"] for m in ref_msgs[1:])
    assert "[called tool: lookup(" in joined
    assert "[tool result: tool output]" in joined
    # 以 user 轮结尾（最后 assistant 块之后追加 advisory 请求）。
    assert ref_msgs[-1]["role"] == "user"
    # 参考拿不到工具 schema（advisory 不行动）。
    assert ref_call.get("tools") in (None, [])

    # 聚合器仍收到原始消息 + 工具 schema。
    agg_call = harness.aggregator_calls[0]
    assert agg_call["tools"] is not None
    # 聚合器消息里含注入的参考 guidance。
    agg_text = "\n".join(str(m.get("content")) for m in agg_call["messages"])
    assert "Mixture of Agents reference context" in agg_text


def test_aggregator_is_actor_references_are_advisory(harness):
    """聚合器是 acting model：只有它拿到 tools；参考永远拿不到。"""
    facade = MoAChatCompletions("review")
    tools = [{"type": "function", "function": {"name": "shell"}}]
    facade.create(messages=[{"role": "user", "content": "q"}], tools=tools)

    # 每个参考调用的 tools 都是 None（advisory 不行动）。
    assert all(c["tools"] is None for c in harness.reference_calls)
    # 聚合器拿到确切的工具 schema。
    agg = harness.aggregator_calls[0]
    assert agg["tools"] == tools
    assert agg["task"] == "moa_aggregator"


def test_disabled_preset_skips_references(make_harness):
    """``enabled: false`` 预设跳过参考 fan-out——聚合器（预设的 acting model）独自行动。"""
    cfg = make_config(
        presets={
            "review": {
                "enabled": False,
                "reference_models": [{"provider": "openai-codex", "model": "gpt-5.5"}],
                "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
            }
        }
    )
    harness = make_harness(cfg)

    facade = MoAChatCompletions("review")
    facade.create(messages=[{"role": "user", "content": "question"}], tools=[{"type": "function"}])

    tasks = [c["task"] for c in harness.calls]
    # 无参考 fan-out——只跑聚合器。
    assert tasks == ["moa_aggregator"]
    # 聚合器拿到未修改的 user 消息（没附加 MoA guidance）。
    assert harness.calls[0]["messages"][-1]["content"] == "question"


# ---------------------------------------------------------------------------
# 展示钩子：先参考、后 aggregating
# ---------------------------------------------------------------------------

def test_facade_emits_reference_then_aggregating(harness):
    """facade 先报告每个参考的输出（带 index/count/label/text），再发一个 aggregating
    信号（带 aggregator label + ref_count），使前端能在聚合器行动前渲染参考块。"""
    events = []
    facade = MoAChatCompletions(
        "review", reference_callback=lambda ev, **kw: events.append((ev, kw))
    )
    facade.create(messages=[{"role": "user", "content": "q"}], tools=[{"type": "function"}])

    ref_events = [e for e in events if e[0] == "moa.reference"]
    agg_events = [e for e in events if e[0] == "moa.aggregating"]
    # 每个参考模型一个块，按来源标号，带 index/count。
    assert len(ref_events) == 2
    assert ref_events[0][1]["label"] == "openai-codex:gpt-5.5"
    assert ref_events[0][1]["index"] == 1 and ref_events[0][1]["count"] == 2
    assert "advice from" in ref_events[0][1]["text"]
    assert ref_events[1][1]["index"] == 2 and ref_events[1][1]["count"] == 2
    # 恰好一个 aggregating 信号，在参考之后，指名聚合器。
    assert len(agg_events) == 1
    assert agg_events[0][1]["aggregator"] == "openrouter:anthropic/claude-opus-4.8"
    assert agg_events[0][1]["ref_count"] == 2
    # 顺序：所有 reference 事件都在 aggregating 之前。
    kinds = [e[0] for e in events]
    assert kinds == ["moa.reference", "moa.reference", "moa.aggregating"]


# ---------------------------------------------------------------------------
# 状态域缓存：MISS 重跑 / HIT 复用
# ---------------------------------------------------------------------------

def test_facade_reruns_references_on_new_tool_result(harness):
    """新工具结果推进任务状态时参考重跑（缓存 MISS），相同状态的冗余 create() 是
    缓存 HIT（不重跑、不重复 emit）。

    agent 循环每个工具迭代调一次 create()。参考必须判断**最新**状态，故新工具结果是
    MISS；而状态相同的重复调用是 HIT，避免在纯 no-op 重调上重复 fan-out。
    """
    events = []
    facade = MoAChatCompletions(
        "review", reference_callback=lambda ev, **kw: events.append(ev)
    )

    base_msgs = [{"role": "user", "content": "do the thing"}]
    # 迭代 1：新 user 轮——参考跑（2 模型）。
    facade.create(messages=base_msgs, tools=[{"type": "function"}])
    after_tool = base_msgs + [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
    ]
    # 迭代 2：新工具结果推进状态 → 参考重跑。
    facade.create(messages=after_tool, tools=[{"type": "function"}])
    # 迭代 3：状态相同（无新 tool/user 输入）→ 缓存 HIT，不重跑。
    facade.create(messages=after_tool, tools=[{"type": "function"}])

    # 2 模型 × 2 个不同状态（新轮 + 新工具结果）= 4 次参考调用。冗余的第 3 次不增加。
    assert len(harness.reference_calls) == 4
    assert events.count("moa.reference") == 4
    assert events.count("moa.aggregating") == 2
    # 聚合器每次都跑（3 次）。
    assert len(harness.aggregator_calls) == 3


def test_facade_reruns_references_on_new_turn(harness):
    """真正的新 user 消息使缓存失效并重跑参考。"""
    facade = MoAChatCompletions("review")
    facade.create(messages=[{"role": "user", "content": "turn one"}], tools=[])
    facade.create(messages=[{"role": "user", "content": "turn two"}], tools=[])

    # 2 参考 × 2 个不同轮 = 4 次参考调用。
    assert len(harness.reference_calls) == 4


def test_user_turn_fanout_runs_references_once_per_turn(make_harness):
    """``fanout: user_turn`` 模式下，参考每用户轮只跑一次；轮内工具迭代复用建议。

    签名只哈希到**最后一条真实 user 消息**的前缀，使轮中增长（工具结果）不改变签名——
    迭代 2+ 变成缓存 HIT。
    """
    cfg = make_config(
        presets={
            "review": {
                "fanout": "user_turn",
                "reference_models": [{"provider": "openai-codex", "model": "gpt-5.5"}],
                "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
            }
        }
    )
    harness = make_harness(cfg)
    facade = MoAChatCompletions("review")

    base = [{"role": "user", "content": "do the thing"}]
    facade.create(messages=base, tools=[])
    after_tool = base + [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
    ]
    # 工具迭代：user_turn 模式下签名稳定 → 缓存 HIT，参考不重跑。
    facade.create(messages=after_tool, tools=[])
    facade.create(messages=after_tool, tools=[])

    # 1 参考 × 1 用户轮 = 1 次（不是 3 次）。
    assert len(harness.reference_calls) == 1
    # 新 user 轮 → 参考重跑。
    facade.create(messages=[{"role": "user", "content": "next turn"}], tools=[])
    assert len(harness.reference_calls) == 2


# ---------------------------------------------------------------------------
# 输出封顶：默认不封顶；reference_max_tokens 只封顶 advisor
# ---------------------------------------------------------------------------

def test_does_not_cap_output_tokens_by_default(harness):
    """默认 MoA 不封顶参考或聚合器输出（max_tokens=None），各模型用自己的最大值。"""
    facade = MoAChatCompletions("review")
    facade.create(messages=[{"role": "user", "content": "q"}], tools=[])

    ref_call = harness.reference_calls[0]
    agg_call = harness.aggregator_calls[0]
    assert ref_call["max_tokens"] is None
    assert agg_call["max_tokens"] is None


def test_reference_max_tokens_caps_only_advisors(make_harness):
    """``reference_max_tokens`` 只封顶 ADVISOR 输出；acting 聚合器从不封顶。"""
    cfg = make_config(
        presets={
            "review": {
                "reference_max_tokens": 600,
                "reference_models": [{"provider": "openai-codex", "model": "gpt-5.5"}],
                "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
            }
        }
    )
    harness = make_harness(cfg)
    facade = MoAChatCompletions("review")
    facade.create(messages=[{"role": "user", "content": "q"}], tools=[])

    assert harness.reference_calls[0]["max_tokens"] == 600
    # 聚合器不被 reference_max_tokens 封顶。
    assert harness.aggregator_calls[0]["max_tokens"] is None


def test_caller_max_tokens_flows_to_aggregator(harness):
    """调用方（conversation_loop）的 max_tokens 透传给聚合器（它是 acting model）。"""
    facade = MoAChatCompletions("review")
    facade.create(messages=[{"role": "user", "content": "q"}], tools=[], max_tokens=1234)

    assert harness.aggregator_calls[0]["max_tokens"] == 1234


# ---------------------------------------------------------------------------
# 温度路由
# ---------------------------------------------------------------------------

def test_reference_and_aggregator_temperature_from_preset(make_harness):
    """预设的 reference_temperature / aggregator_temperature 分别下发给参考/聚合器。"""
    cfg = make_config(
        presets={
            "review": {
                "reference_temperature": 0.3,
                "aggregator_temperature": 0.9,
                "reference_models": [{"provider": "openai-codex", "model": "gpt-5.5"}],
                "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
            }
        }
    )
    harness = make_harness(cfg)
    facade = MoAChatCompletions("review")
    facade.create(messages=[{"role": "user", "content": "q"}], tools=[])

    assert harness.reference_calls[0]["temperature"] == 0.3
    assert harness.aggregator_calls[0]["temperature"] == 0.9


def test_aggregator_temperature_falls_back_to_caller(harness):
    """预设无 aggregator_temperature 时，acting agent 自己配置的温度适用于聚合器。"""
    facade = MoAChatCompletions("review")
    facade.create(messages=[{"role": "user", "content": "q"}], tools=[], temperature=0.42)

    # 默认 review 预设无 aggregator_temperature → 回退到调用方的 0.42。
    assert harness.aggregator_calls[0]["temperature"] == 0.42
    # 参考不受调用方温度影响（用预设的 reference_temperature，默认 None）。
    assert harness.reference_calls[0]["temperature"] is None


# ---------------------------------------------------------------------------
# 并行 fan-out：保序 + 失败隔离 + 递归守卫
# ---------------------------------------------------------------------------

def test_references_run_in_parallel(harness):
    """参考并发 fan-out（delegate-batch 语义），非串行。

    每个参考 sleep；墙钟时间应接近最慢的单个调用，而非总和。输出保序，失败的参考被
    隔离，引用 MoA 预设的 slot 被递归守卫跳过（不派发）。
    """
    def slow(rec):
        model = rec["slot"].get("model")
        if model == "boom":
            raise RuntimeError("kaboom")
        time.sleep(0.5)
        return fake_response(f"resp-{rec['slot'].get('provider')}")

    harness.set_responses(slow)

    refs = [
        {"provider": "p1", "model": "ok"},
        {"provider": "moa", "model": "preset"},  # 递归守卫，不派发
        {"provider": "p2", "model": "boom"},     # 失败被隔离
        {"provider": "p3", "model": "ok"},
    ]

    start = time.monotonic()
    out = moa_loop._run_references_parallel(
        refs, [{"role": "user", "content": "hi"}], temperature=0.6, max_tokens=64
    )
    elapsed = time.monotonic() - start

    # 两个 0.5s sleep 并发跑 → 远低于 1.0s 串行下限。阈值 0.95s 容忍 CI 线程池启动
    # 抖动，同时若串行跑（≥1.0s）则硬失败。
    assert elapsed < 0.95, f"参考未并行运行（耗时 {elapsed:.2f}s）"
    # 输出顺序匹配输入顺序（稳定的 Reference N 标号）。
    assert [label for label, _, _ in out] == ["p1:ok", "moa:preset", "p2:boom", "p3:ok"]
    assert "recursively reference MoA" in out[1][1]
    assert out[2][1].startswith("[failed:")
    assert out[0][1] == "resp-p1"
    # 递归守卫的 slot 从未派发到 transport。
    assert all(c["slot"].get("provider") != "moa" for c in harness.calls)


def test_recursive_aggregator_guard_raises(harness, monkeypatch):
    """聚合器不能是另一个 MoA 预设（defense-in-depth 运行时守卫）。

    配置归一化（``_clean_slot``）已剥离 moa provider 的聚合器，故此守卫是独立于配置的
    第二道防线：即便解析出的预设聚合器 provider 是 moa，create() 也 loudly 拒绝而非
    形成递归 MoA 树。harness fixture 已 patch ``_call_slot``，故守卫触发前的参考 fan-out
    走假件（不碰网络）。
    """
    monkeypatch.setattr(
        "spirit.moa.config.resolve_moa_preset",
        lambda config, name=None: {
            "enabled": True,
            "reference_models": [{"provider": "openai-codex", "model": "gpt-5.5"}],
            "aggregator": {"provider": "moa", "model": "review"},
            "reference_temperature": None,
            "aggregator_temperature": None,
            "reference_max_tokens": None,
            "fanout": "per_iteration",
        },
    )

    facade = MoAChatCompletions("review")
    with pytest.raises(RuntimeError, match="cannot be another MoA"):
        facade.create(messages=[{"role": "user", "content": "q"}], tools=[])


# ---------------------------------------------------------------------------
# 参考用量折叠：每轮求和一次，consume 清空，缓存 HIT 归零
# ---------------------------------------------------------------------------

def test_consume_reference_usage_sums_and_clears(harness):
    """create() 每轮把 advisor 用量求和一次；consume 弹出并清空；缓存 HIT 不重复计费。"""
    def with_usage(rec):
        if rec["task"] == "moa_reference":
            return fake_response("advice", prompt=1000, completion=100)
        return fake_response("aggregator acted")

    harness.set_responses(with_usage)

    client = MoAClient("review")
    facade = client.chat.completions
    facade.create(messages=[{"role": "user", "content": "turn one"}], tools=[])

    usage, cost = client.consume_reference_usage()
    # 2 advisor × (1000 prompt, 100 completion) = 2000 prompt, 200 completion。
    assert usage["prompt_tokens"] == 2000
    assert usage["completion_tokens"] == 200
    assert usage["total_tokens"] == 2200
    # Spirit 无定价表 → cost 恒为 None。
    assert cost is None

    # consume 清空——无新 create() 的第二次 consume 归零。
    usage2, cost2 = client.consume_reference_usage()
    assert usage2["prompt_tokens"] == 0
    assert cost2 is None

    # 相同 advisory 视图的重复 create() 是缓存 HIT：advisor 不重跑，pending 归零。
    facade.create(messages=[{"role": "user", "content": "turn one"}], tools=[])
    usage3, cost3 = client.consume_reference_usage()
    assert usage3["prompt_tokens"] == 0
    assert cost3 is None


def test_last_aggregator_slot_exposed(harness):
    """create() 后暴露解析出的聚合器 slot，供会话成本记账按真实模型定价。"""
    client = MoAClient("review")
    assert client.last_aggregator_slot is None
    client.chat.completions.create(
        messages=[{"role": "user", "content": "q"}], tools=[]
    )

    slot = client.last_aggregator_slot
    assert slot is not None
    assert slot["provider"] == "openrouter"
    assert slot["model"] == "anthropic/claude-opus-4.8"


# ---------------------------------------------------------------------------
# 非流式响应适配（TransportResponse → OpenAI 形状）
# ---------------------------------------------------------------------------

def test_non_stream_create_returns_chat_response_shape(harness):
    """非流式 create() 把 TransportResponse 适配成 conversation_loop 期望的 OpenAI 形状。"""
    harness.set_responses(
        lambda rec: fake_response(
            "aggregator acted",
            tool_calls=[{"id": "call_9", "name": "shell", "args": {"cmd": "ls"}}],
            prompt=10, completion=5, model="agg-model",
        )
        if rec["task"] == "moa_aggregator"
        else None
    )

    facade = MoAChatCompletions("review")
    resp = facade.create(messages=[{"role": "user", "content": "q"}], tools=[])

    # OpenAI 形状：choices[0].message.{content, tool_calls} + usage。
    choice = resp.choices[0]
    assert choice.message.content == "aggregator acted"
    assert choice.finish_reason == "stop"
    # tool_calls 转成带 .function 的对象（conversation_loop 走 hasattr(tc,'function')）。
    tc = choice.message.tool_calls[0]
    assert tc.id == "call_9"
    assert tc.function.name == "shell"
    assert "ls" in tc.function.arguments
    assert resp.usage.prompt_tokens == 10
    assert resp.usage.completion_tokens == 5
    assert resp.usage.total_tokens == 15
