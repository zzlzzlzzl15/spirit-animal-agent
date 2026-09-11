"""tests/agent/test_agent_init.py — Agent 初始化测试。"""

import pytest
import os
from unittest.mock import patch


class TestLoadConfig:
    """配置加载测试。"""

    def test_default_config(self):
        from spirit.agent.agent_init import load_config
        config = load_config()
        # 默认值来自 DEFAULT_CONFIG — model 留空, provider 为 auto
        assert config.model == ""
        assert config.provider == "auto"

    def test_override_config(self):
        from spirit.agent.agent_init import load_config
        config = load_config(model="claude-3", provider="anthropic")
        assert config.model == "claude-3"
        assert config.provider == "anthropic"

    def test_env_config(self):
        from spirit.agent.agent_init import load_config
        with patch.dict(os.environ, {"SPIRIT_MODEL": "gpt-4-turbo"}):
            config = load_config()
            assert config.model == "gpt-4-turbo"


class TestInitializeAgent:
    """Agent 初始化测试。"""

    def test_basic_init(self):
        from spirit.agent.agent_init import initialize_agent
        from spirit.agent.agent import AgentConfig
        config = AgentConfig(
            model="gpt-4o-mini",
            api_key="test-key",
            compression_enabled=False,
        )
        agent = initialize_agent(
            config=config,
            enable_transport=False,  # 不创建真实 Transport
        )
        assert agent.model == "gpt-4o-mini"
        assert agent.session_id

    def test_init_with_hooks(self):
        from spirit.agent.agent_init import initialize_agent
        from spirit.agent.agent import AgentConfig
        config = AgentConfig(api_key="test", compression_enabled=False)
        agent = initialize_agent(config=config, enable_transport=False)
        assert hasattr(agent, "hook_manager")

    def test_init_with_memory(self):
        from spirit.agent.agent_init import initialize_agent
        from spirit.agent.agent import AgentConfig
        config = AgentConfig(api_key="test", compression_enabled=False)
        agent = initialize_agent(config=config, enable_transport=False)
        assert hasattr(agent, "memory_manager")

    def test_init_with_tasks(self):
        from spirit.agent.agent_init import initialize_agent
        from spirit.agent.agent import AgentConfig
        config = AgentConfig(api_key="test", compression_enabled=False)
        agent = initialize_agent(config=config, enable_transport=False)
        assert hasattr(agent, "task_manager")


class TestQuickInit:
    """快速初始化测试。"""

    def test_quick_init(self):
        from spirit.agent.agent_init import quick_init
        agent = quick_init(
            model="gpt-4o-mini",
            api_key="test-key",
            compression_enabled=False,
        )
        assert agent.model == "gpt-4o-mini"
