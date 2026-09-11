"""网关配置测试。"""

import pytest
from pathlib import Path

from spirit.gateway.config import (
    GatewayConfig,
    PlatformConfig,
    Platform,
    HomeChannel,
    SessionResetPolicy,
    ResetPolicy,
    load_gateway_config,
    save_gateway_config,
)


class TestPlatformConfig:
    """PlatformConfig 测试。"""

    def test_create_platform_config(self):
        """测试创建平台配置。"""
        config = PlatformConfig(
            platform=Platform.TELEGRAM,
            enabled=True,
            token="test-token",
        )
        assert config.platform == Platform.TELEGRAM
        assert config.enabled is True
        assert config.token == "test-token"

    def test_user_allowed_all(self):
        """测试允许所有用户。"""
        config = PlatformConfig(
            platform=Platform.TELEGRAM,
            allow_all_users=True,
        )
        assert config.is_user_allowed("any_user") is True

    def test_user_allowed_whitelist(self):
        """测试白名单用户。"""
        config = PlatformConfig(
            platform=Platform.TELEGRAM,
            allowed_users=["user1", "user2"],
        )
        assert config.is_user_allowed("user1") is True
        assert config.is_user_allowed("user3") is False

    def test_user_allowed_empty_whitelist(self):
        """测试空白名单（默认允许）。"""
        config = PlatformConfig(
            platform=Platform.TELEGRAM,
            allowed_users=[],
        )
        assert config.is_user_allowed("any_user") is True


class TestGatewayConfig:
    """GatewayConfig 测试。"""

    def test_create_gateway_config(self):
        """测试创建网关配置。"""
        config = GatewayConfig()
        assert config.agent_cache_size == 32
        assert config.auto_start is True

    def test_get_enabled_platforms(self, gateway_config):
        """测试获取已启用平台。"""
        enabled = gateway_config.get_enabled_platforms()
        assert Platform.TELEGRAM in enabled
        assert Platform.DISCORD in enabled

    def test_get_connected_platforms(self, gateway_config):
        """测试获取已连接平台（有主频道）。"""
        connected = gateway_config.get_connected_platforms()
        # Telegram 有主频道，Discord 没有
        assert Platform.TELEGRAM in connected
        assert Platform.DISCORD not in connected


class TestSessionResetPolicy:
    """SessionResetPolicy 测试。"""

    def test_default_policy(self):
        """测试默认重置策略。"""
        policy = SessionResetPolicy()
        assert policy.policy == ResetPolicy.MANUAL
        assert policy.idle_timeout_minutes == 60

    def test_time_based_policy(self):
        """测试基于时间的策略。"""
        policy = SessionResetPolicy(
            policy=ResetPolicy.TIME_BASED,
            idle_timeout_minutes=30,
        )
        assert policy.policy == ResetPolicy.TIME_BASED
        assert policy.idle_timeout_minutes == 30


class TestConfigPersistence:
    """配置持久化测试。"""

    def test_save_and_load_config(self, tmp_path):
        """测试保存和加载配置。"""
        config_path = tmp_path / "gateway.json"

        # 创建配置
        config = GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(
                    platform=Platform.TELEGRAM,
                    enabled=True,
                    token="my-token",
                    home_channel=HomeChannel(chat_id="12345"),
                ),
            },
            agent_cache_size=16,
        )

        # 保存
        save_gateway_config(config, config_path)
        assert config_path.exists()

        # 加载
        loaded = load_gateway_config(config_path)
        assert loaded.agent_cache_size == 16
        assert Platform.TELEGRAM in loaded.platforms
        assert loaded.platforms[Platform.TELEGRAM].token == "my-token"
