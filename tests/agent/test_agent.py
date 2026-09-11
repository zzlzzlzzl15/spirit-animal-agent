"""tests/agent/test_agent.py — SpiritAgent 核心类测试。"""

import pytest
from unittest.mock import MagicMock, patch


class TestAgentConfig:
    """AgentConfig 数据类测试。"""

    def test_default_config(self):
        from spirit.agent.agent import AgentConfig
        config = AgentConfig()
        # 默认值留空 — 由配置文件/环境变量提供
        assert config.model == ""
        assert config.provider == "auto"
        assert config.max_iterations == 90
        assert config.compression_enabled is True

    def test_custom_config(self):
        from spirit.agent.agent import AgentConfig
        config = AgentConfig(
            model="claude-3-opus",
            provider="anthropic",
            max_iterations=50,
            compression_enabled=False,
        )
        assert config.model == "claude-3-opus"
        assert config.provider == "anthropic"
        assert config.max_iterations == 50
        assert config.compression_enabled is False


class TestSpiritAgent:
    """SpiritAgent 核心类测试。"""

    def test_init_default(self, agent):
        """测试默认初始化。"""
        assert agent.model == "gpt-4o-mini"
        assert agent.provider == "openai"
        assert agent.max_iterations == 5
        assert agent.session_id  # 应自动生成 UUID
        assert len(agent.messages) == 0

    def test_init_kwargs_override(self, agent_config):
        """测试 kwargs 覆盖配置。"""
        from spirit.agent.agent import SpiritAgent
        agent = SpiritAgent(config=agent_config, model="gpt-3.5-turbo")
        assert agent.model == "gpt-3.5-turbo"

    def test_add_message(self, agent):
        """测试消息添加。"""
        agent.add_message("user", "hello")
        agent.add_message("assistant", "hi there")
        assert len(agent.messages) == 2
        assert agent.messages[0]["role"] == "user"
        assert agent.messages[0]["content"] == "hello"
        assert agent.messages[1]["role"] == "assistant"

    def test_add_message_with_kwargs(self, agent):
        """测试带额外字段的消息。"""
        agent.add_message("assistant", "", tool_calls=[{"id": "1"}])
        assert "tool_calls" in agent.messages[0]

    def test_interrupt(self, agent):
        """测试中断请求。"""
        assert agent._interrupt_requested is False
        agent.interrupt()
        assert agent._interrupt_requested is True
        agent.clear_interrupt()
        assert agent._interrupt_requested is False

    def test_reset_session(self, agent):
        """测试会话重置。"""
        old_session_id = agent.session_id
        agent.add_message("user", "test")
        agent.add_message("assistant", "response")
        agent._api_call_count = 5

        agent.reset_session()

        assert agent.session_id != old_session_id
        assert len(agent.messages) == 0
        assert agent._api_call_count == 0
        assert agent._interrupt_requested is False

    def test_get_status(self, agent):
        """测试状态摘要。"""
        agent.add_message("user", "test")
        status = agent.get_status()
        assert "session_id" in status
        assert status["model"] == "gpt-4o-mini"
        assert status["provider"] == "openai"
        assert status["message_count"] == 1
        assert "tool_count" in status

    def test_switch_model(self, agent):
        """测试模型切换。"""
        agent.switch_model("gpt-3.5-turbo", base_url="http://localhost:1234")
        assert agent.model == "gpt-3.5-turbo"
        assert agent.base_url == "http://localhost:1234"
        assert agent._client is None  # 客户端应被清空

    def test_context_compressor_created(self, agent_config):
        """测试压缩器创建（启用时）。"""
        from spirit.agent.agent import SpiritAgent
        agent_config.compression_enabled = True
        agent = SpiritAgent(config=agent_config)
        assert agent._context_compressor is not None

    def test_context_compressor_disabled(self, agent):
        """测试压缩器禁用。"""
        assert agent._context_compressor is None


class TestConversationResult:
    """ConversationResult 数据类测试。"""

    def test_default_result(self):
        from spirit.agent.agent import ConversationResult
        result = ConversationResult(response="hello", messages=[])
        assert result.response == "hello"
        assert result.iterations == 0
        assert result.usage == {}

    def test_result_with_usage(self):
        from spirit.agent.agent import ConversationResult
        result = ConversationResult(
            response="done",
            messages=[],
            usage={"prompt_tokens": 100, "completion_tokens": 50},
            iterations=3,
        )
        assert result.iterations == 3
        assert result.usage["prompt_tokens"] == 100
