"""平台注册表和投递路由测试。"""

import pytest
import asyncio

from spirit.gateway.config import Platform, PlatformConfig
from spirit.gateway.platform_registry import (
    PlatformEntry,
    PlatformRegistry,
)
from spirit.gateway.delivery import (
    DeliveryRouter,
    DeliveryTarget,
    DeliveryResult,
)


class TestPlatformRegistry:
    """PlatformRegistry 测试。"""

    def test_register_platform(self, platform_registry):
        """测试注册平台。"""
        entries = platform_registry.list_registered()
        assert len(entries) == 1
        assert entries[0].platform == Platform.TELEGRAM

    def test_create_adapter(self, platform_registry):
        """测试创建适配器。"""
        config = PlatformConfig(
            platform=Platform.TELEGRAM,
            enabled=True,
            token="test-token",
        )
        adapter = platform_registry.create_adapter(Platform.TELEGRAM, config)
        assert adapter is not None

    def test_create_adapter_cached(self, platform_registry):
        """测试适配器缓存。"""
        config = PlatformConfig(
            platform=Platform.TELEGRAM,
            enabled=True,
            token="test-token",
        )
        adapter1 = platform_registry.create_adapter(Platform.TELEGRAM, config)
        adapter2 = platform_registry.create_adapter(Platform.TELEGRAM, config)
        assert adapter1 is adapter2

    def test_create_unregistered_platform(self, platform_registry):
        """测试创建未注册平台。"""
        config = PlatformConfig(platform=Platform.DISCORD)
        with pytest.raises(ValueError, match="未注册"):
            platform_registry.create_adapter(Platform.DISCORD, config)

    def test_unregister_platform(self, platform_registry):
        """测试注销平台。"""
        platform_registry.unregister(Platform.TELEGRAM)
        entries = platform_registry.list_registered()
        assert len(entries) == 0

    def test_check_requirements(self, platform_registry):
        """测试依赖检查。"""
        assert platform_registry.check_requirements(Platform.TELEGRAM) is True
        assert platform_registry.check_requirements(Platform.DISCORD) is False

    def test_list_available(self, platform_registry):
        """测试列出可用平台。"""
        available = platform_registry.list_available()
        assert len(available) == 1

    def test_get_status(self, platform_registry):
        """测试获取状态。"""
        configs = {
            Platform.TELEGRAM: PlatformConfig(
                platform=Platform.TELEGRAM,
                enabled=True,
                token="test-token",
            ),
        }
        status = platform_registry.get_status(configs)
        assert "telegram" in status
        assert status["telegram"]["state"] == "ready"


class TestDeliveryTarget:
    """DeliveryTarget 测试。"""

    def test_parse_simple(self):
        """测试解析简单目标。"""
        target = DeliveryTarget.parse("telegram:12345")
        assert target is not None
        assert target.platform == Platform.TELEGRAM
        assert target.chat_id == "12345"

    def test_parse_with_thread(self):
        """测试解析带线程的目标。"""
        target = DeliveryTarget.parse("discord:channel:thread")
        assert target is not None
        assert target.platform == Platform.DISCORD
        assert target.chat_id == "channel"
        assert target.thread_id == "thread"

    def test_parse_invalid(self):
        """测试解析无效目标。"""
        target = DeliveryTarget.parse("invalid")
        assert target is None

    def test_parse_unknown_platform(self):
        """测试解析未知平台。"""
        target = DeliveryTarget.parse("unknown:123")
        assert target is None


class TestDeliveryRouter:
    """DeliveryRouter 测试。"""

    def test_create_router(self, delivery_router):
        """测试创建路由器。"""
        assert delivery_router is not None

    def test_deliver_no_adapter(self, delivery_router):
        """测试无适配器时投递。"""
        async def run():
            result = await delivery_router.deliver("telegram:12345", "Hello")
            return result

        result = asyncio.run(run())
        assert result.success is False
        assert "无可用适配器" in result.error

    def test_mark_dead_target(self, delivery_router):
        """测试标记死信目标。"""
        delivery_router.mark_dead(Platform.TELEGRAM, "12345")
        assert "telegram:12345" in delivery_router.dead_targets

    def test_clear_dead_targets(self, delivery_router):
        """测试清除死信。"""
        delivery_router.mark_dead(Platform.TELEGRAM, "12345")
        delivery_router.clear_dead(Platform.TELEGRAM)
        assert len(delivery_router.dead_targets) == 0

    def test_split_message_short(self, delivery_router):
        """测试短消息不分片。"""
        chunks = delivery_router._split_message("Hello", 100)
        assert len(chunks) == 1
        assert chunks[0] == "Hello"

    def test_split_message_long(self, delivery_router):
        """测试长消息分片。"""
        text = "Hello world! " * 100
        chunks = delivery_router._split_message(text, 100)
        assert len(chunks) > 1
        # 所有块都应该在限制内
        for chunk in chunks:
            assert len(chunk) <= 100

    def test_split_message_preserves_content(self, delivery_router):
        """测试分片保留内容。"""
        text = "Line 1\nLine 2\nLine 3\nLine 4\nLine 5"
        chunks = delivery_router._split_message(text, 20)
        # 合并所有块应该包含原始内容
        combined = " ".join(chunks)
        for line in ["Line 1", "Line 2", "Line 3", "Line 4", "Line 5"]:
            assert line in combined


class TestDeliveryResult:
    """DeliveryResult 测试。"""

    def test_ok_result(self):
        """测试成功结果。"""
        target = DeliveryTarget(platform=Platform.TELEGRAM, chat_id="123")
        result = DeliveryResult.ok(target, message_id="msg_001")
        assert result.success is True
        assert result.message_id == "msg_001"

    def test_fail_result(self):
        """测试失败结果。"""
        target = DeliveryTarget(platform=Platform.TELEGRAM, chat_id="123")
        result = DeliveryResult.fail(target, "Connection failed", "network")
        assert result.success is False
        assert result.error == "Connection failed"
        assert result.error_kind == "network"
