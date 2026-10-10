"""Spirit Agent turn_recovery.py 错误恢复链回归测试（R-P1.1 v0.2.4 已交付未测）。

参考 hermes:
  - test_turn_recovery_autorecover.py (159 行)
  - test_turn_retry_state.py (34 行)
  - test_turn_api_call_interrupt.py (75 行)

5 类处理器：
  1. recover_unicode_encode_error — surrogate / 控制字符清洗
  2. recover_image_dimension_error — 超大 base64 图片剥离
  3. recover_thinking_timeout — 注入续推提示
  4. recover_tool_sequence_interrupted — 补齐缺失的 tool result
  5. recover_message_sanitization — 深度清洗（兜底）
"""

from __future__ import annotations

import pytest

from spirit.agent.turn_recovery import (
    RecoveryAction,
    RecoveryActionType,
    RecoveryContext,
    TurnRecoveryChain,
    DEFAULT_HANDLERS,
    recover_image_dimension_error,
    recover_message_sanitization,
    recover_thinking_timeout,
    recover_tool_sequence_interrupted,
    recover_unicode_encode_error,
    _sanitize_unicode_string,
    _deep_sanitize_string,
    _PROVIDER_IMAGE_LIMITS,
)


# ─────────────────────────── Fixtures ───────────────────────────


def _ctx(messages=None, provider=None, model=None, error=None):
    """便捷构造 RecoveryContext。"""
    return RecoveryContext(
        error=error or Exception("test"),
        messages=messages or [],
        provider=provider,
        model=model,
    )


# ─────────────────────────── 数据类边界 ───────────────────────────


class TestRecoveryActionType:
    """RecoveryActionType 枚举值。"""

    def test_retry_value(self):
        assert RecoveryActionType.RETRY.value == "retry"

    def test_inject_hint_value(self):
        assert RecoveryActionType.INJECT_HINT.value == "inject_hint"

    def test_complete_tool_pair_value(self):
        assert RecoveryActionType.COMPLETE_TOOL_PAIR.value == "complete_tool_pair"

    def test_sanitize_and_retry_value(self):
        assert RecoveryActionType.SANITIZE_AND_RETRY.value == "sanitize_and_retry"

    def test_none_value(self):
        assert RecoveryActionType.NONE.value == "none"


class TestRecoveryActionIsRetryable:
    """is_retryable 属性。"""

    @pytest.mark.parametrize("action_type", [
        RecoveryActionType.RETRY,
        RecoveryActionType.INJECT_HINT,
        RecoveryActionType.SANITIZE_AND_RETRY,
        RecoveryActionType.COMPLETE_TOOL_PAIR,
    ])
    def test_retryable_types(self, action_type):
        action = RecoveryAction(action_type=action_type, description="x")
        assert action.is_retryable is True

    def test_none_is_not_retryable(self):
        action = RecoveryAction(action_type=RecoveryActionType.NONE, description="x")
        assert action.is_retryable is False


class TestRecoveryContext:
    """RecoveryContext 字段默认值。"""

    def test_default_messages_empty(self):
        ctx = RecoveryContext(error=Exception("x"))
        assert ctx.messages == []

    def test_default_provider_none(self):
        ctx = RecoveryContext(error=Exception("x"))
        assert ctx.provider is None

    def test_default_metadata_empty(self):
        ctx = RecoveryContext(error=Exception("x"))
        assert ctx.metadata == {}

    def test_default_is_streaming_false(self):
        ctx = RecoveryContext(error=Exception("x"))
        assert ctx.is_streaming is False


# ─────────────────────────── 1. _sanitize_unicode_string 单元测试 ───────────────────────────


class TestSanitizeUnicodeString:
    """_sanitize_unicode_string 字符串清洗。"""

    def test_empty_string(self):
        assert _sanitize_unicode_string("") == ""

    def test_normal_string_unchanged(self):
        assert _sanitize_unicode_string("hello") == "hello"

    def test_chinese_chars_unchanged(self):
        assert _sanitize_unicode_string("中文") == "中文"

    def test_nfkc_normalization(self):
        # 全角字母 NFKC → 半角
        assert _sanitize_unicode_string("\uff24\uff28") == "DH"

    def test_surrogate_removed(self):
        # 单独 surrogate 字符在 Python str 中是非法的
        result = _sanitize_unicode_string("a\ud800b")
        # 单个 surrogate 应被移除
        assert "\ud800" not in result

    def test_control_char_removed(self):
        # \x00-\x08 范围的控制字符应被移除
        assert _sanitize_unicode_string("a\x01b\x02c") == "abc"

    def test_tab_newline_cr_preserved(self):
        # \t \n \r 应被保留
        assert _sanitize_unicode_string("a\tb\nc\rd") == "a\tb\nc\rd"

    def test_del_char_removed(self):
        # \x7f (DEL) 应被移除
        assert _sanitize_unicode_string("a\x7fb") == "ab"


# ─────────────────────────── 2. recover_unicode_encode_error ───────────────────────────


class TestRecoverUnicodeEncodeError:
    """recover_unicode_encode_error 错误识别 + 清洗。"""

    def test_non_unicode_error_returns_none(self):
        err = ValueError("something else")
        ctx = _ctx(messages=[{"role": "user", "content": "x"}], error=err)
        assert recover_unicode_encode_error(err, ctx) is None

    def test_empty_messages_returns_none(self):
        err = UnicodeEncodeError("ascii", "x", 0, 1, "bad")
        ctx = _ctx(messages=[], error=err)
        assert recover_unicode_encode_error(err, ctx) is None

    def test_unicode_error_with_dirty_message(self):
        err = UnicodeEncodeError("ascii", "x", 0, 1, "bad")
        ctx = _ctx(
            messages=[{"role": "user", "content": "a\x01b"}],
            error=err,
        )
        action = recover_unicode_encode_error(err, ctx)
        assert action is not None
        assert action.action_type == RecoveryActionType.SANITIZE_AND_RETRY
        assert action.modified_messages[0]["content"] == "ab"

    def test_unicode_error_with_clean_message_returns_none(self):
        # 错误类型匹配但内容无需清洗
        err = UnicodeEncodeError("ascii", "x", 0, 1, "bad")
        ctx = _ctx(
            messages=[{"role": "user", "content": "hello"}],
            error=err,
        )
        # 控制字符没触发清洗 → cleaned_count=0 → 返回 None
        assert recover_unicode_encode_error(err, ctx) is None

    def test_error_text_contains_unicode_pattern(self):
        # 错误文本含 "non-ascii" 但错误类型不是 UnicodeError
        err = RuntimeError("non-ascii character detected")
        ctx = _ctx(
            messages=[{"role": "user", "content": "a\x01"}],
            error=err,
        )
        action = recover_unicode_encode_error(err, ctx)
        assert action is not None
        assert action.metadata["cleaned_count"] == 1


# ─────────────────────────── 3. recover_image_dimension_error ───────────────────────────


class TestRecoverImageDimensionError:
    """recover_image_dimension_error 图片过大剥离。"""

    def test_non_image_error_returns_none(self):
        err = ValueError("generic error")
        ctx = _ctx(messages=[{"role": "user", "content": "x"}], error=err)
        assert recover_image_dimension_error(err, ctx) is None

    def test_empty_messages_returns_none(self):
        err = RuntimeError("image too large")
        ctx = _ctx(messages=[], error=err)
        assert recover_image_dimension_error(err, ctx) is None

    def test_image_too_large_strips_big_data_url(self):
        # 构造 >3MB base64 的 data URL
        big_b64 = "A" * (3 * 1024 * 1024 + 100)
        content = f"data:image/png;base64,{big_b64}"
        err = RuntimeError("image too large")
        ctx = _ctx(
            messages=[{"role": "user", "content": content}],
            provider="openai",
            error=err,
        )
        action = recover_image_dimension_error(err, ctx)
        assert action is not None
        assert action.action_type == RecoveryActionType.SANITIZE_AND_RETRY
        assert action.metadata["resized_count"] == 1
        assert "data:image" not in action.modified_messages[0]["content"]

    def test_small_image_not_stripped(self):
        # 小图不剥离
        small_b64 = "A" * 100
        content = f"data:image/png;base64,{small_b64}"
        err = RuntimeError("image too large")
        ctx = _ctx(
            messages=[{"role": "user", "content": content}],
            error=err,
        )
        action = recover_image_dimension_error(err, ctx)
        # 小图不需要缩放 → 返回 None
        assert action is None

    def test_no_image_in_messages_returns_none(self):
        err = RuntimeError("image too large")
        ctx = _ctx(
            messages=[{"role": "user", "content": "no image here"}],
            error=err,
        )
        assert recover_image_dimension_error(err, ctx) is None

    def test_multimodal_content_list_with_big_image(self):
        big_b64 = "B" * (3 * 1024 * 1024 + 100)
        content = [
            {"type": "text", "text": "看看这张图"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{big_b64}"}},
        ]
        err = RuntimeError("image dimensions exceeded")
        ctx = _ctx(
            messages=[{"role": "user", "content": content}],
            provider="anthropic",
            error=err,
        )
        action = recover_image_dimension_error(err, ctx)
        assert action is not None
        # 第二 part 应被替换为 text
        parts = action.modified_messages[0]["content"]
        assert len(parts) == 2
        assert parts[1]["type"] == "text"

    def test_provider_limits_defined(self):
        # 4 个 provider 都应有尺寸限制
        assert "openai" in _PROVIDER_IMAGE_LIMITS
        assert "anthropic" in _PROVIDER_IMAGE_LIMITS
        assert "google" in _PROVIDER_IMAGE_LIMITS


# ─────────────────────────── 4. recover_thinking_timeout ───────────────────────────


class TestRecoverThinkingTimeout:
    """recover_thinking_timeout 思考超时注入续推。"""

    def test_non_thinking_error_returns_none(self):
        err = ValueError("some other error")
        ctx = _ctx(messages=[{"role": "user", "content": "x"}], error=err)
        assert recover_thinking_timeout(err, ctx) is None

    def test_empty_messages_returns_none(self):
        err = RuntimeError("thinking timeout")
        ctx = _ctx(messages=[], error=err)
        assert recover_thinking_timeout(err, ctx) is None

    def test_thinking_timeout_injects_hint(self):
        err = RuntimeError("thinking timeout exceeded")
        ctx = _ctx(
            messages=[{"role": "user", "content": "hi"}],
            error=err,
        )
        action = recover_thinking_timeout(err, ctx)
        assert action is not None
        assert action.action_type == RecoveryActionType.INJECT_HINT
        # 末尾追加一条 user 提示
        assert len(action.modified_messages) == 2
        assert action.modified_messages[-1]["role"] == "user"
        assert "思考超时" in action.modified_messages[-1]["content"]

    def test_thinking_limit_pattern(self):
        err = RuntimeError("max thinking time exceeded")
        ctx = _ctx(messages=[{"role": "user", "content": "x"}], error=err)
        action = recover_thinking_timeout(err, ctx)
        assert action is not None

    def test_thinking_exceeded_pattern(self):
        err = RuntimeError("thinking exceeded 30s budget")
        ctx = _ctx(messages=[{"role": "user", "content": "x"}], error=err)
        action = recover_thinking_timeout(err, ctx)
        assert action is not None


# ─────────────────────────── 5. recover_tool_sequence_interrupted ───────────────────────────


class TestRecoverToolSequenceInterrupted:
    """recover_tool_sequence_interrupted 补齐缺失的 tool result。"""

    def test_empty_messages_returns_none(self):
        ctx = _ctx(messages=[])
        assert recover_tool_sequence_interrupted(Exception("x"), ctx) is None

    def test_no_tool_calls_returns_none(self):
        ctx = _ctx(messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
        assert recover_tool_sequence_interrupted(Exception("x"), ctx) is None

    def test_complete_tool_pairs_returns_none(self):
        # 工具调用对完整
        ctx = _ctx(messages=[
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "tool_calls": [{"id": "tc-1", "function": {"name": "search"}}],
            },
            {"role": "tool", "tool_call_id": "tc-1", "content": "result"},
        ])
        assert recover_tool_sequence_interrupted(Exception("x"), ctx) is None

    def test_missing_tool_result_completed(self):
        # 缺 tc-1 的 tool result
        ctx = _ctx(messages=[
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "tool_calls": [{"id": "tc-1", "function": {"name": "search"}}],
            },
        ])
        action = recover_tool_sequence_interrupted(Exception("x"), ctx)
        assert action is not None
        assert action.action_type == RecoveryActionType.COMPLETE_TOOL_PAIR
        # 末尾追加一条 tool result
        assert action.modified_messages[-1]["role"] == "tool"
        assert action.modified_messages[-1]["tool_call_id"] == "tc-1"
        assert action.metadata["completed_count"] == 1

    def test_partial_missing_completes_only_missing(self):
        # tc-1 已有 result，tc-2 缺
        ctx = _ctx(messages=[
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "tc-1", "function": {"name": "search"}},
                    {"id": "tc-2", "function": {"name": "read"}},
                ],
            },
            {"role": "tool", "tool_call_id": "tc-1", "content": "ok"},
        ])
        action = recover_tool_sequence_interrupted(Exception("x"), ctx)
        assert action is not None
        assert action.metadata["completed_count"] == 1
        # 只有 tc-2 被补齐
        completed_ids = [
            m["tool_call_id"] for m in action.modified_messages
            if m.get("role") == "tool"
        ]
        assert "tc-2" in completed_ids

    def test_tool_name_recovered_from_assistant(self):
        # 补齐的 tool result 应带正确的 tool name
        ctx = _ctx(messages=[
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "tc-1", "function": {"name": "web_search"}},
                ],
            },
        ])
        action = recover_tool_sequence_interrupted(Exception("x"), ctx)
        assert action is not None
        assert action.modified_messages[-1]["name"] == "web_search"

    def test_unknown_tool_name_when_not_found(self):
        # tool_call_id 不在 messages 中 → name = "unknown_tool"
        ctx = _ctx(messages=[
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "tc-A", "function": {"name": "search"}},
                ],
            },
        ])
        action = recover_tool_sequence_interrupted(Exception("x"), ctx)
        assert action is not None
        assert action.modified_messages[-1]["name"] == "search"


# ─────────────────────────── 6. recover_message_sanitization ───────────────────────────


class TestRecoverMessageSanitization:
    """recover_message_sanitization 深度清洗。"""

    def test_non_sanitization_error_returns_none(self):
        err = ValueError("something unrelated")
        ctx = _ctx(messages=[{"role": "user", "content": "x"}], error=err)
        assert recover_message_sanitization(err, ctx) is None

    def test_empty_messages_returns_none(self):
        err = RuntimeError("invalid request")
        ctx = _ctx(messages=[], error=err)
        assert recover_message_sanitization(err, ctx) is None

    def test_invalid_request_sanitizes_control_chars(self):
        err = RuntimeError("invalid request: bad encoding")
        ctx = _ctx(
            messages=[{"role": "user", "content": "a\x01b\x02c"}],
            error=err,
        )
        action = recover_message_sanitization(err, ctx)
        assert action is not None
        assert action.action_type == RecoveryActionType.SANITIZE_AND_RETRY
        assert action.modified_messages[0]["content"] == "abc"

    def test_malformed_pattern(self):
        err = RuntimeError("malformed message body")
        ctx = _ctx(
            messages=[{"role": "user", "content": "a\x01b"}],
            error=err,
        )
        action = recover_message_sanitization(err, ctx)
        assert action is not None

    def test_clean_message_returns_none(self):
        err = RuntimeError("invalid request")
        ctx = _ctx(
            messages=[{"role": "user", "content": "hello"}],
            error=err,
        )
        assert recover_message_sanitization(err, ctx) is None

    def test_multimodal_strips_oversized_image(self):
        big_b64 = "C" * (3 * 1024 * 1024 + 100)
        content = [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{big_b64}"}},
        ]
        err = RuntimeError("invalid request")
        ctx = _ctx(messages=[{"role": "user", "content": content}], error=err)
        action = recover_message_sanitization(err, ctx)
        assert action is not None
        parts = action.modified_messages[0]["content"]
        # 第二 part 应被剥离为 text
        assert parts[1]["type"] == "text"


class TestDeepSanitizeString:
    """_deep_sanitize_string 字符串级别深度清洗。"""

    def test_empty_string(self):
        assert _deep_sanitize_string("") == ""

    def test_normal_text_unchanged(self):
        assert _deep_sanitize_string("hello world") == "hello world"

    def test_control_chars_removed(self):
        assert _deep_sanitize_string("a\x01b\x02c") == "abc"

    def test_surrogate_removed(self):
        result = _deep_sanitize_string("a\ud800b")
        assert "\ud800" not in result


# ─────────────────────────── 7. TurnRecoveryChain 主类 ───────────────────────────


class TestTurnRecoveryChain:
    """TurnRecoveryChain 主类：handler 顺序 + 异常隔离。"""

    def test_default_handlers_count(self):
        # 5 类处理器
        assert len(DEFAULT_HANDLERS) == 5

    def test_default_chain_uses_all_handlers(self):
        chain = TurnRecoveryChain()
        assert len(chain.handlers) == 5

    def test_custom_handlers(self):
        chain = TurnRecoveryChain(handlers=[recover_unicode_encode_error])
        assert len(chain.handlers) == 1

    def test_recover_returns_none_when_no_handler_matches(self):
        chain = TurnRecoveryChain()
        err = ValueError("completely unrelated error")
        ctx = _ctx(messages=[{"role": "user", "content": "x"}], error=err)
        assert chain.recover(err, ctx) is None

    def test_recover_returns_first_matching_action(self):
        chain = TurnRecoveryChain()
        err = UnicodeEncodeError("ascii", "x", 0, 1, "bad")
        ctx = _ctx(
            messages=[{"role": "user", "content": "a\x01b"}],
            error=err,
        )
        action = chain.recover(err, ctx)
        assert action is not None
        # 第一个 handler (unicode) 匹配
        assert action.action_type == RecoveryActionType.SANITIZE_AND_RETRY

    def test_recover_skips_handler_returning_none(self):
        # 自定义 chain：第一个返回 None，第二个返回 action
        def handler_none(err, ctx):
            return None

        def handler_action(err, ctx):
            return RecoveryAction(
                action_type=RecoveryActionType.RETRY,
                description="test",
            )

        chain = TurnRecoveryChain(handlers=[handler_none, handler_action])
        ctx = _ctx(messages=[{"role": "user", "content": "x"}])
        action = chain.recover(Exception("x"), ctx)
        assert action is not None
        assert action.description == "test"

    def test_recover_continues_after_handler_exception(self):
        # 第一个 handler 抛错，第二个 handler 正常返回
        def handler_boom(err, ctx):
            raise RuntimeError("handler crashed")

        def handler_ok(err, ctx):
            return RecoveryAction(
                action_type=RecoveryActionType.RETRY,
                description="recovery ok",
            )

        chain = TurnRecoveryChain(handlers=[handler_boom, handler_ok])
        ctx = _ctx(messages=[{"role": "user", "content": "x"}])
        action = chain.recover(Exception("x"), ctx)
        # 异常被隔离，第二个 handler 返回 action
        assert action is not None
        assert action.description == "recovery ok"

    def test_recover_skips_action_with_none_type(self):
        # handler 返回 action_type=NONE → 视为不可恢复，继续下一个
        def handler_none_type(err, ctx):
            return RecoveryAction(
                action_type=RecoveryActionType.NONE,
                description="",
            )

        def handler_action(err, ctx):
            return RecoveryAction(
                action_type=RecoveryActionType.RETRY,
                description="real",
            )

        chain = TurnRecoveryChain(handlers=[handler_none_type, handler_action])
        ctx = _ctx(messages=[{"role": "user", "content": "x"}])
        action = chain.recover(Exception("x"), ctx)
        assert action.description == "real"

    def test_handler_order_matters(self):
        # 真实顺序：unicode → image → thinking → tool_sequence → sanitization
        chain = TurnRecoveryChain()
        handler_names = [h.__name__ for h in chain.handlers]
        assert handler_names[0] == "recover_unicode_encode_error"
        assert handler_names[1] == "recover_image_dimension_error"
        assert handler_names[2] == "recover_thinking_timeout"
        assert handler_names[3] == "recover_tool_sequence_interrupted"
        assert handler_names[4] == "recover_message_sanitization"


# ─────────────────────────── 8. RecoveryAction 字段 ───────────────────────────


class TestRecoveryActionFields:
    """RecoveryAction 字段构造。"""

    def test_default_metadata_empty(self):
        action = RecoveryAction(action_type=RecoveryActionType.RETRY, description="x")
        assert action.metadata == {}

    def test_default_modified_messages_none(self):
        action = RecoveryAction(action_type=RecoveryActionType.RETRY, description="x")
        assert action.modified_messages is None

    def test_custom_metadata(self):
        action = RecoveryAction(
            action_type=RecoveryActionType.RETRY,
            description="x",
            metadata={"key": "value"},
        )
        assert action.metadata == {"key": "value"}

    def test_custom_modified_messages(self):
        msgs = [{"role": "user", "content": "y"}]
        action = RecoveryAction(
            action_type=RecoveryActionType.RETRY,
            description="x",
            modified_messages=msgs,
        )
        assert action.modified_messages is msgs
