"""钉钉平台适配器。

参考 Hermes plugins/platforms/dingtalk/adapter.py 设计：
- 使用 dingtalk-stream SDK（WebSocket 长连接，无需公网 IP）
- 支持文本 / 图片 / 富文本 / 文件
- 支持群聊 @mention 检测
- 支持 session webhook 回复
- 支持 Markdown 格式回复

依赖安装: pip install "dingtalk-stream>=0.20" httpx
环境变量: DINGTALK_CLIENT_ID, DINGTALK_CLIENT_SECRET
"""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from spirit.gateway.config import Platform, PlatformConfig
from spirit.gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    SendResult,
)

from spirit.config import get_config_value

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = get_config_value("platforms.dingtalk.max_message_length", 20000)
RECONNECT_BACKOFF = get_config_value("platforms.default_reconnect_backoff", [2, 5, 10, 30, 60])

# 可选依赖标记
try:
    import dingtalk_stream
    from dingtalk_stream import ChatbotMessage
    from dingtalk_stream.frames import CallbackMessage, AckMessage
    DINGTALK_AVAILABLE = True
except Exception:
    DINGTALK_AVAILABLE = False
    dingtalk_stream = None
    ChatbotMessage = None
    CallbackMessage = None
    AckMessage = type("AckMessage", (), {"STATUS_OK": 200, "STATUS_SYSTEM_EXCEPTION": 500})

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None


def check_dingtalk_deps() -> bool:
    """检查钉钉依赖是否可用。"""
    if not DINGTALK_AVAILABLE or not HTTPX_AVAILABLE:
        return False
    return bool(os.getenv("DINGTALK_CLIENT_ID") and os.getenv("DINGTALK_CLIENT_SECRET"))


class DingTalkAdapter(BasePlatformAdapter):
    """钉钉 Bot 适配器（Stream Mode）。

    通过 dingtalk-stream SDK 建立 WebSocket 长连接，
    无需公网 IP 即可接收消息。回复通过 session_webhook 发送。

    支持功能：
    - 文本 / 富文本消息
    - 图片 / 音频 / 视频 / 文件
    - 群聊 @mention 检测
    - Markdown 格式回复
    - 自动重连
    """

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.DINGTALK)

        extra = config.extra or {}
        self._client_id = extra.get("client_id") or os.getenv("DINGTALK_CLIENT_ID", "")
        self._client_secret = extra.get("client_secret") or os.getenv("DINGTALK_CLIENT_SECRET", "")

        self._stream_client = None
        self._stream_task: Optional[asyncio.Task] = None
        self._http_client = None

        # 群聊控制
        self._require_mention = self._parse_bool_env("DINGTALK_REQUIRE_MENTION", True)
        self._mention_patterns = self._compile_patterns()
        self._allowed_users: Set[str] = self._load_set("DINGTALK_ALLOWED_USERS")

        # session webhook 缓存: chat_id -> (webhook, expired_time_ms)
        self._session_webhooks: Dict[str, tuple] = {}
        self._message_contexts: Dict[str, Any] = {}

    def _parse_bool_env(self, key: str, default: bool) -> bool:
        val = (self.config.extra or {}).get(key.lower().replace("dingtalk_", ""))
        if val is not None:
            return str(val).lower() in ("true", "1", "yes")
        return os.getenv(key, str(default)).lower() in ("true", "1", "yes")

    def _load_set(self, env_key: str) -> Set[str]:
        raw = (self.config.extra or {}).get(env_key.lower()) or os.getenv(env_key, "")
        if isinstance(raw, list):
            return {str(x).strip().lower() for x in raw if str(x).strip()}
        return {x.strip().lower() for x in str(raw).split(",") if x.strip()}

    def _compile_patterns(self) -> List[re.Pattern]:
        raw = (self.config.extra or {}).get("mention_patterns", [])
        if isinstance(raw, str):
            raw = [raw]
        compiled = []
        for p in raw:
            try:
                compiled.append(re.compile(p, re.IGNORECASE))
            except re.error:
                pass
        return compiled

    async def connect(self) -> bool:
        """通过 Stream Mode 连接钉钉。"""
        if not DINGTALK_AVAILABLE:
            logger.warning("[%s] dingtalk-stream 未安装", self.name)
            return False
        if not self._client_id or not self._client_secret:
            logger.warning("[%s] DINGTALK_CLIENT_ID/SECRET 未配置", self.name)
            return False

        try:
            if HTTPX_AVAILABLE:
                self._http_client = httpx.AsyncClient(timeout=30.0)

            credential = dingtalk_stream.Credential(self._client_id, self._client_secret)
            self._stream_client = dingtalk_stream.DingTalkStreamClient(credential)

            loop = asyncio.get_running_loop()
            handler = _IncomingHandler(self, loop)
            self._stream_client.register_callback_handler(
                dingtalk_stream.ChatbotMessage.TOPIC, handler
            )

            self._stream_task = asyncio.create_task(self._run_stream())
            self._mark_connected()
            logger.info("[%s] 钉钉 Stream Mode 已连接", self.name)
            return True
        except Exception as e:
            logger.error("[%s] 钉钉连接失败: %s", self.name, e)
            return False

    async def _run_stream(self) -> None:
        """带自动重连的流客户端。"""
        backoff_idx = 0
        while self._running:
            try:
                await self._stream_client.start()
            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._running:
                    return
                logger.warning("[%s] 流客户端错误: %s", self.name, e)

            delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
            logger.info("[%s] %d秒后重连...", self.name, delay)
            await asyncio.sleep(delay)
            backoff_idx += 1

    async def disconnect(self) -> None:
        """断开钉钉连接。"""
        self._running = False
        self._mark_disconnected()

        if self._stream_task:
            self._stream_task.cancel()
            try:
                await asyncio.wait_for(self._stream_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        self._session_webhooks.clear()
        self._message_contexts.clear()
        logger.info("[%s] 已断开钉钉", self.name)

    async def _process_message(self, message) -> None:
        """处理收到的钉钉消息。"""
        msg_id = getattr(message, "message_id", None) or uuid.uuid4().hex
        conversation_id = getattr(message, "conversation_id", "") or ""
        conversation_type = getattr(message, "conversation_type", "1")
        is_group = str(conversation_type) == "2"
        sender_id = getattr(message, "sender_id", "") or ""
        sender_nick = getattr(message, "sender_nick", "") or sender_id
        sender_staff_id = getattr(message, "sender_staff_id", "") or ""

        chat_id = conversation_id or sender_id
        chat_type = "group" if is_group else "dm"

        # 用户权限检查
        if self._allowed_users and "*" not in self._allowed_users:
            candidates = {sender_id.lower(), sender_staff_id.lower()}
            if not (candidates & self._allowed_users):
                return

        # 群聊 mention 检查
        if is_group and self._require_mention:
            mentions_bot = getattr(message, "is_in_at_list", False)
            text_early = self._extract_text(message)
            matches_pattern = any(p.search(text_early) for p in self._mention_patterns)
            if not mentions_bot and not matches_pattern:
                return

        # 缓存 session webhook
        webhook = getattr(message, "session_webhook", "") or ""
        if webhook and chat_id:
            expired = getattr(message, "session_webhook_expired_time", 0) or 0
            self._session_webhooks[chat_id] = (webhook, expired)
            self._message_contexts[chat_id] = message

        text = self._extract_text(message)
        if not text:
            return

        event = MessageEvent(
            platform=Platform.DINGTALK,
            user_id=sender_id,
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            username=sender_nick,
            chat_type=chat_type,
            raw_data={"staff_id": sender_staff_id},
        )
        await self.handle_message(event)

    @staticmethod
    def _extract_text(message) -> str:
        """从钉钉消息提取文本。"""
        text = getattr(message, "text", None) or ""
        if hasattr(text, "content"):
            return (text.content or "").strip()
        if isinstance(text, dict):
            return text.get("content", "").strip()
        return str(text).strip()

    def _get_valid_webhook(self, chat_id: str) -> Optional[str]:
        """获取有效的 session webhook。"""
        info = self._session_webhooks.get(chat_id)
        if not info:
            return None
        webhook, expired_ms = info
        if expired_ms and expired_ms > 0:
            now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
            if now_ms + 300000 >= expired_ms:
                self._session_webhooks.pop(chat_id, None)
                return None
        return webhook

    async def send(
        self,
        chat_id: str,
        text: str,
        **kwargs,
    ) -> SendResult:
        """通过 session webhook 发送 Markdown 消息。"""
        webhook = self._get_valid_webhook(chat_id)
        if not webhook:
            return SendResult.fail("无可用 session_webhook，需先收到消息")
        if not self._http_client:
            return SendResult.fail("HTTP 客户端未初始化")

        # 截断
        if len(text) > self.MAX_MESSAGE_LENGTH:
            text = text[:self.MAX_MESSAGE_LENGTH - 50] + "\n\n... [已截断]"

        payload = {
            "msgtype": "markdown",
            "markdown": {"title": "Spirit", "text": text},
        }

        try:
            resp = await self._http_client.post(webhook, json=payload, timeout=15.0)
            if resp.status_code < 300:
                return SendResult.ok(uuid.uuid4().hex[:12])
            return SendResult.fail(f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            return SendResult.fail(str(e), error_kind="network")

    async def send_typing(self, chat_id: str, **kwargs) -> None:
        """钉钉不支持 typing 指示器。"""
        pass


# ---------------------------------------------------------------------------
# 内部流处理器
# ---------------------------------------------------------------------------

class _IncomingHandler(
    dingtalk_stream.ChatbotHandler if DINGTALK_AVAILABLE else object
):
    """dingtalk-stream 消息处理器。"""

    def __init__(self, adapter, loop=None):
        if DINGTALK_AVAILABLE:
            super().__init__()
        self._adapter = adapter
        self._loop = loop

    def pre_start(self):
        return

    async def process(self, message):
        try:
            data = message.data
            if isinstance(data, str):
                data = json.loads(data)
            chatbot_msg = ChatbotMessage.from_dict(data)

            # 确保 session_webhook 存在
            if not getattr(chatbot_msg, "session_webhook", None):
                wh = data.get("sessionWebhook") or data.get("session_webhook", "")
                if wh:
                    chatbot_msg.session_webhook = wh

            # 确保 is_in_at_list
            if not getattr(chatbot_msg, "is_in_at_list", False):
                if data.get("isInAtList"):
                    chatbot_msg.is_in_at_list = True

            asyncio.create_task(self._safe_process(chatbot_msg))
        except Exception:
            logger.exception("[%s] 处理消息错误", self._adapter.name)
            return AckMessage.STATUS_SYSTEM_EXCEPTION, "error"
        return AckMessage.STATUS_OK, "OK"

    async def _safe_process(self, msg):
        try:
            await self._adapter._process_message(msg)
        except Exception:
            logger.exception("[%s] 消息处理异常", self._adapter.name)


# ---------------------------------------------------------------------------
# 自动注册
# ---------------------------------------------------------------------------
from spirit.gateway.platform_registry import platform_registry, PlatformEntry

platform_registry.register(PlatformEntry(
    platform=Platform.DINGTALK,
    label="钉钉 DingTalk",
    adapter_factory=lambda cfg: DingTalkAdapter(cfg),
    check_fn=check_dingtalk_deps,
    required_env=["DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"],
    install_hint="pip install 'dingtalk-stream>=0.20' httpx",
    emoji="🐳",
))
