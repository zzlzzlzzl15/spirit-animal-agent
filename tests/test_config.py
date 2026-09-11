"""tests/test_config.py — 集中式配置系统测试。"""

import os
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch

from spirit.config import (
    DEFAULT_CONFIG,
    PROVIDER_BASE_URLS,
    PROVIDER_KEY_ENV,
    load_config,
    save_config,
    get_config_path,
    get_model_config,
    get_spirit_home,
    get_config_value,
    _deep_merge,
    _set_nested,
    _str_to_bool,
)
from spirit.agent.agent import AgentConfig


# ---------------------------------------------------------------------------
# DEFAULT_CONFIG 测试
# ---------------------------------------------------------------------------

class TestDefaultConfig:

    def test_default_config_has_llm(self):
        assert "llm" in DEFAULT_CONFIG

    def test_default_model_is_empty(self):
        """模型默认留空 — 不能硬编码具体模型。"""
        assert DEFAULT_CONFIG["llm"]["model"] == ""

    def test_default_provider_is_auto(self):
        assert DEFAULT_CONFIG["llm"]["provider"] == "auto"

    def test_default_base_url_is_empty(self):
        """base_url 默认留空 — 由 provider 自动解析。"""
        assert DEFAULT_CONFIG["llm"]["base_url"] == ""

    def test_default_config_has_agent(self):
        assert "agent" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["agent"]["max_iterations"] == 90

    def test_default_config_has_compression(self):
        assert "compression" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["compression"]["enabled"] is True

    def test_default_config_has_streaming(self):
        assert "streaming" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["streaming"]["enabled"] is False

    def test_default_config_has_guardrails(self):
        assert "tool_loop_guardrails" in DEFAULT_CONFIG

    # ── 新增配置节测试 ──

    def test_default_timeouts(self):
        assert "timeouts" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["timeouts"]["terminal_default"] == 120
        assert DEFAULT_CONFIG["timeouts"]["web_default"] == 30.0

    def test_default_backoff(self):
        assert "backoff" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["backoff"]["overloaded_base"] == 10.0
        assert DEFAULT_CONFIG["backoff"]["jitter_ratio"] == 0.5

    def test_default_limits(self):
        assert "limits" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["limits"]["tool_output_max_chars"] == 100_000
        assert DEFAULT_CONFIG["limits"]["terminal_output_max_chars"] == 50_000

    def test_default_platforms(self):
        assert "platforms" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["platforms"]["discord"]["max_message_length"] == 2000
        assert DEFAULT_CONFIG["platforms"]["telegram"]["typing_interval"] == 4.0

    def test_default_memora(self):
        assert "memora" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["memora"]["base_url"] == "http://127.0.0.1:8080"

    def test_default_browser(self):
        assert "browser" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["browser"]["console_history_max"] == 50

    def test_default_internal(self):
        assert "internal" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["internal"]["skills_index_cache_ttl"] == 3600

    def test_default_integrations(self):
        assert "integrations" in DEFAULT_CONFIG
        assert "graph_base_url" in DEFAULT_CONFIG["integrations"]

    def test_default_session_reset(self):
        assert "session_reset" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["session_reset"]["max_messages"] == 500

    def test_default_gateway(self):
        assert "gateway" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["gateway"]["agent_cache_size"] == 32

    def test_default_hook_outputs(self):
        assert "hook_outputs" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["hook_outputs"]["spill_max_chars"] == 10_000

    def test_compression_protect_last_n_is_6(self):
        """压缩保护条数统一为 6（修复不一致）。"""
        assert DEFAULT_CONFIG["compression"]["protect_last_n"] == 6

    def test_chars_per_token_is_3_5(self):
        """token 估算比例统一为 3.5（修复不一致）。"""
        assert DEFAULT_CONFIG["compression"]["chars_per_token"] == 3.5


# ---------------------------------------------------------------------------
# get_config_value 测试
# ---------------------------------------------------------------------------

class TestGetConfigValue:

    def test_simple_key(self):
        assert get_config_value("llm.provider") == "auto"

    def test_nested_key(self):
        assert get_config_value("timeouts.terminal_default") == 120

    def test_deep_nested_key(self):
        assert get_config_value("tool_loop_guardrails.warn_after.exact_failure") == 2

    def test_missing_key_returns_default(self):
        assert get_config_value("nonexistent.key") is None
        assert get_config_value("nonexistent.key", 42) == 42

    def test_platform_values(self):
        assert get_config_value("platforms.discord.max_message_length") == 2000
        assert get_config_value("platforms.default_reconnect_backoff") == [2, 5, 10, 30, 60]


# ---------------------------------------------------------------------------
# Provider 映射表测试
# ---------------------------------------------------------------------------

class TestProviderMaps:

    def test_provider_base_urls(self):
        assert "openai" in PROVIDER_BASE_URLS
        assert "ollama" in PROVIDER_BASE_URLS
        assert PROVIDER_BASE_URLS["openai"] == "https://api.openai.com/v1"
        assert PROVIDER_BASE_URLS["ollama"] == "http://127.0.0.1:11434/v1"

    def test_provider_key_env(self):
        assert "openai" in PROVIDER_KEY_ENV
        assert "OPENAI_API_KEY" in PROVIDER_KEY_ENV["openai"]

    def test_ollama_no_key_needed(self):
        assert PROVIDER_KEY_ENV.get("ollama") == []


# ---------------------------------------------------------------------------
# 内部工具函数测试
# ---------------------------------------------------------------------------

class TestHelpers:

    def test_deep_merge_simple(self):
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        _deep_merge(base, override)
        assert base == {"a": 1, "b": 3, "c": 4}

    def test_deep_merge_nested(self):
        base = {"llm": {"model": "", "provider": "auto"}}
        override = {"llm": {"model": "gpt-4o"}}
        _deep_merge(base, override)
        assert base["llm"]["model"] == "gpt-4o"
        assert base["llm"]["provider"] == "auto"  # 未被覆盖

    def test_deep_merge_dict_overrides_scalar(self):
        base = {"x": 1}
        override = {"x": {"nested": True}}
        _deep_merge(base, override)
        assert base["x"] == {"nested": True}

    def test_set_nested(self):
        d = {}
        _set_nested(d, "llm.model", "gpt-4o")
        assert d["llm"]["model"] == "gpt-4o"

    def test_set_nested_deep(self):
        d = {}
        _set_nested(d, "a.b.c", 42)
        assert d["a"]["b"]["c"] == 42

    def test_str_to_bool(self):
        assert _str_to_bool("true") is True
        assert _str_to_bool("True") is True
        assert _str_to_bool("1") is True
        assert _str_to_bool("yes") is True
        assert _str_to_bool("false") is False
        assert _str_to_bool("0") is False
        assert _str_to_bool("") is False


# ---------------------------------------------------------------------------
# load_config 测试
# ---------------------------------------------------------------------------

class TestLoadConfig:

    def test_load_default(self):
        """无配置文件时返回默认值。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = load_config(config_path=os.path.join(tmpdir, "nonexistent.yaml"))
            assert cfg["llm"]["model"] == ""
            assert cfg["llm"]["provider"] == "auto"
            assert cfg["agent"]["max_iterations"] == 90

    def test_load_from_yaml(self):
        """从 YAML 文件加载配置。"""
        yaml_content = """
llm:
  model: "qwen2.5:7b"
  provider: "ollama"
  base_url: "http://localhost:11434/v1"
agent:
  max_iterations: 50
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(yaml_content)
            f.flush()
            cfg = load_config(config_path=f.name)

        assert cfg["llm"]["model"] == "qwen2.5:7b"
        assert cfg["llm"]["provider"] == "ollama"
        assert cfg["llm"]["base_url"] == "http://localhost:11434/v1"
        assert cfg["agent"]["max_iterations"] == 50
        os.unlink(f.name)

    def test_env_overrides_yaml(self):
        """环境变量优先于 YAML 文件。"""
        yaml_content = """
llm:
  model: "from-yaml"
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(yaml_content)
            f.flush()

            with patch.dict(os.environ, {"SPIRIT_MODEL": "from-env"}):
                cfg = load_config(config_path=f.name)

        assert cfg["llm"]["model"] == "from-env"
        os.unlink(f.name)

    def test_overrides_beat_env(self):
        """参数覆盖优先于环境变量。"""
        with patch.dict(os.environ, {"SPIRIT_MODEL": "from-env"}):
            cfg = load_config(
                config_path="/nonexistent.yaml",
                llm={"model": "from-override"},
            )
        assert cfg["llm"]["model"] == "from-override"

    def test_provider_auto_resolves_base_url(self):
        """provider=ollama 时自动填充 base_url。"""
        yaml_content = """
llm:
  provider: "ollama"
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(yaml_content)
            f.flush()
            cfg = load_config(config_path=f.name)

        assert cfg["llm"]["base_url"] == "http://127.0.0.1:11434/v1"
        os.unlink(f.name)

    def test_explicit_base_url_not_overridden(self):
        """用户显式设置的 base_url 不被 provider 覆盖。"""
        yaml_content = """
llm:
  provider: "ollama"
  base_url: "http://custom:9999/v1"
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(yaml_content)
            f.flush()
            cfg = load_config(config_path=f.name)

        assert cfg["llm"]["base_url"] == "http://custom:9999/v1"
        os.unlink(f.name)

    def test_env_bool_conversion(self):
        """环境变量的布尔值正确转换。"""
        with patch.dict(os.environ, {
            "SPIRIT_COMPRESSION_ENABLED": "false",
            "SPIRIT_STREAMING_ENABLED": "true",
        }):
            cfg = load_config(config_path="/nonexistent.yaml")

        assert cfg["compression"]["enabled"] is False
        assert cfg["streaming"]["enabled"] is True

    def test_env_int_conversion(self):
        """环境变量的整数值正确转换。"""
        with patch.dict(os.environ, {
            "SPIRIT_MAX_ITERATIONS": "42",
            "SPIRIT_CONTEXT_LENGTH": "32000",
        }):
            cfg = load_config(config_path="/nonexistent.yaml")

        assert cfg["agent"]["max_iterations"] == 42
        assert cfg["llm"]["context_length"] == 32000


# ---------------------------------------------------------------------------
# save_config 测试
# ---------------------------------------------------------------------------

class TestSaveConfig:

    def test_save_and_reload(self):
        """保存后重新加载应得到相同值。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test_config.yaml")
            data = {
                "llm": {"model": "gpt-4o", "provider": "openai"},
                "agent": {"max_iterations": 30},
            }
            save_config(data, config_path=path)

            cfg = load_config(config_path=path)
            assert cfg["llm"]["model"] == "gpt-4o"
            assert cfg["agent"]["max_iterations"] == 30

    def test_save_creates_directory(self):
        """保存时自动创建目录。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "subdir", "config.yaml")
            save_config({"test": True}, config_path=path)
            assert Path(path).exists()


# ---------------------------------------------------------------------------
# get_model_config 测试
# ---------------------------------------------------------------------------

class TestGetModelConfig:

    def test_returns_llm_section(self):
        with patch.dict(os.environ, {"SPIRIT_MODEL": "test-model"}, clear=False):
            cfg = get_model_config(config_path="/nonexistent.yaml")
        assert cfg["model"] == "test-model"
        assert "provider" in cfg


# ---------------------------------------------------------------------------
# AgentConfig.from_dict 测试
# ---------------------------------------------------------------------------

class TestAgentConfigFromDict:

    def test_nested_format(self):
        data = {
            "llm": {"model": "claude-3", "provider": "anthropic"},
            "agent": {"max_iterations": 50},
            "compression": {"enabled": False},
        }
        config = AgentConfig.from_dict(data)
        assert config.model == "claude-3"
        assert config.provider == "anthropic"
        assert config.max_iterations == 50
        assert config.compression_enabled is False

    def test_flat_format(self):
        data = {"model": "gpt-4o", "provider": "openai"}
        config = AgentConfig.from_dict(data)
        assert config.model == "gpt-4o"
        assert config.provider == "openai"

    def test_empty_dict_gives_defaults(self):
        config = AgentConfig.from_dict({})
        assert config.model == ""
        assert config.provider == "auto"
        assert config.max_iterations == 90

    def test_no_hardcoded_model(self):
        """验证 AgentConfig 默认值不含硬编码模型。"""
        config = AgentConfig()
        assert config.model == ""
        assert config.base_url == ""
        assert config.provider == "auto"


# ---------------------------------------------------------------------------
# 集成测试: load_config → AgentConfig.from_dict
# ---------------------------------------------------------------------------

class TestConfigIntegration:

    def test_full_pipeline(self):
        """完整流水线: YAML → load_config → AgentConfig。"""
        yaml_content = """
llm:
  model: "deepseek-chat"
  provider: "custom"
  base_url: "https://api.deepseek.com/v1"
agent:
  max_iterations: 100
compression:
  threshold: 0.8
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(yaml_content)
            f.flush()

            from spirit.agent.agent_init import load_config as init_load_config
            config = init_load_config(config_path=f.name)

        assert config.model == "deepseek-chat"
        assert config.provider == "custom"
        assert config.base_url == "https://api.deepseek.com/v1"
        assert config.max_iterations == 100
        assert config.compression_threshold == 0.8
        os.unlink(f.name)

    def test_quick_init_no_hardcode(self):
        """quick_init 不再硬编码 gpt-4o。"""
        import inspect
        from spirit.agent.agent_init import quick_init
        sig = inspect.signature(quick_init)
        # model 默认值应为 None（不是 "gpt-4o"）
        assert sig.parameters["model"].default is None
        assert sig.parameters["provider"].default is None
