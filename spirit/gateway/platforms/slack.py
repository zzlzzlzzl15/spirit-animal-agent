"""Slack 平台适配器。

参考 Hermes plugins/platforms/slack/adapter.py 设计：
- 使用 Slack Bolt SDK (slack-bolt) 或 Slack Web API
- 支持 DM / 频道 / 线程
- 支持 Block Kit 消息格式
- 支持消息编辑 / 线程回复
- 支持 @mention 触发

依赖安装: pip install slack-bolt slack-sdk
环境变量: SLACK_BOT_TOKEN, SLACK_APP_TOKEN (Socket Mode)
"""

import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional

from spirit.gateway.config import Platform, PlatformConfig
from spirit.gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    SendResult,
)

logger = logging.getLogger(__name__)

from spirit.config import get_config_value

SLACK_MAX_MESSAGE_LENGTH = get_config_value("platforms.slack.max_message_length", 40000)
SLACK_TYPING_INTERVAL = get_config_value("platforms.slack.typing_interval", 5.0)


def check_slack_deps() -> bool:
    """检查 Slack 依赖是否可用。"""
    try:
        import slack_sdk  # noqa: F401
        return True
    except ImportError:
        return False


class SlackAdapter(BasePlatformAdapter):
    """Slack Bot 适配器。

    支持功能：
    - DM / 频道 / 线程回复
    - Block Kit 富文本消息
    - 消息编辑 / 删除
    - @mention 触发检测
    - Socket Mode 实时连接
    - 文件上传

    Usage:
        config = PlatformConfig(
            platform=Platform.SLACK,
            extra={"bot_token": "xoxb-...", "app_token": "xapp-..."}
        )
        adapter = SlackAdapter(config)
        await adapter.connect()
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.SLACK)
        self.max_message_length = SLACK_MAX_MESSAGE_LENGTH

        extra = config.extra or {}
        self._bot_token = extra.get("bot_token") or os.getenv("SLACK_BOT_TOKEN", "")
        self._app_token = extra.get("app_token") or os.getenv("SLACK_APP_TOKEN", "")
        self._signing_secret = extra.get("signing_secret") or os.getenv("SLACK_SIGNING_SECRET", "")

        self._client = None  # slack_sdk.WebClient
        self._socket_mode_client = None
        self._app = None  # slack_bolt.App
        self._socket_handler = None
        self._connect_task: Optional[asyncio.Task] = None
        self._bot_user_id: Optional[str] = None
        self._mention_patterns: List[re.Pattern] = self._compile_mention_patterns()

    def _compile_mention_patterns(self) -> List[re.Pattern]:
        """编译 mention_patterns 配置。"""
        raw = (self.config.extra or {}).get("mention_patterns", [])
        if isinstance(raw, str):
            raw = [raw]
        patterns = []
        for p in raw:
            try:
                patterns.append(re.compile(p, re.IGNORECASE))
            except re.error:
                logger.warning("无效的 Slack mention pattern: %s", p)
        return patterns

    async def connect(self) -> bool:
        """连接到 Slack。"""
        if not self._bot_token:
            logger.warning("[%s] SLACK_BOT_TOKEN 未配置", self.name)
            return False

        try:
            import slack_sdk
            self._client = slack_sdk.WebClient(token=self._bot_token)

            # 获取 Bot 用户 ID
            auth = self._client.auth_test()
            self._bot_user_id = auth.get("user_id", "")
            logger.info("[%s] Bot 身份: %s (%s)", self.name, auth.get("bot_id"), self._bot_user_id)

            # Socket Mode（实时消息）
            if self._app_token:
                try:
                    from slack_bolt import App
                    from slack_bolt.adapter.socket_mode import SocketModeHandler

                    self._app = App(token=self._bot_token, name=self.name)
                    self._setup_event_handlers()
                    self._socket_handler = SocketModeHandler(self._app, self._app_token)

                    self._connect_task = asyncio.create_task(self._run_socket_mode())
                    logger.info("[%s] Socket Mode 已启动", self.name)
                except ImportError:
                    logger.warning("[%s] slack-bolt 未安装，Socket Mode 不可用", self.name)

            self._mark_connected()
            return True

        except Exception as e:
            logger.error("[%s] Slack 连接失败: %s", self.name, e)
            return False

    async def _run_socket_mode(self) -> None:
        """在后台线程运行 Socket Mode。"""
        try:
            await asyncio.to_thread(self._socket_handler.start)
        except Exception as e:
            if self._running:
                logger.error("[%s] Socket Mode 异常: %s", self.name, e)

    def _setup_event_handlers(self) -> None:
        """注册 Slack 事件处理器。"""
        if not self._app:
            return

        @self._app.message("")
        async def handle_message(message, say):
            """处理所有消息事件。"""
            await self._process_message(message)

        @self._app.event("app_mention")
        async def handle_mention(event, say):
            """处理 @mention 事件。"""
            await self._process_message(event)

    async def _process_message(self, event: Dict) -> None:
        """处理收到的 Slack 消息。"""
        user_id = event.get("user", "")
        channel = event.get("channel", "")
        text = event.get("text", "")
        ts = event.get("ts", "")
        thread_ts = event.get("thread_ts")

        # 忽略 Bot 自己的消息
        if user_id == self._bot_user_id:
            return

        # 去除 @mention
        if self._bot_user_id:
            text = text.replace(f"<@{self._bot_user_id}>", "").strip()

        # 构建消息事件
        msg_event = MessageEvent(
            platform=Platform.SLACK,
            user_id=user_id,
            chat_id=channel,
            message_id=ts,
            thread_id=thread_ts,
            text=text,
            chat_type="dm" if channel.startswith("D") else "channel",
            timestamp=None,
            raw_data=event,
        )
        await self.handle_message(msg_event)

    async def disconnect(self) -> None:
        """断开 Slack 连接。"""
        self._running = False
        self._mark_disconnected()

        if self._socket_handler:
            try:
                self._socket_handler.close()
            except Exception:
                pass
            self._socket_handler = None

        if self._connect_task:
            self._connect_task.cancel()
            self._connect_task = None

        self._client = None
        self._app = None
        logger.info("[%s] 已断开 Slack", self.name)

    async def send(
        self,
        chat_id: str,
        text: str,
        thread_id: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """发送消息到 Slack 频道。"""
        if not self._client:
            return SendResult.fail("Slack 客户端未初始化")

        try:
            # 截断过长消息
            if len(text) > self.max_message_length:
                text = text[:self.max_message_length - 100] + "\n\n... [消息已截断]"

            resp = self._client.chat_postMessage(
                channel=chat_id,
                text=text,
                thread_ts=thread_id,
                unfurl_links=False,
            )
            if resp.get("ok"):
                return SendResult.ok(resp.get("ts"))
            return SendResult.fail(resp.get("error", "未知错误"))
        except Exception as e:
            logger.error("[%s] 发送失败: %s", self.name, e)
            return SendResult.fail(str(e), error_kind="network")

    async def send_typing(self, chat_id: str, **kwargs) -> None:
        """Slack 没有原生 typing indicator via API（Socket Mode 自动处理）。"""
        pass

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        **kwargs,
    ) -> SendResult:
        """编辑已发送的消息。"""
        if not self._client:
            return SendResult.fail("Slack 客户端未初始化")
        try:
            resp = self._client.chat_update(
                channel=chat_id,
                ts=message_id,
                text=text,
            )
            if resp.get("ok"):
                return SendResult.ok(message_id)
            return SendResult.fail(resp.get("error"))
        except Exception as e:
            return SendResult.fail(str(e))

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """通过 URL 发送图片（使用 Block Kit）。"""
        blocks = []
        if caption:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": caption}})
        blocks.append({"type": "image", "image_url": image_url, "alt_text": caption or "image"})

        if not self._client:
            return SendResult.fail("客户端未初始化")
        try:
            resp = self._client.chat_postMessage(
                channel=chat_id,
                text=caption or "image",
                blocks=blocks,
            )
            return SendResult.ok(resp.get("ts")) if resp.get("ok") else SendResult.fail(resp.get("error"))
        except Exception as e:
            return SendResult.fail(str(e))


# ---------------------------------------------------------------------------
# 自动注册
# ---------------------------------------------------------------------------
from spirit.gateway.platform_registry import platform_registry, PlatformEntry

platform_registry.register(PlatformEntry(
    platform=Platform.SLACK,
    label="Slack",
    adapter_factory=lambda cfg: SlackAdapter(cfg),
    check_fn=check_slack_deps,
    required_env=["SLACK_BOT_TOKEN"],
    install_hint="pip install slack-bolt slack-sdk",
    emoji="🔮",
))
