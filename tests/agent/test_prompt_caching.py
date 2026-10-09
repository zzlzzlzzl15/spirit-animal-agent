"""Spirit Agent prompt_caching 行为测试。

回归目标：
  - Spirit prompt_caching 注入 cache_control 断点（system_and_3 策略）
  - 节省多轮对话输入 token 成本（~75%）
  - 兼容原生 Anthropic / OpenRouter 两种布局

参考 hermes:
  - test_anthropic_prompt_cache_policy.py (1035 lines)
"""

from __future__ import annotations

import pytest


# ─────────────────────────── _build_marker ───────────────────────────


class TestBuildMarker:
    """_build_marker 构建 cache_control 标记。"""

    def test_build_marker_5m(self):
        """ttl='5m' → marker = {"type": "ephemeral"} (无 ttl 字段)"""
        from spirit.agent.prompt_caching import _build_marker
        marker = _build_marker("5m")
        assert marker == {"type": "ephemeral"}

    def test_build_marker_1h(self):
        """ttl='1h' → marker 含 ttl='1h'"""
        from spirit.agent.prompt_caching import _build_marker
        marker = _build_marker("1h")
        assert marker == {"type": "ephemeral", "ttl": "1h"}


# ─────────────────────────── apply_cache_control ───────────────────────────


class TestApplyCacheControlEmpty:
    """空消息列表。"""

    def test_empty_messages_returns_empty(self):
        """apply_cache_control([]) → []"""
        from spirit.agent.prompt_caching import apply_cache_control
        result = apply_cache_control([])
        assert result == []


class TestApplyCacheControlSystemAndThree:
    """system_and_3 策略：最多 4 个断点。"""

    def test_only_system_message(self):
        """只有 system 消息 → 1 个断点"""
        from spirit.agent.prompt_caching import apply_cache_control
        messages = [{"role": "system", "content": "you are helpful"}]
        result = apply_cache_control(messages)
        # 验证 system 消息有 cache_control（顶层或 content 内）
        assert _has_cache_control(result[0])

    def test_system_plus_3_messages_4_breakpoints(self):
        """system + 3 条消息 → 4 个断点（1 system + 3 last）"""
        from spirit.agent.prompt_caching import apply_cache_control
        messages = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "how are you?"},
        ]
        result = apply_cache_control(messages)
        # 验证 4 个断点
        count = sum(1 for m in result if _has_cache_control(m))
        assert count == 4

    def test_system_plus_5_messages_4_breakpoints(self):
        """system + 5 条消息 → 还是 4 个断点（system + 末尾 3）"""
        from spirit.agent.prompt_caching import apply_cache_control
        messages = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "msg1"},
            {"role": "assistant", "content": "r1"},
            {"role": "user", "content": "msg2"},
            {"role": "assistant", "content": "r2"},
            {"role": "user", "content": "msg3"},
        ]
        result = apply_cache_control(messages)
        # 还是 4 个断点
        count = sum(1 for m in result if _has_cache_control(m))
        assert count == 4

    def test_no_system_message_4_breakpoints(self):
        """无 system 消息 → 4 个断点（末尾 4 条都能 carry marker）"""
        from spirit.agent.prompt_caching import apply_cache_control
        messages = [
            {"role": "user", "content": "m1"},
            {"role": "assistant", "content": "r1"},
            {"role": "user", "content": "m2"},
            {"role": "assistant", "content": "r2"},
            {"role": "user", "content": "m3"},
        ]
        result = apply_cache_control(messages)
        count = sum(1 for m in result if _has_cache_control(m))
        assert count == 4

    def test_no_system_message_3_messages_3_breakpoints(self):
        """无 system + 只有 3 条消息 → 3 个断点（受消息数限制）"""
        from spirit.agent.prompt_caching import apply_cache_control
        messages = [
            {"role": "user", "content": "m1"},
            {"role": "assistant", "content": "r1"},
            {"role": "user", "content": "m2"},
        ]
        result = apply_cache_control(messages)
        count = sum(1 for m in result if _has_cache_control(m))
        assert count == 3


class TestApplyCacheControlContentConversion:
    """content 字符串 → 列表转换。"""

    def test_string_content_converted_to_list(self):
        """content 是字符串时被转为带 cache_control 的 block 列表"""
        from spirit.agent.prompt_caching import apply_cache_control
        messages = [{"role": "user", "content": "hello"}]
        result = apply_cache_control(messages)
        # content 变成 list
        assert isinstance(result[0]["content"], list)
        assert result[0]["content"][0]["type"] == "text"
        assert result[0]["content"][0]["text"] == "hello"
        assert "cache_control" in result[0]["content"][0]


class TestApplyCacheControlNativeVsProxy:
    """原生 Anthropic vs OpenRouter 代理布局差异。"""

    def test_native_anthropic_tool_message_top_level_marker(self):
        """native_anthropic=True + role=tool → 顶层 cache_control"""
        from spirit.agent.prompt_caching import apply_cache_control
        messages = [
            {"role": "tool", "content": "result", "tool_call_id": "abc"},
        ]
        result = apply_cache_control(messages, native_anthropic=True)
        # 顶层有 cache_control
        assert "cache_control" in result[0]
        # content 不变
        assert result[0]["content"] == "result"

    def test_proxy_layout_tool_message_keeps_content(self):
        """native_anthropic=False + role=tool → content 字符串变 list 承载 marker"""
        from spirit.agent.prompt_caching import apply_cache_control
        messages = [
            {"role": "tool", "content": "result", "tool_call_id": "abc"},
        ]
        result = apply_cache_control(messages, native_anthropic=False)
        # OpenRouter: 字符串 content 转 list
        # role=tool 在 OpenRouter 代理布局下也会被转 list（按 _apply_cache_marker 逻辑）
        # 验证至少有 cache_control（顶层或 content 内）
        assert _has_cache_control(result[0])

    def test_does_not_mutate_original(self):
        """apply_cache_control 深拷贝，不修改原 messages"""
        from spirit.agent.prompt_caching import apply_cache_control
        original = [{"role": "user", "content": "hi"}]
        apply_cache_control(original)
        # 原 messages 不变
        assert original[0]["content"] == "hi"
        assert "cache_control" not in original[0]


class TestApplyCacheControlTtl:
    """ttl 参数透传。"""

    def test_5m_ttl_no_ttl_field(self):
        """cache_ttl='5m' 时 marker 无 ttl 字段"""
        from spirit.agent.prompt_caching import apply_cache_control
        messages = [{"role": "user", "content": "hi"}]
        result = apply_cache_control(messages, cache_ttl="5m")
        # 找到 marker
        marker = _find_first_marker(result[0])
        assert marker == {"type": "ephemeral"}

    def test_1h_ttl_has_ttl_field(self):
        """cache_ttl='1h' 时 marker 含 ttl='1h'"""
        from spirit.agent.prompt_caching import apply_cache_control
        messages = [{"role": "user", "content": "hi"}]
        result = apply_cache_control(messages, cache_ttl="1h")
        marker = _find_first_marker(result[0])
        assert marker == {"type": "ephemeral", "ttl": "1h"}


# ─────────────────────────── should_use_prompt_caching ───────────────────────────


class TestShouldUsePromptCaching:
    """should_use_prompt_caching 决策。"""

    def test_anthropic_provider_returns_native(self):
        """provider='anthropic' → (True, True)"""
        from spirit.agent.prompt_caching import should_use_prompt_caching
        should, native = should_use_prompt_caching(
            provider="anthropic", model="claude-3-5-sonnet", base_url="",
        )
        assert should is True
        assert native is True

    def test_openrouter_with_claude_uses_proxy(self):
        """provider='openrouter' + claude 模型 → (True, False)"""
        from spirit.agent.prompt_caching import should_use_prompt_caching
        should, native = should_use_prompt_caching(
            provider="openrouter", model="claude-3-5-sonnet", base_url="",
        )
        assert should is True
        assert native is False

    def test_openrouter_with_non_claude_no_cache(self):
        """provider='openrouter' + 非 claude 模型 → (False, False)"""
        from spirit.agent.prompt_caching import should_use_prompt_caching
        should, native = should_use_prompt_caching(
            provider="openrouter", model="gpt-4o", base_url="",
        )
        assert should is False
        assert native is False

    def test_base_url_openrouter_with_claude(self):
        """base_url 含 openrouter + claude → (True, False)"""
        from spirit.agent.prompt_caching import should_use_prompt_caching
        should, native = should_use_prompt_caching(
            provider="custom", model="claude-3-5-sonnet", base_url="https://openrouter.ai",
        )
        assert should is True
        assert native is False

    def test_other_provider_with_claude_uses_proxy(self):
        """其他 provider + claude 模型 → (True, False) 代理布局"""
        from spirit.agent.prompt_caching import should_use_prompt_caching
        should, native = should_use_prompt_caching(
            provider="custom_proxy", model="claude-3-5-sonnet", base_url="",
        )
        assert should is True
        assert native is False

    def test_gpt_model_no_cache(self):
        """gpt 模型 → (False, False)"""
        from spirit.agent.prompt_caching import should_use_prompt_caching
        should, native = should_use_prompt_caching(
            provider="openai", model="gpt-4o", base_url="",
        )
        assert should is False
        assert native is False

    def test_empty_inputs_default_no_cache(self):
        """空输入 → (False, False)"""
        from spirit.agent.prompt_caching import should_use_prompt_caching
        should, native = should_use_prompt_caching()
        assert should is False
        assert native is False

    def test_case_insensitive_provider(self):
        """provider 大小写不敏感"""
        from spirit.agent.prompt_caching import should_use_prompt_caching
        should1, native1 = should_use_prompt_caching(
            provider="Anthropic", model="claude-3-5-sonnet", base_url="",
        )
        should2, native2 = should_use_prompt_caching(
            provider="ANTHROPIC", model="claude-3-5-sonnet", base_url="",
        )
        assert should1 == should2 == True
        assert native1 == native2 == True


# ─────────────────────────── _can_carry_marker ───────────────────────────


class TestCanCarryMarker:
    """_can_carry_marker 边界。"""

    def test_native_anthropic_allows_all(self):
        """native_anthropic=True 时所有消息都能承载 marker"""
        from spirit.agent.prompt_caching import _can_carry_marker
        assert _can_carry_marker({"role": "tool", "content": ""}, True) is True
        assert _can_carry_marker({"role": "assistant", "content": ""}, True) is True
        assert _can_carry_marker({"role": "user", "content": "hi"}, True) is True

    def test_proxy_rejects_empty_content(self):
        """native_anthropic=False 时空 content 消息不能承载 marker"""
        from spirit.agent.prompt_caching import _can_carry_marker
        assert _can_carry_marker({"role": "user", "content": ""}, False) is False
        assert _can_carry_marker({"role": "user", "content": None}, False) is False

    def test_proxy_allows_non_empty_string(self):
        """native_anthropic=False 时非空 string content 可承载"""
        from spirit.agent.prompt_caching import _can_carry_marker
        assert _can_carry_marker({"role": "user", "content": "hi"}, False) is True

    def test_proxy_allows_list_with_dict_last(self):
        """native_anthropic=False 时 list content 且最后是 dict 可承载"""
        from spirit.agent.prompt_caching import _can_carry_marker
        msg = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
        assert _can_carry_marker(msg, False) is True

    def test_proxy_rejects_empty_list(self):
        """native_anthropic=False 时空 list 不能承载"""
        from spirit.agent.prompt_caching import _can_carry_marker
        assert _can_carry_marker({"role": "user", "content": []}, False) is False


# ─────────────────────────── 工具函数 ───────────────────────────


def _has_cache_control(msg: dict) -> bool:
    """检查消息（顶层或 content 内）是否有 cache_control。"""
    if "cache_control" in msg:
        return True
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and "cache_control" in block:
                return True
    return False


def _find_first_marker(msg: dict) -> dict:
    """找消息中第一个 cache_control marker。"""
    if "cache_control" in msg:
        return msg["cache_control"]
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and "cache_control" in block:
                return block["cache_control"]
    return {}
