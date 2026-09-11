"""tests/agent/test_transports.py — Transport 层全测试。

覆盖：
- 基类（TransportConfig/TransportError/TransportResponse/ToolCallData/UsageData）
- 工具函数（build_tool_call/map_finish_reason）
- 消息清洗（_sanitize_messages）
- 工厂（auto 检测 + 全 Provider 注册）
- OpenAI Transport（reasoning/refusal/cache_stats/validate）
- Anthropic Transport（thinking 块/cache_stats/validate）
- Gemini Transport（消息转换/工具转换/错误包装）
- Bedrock Transport（消息转换/modelId 解析/错误包装）
"""

import json
import pytest
from unittest.mock import MagicMock, patch
from spirit.agent.transports.base import (
    Transport, TransportConfig, TransportError, TransportResponse,
    ToolCallData, UsageData, RateLimitError, AuthenticationError,
    build_tool_call, map_finish_reason,
)


class TestTransportConfig:
    """TransportConfig 测试。"""

    def test_default_config(self):
        config = TransportConfig()
        assert config.provider == "openai"
        assert config.model == "gpt-4o"
        assert config.streaming is True
        assert config.tools is True
        assert config.reasoning is None

    def test_custom_config(self):
        config = TransportConfig(
            provider="anthropic",
            model="claude-3-opus",
            api_key="sk-ant-test",
        )
        assert config.provider == "anthropic"
        assert config.model == "claude-3-opus"

    def test_reasoning_config(self):
        config = TransportConfig(
            reasoning={"enabled": True, "effort": "high"}
        )
        assert config.reasoning["enabled"] is True
        assert config.reasoning["effort"] == "high"


class TestTransportError:
    """TransportError 测试。"""

    def test_basic_error(self):
        err = TransportError("something failed")
        assert str(err) == "something failed"
        assert err.retryable is False

    def test_retryable_error(self):
        err = TransportError("timeout", retryable=True, status_code=504)
        assert err.retryable is True
        assert err.status_code == 504

    def test_rate_limit_error(self):
        err = RateLimitError(retry_after=30.0)
        assert err.retryable is True
        assert err.retry_after == 30.0

    def test_auth_error(self):
        err = AuthenticationError()
        assert err.retryable is False


class TestTransportResponse:
    """TransportResponse 测试（包含新增的 reasoning/provider_data）。"""

    def test_text_response(self):
        resp = TransportResponse(content="Hello!", finish_reason="stop")
        assert resp.content == "Hello!"
        assert resp.tool_calls == []
        assert resp.finish_reason == "stop"

    def test_tool_call_response(self):
        resp = TransportResponse(
            content="",
            tool_calls=[{"id": "1", "name": "read_file", "args": "{}"}],
        )
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0]["name"] == "read_file"

    def test_usage(self):
        resp = TransportResponse(
            content="ok",
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        )
        assert resp.usage["total_tokens"] == 150

    def test_reasoning_fields(self):
        resp = TransportResponse(
            content="Answer",
            reasoning="Let me think...",
            reasoning_content="DeepSeek reasoning...",
        )
        assert resp.reasoning == "Let me think..."
        assert resp.reasoning_content == "DeepSeek reasoning..."

    def test_provider_data(self):
        resp = TransportResponse(
            content="ok",
            provider_data={"reasoning_details": [{"type": "thinking"}]},
        )
        assert resp.reasoning_details == [{"type": "thinking"}]

    def test_refusal_property(self):
        resp = TransportResponse(
            content="",
            provider_data={"refusal": "I cannot help with that"},
        )
        assert resp.refusal == "I cannot help with that"

    def test_no_provider_data(self):
        resp = TransportResponse(content="ok")
        assert resp.reasoning_details is None
        assert resp.refusal is None


class TestTransportFactory:
    """Transport 工厂测试（包含 auto 检测）。"""

    def test_create_openai_transport(self):
        from spirit.agent.transports.factory import create_transport
        config = TransportConfig(provider="openai", api_key="sk-test")
        transport = create_transport(config)
        assert transport.provider_name == "openai"

    def test_create_unsupported_provider(self):
        from spirit.agent.transports.factory import create_transport
        config = TransportConfig(provider="unsupported_xyz")
        with pytest.raises(TransportError):
            create_transport(config)

    def test_list_providers(self):
        from spirit.agent.transports.factory import list_providers
        providers = list_providers()
        assert "openai" in providers
        assert "anthropic" in providers
        assert "gemini" in providers
        assert "bedrock" in providers

    def test_auto_detect_anthropic_by_model(self):
        from spirit.agent.transports.factory import _detect_provider
        config = TransportConfig(model="claude-3-5-sonnet")
        assert _detect_provider(config) == "anthropic"

    def test_auto_detect_gemini_by_model(self):
        from spirit.agent.transports.factory import _detect_provider
        config = TransportConfig(model="gemini-2.0-flash")
        assert _detect_provider(config) == "gemini"

    def test_auto_detect_bedrock_by_model(self):
        from spirit.agent.transports.factory import _detect_provider
        config = TransportConfig(model="anthropic.claude-3-5-sonnet-v2")
        assert _detect_provider(config) == "bedrock"

    def test_auto_detect_anthropic_by_url(self):
        from spirit.agent.transports.factory import _detect_provider
        config = TransportConfig(base_url="https://api.anthropic.com/v1")
        assert _detect_provider(config) == "anthropic"

    def test_auto_detect_gemini_by_url(self):
        from spirit.agent.transports.factory import _detect_provider
        config = TransportConfig(base_url="https://generativelanguage.googleapis.com/v1beta")
        assert _detect_provider(config) == "gemini"

    def test_auto_detect_openai_compatible_gemini(self):
        from spirit.agent.transports.factory import _detect_provider
        config = TransportConfig(base_url="https://generativelanguage.googleapis.com/v1beta/openai")
        assert _detect_provider(config) == "openai"

    def test_auto_detect_default_openai(self):
        from spirit.agent.transports.factory import _detect_provider
        config = TransportConfig(model="gpt-4o")
        assert _detect_provider(config) == "openai"

    def test_auto_mode_creates_correct_transport(self):
        from spirit.agent.transports.factory import create_transport
        config = TransportConfig(provider="auto", model="claude-3-5-sonnet", api_key="test")
        transport = create_transport(config)
        assert transport.provider_name == "anthropic"

    def test_openai_compatible_providers(self):
        from spirit.agent.transports.factory import create_transport
        for provider in ("azure", "ollama", "lmstudio", "vllm", "openrouter", "groq"):
            config = TransportConfig(provider=provider, api_key="test")
            transport = create_transport(config)
            assert transport.provider_name == "openai"

    def test_get_transport_info(self):
        from spirit.agent.transports.factory import get_transport_info
        info = get_transport_info()
        assert "openai" in info
        assert info["openai"]["class"] == "OpenAITransport"
        assert info["openai"]["available"] is True


# ---------------------------------------------------------------------------
# 新增类型测试
# ---------------------------------------------------------------------------

class TestToolCallData:
    """ToolCallData 数据类测试（借鉴 Hermes ToolCall）。"""

    def test_basic_creation(self):
        tc = ToolCallData(id="tc_1", name="read_file", arguments='{"path": "/tmp"}')
        assert tc.id == "tc_1"
        assert tc.name == "read_file"
        assert tc.type == "function"

    def test_function_backward_compat(self):
        tc = ToolCallData(id="tc_1", name="read_file", arguments='{"path": "/tmp"}')
        assert tc.function.name == "read_file"
        assert tc.function.arguments == '{"path": "/tmp"}'

    def test_provider_data(self):
        tc = ToolCallData(
            id="tc_1", name="test", arguments="{}",
            provider_data={"extra_content": {"google": {"thought_signature": "abc"}}}
        )
        assert tc.provider_data["extra_content"]["google"]["thought_signature"] == "abc"


class TestUsageData:
    """UsageData 数据类测试。"""

    def test_default_usage(self):
        u = UsageData()
        assert u.prompt_tokens == 0
        assert u.cached_tokens == 0

    def test_custom_usage(self):
        u = UsageData(prompt_tokens=100, completion_tokens=50, total_tokens=150, cached_tokens=30)
        assert u.total_tokens == 150
        assert u.cached_tokens == 30


class TestBuildToolCall:
    """build_tool_call 工具函数测试。"""

    def test_dict_arguments_serialized(self):
        tc = build_tool_call("id1", "read_file", {"path": "/tmp"})
        assert isinstance(tc.arguments, str)
        assert json.loads(tc.arguments) == {"path": "/tmp"}

    def test_string_arguments_preserved(self):
        tc = build_tool_call("id1", "read_file", '{"path": "/tmp"}')
        assert tc.arguments == '{"path": "/tmp"}'

    def test_provider_fields_collected(self):
        tc = build_tool_call("id1", "read", "{}", extra_content={"sig": "abc"})
        assert tc.provider_data is not None
        assert tc.provider_data["extra_content"] == {"sig": "abc"}


class TestMapFinishReason:
    """map_finish_reason 工具函数测试。"""

    def test_known_mapping(self):
        mapping = {"end_turn": "stop", "tool_use": "tool_calls"}
        assert map_finish_reason("end_turn", mapping) == "stop"
        assert map_finish_reason("tool_use", mapping) == "tool_calls"

    def test_unknown_falls_back_to_stop(self):
        assert map_finish_reason("unknown", {}) == "stop"

    def test_none_falls_back_to_stop(self):
        assert map_finish_reason(None, {}) == "stop"


# ---------------------------------------------------------------------------
# 消息清洗测试
# ---------------------------------------------------------------------------

class TestSanitizeMessages:
    """消息清洗测试（借鉴 Hermes convert_messages 清洗逻辑）。"""

    def test_no_sanitization_needed(self):
        msgs = [{"role": "user", "content": "hello"}]
        result = Transport._sanitize_messages(msgs)
        assert result is msgs  # 同一对象，未修改

    def test_strip_internal_keys(self):
        msgs = [{"role": "user", "content": "hello", "_internal_flag": True}]
        result = Transport._sanitize_messages(msgs)
        assert "_internal_flag" not in result[0]
        assert result[0]["content"] == "hello"

    def test_strip_codex_fields(self):
        msgs = [{"role": "user", "content": "hi",
                 "codex_reasoning_items": [], "codex_message_items": []}]
        result = Transport._sanitize_messages(msgs)
        assert "codex_reasoning_items" not in result[0]
        assert "codex_message_items" not in result[0]

    def test_strip_tool_name(self):
        msgs = [{"role": "tool", "content": "result", "tool_name": "read_file"}]
        result = Transport._sanitize_messages(msgs)
        assert "tool_name" not in result[0]

    def test_strip_tool_call_internal_fields(self):
        msgs = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "1", "function": {"name": "test"},
                           "call_id": "c1", "response_item_id": "r1"}]
        }]
        result = Transport._sanitize_messages(msgs)
        tc = result[0]["tool_calls"][0]
        assert "call_id" not in tc
        assert "response_item_id" not in tc
        assert tc["id"] == "1"

    def test_strip_timestamp(self):
        msgs = [{"role": "user", "content": "hi", "timestamp": "2024-01-01T00:00:00Z"}]
        result = Transport._sanitize_messages(msgs)
        assert "timestamp" not in result[0]


# ---------------------------------------------------------------------------
# OpenAI Transport 增强测试
# ---------------------------------------------------------------------------

class TestOpenAITransportEnhanced:
    """OpenAI Transport 增强功能测试。"""

    def _make_transport(self, **kwargs):
        from spirit.agent.transports.openai_transport import OpenAITransport
        config = TransportConfig(provider="openai", api_key="sk-test", **kwargs)
        return OpenAITransport(config)

    def test_validate_response_valid(self):
        t = self._make_transport()
        resp = MagicMock()
        resp.choices = [MagicMock()]
        assert t.validate_response(resp) is True

    def test_validate_response_none(self):
        t = self._make_transport()
        assert t.validate_response(None) is False

    def test_validate_response_empty_choices(self):
        t = self._make_transport()
        resp = MagicMock()
        resp.choices = []
        assert t.validate_response(resp) is False

    def test_extract_cache_stats_with_details(self):
        t = self._make_transport()
        resp = MagicMock()
        resp.usage.prompt_tokens_details.cached_tokens = 50
        resp.usage.prompt_tokens_details.cache_write_tokens = 10
        stats = t.extract_cache_stats(resp)
        assert stats == {"cached_tokens": 50, "creation_tokens": 10}

    def test_extract_cache_stats_none(self):
        t = self._make_transport()
        resp = MagicMock(spec=[])  # 无 usage 属性
        assert t.extract_cache_stats(resp) is None

    def test_apply_reasoning_openai_style(self):
        t = self._make_transport()
        kwargs: dict = {}
        t._apply_reasoning(kwargs, {"enabled": True, "effort": "high"})
        assert "extra_body" in kwargs
        assert kwargs["extra_body"]["reasoning"]["effort"] == "high"

    def test_apply_reasoning_gemini_style(self):
        t = self._make_transport(model="gemini-2.5-flash")
        kwargs: dict = {}
        t._apply_reasoning(kwargs, {"enabled": True, "effort": "high"})
        assert "extra_body" in kwargs
        assert "thinking_config" in kwargs["extra_body"]

    def test_apply_reasoning_disabled(self):
        t = self._make_transport()
        kwargs: dict = {}
        t._apply_reasoning(kwargs, {"enabled": False})
        assert "extra_body" not in kwargs


# ---------------------------------------------------------------------------
# Anthropic Transport 增强测试
# ---------------------------------------------------------------------------

class TestAnthropicTransportEnhanced:
    """Anthropic Transport 增强功能测试。"""

    def _make_transport(self):
        from spirit.agent.transports.anthropic_transport import AnthropicTransport
        config = TransportConfig(provider="anthropic", api_key="sk-ant-test")
        return AnthropicTransport(config)

    def _get_cls(self):
        from spirit.agent.transports.anthropic_transport import AnthropicTransport
        return AnthropicTransport

    def test_validate_response_valid(self):
        t = self._make_transport()
        resp = MagicMock()
        resp.content = [MagicMock()]
        assert t.validate_response(resp) is True

    def test_validate_response_empty_end_turn(self):
        t = self._make_transport()
        resp = MagicMock()
        resp.content = []
        resp.stop_reason = "end_turn"
        assert t.validate_response(resp) is True

    def test_validate_response_empty_refusal(self):
        t = self._make_transport()
        resp = MagicMock()
        resp.content = []
        resp.stop_reason = "refusal"
        assert t.validate_response(resp) is True

    def test_validate_response_empty_other(self):
        t = self._make_transport()
        resp = MagicMock()
        resp.content = []
        resp.stop_reason = "tool_use"
        assert t.validate_response(resp) is False

    def test_extract_cache_stats(self):
        t = self._make_transport()
        resp = MagicMock()
        resp.usage.cache_read_input_tokens = 100
        resp.usage.cache_creation_input_tokens = 50
        stats = t.extract_cache_stats(resp)
        assert stats == {"cached_tokens": 100, "creation_tokens": 50}

    def test_block_to_dict(self):
        cls = self._get_cls()
        block = MagicMock()
        block.__dict__ = {"type": "text", "text": "hello"}
        result = cls._block_to_dict(block)
        assert result.get("type") == "text"

    def test_sanitize_replay_block(self):
        cls = self._get_cls()
        block = {"type": "thinking", "thinking": "...", "parsed_output": "x", "caller": "y"}
        clean = cls._sanitize_replay_block(block)
        assert "parsed_output" not in clean
        assert "caller" not in clean
        assert clean["type"] == "thinking"


# ---------------------------------------------------------------------------
# Gemini Transport 测试
# ---------------------------------------------------------------------------

class TestGeminiTransport:
    """Gemini Transport 测试。"""

    def _make_transport(self):
        from spirit.agent.transports.gemini_transport import GeminiTransport
        config = TransportConfig(provider="gemini", api_key="test-key")
        return GeminiTransport(config)

    def test_provider_name(self):
        t = self._make_transport()
        assert t.provider_name == "gemini"

    def test_resolve_model_strip_prefix(self):
        t = self._make_transport()
        t.config.model = "gemini/gemini-2.0-flash"
        assert t._resolve_model() == "gemini-2.0-flash"

    def test_resolve_model_add_prefix(self):
        t = self._make_transport()
        t.config.model = "2.0-flash"
        assert t._resolve_model() == "gemini-2.0-flash"

    def test_convert_messages_system_extraction(self):
        t = self._make_transport()
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
        ]
        sys_inst, contents = t._convert_messages(messages)
        assert sys_inst == "You are helpful"
        assert len(contents) == 1
        assert contents[0]["role"] == "user"

    def test_convert_messages_tool_result(self):
        t = self._make_transport()
        messages = [
            {"role": "tool", "content": '{"result": 42}', "name": "calculate"},
        ]
        _, contents = t._convert_messages(messages)
        assert len(contents) == 1
        assert contents[0]["role"] == "user"
        assert "function_response" in contents[0]["parts"][0]

    def test_convert_tools(self):
        t = self._make_transport()
        tools = [{
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        }]
        result = t._convert_tools(tools)
        assert len(result) == 1
        assert "function_declarations" in result[0]
        decl = result[0]["function_declarations"][0]
        assert decl["name"] == "read_file"

    def test_convert_schema_type_mapping(self):
        t = self._make_transport()
        schema = {"type": "string"}
        result = t._convert_schema(schema)
        assert result["type"] == "STRING"

    def test_wrap_error_rate_limit(self):
        t = self._make_transport()
        err = t._wrap_error(Exception("429 rate limit exceeded"))
        assert isinstance(err, RateLimitError)

    def test_wrap_error_auth(self):
        t = self._make_transport()
        err = t._wrap_error(Exception("401 invalid api key"))
        assert isinstance(err, AuthenticationError)

    def test_wrap_error_server(self):
        t = self._make_transport()
        err = t._wrap_error(Exception("503 service unavailable"))
        assert err.retryable is True


# ---------------------------------------------------------------------------
# Bedrock Transport 测试
# ---------------------------------------------------------------------------

class TestBedrockTransport:
    """Bedrock Transport 测试。"""

    def _make_transport(self):
        from spirit.agent.transports.bedrock_transport import BedrockTransport
        config = TransportConfig(provider="bedrock", model="claude-3-5-sonnet")
        return BedrockTransport(config)

    def test_provider_name(self):
        t = self._make_transport()
        assert t.provider_name == "bedrock"

    def test_resolve_model_id_full(self):
        t = self._make_transport()
        t.config.model = "anthropic.claude-3-5-sonnet-20241022-v2:0"
        assert t._resolve_model_id() == "anthropic.claude-3-5-sonnet-20241022-v2:0"

    def test_resolve_model_id_short_name(self):
        t = self._make_transport()
        t.config.model = "claude-3-5-sonnet"
        result = t._resolve_model_id()
        assert result.startswith("anthropic.claude")

    def test_resolve_model_id_titan(self):
        t = self._make_transport()
        t.config.model = "titan-text-express"
        assert t._resolve_model_id() == "amazon.titan-text-express-v1"

    def test_convert_messages_system_extraction(self):
        t = self._make_transport()
        messages = [
            {"role": "system", "content": "Be helpful"},
            {"role": "user", "content": "Hello"},
        ]
        sys_list, bedrock_msgs = t._convert_messages(messages)
        assert len(sys_list) == 1
        assert sys_list[0]["text"] == "Be helpful"
        assert len(bedrock_msgs) == 1

    def test_convert_messages_tool_calls(self):
        t = self._make_transport()
        messages = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "tc1", "function": {"name": "read_file", "arguments": '{"path":"/tmp"}'}}]
        }]
        _, bedrock_msgs = t._convert_messages(messages)
        blocks = bedrock_msgs[0]["content"]
        assert any("toolUse" in b for b in blocks)

    def test_convert_tools(self):
        t = self._make_transport()
        tools = [{"function": {"name": "test", "description": "Test tool",
                               "parameters": {"type": "object", "properties": {}}}}]
        result = t._convert_tools(tools)
        assert "tools" in result
        assert result["tools"][0]["toolSpec"]["name"] == "test"

    def test_wrap_error_throttling(self):
        t = self._make_transport()
        err = t._wrap_error(Exception("ThrottlingException: rate exceeded"))
        assert isinstance(err, RateLimitError)

    def test_wrap_error_unauthorized(self):
        t = self._make_transport()
        err = t._wrap_error(Exception("UnauthorizedException: bad creds"))
        assert isinstance(err, AuthenticationError)

    def test_wrap_error_server(self):
        t = self._make_transport()
        err = t._wrap_error(Exception("InternalServerException: oops"))
        assert err.retryable is True

    def test_wrap_error_model_not_found(self):
        t = self._make_transport()
        err = t._wrap_error(Exception("ResourceNotFoundException: model xyz"))
        assert err.retryable is False
