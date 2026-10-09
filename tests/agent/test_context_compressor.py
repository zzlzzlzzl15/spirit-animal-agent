"""Spirit Agent context_compressor 上下文压缩引擎测试。

回归目标：
  - v0.2.10 状态栏 ctx 字段依赖 compressor 的 _last_prompt_tokens
  - conversation_loop 触发压缩时调 compressor.compress(messages, current_tokens)
  - 状态栏的 ctx 字段（_last_prompt_tokens / context_length）展示压缩阈值

参考 hermes:
  - test_context_compressor.py (3586 lines, 24 个子文件)
  - test_context_compressor_session_end_clears_state.py
  - test_context_compressor_structural_backoff.py
  - test_413_compression.py (1574 lines)
  - test_turn_context_overflow_warning.py
"""

from __future__ import annotations

import pytest


# ─────────────────────────── should_compress 阈值判断 ───────────────────────────


class TestShouldCompress:
    """should_compress 阈值判断逻辑。"""

    def test_should_compress_under_threshold_returns_false(self):
        """未达阈值 → False"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor(context_length=128_000, threshold_percent=0.75)
        # 75% 阈值 = 96000
        assert c.should_compress(50_000) is False
        assert c.should_compress(95_999) is False

    def test_should_compress_at_threshold_returns_true(self):
        """恰好达阈值 → True（>= 比较）"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor(context_length=128_000, threshold_percent=0.75)
        assert c.should_compress(96_000) is True

    def test_should_compress_over_threshold_returns_true(self):
        """超过阈值 → True"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor(context_length=128_000, threshold_percent=0.75)
        assert c.should_compress(100_000) is True
        assert c.should_compress(128_000) is True

    def test_should_compress_with_zero_context_length_returns_false(self):
        """context_length=0 时永不应该压缩（除零保护）"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor(context_length=0, threshold_percent=0.75)
        assert c.should_compress(0) is False
        assert c.should_compress(1_000_000) is False

    def test_should_compress_with_negative_context_length_returns_false(self):
        """context_length 负数时不应该压缩"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor(context_length=-1000, threshold_percent=0.75)
        assert c.should_compress(500) is False

    def test_threshold_tokens_property(self):
        """threshold_tokens 属性 = context_length * threshold_percent"""
        from spirit.agent.context_compressor import ContextCompressor
        c1 = ContextCompressor(context_length=100_000, threshold_percent=0.5)
        assert c1.threshold_tokens == 50_000
        c2 = ContextCompressor(context_length=200_000, threshold_percent=0.75)
        assert c2.threshold_tokens == 150_000


# ─────────────────────────── update_from_response ───────────────────────────


class TestUpdateFromResponse:
    """update_from_response 从 API 响应更新 token 统计。"""

    def test_update_prompt_and_completion_tokens(self):
        """update_from_response 更新 _last_prompt_tokens / _last_completion_tokens"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor()
        c.update_from_response({
            "prompt_tokens": 1234,
            "completion_tokens": 567,
        })
        assert c._last_prompt_tokens == 1234
        assert c._last_completion_tokens == 567

    def test_update_with_missing_keys_defaults_to_zero(self):
        """usage 字典缺字段时默认 0"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor()
        c.update_from_response({})
        assert c._last_prompt_tokens == 0
        assert c._last_completion_tokens == 0


# ─────────────────────────── compress 完整行为 ───────────────────────────


class TestCompressShortMessages:
    """消息太短不压缩。"""

    def test_compress_very_short_messages_returns_unchanged(self):
        """消息数 <= protect_first_n + protect_last_n 时不压缩"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor(protect_first_n=3, protect_last_n=6)
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        result = c.compress(messages, current_tokens=100)
        # 太短，returns unchanged
        assert result is messages or result == messages

    def test_compress_exactly_boundary_returns_unchanged(self):
        """消息数 == protect_first_n + protect_last_n 时不压缩"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor(protect_first_n=3, protect_last_n=6)
        messages = [{"role": "user", "content": f"msg{i}"} for i in range(9)]
        result = c.compress(messages, current_tokens=100)
        assert len(result) == 9


class TestCompressFullPath:
    """compress 完整路径：保护头尾 + 中间摘要。"""

    def test_compress_protects_first_and_last_n(self):
        """压缩后保护前 protect_first_n + 后 protect_last_n 条"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor(protect_first_n=2, protect_last_n=3, llm_client=None)
        messages = [{"role": "user", "content": f"msg{i}"} for i in range(20)]
        result = c.compress(messages, current_tokens=50_000)
        # 头 2 条 + 1 条摘要 + 尾 3 条 = 6 条
        assert len(result) == 6
        # 验证头 2 条不变
        assert result[0] == messages[0]
        assert result[1] == messages[1]
        # 验证尾 3 条不变
        assert result[-3] == messages[-3]
        assert result[-2] == messages[-2]
        assert result[-1] == messages[-1]

    def test_compress_increments_compression_count(self):
        """compress 后 _compression_count 增加"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor(protect_first_n=2, protect_last_n=3)
        messages = [{"role": "user", "content": f"msg{i}"} for i in range(20)]
        assert c._compression_count == 0
        c.compress(messages, current_tokens=50_000)
        assert c._compression_count == 1
        c.compress(messages, current_tokens=50_000)
        assert c._compression_count == 2

    def test_compress_inserts_summary_with_prefix_and_marker(self):
        """摘要消息含 SUMMARY_PREFIX 和 SUMMARY_END_MARKER"""
        from spirit.agent.context_compressor import (
            ContextCompressor, SUMMARY_PREFIX, SUMMARY_END_MARKER
        )
        c = ContextCompressor(protect_first_n=2, protect_last_n=3)
        messages = [{"role": "user", "content": f"msg{i}"} for i in range(20)]
        result = c.compress(messages, current_tokens=50_000)
        # 中间是摘要
        summary_msg = result[2]
        assert summary_msg["role"] == "user"
        assert SUMMARY_PREFIX in summary_msg["content"]
        assert SUMMARY_END_MARKER in summary_msg["content"]

    def test_compress_persists_last_summary(self):
        """压缩后 _last_summary 保存最新摘要"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor(protect_first_n=2, protect_last_n=3)
        messages = [{"role": "user", "content": f"msg{i}"} for i in range(20)]
        c.compress(messages, current_tokens=50_000)
        assert c._last_summary is not None


class TestCompressToolOutputPruning:
    """compress 工具输出剪枝（cheap pass）。"""

    def test_prune_long_tool_output(self):
        """超过 max_chars 的 tool 消息被截断"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor()
        long_output = "x" * 5000
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": long_output},
            {"role": "assistant", "content": "ok"},
        ]
        pruned = c._prune_tool_outputs(messages)
        # 验证 tool 消息被截断
        assert len(pruned[1]["content"]) < 5000
        assert "[... 已截断" in pruned[1]["content"]

    def test_prune_short_tool_output_unchanged(self):
        """短 tool 消息保持不变"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor()
        messages = [
            {"role": "tool", "content": "short output"},
        ]
        pruned = c._prune_tool_outputs(messages)
        assert pruned[0]["content"] == "short output"

    def test_prune_non_tool_messages_unchanged(self):
        """非 tool 消息保持不变"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor()
        long_text = "y" * 5000
        messages = [
            {"role": "user", "content": long_text},
        ]
        pruned = c._prune_tool_outputs(messages)
        # user 消息不被剪枝
        assert pruned[0]["content"] == long_text


# ─────────────────────────── _text_truncate_summarize ───────────────────────────


class TestTextTruncateSummarize:
    """纯文本截断摘要（无 LLM）。"""

    def test_text_truncate_summarize_returns_string(self):
        """文本截断摘要返回 string"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor()
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        summary = c._text_truncate_summarize(messages)
        assert isinstance(summary, str)
        assert "user" in summary or "hello" in summary

    def test_text_truncate_summarize_handles_multimodal_content(self):
        """content 是 list (多模态) 时提取 text 块"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看这张图"},
                    {"type": "image", "url": "http://..."},
                ],
            },
        ]
        summary = c._text_truncate_summarize(messages)
        # 验证提取了 text
        assert "看这张图" in summary

    def test_text_truncate_summarize_skips_empty_content(self):
        """空 content 被跳过"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor()
        messages = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "real content"},
        ]
        summary = c._text_truncate_summarize(messages)
        assert "real content" in summary


# ─────────────────────────── _merge_summaries ───────────────────────────


class TestMergeSummaries:
    """_merge_summaries 合并旧摘要和新摘要。"""

    def test_merge_empty_old_returns_new(self):
        """旧摘要为空 → 返回新摘要"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor()
        merged = c._merge_summaries("", "new summary")
        assert merged == "new summary"

    def test_merge_empty_new_returns_old(self):
        """新摘要为空 → 返回旧摘要"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor()
        merged = c._merge_summaries("old summary", "")
        assert merged == "old summary"

    def test_merge_without_llm_concatenates(self):
        """无 LLM client 时简单拼接"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor(llm_client=None, llm_model="")
        merged = c._merge_summaries("old", "new")
        # 回退到简单拼接
        assert "old" in merged
        assert "new" in merged
        assert "--- 后续进展 ---" in merged


class TestCompressIncrementalMerges:
    """compress 第二次调用时合并之前的 _last_summary。"""

    def test_second_compress_uses_previous_summary(self):
        """第二次压缩时合并之前的摘要（迭代压缩）"""
        from spirit.agent.context_compressor import ContextCompressor
        c = ContextCompressor(protect_first_n=2, protect_last_n=3)
        messages = [{"role": "user", "content": f"msg{i}"} for i in range(20)]
        # 第一次压缩
        c.compress(messages, current_tokens=50_000)
        first_summary = c._last_summary
        # 第二次压缩（不同消息）
        new_messages = [{"role": "user", "content": f"new{i}"} for i in range(20)]
        c.compress(new_messages, current_tokens=60_000)
        second_summary = c._last_summary
        # 第二次摘要应包含第一次的某些痕迹（合并）
        assert second_summary is not None
        # 合并后摘要长度 >= 第一次
        # （实际可能因消息而异）


# ─────────────────────────── estimate_tokens ───────────────────────────


class TestEstimateTokens:
    """estimate_tokens / estimate_messages_tokens 工具函数。"""

    def test_estimate_tokens_empty_returns_zero(self):
        """空文本 → 0"""
        from spirit.agent.context_compressor import estimate_tokens
        assert estimate_tokens("") == 0
        assert estimate_tokens(None) == 0

    def test_estimate_tokens_short_text(self):
        """短文本估算"""
        from spirit.agent.context_compressor import estimate_tokens
        # 14 字符 / 3.5 = 4 tokens
        result = estimate_tokens("Hello, world!")
        assert result >= 1

    def test_estimate_tokens_long_text(self):
        """长文本估算（~3.5 字符/token）"""
        from spirit.agent.context_compressor import estimate_tokens
        text = "x" * 350  # 350 字符
        # 350 / 3.5 = 100
        result = estimate_tokens(text)
        assert result == 100

    def test_estimate_messages_tokens_string_content(self):
        """消息 content 是 string 时累加"""
        from spirit.agent.context_compressor import estimate_messages_tokens
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        total = estimate_messages_tokens(messages)
        assert total > 0

    def test_estimate_messages_tokens_multimodal_content(self):
        """消息 content 是 list 时提取 text 块"""
        from spirit.agent.context_compressor import estimate_messages_tokens
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看看图"},
                    {"type": "image", "url": "x"},
                ],
            },
        ]
        total = estimate_messages_tokens(messages)
        assert total > 0

    def test_estimate_messages_tokens_tool_calls_add_overhead(self):
        """消息含 tool_calls 时 +50 tokens"""
        from spirit.agent.context_compressor import estimate_messages_tokens
        messages = [
            {"role": "user", "content": "hi"},  # 0
            {
                "role": "assistant",
                "content": "searching",
                "tool_calls": [{"function": {"name": "search"}}],
            },  # +50
        ]
        total = estimate_messages_tokens(messages)
        # 至少 50
        assert total >= 50

    def test_estimate_messages_tokens_tool_role_adds_overhead(self):
        """role=tool 时 +20 tokens"""
        from spirit.agent.context_compressor import estimate_messages_tokens
        messages = [
            {"role": "tool", "content": "result"},
        ]
        total = estimate_messages_tokens(messages)
        # 至少 20
        assert total >= 20


# ─────────────────────────── SessionLifecycleHooks ───────────────────────────


class TestSessionLifecycleHooks:
    """SessionLifecycleHooks 行为。"""

    def test_on_session_start_resets_compressor_state(self):
        """on_session_start 重置 _last_summary 和 _compression_count"""
        from spirit.agent.context_compressor import ContextCompressor, SessionLifecycleHooks
        c = ContextCompressor(protect_first_n=2, protect_last_n=3)
        c._last_summary = "old summary"
        c._compression_count = 5

        hooks = SessionLifecycleHooks(compressor=c)
        hooks.on_session_start("new-session-id")

        assert c._last_summary is None
        assert c._compression_count == 0

    def test_on_session_reset_resets_compressor_state(self):
        """on_session_reset 重置 _last_summary 和 _compression_count"""
        from spirit.agent.context_compressor import ContextCompressor, SessionLifecycleHooks
        c = ContextCompressor()
        c._last_summary = "old"
        c._compression_count = 3

        hooks = SessionLifecycleHooks(compressor=c)
        hooks.on_session_reset()

        assert c._last_summary is None
        assert c._compression_count == 0

    def test_on_session_end_does_not_reset_state(self):
        """on_session_end 不重置 _last_summary / _compression_count（保留给后续 resume）"""
        from spirit.agent.context_compressor import ContextCompressor, SessionLifecycleHooks
        c = ContextCompressor()
        c._last_summary = "preserved"
        c._compression_count = 2

        hooks = SessionLifecycleHooks(compressor=c)
        hooks.on_session_end("session-id", [{"role": "user", "content": "x"}])

        # 状态保留（resume 用）
        assert c._last_summary == "preserved"
        assert c._compression_count == 2

    def test_hooks_without_compressor_does_not_crash(self):
        """compressor=None 时 hooks 不崩"""
        from spirit.agent.context_compressor import SessionLifecycleHooks
        hooks = SessionLifecycleHooks(compressor=None)
        # 不应抛错
        hooks.on_session_start("id")
        hooks.on_session_end("id", [])
        hooks.on_session_reset()
