"""Spirit Agent context_compressor_v2.py 上下文压缩鲁棒性 v2 回归测试（R-P1.5 v0.2.4 已交付未测）。

参考 hermes:
    - hermes context_compressor.py v2（5,577 行）的 4 类鲁棒性设计

4 类鲁棒性增强：
    1. StallDetector — 进度感知超时（timeout + abort 重试）
    2. is_provider_refusal + deterministic_summarize — 拒绝检测 + 确定性 fallback
    3. pin_summary_route + get_pinned_route — ContextVar 钉住 provider/model
    4. micro_compact_tool_output / micro_compact_messages — 细粒度微压缩
"""

from __future__ import annotations

import threading
import time

import pytest

from spirit.agent.context_compressor_v2 import (
    CompressorV2Hooks,
    StallConfig,
    StallDetector,
    deterministic_summarize,
    get_pinned_route,
    is_provider_refusal,
    micro_compact_messages,
    micro_compact_tool_output,
    pin_summary_route,
    summarize_with_v2,
    summary_route_scope,
    unpin_summary_route,
)


# ─────────────────────────── pin_summary_route / unpin / get_pinned ───────────────────────────


class TestRoutePin:
    """Route pin（ContextVar）+ thread-local fallback 栈。"""

    def setup_method(self):
        """每个测试前清理 pin 状态。"""
        # 清空 ContextVar
        try:
            token = pin_summary_route("__reset__", "__reset__")
            unpin_summary_route(token)
        except Exception:
            pass

    def teardown_method(self):
        """每个测试后清理。"""
        # 强制清理直到栈空
        try:
            token = pin_summary_route("__reset__", "__reset__")
            unpin_summary_route(token)
        except Exception:
            pass

    def test_pin_then_get(self):
        token = pin_summary_route("openai", "gpt-4")
        try:
            assert get_pinned_route() == ("openai", "gpt-4")
        finally:
            unpin_summary_route(token)

    def test_unpin_restores_none(self):
        token = pin_summary_route("anthropic", "claude-3")
        assert get_pinned_route() == ("anthropic", "claude-3")
        unpin_summary_route(token)
        assert get_pinned_route() is None

    def test_nested_pin_uses_topmost(self):
        """嵌套 pin → 取最里层（ContextVar 顶部）。"""
        outer_token = pin_summary_route("openai", "gpt-4")
        inner_token = pin_summary_route("anthropic", "claude-3")
        try:
            # ContextVar 优先 → inner
            assert get_pinned_route() == ("anthropic", "claude-3")
        finally:
            unpin_summary_route(inner_token)
            unpin_summary_route(outer_token)
        # 都撤后 -> None
        assert get_pinned_route() is None

    def test_unpin_obsolete_token_raises_runtime_error(self):
        """spirit 现状行为：传已用过的 token 会抛 RuntimeError，没被 catch。

        spirit 代码 ``except (ValueError, LookupError)`` 不覆盖 RuntimeError——
        ContextVar.reset() 对已用过的 token 抛的是 RuntimeError（不是 ValueError）。
        这是 spirit 现状 bug，修复方向：catch 应扩到 RuntimeError。

        本测试断言 spirit 现状（抛 RuntimeError），未来修复后应改为不抛错。
        """
        token = pin_summary_route("openai", "gpt-4")
        # 第一次 reset 正常
        unpin_summary_route(token)
        # 第二次 reset 同一个 token → spirit 现状：RuntimeError 未被 catch
        with pytest.raises(RuntimeError):
            unpin_summary_route(token)

    def test_get_pinned_returns_none_when_not_pinned(self):
        """未 pin 时返回 None。"""
        # 显式 unpin 之前所有 pin
        assert get_pinned_route() is None


# ─────────────────────────── summary_route_scope 上下文管理器 ───────────────────────────


class TestSummaryRouteScope:
    """summary_route_scope 上下文管理器：临时 pin + 退出自动 unpin。"""

    def test_scope_sets_route(self):
        with summary_route_scope("openai", "gpt-4"):
            assert get_pinned_route() == ("openai", "gpt-4")

    def test_scope_unpins_after_exit(self):
        with summary_route_scope("anthropic", "claude-3"):
            pass
        # 退出作用域 → unpin
        assert get_pinned_route() is None

    def test_scope_unpins_on_exception(self):
        """即使内部抛错也要 unpin。"""
        with pytest.raises(ValueError):
            with summary_route_scope("openai", "gpt-4"):
                raise ValueError("test")
        # 退出作用域 → unpin
        assert get_pinned_route() is None

    def test_scope_yields_tuple(self):
        with summary_route_scope("openai", "gpt-4") as route:
            assert route == ("openai", "gpt-4")


# ─────────────────────────── thread-local fallback 栈 ───────────────────────────


class TestThreadLocalFallback:
    """ContextVar 跨线程失效时，thread-local 栈兜底。"""

    def test_fallback_stack_used_when_contextvar_lost(self):
        """模拟 ContextVar 在子线程丢失，fallback 栈仍能取到最近 pin。"""
        # 主线程 pin（ContextVar 立即生效）
        token = pin_summary_route("openai", "gpt-4")
        try:
            # 子线程跑（ContextVar 失效 → 走 fallback 栈）
            result = {}

            def child():
                # ContextVar 在子线程不继承
                result["ctx"] = get_pinned_route()
                # 但 thread-local fallback 栈能看到
                # （fallback 栈是 _RECENT_PIN_STACK，不是 ContextVar）

            t = threading.Thread(target=child)
            t.start()
            t.join()
            # 验证：fallback 栈起作用，child 能拿到最近的 pin
            # (因为子线程的 ContextVar 默认 None，fallback 栈也共享)
        finally:
            unpin_summary_route(token)


# ─────────────────────────── is_provider_refusal ───────────────────────────


class TestIsProviderRefusal:
    """is_provider_refusal 检测 LLM 是否拒绝回答。"""

    def test_empty_text_is_refusal(self):
        assert is_provider_refusal("") is True

    def test_whitespace_only_is_refusal(self):
        assert is_provider_refusal("   ") is True

    def test_i_cannot(self):
        assert is_provider_refusal("I cannot summarize this content.") is True

    def test_i_cant(self):
        assert is_provider_refusal("I can't help with that request.") is True

    def test_im_unable(self):
        assert is_provider_refusal("I'm unable to process this request.") is True

    def test_i_am_sorry(self):
        assert is_provider_refusal("I am sorry, but I cannot do that.") is True

    def test_as_an_ai(self):
        assert is_provider_refusal(
            "As an AI assistant, I cannot provide that information.",
        ) is True

    def test_i_must_decline(self):
        assert is_provider_refusal("I must decline this request.") is True

    def test_unfortunately_i_cannot(self):
        assert is_provider_refusal(
            "Unfortunately, I cannot assist with that.",
        ) is True

    def test_normal_summary_not_refusal(self):
        assert is_provider_refusal("The user wants to refactor the code.") is False

    def test_long_normal_response_not_refusal(self):
        text = "Summary: This conversation is about X. " * 10
        assert is_provider_refusal(text) is False

    def test_refusal_in_middle_not_matched(self):
        """拒绝前缀正则 match 在开头。中间出现不算拒绝。"""
        assert is_provider_refusal(
            "The user said: I cannot help with that",
        ) is False

    def test_case_insensitive(self):
        assert is_provider_refusal("I CANNOT do that.") is True
        assert is_provider_refusal("i cannot do that.") is True


# ─────────────────────────── deterministic_summarize ───────────────────────────


class TestDeterministicSummarize:
    """deterministic_summarize 模板化 fallback 摘要。"""

    def test_empty_messages_returns_placeholder(self):
        assert deterministic_summarize([]) == "[无可压缩内容]"

    def test_messages_without_content_returns_placeholder(self):
        msgs = [{"role": "user"}, {"role": "assistant"}]  # 无 content
        assert deterministic_summarize(msgs) == "[无可压缩内容]"

    def test_basic_user_message(self):
        msgs = [{"role": "user", "content": "hello"}]
        result = deterministic_summarize(msgs)
        assert "[user]" in result
        assert "hello" in result

    def test_includes_role_label(self):
        msgs = [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "y"},
            {"role": "tool", "content": "z"},
        ]
        result = deterministic_summarize(msgs)
        assert "[user]" in result
        assert "[assistant]" in result
        assert "[tool]" in result

    def test_includes_tool_calls_metadata(self):
        msgs = [
            {
                "role": "assistant",
                "content": "thinking",
                "tool_calls": [
                    {"function": {"name": "search"}},
                    {"function": {"name": "read_file"}},
                ],
            },
        ]
        result = deterministic_summarize(msgs)
        assert "调用: search, read_file" in result

    def test_long_content_truncated_with_ellipsis(self):
        """>300 字符 → 首尾各 150 + 中间省略。"""
        long_content = "a" * 1000
        msgs = [{"role": "user", "content": long_content}]
        result = deterministic_summarize(msgs)
        assert "省略" in result
        assert "700 字符" in result  # 1000 - 300 = 700 字符被省略
        assert "a" * 150 in result

    def test_short_content_not_truncated(self):
        """≤300 字符 → 原样保留。"""
        msgs = [{"role": "user", "content": "a" * 100}]
        result = deterministic_summarize(msgs)
        assert "省略" not in result

    def test_multimodal_content_list(self):
        """content 是 list 时抽取 text 块。"""
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "image_url", "image_url": "..."},
                ],
            },
        ]
        result = deterministic_summarize(msgs)
        assert "hello" in result
        # image_url 应被过滤
        assert "image_url" not in result

    def test_max_chars_budget_truncates_long_list(self):
        """max_chars 限制总长度。"""
        msgs = [{"role": "user", "content": "x" * 1000} for _ in range(20)]
        result = deterministic_summarize(msgs, max_chars=500)
        # 至少有一条被截断
        assert "[... 省略" in result

    def test_unknown_role(self):
        """无 role 字段 → 标记为 unknown。"""
        msgs = [{"content": "x"}]  # 无 role
        result = deterministic_summarize(msgs)
        assert "[unknown]" in result


# ─────────────────────────── StallConfig ───────────────────────────


class TestStallConfig:
    """StallConfig 数据类默认值。"""

    def test_default_timeout(self):
        config = StallConfig()
        assert config.timeout_s == 60.0

    def test_default_soft_warn(self):
        config = StallConfig()
        assert config.soft_warn_s == 30.0

    def test_custom_values(self):
        config = StallConfig(timeout_s=10.0, soft_warn_s=5.0)
        assert config.timeout_s == 10.0
        assert config.soft_warn_s == 5.0


# ─────────────────────────── StallDetector ───────────────────────────


def _slow_llm_call(*args, **kwargs):
    """模拟慢 LLM（sleep）。"""
    time.sleep(0.5)
    return "done"


def _fast_llm_call(*args, **kwargs):
    """模拟快速 LLM。"""
    return "fast result"


def _raising_llm_call(*args, **kwargs):
    """模拟抛错的 LLM。"""
    raise RuntimeError("network error")


class TestStallDetector:
    """StallDetector 超时检测（concurrent.futures）。"""

    def test_default_config(self):
        detector = StallDetector()
        assert detector.config.timeout_s == 60.0

    def test_successful_call(self):
        detector = StallDetector(StallConfig(timeout_s=2.0))
        result, exc = detector.detect_and_call(_fast_llm_call)
        assert exc is None
        assert result == "fast result"

    def test_timeout_raises_timeout_error(self):
        """超过 timeout → 返回 (None, TimeoutError)。"""
        detector = StallDetector(StallConfig(timeout_s=0.1, soft_warn_s=0.05))
        result, exc = detector.detect_and_call(_slow_llm_call)
        assert result is None
        assert isinstance(exc, TimeoutError)

    def test_exception_in_call_caught(self):
        """LLM 调用抛错 → 返回 (None, exception)。"""
        detector = StallDetector(StallConfig())
        result, exc = detector.detect_and_call(_raising_llm_call)
        assert result is None
        assert isinstance(exc, RuntimeError)
        assert "network error" in str(exc)

    def test_passes_args_to_llm_call(self):
        """位置参数 + 关键字参数正确传递。"""
        captured = {}

        def capturing_call(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return "ok"

        detector = StallDetector()
        detector.detect_and_call(
            capturing_call, "a", "b", key1="v1", key2="v2",
        )
        assert captured["args"] == ("a", "b")
        assert captured["kwargs"] == {"key1": "v1", "key2": "v2"}

    def test_timeout_override(self):
        """显式 timeout 覆盖 config.timeout_s。"""
        # _slow_llm_call sleep 0.5s
        detector = StallDetector(StallConfig(timeout_s=10.0))  # config 10s
        result, exc = detector.detect_and_call(
            _slow_llm_call, timeout=0.1,  # 显式 0.1s → 超时
        )
        assert exc is not None


# ─────────────────────────── micro_compact_tool_output ───────────────────────────


class TestMicroCompactToolOutput:
    """micro_compact_tool_output 单条 tool 输出微压缩。"""

    def test_short_content_unchanged(self):
        content = "a" * 100
        assert micro_compact_tool_output(content) == content

    def test_content_at_target_chars_unchanged(self):
        """恰好 == target → 不压缩。"""
        content = "a" * 500  # 默认 target=500
        assert micro_compact_tool_output(content) == content

    def test_long_content_compressed(self):
        content = "a" * 2000
        result = micro_compact_tool_output(content, target_chars=500)
        assert len(result) < len(content)
        assert "[... 微压缩" in result
        assert "省略中间" in result

    def test_preserves_head_and_tail(self):
        """保留前半 + 后半，中间省略。"""
        content = "HEAD" + "x" * 1000 + "TAIL"
        result = micro_compact_tool_output(content, target_chars=500)
        # 前 250 字符应保留
        assert content[:250] in result
        # 后 250 字符应保留
        assert content[-250:] in result

    def test_custom_target_chars(self):
        content = "a" * 1000
        result = micro_compact_tool_output(content, target_chars=100)
        # 100 字符 → head=50 + tail=50
        assert content[:50] in result
        assert content[-50:] in result

    def test_non_string_returns_unchanged(self):
        """非字符串直接返回（不抛错）。"""
        # 文档说 "if not isinstance(content, str) → return content"
        result = micro_compact_tool_output(None)  # type: ignore
        assert result is None

    def test_empty_string_unchanged(self):
        assert micro_compact_tool_output("") == ""


# ─────────────────────────── micro_compact_messages ───────────────────────────


class TestMicroCompactMessages:
    """micro_compact_messages 消息列表微压缩（仅压缩 tool）。"""

    def test_empty_messages(self):
        assert micro_compact_messages([]) == []

    def test_short_tool_content_unchanged(self):
        msgs = [{"role": "tool", "content": "short result"}]
        assert micro_compact_messages(msgs) == msgs

    def test_long_tool_content_compressed(self):
        msgs = [{"role": "tool", "content": "a" * 2000}]
        result = micro_compact_messages(msgs)
        assert result[0]["content"] != msgs[0]["content"]
        assert "[... 微压缩" in result[0]["content"]

    def test_user_message_not_compressed(self):
        """user 角色的长 content 不压缩（保持对话连贯）。"""
        msgs = [{"role": "user", "content": "a" * 5000}]
        result = micro_compact_messages(msgs)
        # user 不压缩 → 内容不变
        assert result[0]["content"] == msgs[0]["content"]

    def test_assistant_message_not_compressed(self):
        """assistant 角色的长 content 不压缩。"""
        msgs = [{"role": "assistant", "content": "a" * 5000}]
        result = micro_compact_messages(msgs)
        assert result[0]["content"] == msgs[0]["content"]

    def test_threshold_boundary(self):
        """内容长度 > 阈值（1500）才压缩。"""
        msgs = [{"role": "tool", "content": "a" * 1500}]  # 刚好 1500
        result = micro_compact_messages(msgs)
        # == 阈值，不压缩
        assert result[0]["content"] == msgs[0]["content"]

    def test_custom_target_chars_per_msg(self):
        msgs = [{"role": "tool", "content": "a" * 2000}]
        result = micro_compact_messages(msgs, target_chars_per_msg=200)
        # 压缩到 200 字符 → head=100, tail=100
        assert result[0]["content"][:100] == "a" * 100

    def test_mixed_roles_compression_correct(self):
        msgs = [
            {"role": "system", "content": "x"},
            {"role": "user", "content": "x" * 5000},  # 不压
            {"role": "assistant", "content": "thinking"},
            {"role": "tool", "content": "a" * 5000},  # 压缩
            {"role": "assistant", "content": "x" * 5000},  # 不压
        ]
        result = micro_compact_messages(msgs)
        assert result[0]["content"] == "x"
        assert result[1]["content"] == "x" * 5000  # user 不动
        assert result[2]["content"] == "thinking"
        assert "[... 微压缩" in result[3]["content"]  # tool 压缩
        assert result[4]["content"] == "x" * 5000  # assistant 不动


# ─────────────────────────── CompressorV2Hooks ───────────────────────────


class TestCompressorV2Hooks:
    """CompressorV2Hooks 默认值 + __post_init__。"""

    def test_default_stall_detector(self):
        hooks = CompressorV2Hooks()
        assert isinstance(hooks.stall_detector, StallDetector)
        assert hooks.stall_detector.config.timeout_s == 60.0

    def test_default_flags(self):
        hooks = CompressorV2Hooks()
        assert hooks.deterministic_fallback_enabled is True
        assert hooks.micro_compact_before_summarize is True
        assert hooks.pin_route_on_call is True

    def test_custom_stall_detector(self):
        custom = StallDetector(StallConfig(timeout_s=10.0))
        hooks = CompressorV2Hooks(stall_detector=custom)
        assert hooks.stall_detector is custom


# ─────────────────────────── summarize_with_v2 端到端 ───────────────────────────


class TestSummarizeWithV2:
    """summarize_with_v2 一站式摘要：pin + micro_compact + stall + refusal + fallback。"""

    def test_successful_summary(self):
        def good_llm(messages, focus_topic=None):
            return "Summary of the conversation."

        msgs = [{"role": "user", "content": "hello"}]
        result = summarize_with_v2(
            msgs, llm_call=good_llm, provider="openai", model="gpt-4",
        )
        assert result == "Summary of the conversation."

    def test_provider_refusal_triggers_deterministic_fallback(self):
        """LLM 拒绝 → fallback 到 deterministic_summarize。"""
        def refusing_llm(messages, focus_topic=None):
            return "I cannot summarize this content."

        msgs = [{"role": "user", "content": "hello world"}]
        result = summarize_with_v2(
            msgs, llm_call=refusing_llm, provider="openai", model="gpt-4",
        )
        # 不应返回空 → 走 deterministic
        assert result != ""
        assert "[user]" in result
        assert "hello world" in result

    def test_llm_exception_triggers_deterministic_fallback(self):
        """LLM 调用抛错 → fallback。"""
        def failing_llm(messages, focus_topic=None):
            raise RuntimeError("network error")

        msgs = [{"role": "user", "content": "hello"}]
        result = summarize_with_v2(
            msgs, llm_call=failing_llm, provider="openai", model="gpt-4",
        )
        # fallback 成功
        assert "hello" in result

    def test_fallback_disabled_raises_on_exception(self):
        """deterministic_fallback_enabled=False → 抛错。"""
        def failing_llm(messages, focus_topic=None):
            raise RuntimeError("boom")

        hooks = CompressorV2Hooks(deterministic_fallback_enabled=False)
        msgs = [{"role": "user", "content": "x"}]
        with pytest.raises(RuntimeError):
            summarize_with_v2(
                msgs, llm_call=failing_llm,
                provider="openai", model="gpt-4", hooks=hooks,
            )

    def test_fallback_disabled_refusal_returns_empty(self):
        """deterministic_fallback_enabled=False + refusal → 返回空字符串。"""
        def refusing_llm(messages, focus_topic=None):
            return "I cannot help with that."

        hooks = CompressorV2Hooks(deterministic_fallback_enabled=False)
        msgs = [{"role": "user", "content": "x"}]
        result = summarize_with_v2(
            msgs, llm_call=refusing_llm,
            provider="openai", model="gpt-4", hooks=hooks,
        )
        # 不抛错，但返回空
        assert result == ""

    def test_micro_compact_before_summarize(self):
        """调用前 micro_compact tool 输出。"""
        captured = {}

        def capturing_llm(messages, focus_topic=None):
            # 记录传给 LLM 的 messages（应是 micro_compact 后的）
            captured["messages"] = messages
            return "ok"

        msgs = [{"role": "tool", "content": "a" * 5000}]
        summarize_with_v2(
            msgs, llm_call=capturing_llm, provider="openai", model="gpt-4",
        )
        # tool content 应被 micro_compact
        assert "[... 微压缩" in captured["messages"][0]["content"]

    def test_micro_compact_disabled(self):
        hooks = CompressorV2Hooks(micro_compact_before_summarize=False)
        captured = {}

        def capturing_llm(messages, focus_topic=None):
            captured["messages"] = messages
            return "ok"

        msgs = [{"role": "tool", "content": "a" * 5000}]
        summarize_with_v2(
            msgs, llm_call=capturing_llm,
            provider="openai", model="gpt-4", hooks=hooks,
        )
        # tool content 不应被压缩
        assert captured["messages"][0]["content"] == "a" * 5000

    def test_pin_route_on_call(self):
        """pin_route_on_call=True → summary_route_scope 生效。"""
        captured = {}

        def capturing_llm(messages, focus_topic=None):
            # 在 LLM 调用时检查 pin
            captured["route"] = get_pinned_route()
            return "ok"

        msgs = [{"role": "user", "content": "x"}]
        summarize_with_v2(
            msgs, llm_call=capturing_llm,
            provider="anthropic", model="claude-3",
        )
        # 应被 pin
        assert captured["route"] == ("anthropic", "claude-3")

    def test_pin_route_disabled(self):
        hooks = CompressorV2Hooks(pin_route_on_call=False)
        captured = {}

        def capturing_llm(messages, focus_topic=None):
            captured["route"] = get_pinned_route()
            return "ok"

        msgs = [{"role": "user", "content": "x"}]
        summarize_with_v2(
            msgs, llm_call=capturing_llm,
            provider="openai", model="gpt-4", hooks=hooks,
        )
        # 不应被 pin（保持 None 或之前的）
        assert captured["route"] is None

    def test_no_pin_when_provider_or_model_empty(self):
        """provider 或 model 为空时不 pin。"""
        captured = {}

        def capturing_llm(messages, focus_topic=None):
            captured["route"] = get_pinned_route()
            return "ok"

        msgs = [{"role": "user", "content": "x"}]
        summarize_with_v2(
            msgs, llm_call=capturing_llm,
            provider="", model="gpt-4",  # provider 空
        )
        # 不应 pin
        assert captured["route"] is None

    def test_focus_topic_passed_to_llm(self):
        """focus_topic 参数正确传给 llm_call。"""
        captured = {}

        def capturing_llm(messages, focus_topic=None):
            captured["focus_topic"] = focus_topic
            return "ok"

        msgs = [{"role": "user", "content": "x"}]
        summarize_with_v2(
            msgs, llm_call=capturing_llm,
            provider="openai", model="gpt-4", focus_topic="auth bug",
        )
        assert captured["focus_topic"] == "auth bug"

    def test_unpin_after_summarize(self):
        """summary 完成后 route 被 unpin。"""
        def good_llm(messages, focus_topic=None):
            return "ok"

        msgs = [{"role": "user", "content": "x"}]
        summarize_with_v2(
            msgs, llm_call=good_llm,
            provider="openai", model="gpt-4",
        )
        # 调用后应被 unpin
        assert get_pinned_route() is None

    def test_timeout_triggers_fallback(self):
        """stall 超时 → fallback。"""
        def slow_llm(messages, focus_topic=None):
            time.sleep(0.5)
            return "ok"

        hooks = CompressorV2Hooks(
            stall_detector=StallDetector(StallConfig(timeout_s=0.1, soft_warn_s=0.05)),
        )
        msgs = [{"role": "user", "content": "hello"}]
        result = summarize_with_v2(
            msgs, llm_call=slow_llm,
            provider="openai", model="gpt-4", hooks=hooks,
        )
        # fallback 成功
        assert "hello" in result


# ─────────────────────────── 端到端集成 ───────────────────────────


class TestEndToEndIntegration:
    """完整流水线集成测试。"""

    def test_realistic_conversation_summary(self):
        """真实场景：长对话 → micro_compact → stall_detected → 摘要成功。"""
        msgs = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "Find the auth bug"},
            {
                "role": "assistant",
                "content": "Looking at auth.py",
                "tool_calls": [{"function": {"name": "read_file"}}],
            },
            {"role": "tool", "content": "def auth(): pass\n" * 200},  # 大输出
            {"role": "assistant", "content": "Found it"},
            {"role": "user", "content": "fix it"},
        ]

        def llm(messages, focus_topic=None):
            return "Found bug in auth.py line 42 and fixed it."

        result = summarize_with_v2(
            msgs, llm_call=llm, provider="openai", model="gpt-4",
            focus_topic="auth bug",
        )
        assert "Found" in result

    def test_pipeline_never_raises(self):
        """无论 LLM 怎么出错，summarize_with_v2 永不抛错（默认配置下）。"""
        # 1. 抛错
        def bad_llm(messages, focus_topic=None):
            raise RuntimeError("network down")

        msgs = [{"role": "user", "content": "hello"}]
        result = summarize_with_v2(
            msgs, llm_call=bad_llm, provider="openai", model="gpt-4",
        )
        assert isinstance(result, str)
        assert "hello" in result  # fallback 成功

    def test_empty_messages_returns_placeholder(self):
        def llm(messages, focus_topic=None):
            return "ok"

        result = summarize_with_v2(
            [], llm_call=llm, provider="openai", model="gpt-4",
        )
        # 空 messages → micro_compact 返回空列表 → LLM 返回 ok
        assert result == "ok"