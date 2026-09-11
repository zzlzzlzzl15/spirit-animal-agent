"""Gateway 和 Sessions 模块测试共享 fixtures。"""

import pytest
from pathlib import Path

from spirit.gateway.config import (
    GatewayConfig,
    PlatformConfig,
    Platform,
    HomeChannel,
    SessionResetPolicy,
    ResetPolicy,
)
from spirit.gateway.session import (
    SessionContext,
    SessionSource,
    SessionStore,
)
from spirit.gateway.platform_registry import (
    PlatformEntry,
    PlatformRegistry,
)
from spirit.gateway.delivery import (
    DeliveryRouter,
    DeliveryTarget,
)


@pytest.fixture
def gateway_config():
    """基础网关配置。"""
    return GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                platform=Platform.TELEGRAM,
                enabled=True,
                token="test-token-123",
                home_channel=HomeChannel(chat_id="12345"),
            ),
            Platform.DISCORD: PlatformConfig(
                platform=Platform.DISCORD,
                enabled=True,
                token="discord-token-456",
            ),
        },
    )


@pytest.fixture
def telegram_source():
    """Telegram 消息来源。"""
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="user_001",
        chat_id="chat_123",
        username="testuser",
        display_name="Test User",
        chat_type="dm",
    )


@pytest.fixture
def discord_source():
    """Discord 消息来源。"""
    return SessionSource(
        platform=Platform.DISCORD,
        user_id="discord_user_001",
        chat_id="channel_456",
        thread_id="thread_789",
        username="discord_user",
        chat_type="group",
    )


@pytest.fixture
def session_store(tmp_path):
    """会话存储。"""
    return SessionStore(
        max_size=10,
        idle_ttl_seconds=3600,
        persist_dir=tmp_path / "sessions",
    )


@pytest.fixture
def platform_registry():
    """平台注册表。"""
    registry = PlatformRegistry()

    # 注册一个测试平台
    class MockAdapter:
        def __init__(self, config):
            self.config = config
            self.connected = False

        async def connect(self):
            self.connected = True
            return True

        async def disconnect(self):
            self.connected = False

    registry.register(PlatformEntry(
        platform=Platform.TELEGRAM,
        label="Telegram (Test)",
        adapter_factory=lambda cfg: MockAdapter(cfg),
        check_fn=lambda: True,
    ))

    return registry


@pytest.fixture
def delivery_router(gateway_config):
    """投递路由器。"""
    return DeliveryRouter(gateway_config)
