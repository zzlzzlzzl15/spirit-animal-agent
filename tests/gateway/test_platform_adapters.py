"""平台适配器扩展测试 — Slack / DingTalk / Feishu / WeCom / QQ。

测试范围：
- Platform 枚举完整性
- 适配器自动注册
- 依赖检查函数（无 SDK 环境）
- 适配器初始化（配置解析）
- 基类辅助方法（name / _mark_connected / handle_message 等）
- 消息事件处理逻辑（mock）
"""

import asyncio
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from spirit.gateway.config import Platform, PlatformConfig
from spirit.gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    SendResult,
)


# ---------------------------------------------------------------------------
# Platform 枚举测试
# ---------------------------------------------------------------------------

class TestPlatformEnum:
    """Platform 枚举扩展测试。"""

    def test_slack_value(self):
        assert Platform.SLACK.value == "slack"

    def test_dingtalk_value(self):
        assert Platform.DINGTALK.value == "dingtalk"

    def test_feishu_value(self):
        assert Platform.FEISHU.value == "feishu"

    def test_wecom_value(self):
        assert Platform.WECOM.value == "wecom"

    def test_qq_value(self):
        assert Platform.QQ.value == "qq"

    def test_all_platforms_count(self):
        """确保所有平台枚举都存在。"""
        expected = {"cli", "api", "web", "telegram", "discord", "webhook",
                    "slack", "wechat", "wecom", "dingtalk", "feishu", "qq"}
        actual = {p.value for p in Platform}
        assert expected.issubset(actual), f"缺少: {expected - actual}"


# ---------------------------------------------------------------------------
# 基类辅助方法测试
# ---------------------------------------------------------------------------

class TestBaseAdapterHelpers:
    """基类新增辅助方法测试。"""

    def _make_config(self, platform=Platform.SLACK):
        return PlatformConfig(platform=platform)

    def test_name_property(self):
        """name 属性返回平台枚举值。"""
        from spirit.gateway.platforms.slack import SlackAdapter
        cfg = self._make_config(Platform.SLACK)
        adapter = SlackAdapter(cfg)
        assert adapter.name == "slack"

    def test_mark_connected(self):
        """_mark_connected 设置状态。"""
        from spirit.gateway.platforms.slack import SlackAdapter
        cfg = self._make_config(Platform.SLACK)
        adapter = SlackAdapter(cfg)
        assert adapter.is_connected is False
        adapter._mark_connected()
        assert adapter.is_connected is True
        assert adapter._running is True

    def test_mark_disconnected(self):
        """_mark_disconnected 清除状态。"""
        from spirit.gateway.platforms.slack import SlackAdapter
        cfg = self._make_config(Platform.SLACK)
        adapter = SlackAdapter(cfg)
        adapter._mark_connected()
        adapter._mark_disconnected()
        assert adapter.is_connected is False
        assert adapter._running is False

    def test_handle_message_with_callback(self):
        """handle_message 调用已注册回调。"""
        from spirit.gateway.platforms.slack import SlackAdapter
        cfg = self._make_config(Platform.SLACK)
        adapter = SlackAdapter(cfg)

        received = []

        async def handler(event):
            received.append(event)

        adapter.set_message_handler(handler)

        event = MessageEvent(
            platform=Platform.SLACK,
            user_id="U123",
            chat_id="C456",
            text="hello",
        )
        asyncio.run(adapter.handle_message(event))
        assert len(received) == 1
        assert received[0].text == "hello"

    def test_handle_message_no_callback(self):
        """handle_message 无回调时不报错。"""
        from spirit.gateway.platforms.slack import SlackAdapter
        cfg = self._make_config(Platform.SLACK)
        adapter = SlackAdapter(cfg)

        event = MessageEvent(
            platform=Platform.SLACK,
            user_id="U123",
            chat_id="C456",
            text="hello",
        )
        # 不应抛出异常
        asyncio.run(adapter.handle_message(event))

    def test_handle_message_callback_error_swallowed(self):
        """handle_message 回调异常被吞掉。"""
        from spirit.gateway.platforms.slack import SlackAdapter
        cfg = self._make_config(Platform.SLACK)
        adapter = SlackAdapter(cfg)

        async def bad_handler(event):
            raise RuntimeError("boom")

        adapter.set_message_handler(bad_handler)

        event = MessageEvent(
            platform=Platform.SLACK,
            user_id="U123",
            chat_id="C456",
            text="hello",
        )
        # 不应抛出异常
        asyncio.run(adapter.handle_message(event))


# ---------------------------------------------------------------------------
# 注册表测试
# ---------------------------------------------------------------------------

class TestAdapterRegistration:
    """适配器自动注册测试。"""

    def test_slack_registered(self):
        """Slack 已注册到全局注册表。"""
        from spirit.gateway.platform_registry import platform_registry
        # 导入触发注册
        import spirit.gateway.platforms.slack  # noqa: F401
        entry = platform_registry.get_entry(Platform.SLACK)
        assert entry is not None
        assert entry.label == "Slack"

    def test_dingtalk_registered(self):
        from spirit.gateway.platform_registry import platform_registry
        import spirit.gateway.platforms.dingtalk  # noqa: F401
        entry = platform_registry.get_entry(Platform.DINGTALK)
        assert entry is not None
        assert "钉钉" in entry.label

    def test_feishu_registered(self):
        from spirit.gateway.platform_registry import platform_registry
        import spirit.gateway.platforms.feishu  # noqa: F401
        entry = platform_registry.get_entry(Platform.FEISHU)
        assert entry is not None
        assert "飞书" in entry.label

    def test_wecom_registered(self):
        from spirit.gateway.platform_registry import platform_registry
        import spirit.gateway.platforms.wecom  # noqa: F401
        entry = platform_registry.get_entry(Platform.WECOM)
        assert entry is not None
        assert "企业微信" in entry.label

    def test_qq_registered(self):
        from spirit.gateway.platform_registry import platform_registry
        import spirit.gateway.platforms.qq  # noqa: F401
        entry = platform_registry.get_entry(Platform.QQ)
        assert entry is not None
        assert "QQ" in entry.label

    def test_all_have_required_env(self):
        """所有新适配器都声明了 required_env。"""
        from spirit.gateway.platform_registry import platform_registry
        import spirit.gateway.platforms.slack  # noqa: F401
        import spirit.gateway.platforms.dingtalk  # noqa: F401
        import spirit.gateway.platforms.feishu  # noqa: F401
        import spirit.gateway.platforms.wecom  # noqa: F401
        import spirit.gateway.platforms.qq  # noqa: F401

        for platform in [Platform.SLACK, Platform.DINGTALK, Platform.FEISHU,
                         Platform.WECOM, Platform.QQ]:
            entry = platform_registry.get_entry(platform)
            assert entry is not None, f"{platform.value} 未注册"
            assert len(entry.required_env) > 0, f"{platform.value} 缺少 required_env"

    def test_all_have_emoji(self):
        """所有新适配器都有 emoji 标识。"""
        from spirit.gateway.platform_registry import platform_registry
        import spirit.gateway.platforms.slack  # noqa: F401
        import spirit.gateway.platforms.dingtalk  # noqa: F401
        import spirit.gateway.platforms.feishu  # noqa: F401
        import spirit.gateway.platforms.wecom  # noqa: F401
        import spirit.gateway.platforms.qq  # noqa: F401

        for platform in [Platform.SLACK, Platform.DINGTALK, Platform.FEISHU,
                         Platform.WECOM, Platform.QQ]:
            entry = platform_registry.get_entry(platform)
            assert entry.emoji, f"{platform.value} 缺少 emoji"


# ---------------------------------------------------------------------------
# 依赖检查测试（无 SDK 环境）
# ---------------------------------------------------------------------------

class TestDependencyChecks:
    """依赖检查函数测试。"""

    def test_slack_deps_without_sdk(self):
        """无 slack_sdk 时返回 False。"""
        from spirit.gateway.platforms.slack import check_slack_deps
        # 在没有安装 slack-sdk 的测试环境中
        try:
            import slack_sdk  # noqa: F401
            # 如果安装了，跳过
            pytest.skip("slack-sdk 已安装")
        except ImportError:
            assert check_slack_deps() is False

    def test_dingtalk_deps_without_sdk(self):
        from spirit.gateway.platforms.dingtalk import check_dingtalk_deps
        try:
            import dingtalk_stream  # noqa: F401
            pytest.skip("dingtalk-stream 已安装")
        except ImportError:
            assert check_dingtalk_deps() is False

    def test_feishu_deps_without_sdk(self):
        from spirit.gateway.platforms.feishu import check_feishu_deps
        try:
            import lark_oapi  # noqa: F401
            pytest.skip("lark-oapi 已安装")
        except ImportError:
            assert check_feishu_deps() is False

    def test_wecom_deps_without_sdk(self):
        """WeCom 只需要 aiohttp + 环境变量。"""
        from spirit.gateway.platforms.wecom import check_wecom_deps
        # 即使 aiohttp 存在，没有环境变量也应返回 False
        old_id = os.environ.get("WECOM_BOT_ID")
        old_secret = os.environ.get("WECOM_SECRET")
        try:
            os.environ.pop("WECOM_BOT_ID", None)
            os.environ.pop("WECOM_SECRET", None)
            assert check_wecom_deps() is False
        finally:
            if old_id:
                os.environ["WECOM_BOT_ID"] = old_id
            if old_secret:
                os.environ["WECOM_SECRET"] = old_secret

    def test_qq_deps_without_sdk(self):
        """QQ 只需要 aiohttp + 环境变量。"""
        from spirit.gateway.platforms.qq import check_qq_deps
        old_id = os.environ.get("QQ_APP_ID")
        old_secret = os.environ.get("QQ_CLIENT_SECRET")
        try:
            os.environ.pop("QQ_APP_ID", None)
            os.environ.pop("QQ_CLIENT_SECRET", None)
            assert check_qq_deps() is False
        finally:
            if old_id:
                os.environ["QQ_APP_ID"] = old_id
            if old_secret:
                os.environ["QQ_CLIENT_SECRET"] = old_secret


# ---------------------------------------------------------------------------
# 适配器初始化测试
# ---------------------------------------------------------------------------

class TestAdapterInit:
    """适配器配置解析测试。"""

    def test_slack_init_from_extra(self):
        from spirit.gateway.platforms.slack import SlackAdapter
        cfg = PlatformConfig(
            platform=Platform.SLACK,
            extra={"bot_token": "xoxb-test", "app_token": "xapp-test"},
        )
        adapter = SlackAdapter(cfg)
        assert adapter._bot_token == "xoxb-test"
        assert adapter._app_token == "xapp-test"

    def test_slack_init_from_env(self):
        from spirit.gateway.platforms.slack import SlackAdapter
        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": "xoxb-env"}):
            cfg = PlatformConfig(platform=Platform.SLACK)
            adapter = SlackAdapter(cfg)
            assert adapter._bot_token == "xoxb-env"

    def test_dingtalk_init_from_extra(self):
        from spirit.gateway.platforms.dingtalk import DingTalkAdapter
        cfg = PlatformConfig(
            platform=Platform.DINGTALK,
            extra={"client_id": "ding-id", "client_secret": "ding-secret"},
        )
        adapter = DingTalkAdapter(cfg)
        assert adapter._client_id == "ding-id"
        assert adapter._client_secret == "ding-secret"

    def test_feishu_init_from_extra(self):
        from spirit.gateway.platforms.feishu import FeishuAdapter
        cfg = PlatformConfig(
            platform=Platform.FEISHU,
            extra={"app_id": "feishu-id", "app_secret": "feishu-secret"},
        )
        adapter = FeishuAdapter(cfg)
        assert adapter._app_id == "feishu-id"
        assert adapter._app_secret == "feishu-secret"

    def test_wecom_init_from_extra(self):
        from spirit.gateway.platforms.wecom import WeComAdapter
        cfg = PlatformConfig(
            platform=Platform.WECOM,
            extra={"bot_id": "wecom-bot", "secret": "wecom-sec"},
        )
        adapter = WeComAdapter(cfg)
        assert adapter._bot_id == "wecom-bot"
        assert adapter._secret == "wecom-sec"

    def test_qq_init_from_extra(self):
        from spirit.gateway.platforms.qq import QQAdapter
        cfg = PlatformConfig(
            platform=Platform.QQ,
            extra={"app_id": "qq-id", "client_secret": "qq-sec"},
        )
        adapter = QQAdapter(cfg)
        assert adapter._app_id == "qq-id"
        assert adapter._client_secret == "qq-sec"

    def test_slack_max_message_length(self):
        from spirit.gateway.platforms.slack import SlackAdapter
        cfg = PlatformConfig(platform=Platform.SLACK)
        adapter = SlackAdapter(cfg)
        assert adapter.max_message_length == 40000

    def test_dingtalk_max_message_length(self):
        from spirit.gateway.platforms.dingtalk import DingTalkAdapter
        cfg = PlatformConfig(platform=Platform.DINGTALK)
        adapter = DingTalkAdapter(cfg)
        assert adapter.MAX_MESSAGE_LENGTH == 20000

    def test_wecom_max_message_length(self):
        from spirit.gateway.platforms.wecom import WeComAdapter
        cfg = PlatformConfig(platform=Platform.WECOM)
        adapter = WeComAdapter(cfg)
        assert adapter.MAX_MESSAGE_LENGTH == 4000


# ---------------------------------------------------------------------------
# 适配器连接测试（mock / 无网络）
# ---------------------------------------------------------------------------

class TestAdapterConnectFailure:
    """连接失败场景测试（不需要真实网络）。"""

    def test_slack_connect_no_token(self):
        from spirit.gateway.platforms.slack import SlackAdapter
        cfg = PlatformConfig(platform=Platform.SLACK)
        adapter = SlackAdapter(cfg)
        result = asyncio.run(adapter.connect())
        assert result is False

    def test_dingtalk_connect_no_sdk(self):
        from spirit.gateway.platforms.dingtalk import DingTalkAdapter, DINGTALK_AVAILABLE
        if DINGTALK_AVAILABLE:
            pytest.skip("dingtalk-stream 已安装")
        cfg = PlatformConfig(
            platform=Platform.DINGTALK,
            extra={"client_id": "x", "client_secret": "y"},
        )
        adapter = DingTalkAdapter(cfg)
        result = asyncio.run(adapter.connect())
        assert result is False

    def test_feishu_connect_no_sdk(self):
        from spirit.gateway.platforms.feishu import FeishuAdapter, LARK_AVAILABLE
        if LARK_AVAILABLE:
            pytest.skip("lark-oapi 已安装")
        cfg = PlatformConfig(
            platform=Platform.FEISHU,
            extra={"app_id": "x", "app_secret": "y"},
        )
        adapter = FeishuAdapter(cfg)
        result = asyncio.run(adapter.connect())
        assert result is False

    def test_wecom_connect_no_config(self):
        from spirit.gateway.platforms.wecom import WeComAdapter
        cfg = PlatformConfig(platform=Platform.WECOM)
        adapter = WeComAdapter(cfg)
        result = asyncio.run(adapter.connect())
        assert result is False

    def test_qq_connect_no_config(self):
        from spirit.gateway.platforms.qq import QQAdapter
        cfg = PlatformConfig(platform=Platform.QQ)
        adapter = QQAdapter(cfg)
        result = asyncio.run(adapter.connect())
        assert result is False


# ---------------------------------------------------------------------------
# 发送失败场景测试
# ---------------------------------------------------------------------------

class TestAdapterSendFailure:
    """发送消息失败场景。"""

    def test_slack_send_no_client(self):
        from spirit.gateway.platforms.slack import SlackAdapter
        cfg = PlatformConfig(platform=Platform.SLACK)
        adapter = SlackAdapter(cfg)
        result = asyncio.run(adapter.send("C123", "hello"))
        assert result.success is False
        assert "未初始化" in result.error

    def test_dingtalk_send_no_webhook(self):
        from spirit.gateway.platforms.dingtalk import DingTalkAdapter
        cfg = PlatformConfig(
            platform=Platform.DINGTALK,
            extra={"client_id": "x", "client_secret": "y"},
        )
        adapter = DingTalkAdapter(cfg)
        result = asyncio.run(adapter.send("chat123", "hello"))
        assert result.success is False
        assert "webhook" in result.error.lower()

    def test_feishu_send_no_client(self):
        from spirit.gateway.platforms.feishu import FeishuAdapter
        cfg = PlatformConfig(platform=Platform.FEISHU)
        adapter = FeishuAdapter(cfg)
        result = asyncio.run(adapter.send("oc_xxx", "hello"))
        assert result.success is False

    def test_wecom_send_no_ws(self):
        from spirit.gateway.platforms.wecom import WeComAdapter
        cfg = PlatformConfig(platform=Platform.WECOM)
        adapter = WeComAdapter(cfg)
        result = asyncio.run(adapter.send("chat123", "hello"))
        assert result.success is False
        assert "未连接" in result.error

    def test_qq_send_no_client(self):
        from spirit.gateway.platforms.qq import QQAdapter
        cfg = PlatformConfig(platform=Platform.QQ)
        adapter = QQAdapter(cfg)
        result = asyncio.run(adapter.send("user123", "hello"))
        assert result.success is False


# ---------------------------------------------------------------------------
# 消息去重和权限测试
# ---------------------------------------------------------------------------

class TestMessageDedup:
    """消息去重逻辑测试。"""

    def test_wecom_dedup(self):
        """WeCom 消息去重。"""
        from spirit.gateway.platforms.wecom import WeComAdapter
        cfg = PlatformConfig(
            platform=Platform.WECOM,
            extra={"bot_id": "x", "secret": "y"},
        )
        adapter = WeComAdapter(cfg)

        # 模拟 _on_callback 两次相同 msg_id
        received = []

        async def handler(event):
            received.append(event)

        adapter.set_message_handler(handler)

        # _on_callback 期望外层有 data 字段
        inner_data = {
            "msg_id": "msg_001",
            "chat_id": "chat_1",
            "chat_type": "group",
            "from": {"user_id": "user_1", "name": "Test"},
            "msg_type": "text",
            "text": {"content": "hello"},
        }
        msg_wrapper = {"cmd": "aibot_msg_callback", "data": inner_data}

        asyncio.run(adapter._on_callback(msg_wrapper))
        asyncio.run(adapter._on_callback(msg_wrapper))  # 重复

        assert len(received) == 1  # 只处理一次

    def test_qq_dedup(self):
        """QQ 消息去重。"""
        from spirit.gateway.platforms.qq import QQAdapter
        cfg = PlatformConfig(
            platform=Platform.QQ,
            extra={"app_id": "x", "client_secret": "y"},
        )
        adapter = QQAdapter(cfg)

        received = []

        async def handler(event):
            received.append(event)

        adapter.set_message_handler(handler)

        msg_data = {
            "id": "qq_msg_001",
            "author": {"user_openid": "user_1"},
            "content": "hello",
        }

        asyncio.run(adapter._process_message(msg_data, is_group=False))
        asyncio.run(adapter._process_message(msg_data, is_group=False))

        assert len(received) == 1

    def test_qq_user_allowlist(self):
        """QQ 用户白名单过滤。"""
        from spirit.gateway.platforms.qq import QQAdapter
        cfg = PlatformConfig(
            platform=Platform.QQ,
            extra={"app_id": "x", "client_secret": "y", "allowed_users": ["allowed_user"]},
        )
        adapter = QQAdapter(cfg)

        received = []

        async def handler(event):
            received.append(event)

        adapter.set_message_handler(handler)

        # 不在白名单的用户
        asyncio.run(adapter._process_message({
            "id": "msg_1",
            "author": {"user_openid": "stranger"},
            "content": "hi",
        }))
        assert len(received) == 0

        # 白名单用户
        asyncio.run(adapter._process_message({
            "id": "msg_2",
            "author": {"user_openid": "allowed_user"},
            "content": "hi",
        }))
        assert len(received) == 1


# ---------------------------------------------------------------------------
# Disconnect 清理测试
# ---------------------------------------------------------------------------

class TestDisconnectCleanup:
    """断开连接清理测试。"""

    def test_wecom_disconnect_clears_state(self):
        from spirit.gateway.platforms.wecom import WeComAdapter
        cfg = PlatformConfig(
            platform=Platform.WECOM,
            extra={"bot_id": "x", "secret": "y"},
        )
        adapter = WeComAdapter(cfg)
        adapter._seen_msg_ids.add("test_msg")

        async def run():
            adapter._pending["req_1"] = asyncio.get_running_loop().create_future()
            await adapter.disconnect()

        asyncio.run(run())
        assert len(adapter._seen_msg_ids) == 0
        assert len(adapter._pending) == 0

    def test_qq_disconnect_clears_state(self):
        from spirit.gateway.platforms.qq import QQAdapter
        cfg = PlatformConfig(
            platform=Platform.QQ,
            extra={"app_id": "x", "client_secret": "y"},
        )
        adapter = QQAdapter(cfg)
        adapter._seen_msg_ids.add("test_msg")

        asyncio.run(adapter.disconnect())
        assert len(adapter._seen_msg_ids) == 0
