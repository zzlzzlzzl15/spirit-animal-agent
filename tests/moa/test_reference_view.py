"""advisory 视图构造测试 — 对标 Hermes ``tests/run_agent/test_moa_loop_mode.py``。

覆盖两个纯函数：

- ``_reference_messages``：把对话转录渲染成参考模型看到的 advisory 视图——丢 system、
  把 tool_calls/tool 结果扁平化成文本、丢弃空 user 轮、必须以 user 轮结尾（满足
  Anthropic 无 prefill 规则）、对 cache 装饰 / 多模态 content 做扁平化。
- ``_attach_reference_guidance``：把 per-turn 参考块附加到聚合器提示**末尾**（保 KV
  缓存前缀稳定），结尾 user 轮就地合并（str 或 content-part list 两种形状）。

这些是 MoA 正确性的核心不变量：参考必须看到 agent 的真实动作（工具调用 + 结果）才能
给出知情判断，同时视图必须是干净的 user/assistant 文本（严格 provider 拒绝 tool-role
消息 / 未产生的 tool_calls），且必须以 user 轮结尾。
"""

from __future__ import annotations

from spirit.moa.moa_loop import (
    _ADVISORY_INSTRUCTION,
    _REFERENCE_TOOL_RESULT_BUDGET,
    _attach_reference_guidance,
    _reference_messages,
)


# ---------------------------------------------------------------------------
# _reference_messages — 工具调用 / 结果渲染
# ---------------------------------------------------------------------------

def test_reference_messages_renders_tool_calls_and_results():
    """参考必须看到 agent 做了什么（工具调用）与返回了什么（工具结果）才能给出知情
    判断——故两者都不被剥离。它们被扁平化成文本，使视图携带零个 tool-role 消息 / 零个
    tool_calls 数组（严格 provider 拒绝那些），而参考仍有完整图景。视图以 user 轮结尾。
    """
    messages = [
        {"role": "system", "content": "huge spirit system prompt"},
        {"role": "user", "content": "do the thing"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "tool result"},
        {"role": "assistant", "content": "here is my answer"},
    ]

    view = _reference_messages(messages)

    # 线格式安全：只有 user/assistant 文本，无 tool 角色 / tool_calls。
    assert all(m["role"] in ("user", "assistant") for m in view)
    assert all("tool_calls" not in m for m in view)
    # system 提示消失。
    assert all("huge spirit system prompt" not in m["content"] for m in view)
    # agent 的动作与工具结果作为文本保留。
    joined = "\n".join(m["content"] for m in view)
    assert "[called tool: f(" in joined
    assert "[tool result: tool result]" in joined
    assert "here is my answer" in joined
    # 以 user 轮结尾（最后一个 assistant 之后追加 advisory 请求）。
    assert view[-1]["role"] == "user"


def test_reference_messages_ends_with_user_not_assistant_prefill():
    """advisory 参考视图绝不能以 assistant 轮结尾。

    工具循环中途对话以 assistant/tool 交换结尾。Anthropic（及 OpenRouter→Anthropic）
    把结尾的 assistant 轮当作要续写的 assistant prefill，而无 prefill 的模型用
    ``400 ... must end with a user message`` 拒绝它。我们追加一个合成 user 轮请求判断，
    而非删除 agent 的最新上下文——参考仍须看到当前状态才能对其建议。
    """
    messages = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2 current"},
        {
            "role": "assistant",
            "content": "let me reason then call a tool",
            "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "the tool output"},
    ]

    view = _reference_messages(messages)

    assert view, "advisory 视图不应为空"
    assert view[-1]["role"] == "user"
    joined = "\n".join(m["content"] for m in view)
    # agent 的最新动作及其结果被保留，而非丢弃。
    assert "let me reason then call a tool" in joined
    assert "[called tool: f(" in joined
    assert "[tool result: the tool output]" in joined
    # 早先上下文也保留。
    assert "q1" in joined and "a1" in joined and "q2 current" in joined
    # 结尾正是合成 advisory 请求。
    assert view[-1]["content"] == _ADVISORY_INSTRUCTION


def test_reference_messages_truncates_large_tool_results():
    """大工具结果被 head+tail 预览，而非逐字重放。"""
    huge = "A" * (_REFERENCE_TOOL_RESULT_BUDGET * 3)
    messages = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": huge},
    ]

    view = _reference_messages(messages)
    joined = "\n".join(m["content"] for m in view)
    assert "chars omitted" in joined
    # 折叠后的结果远小于原始 payload。
    assert len(joined) < len(huge)


def test_reference_messages_fresh_user_turn_ends_on_that_user():
    """尚无 agent 动作的新用户提示以该 user 轮结尾。"""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2 current"},
    ]

    view = _reference_messages(messages)
    assert view[-1] == {"role": "user", "content": "q2 current"}


def test_reference_messages_drops_empty_user_turns():
    """空 user 轮不得泄进 advisory 视图。

    content 为 "" 或非字符串 / 多模态 payload（被文本提取步骤扁平化为 ""）的 user 消息
    不携带任何 advisory 内容。严格 provider（Kimi/Moonshot 等强制非空 user content 的）
    用 400 "message ... with role 'user' must not be empty" 拒绝，而宽松 provider
    （DeepSeek）接受——故对同一渲染视图的 fan-out 会在一个参考上失败、另一个通过。
    渲染器必须不发空 user 轮，镜像空 assistant 轮被丢弃的方式。
    """
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "real question"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "read_file", "arguments": '{"path":"c.yaml"}'}}
        ]},
        {"role": "tool", "content": "some result"},
        {"role": "user", "content": ""},  # 空字符串 user 轮
    ]

    view = _reference_messages(messages)

    # 视图里没有 user 轮是空 / 纯空白的。
    empty_users = [
        m for m in view
        if m.get("role") == "user" and not str(m.get("content", "")).strip()
    ]
    assert empty_users == [], f"空 user 轮泄进 advisory 视图: {empty_users}"
    # 真实用户提示存活，且视图仍以 user 轮结尾。
    assert view[0] == {"role": "user", "content": "real question"}
    assert view[-1]["role"] == "user"


def test_reference_messages_drops_whitespace_only_string_user_turn():
    """纯空白的**字符串** user 轮被丢弃，而非占位。

    非文本占位符是为结构化 content（纯图片轮）存在的——那里确实发生了一个参考应知道的
    真实轮。裸空白字符串不携带任何内容——发它会 400 严格 provider，占位它会捏造一个
    从未存在的附件。
    """
    messages = [
        {"role": "user", "content": "   "},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "real"},
    ]

    view = _reference_messages(messages)

    assert view[0] == {"role": "assistant", "content": "a"}
    assert view[-1] == {"role": "user", "content": "real"}
    assert all(str(m["content"]).strip() for m in view)


# ---------------------------------------------------------------------------
# _reference_messages — 结构化 / 多模态 content 扁平化
# ---------------------------------------------------------------------------

def test_reference_messages_flattens_cache_decorated_content():
    """cache 装饰过的轮（content-part 列表）不得让参考失明。

    conversation_loop 在 MoA facade **之前**跑 apply_cache_control（当预设聚合器是
    尊重缓存的 Claude 路由时）。那把字符串 content 转成 [{"type":"text","text":...,
    "cache_control":...}] 列表。advisory 视图必须提取文本部分，使用户的整个提示不会
    扁平化为 ""（否则 Claude 参考会 400 "at least one message is required"）。
    """
    plain = [
        {"role": "system", "content": "spirit system prompt"},
        {"role": "user", "content": "Can we get codex usage resets into spirit?"},
    ]
    # 手动构造 cache 装饰后的形状（content 变成带 cache_control 的 part 列表）。
    decorated = [
        {"role": "system", "content": "spirit system prompt"},
        {"role": "user", "content": [
            {"type": "text", "text": "Can we get codex usage resets into spirit?",
             "cache_control": {"type": "ephemeral"}},
        ]},
    ]
    assert isinstance(decorated[1]["content"], list)

    view = _reference_messages(decorated)

    assert view == [
        {"role": "user", "content": "Can we get codex usage resets into spirit?"}
    ]
    # 不变量：装饰过的与未装饰的转录产出**相同**的 advisory 视图——故装饰永不能改变
    # 参考看到的内容，且 advisory 前缀保持字节稳定供 advisor prompt caching。
    assert view == _reference_messages(plain)


def test_reference_messages_flattens_multimodal_user_turn():
    """多模态 user 轮（文本 + 图片部分）在视图里保留其文本。

    图片部分不携带 advisory 文本、被跳过；文本部分必须存活。
    """
    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "what is in this screenshot?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]},
    ]

    view = _reference_messages(messages)

    assert view == [{"role": "user", "content": "what is in this screenshot?"}]
    # base64 payload 不泄进 advisory 视图。
    assert all("base64" not in m["content"] for m in view)


def test_reference_messages_image_only_user_turn_gets_placeholder():
    """纯图片 user 轮不得变成空 user 消息。

    Anthropic 拒绝空文本块（原始的 400 类），而静默跳过该轮会打乱视图里的 user/assistant
    交替——故用占位符替代非文本内容。
    """
    messages = [
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]},
        {"role": "assistant", "content": "I see a diagram."},
        {"role": "user", "content": "now explain it"},
    ]

    view = _reference_messages(messages)

    assert view[0]["role"] == "user"
    assert view[0]["content"].strip(), "纯图片轮不得为空"
    assert "non-text" in view[0]["content"]
    assert view[-1] == {"role": "user", "content": "now explain it"}


def test_reference_messages_flattens_structured_assistant_and_tool_content():
    """带 content-part 列表的 assistant 与 tool 轮也被扁平化。

    多模态工具结果（如 computer_use 截图）与 adapter 形状的 assistant 轮以列表到达；
    它们的文本必须到达参考，它们的图片部分不得泄露。
    """
    messages = [
        {"role": "user", "content": "check the screen"},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "taking a screenshot"}],
            "tool_calls": [{"id": "c1", "function": {"name": "capture", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": [
            {"type": "text", "text": "screenshot captured: login page visible"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}},
        ]},
    ]

    view = _reference_messages(messages)

    joined = "\n".join(m["content"] for m in view)
    assert "taking a screenshot" in joined
    assert "[called tool: capture(" in joined
    assert "[tool result: screenshot captured: login page visible]" in joined
    assert "BBBB" not in joined
    assert view[-1]["role"] == "user"


# ---------------------------------------------------------------------------
# _attach_reference_guidance — 末尾附加（保 KV 缓存前缀稳定）
# ---------------------------------------------------------------------------

def test_reference_guidance_appended_at_end_in_tool_loop():
    """在 agentic 循环里，参考块必须落在提示**末尾**。

    最近的 user 轮是坐在上下文顶部附近的原始任务；把 per-turn（易变的）参考块合并进它
    会在早期就分叉提示前缀、击穿服务器的 KV 缓存复用，迫使每个工具循环步完整重新
    prefill 整个对话。
    """
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "ORIGINAL TASK"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "tool result", "tool_call_id": "1"},
    ]
    _attach_reference_guidance(messages, "REFERENCE BLOCK")

    # 原始（上下文顶部）user 轮未被触碰，故前缀跨步保持缓存可复用。
    assert messages[1]["content"] == "ORIGINAL TASK"
    # 参考块作为新的结尾轮附加，而非向上游合并。
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "REFERENCE BLOCK"
    assert len(messages) == 5


def test_reference_guidance_merges_into_trailing_user_in_plain_chat():
    """纯聊天以 user 轮结尾，故块合并到那里（仍在末尾）。"""
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "hello"},
    ]
    _attach_reference_guidance(messages, "REFERENCE BLOCK")

    # 无额外消息；块并入结尾的 user 轮（即末尾）。
    assert len(messages) == 2
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "hello\n\nREFERENCE BLOCK"


def test_reference_guidance_appends_text_part_to_decorated_trailing_user():
    """cache 装饰过的结尾 user 轮仍收到 guidance 块。

    装饰把结尾 user 轮转成 content-part 列表；guidance 必须作为**新**文本部分附加在
    cache_control 标记的部分**之后**（缓存前缀保持字节稳定，无连续 user 轮 400），
    而非被静默丢弃、也非作为第二条 user 消息添加。
    """
    marked_part = {
        "type": "text",
        "text": "hello",
        "cache_control": {"type": "ephemeral"},
    }
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": [dict(marked_part)]},
    ]
    _attach_reference_guidance(messages, "REFERENCE BLOCK")

    # 无额外消息（会破坏 user/user 交替）。
    assert len(messages) == 2
    content = messages[-1]["content"]
    assert isinstance(content, list) and len(content) == 2
    # cache 标记的部分字节一致（前缀稳定）。
    assert content[0] == marked_part
    # guidance 作为缓存跨度之外的结尾文本部分骑乘。
    assert content[1] == {"type": "text", "text": "\n\nREFERENCE BLOCK"}
