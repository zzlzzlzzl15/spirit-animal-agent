"""企业微信 (WeCom) 平台适配器。

参考 Hermes plugins/platforms/wecom/adapter.py 设计：
- 使用企业微信 AI Bot WebSocket 网关
- 支持私聊 / 群组
- 支持 Markdown 消息
- 支持文件 / 图片收发
- 心跳保活 + 自动重连

依赖安装: pip install aiohttp httpx
环境变量: WECOM_BOT_ID, WECOM_SECRET
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Set

from spirit.gateway.config import Platform, PlatformConfig
from spirit.gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    SendResult,
)

from spirit.config import get_config_value

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = get_config_value("platforms.wecom.max_message_length", 4000)
CONNECT_TIMEOUT = get_config_value("platforms.wecom.connect_timeout", 20.0)
HEARTBEAT_INTERVAL = get_config_value("platforms.default_heartbeat_interval", 30.0)
RECONNECT_BACKOFF = get_config_value("platforms.default_reconnect_backoff", [2, 5, 10, 30, 60])

# WebSocket 命令
CMD_SUBSCRIBE = "aibot_subscribe"
CMD_CALLBACK = "aibot_msg_callback"
CMD_SEND = "aibot_send_msg"
CMD_PING = "ping"

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    aiohttp = None

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None

DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"


def check_wecom_deps() -> bool:
    """检查企业微信依赖。"""
    if not AIOHTTP_AVAILABLE:
        return False
    return bool(os.getenv("WECOM_BOT_ID") and os.getenv("WECOM_SECRET"))


class WeComAdapter(BasePlatformAdapter):
    """企业微信 AI Bot 适配器。

    通过 WebSocket 连接企业微信 AI Bot 网关，
    支持 Markdown 消息收发、文件上传、心跳保活。

    支持功能：
    - 私聊 / 群组
    - Markdown 格式消息
    - 文件 / 图片上传（分片）
    - 心跳保活 + 自动重连
    - 消息去重
    """

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WECOM)

        extra = config.extra or {}
        self._bot_id = str(extra.get("bot_id") or os.getenv("WECOM_BOT_ID", "")).strip()
        self._secret = str(extra.get("secret") or os.getenv("WECOM_SECRET", "")).strip()
        self._ws_url = str(extra.get("websocket_url") or os.getenv("WECOM_WEBSOCKET_URL", DEFAULT_WS_URL)).strip()

        self._session: Optional["aiohttp.ClientSession"] = None
        self._ws: Optional["aiohttp.ClientWebSocketResponse"] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._pending: Dict[str, asyncio.Future] = {}
        self._seen_msg_ids: Set[str] = set()
        self._device_id = uuid.uuid4().hex

        # 允许列表
        self._allowed_users: Set[str] = self._load_set("WECOM_ALLOWED_USERS")
        self._group_allowed: Set[str] = self._load_set("WECOM_ALLOWED_GROUPS")

    def _load_set(self, env_key: str) -> Set[str]:
        raw = (self.config.extra or {}).get(env_key.lower().replace("wecom_", "")) or os.getenv(env_key, "")
        if isinstance(raw, list):
            return {str(x).strip().lower() for x in raw if str(x).strip()}
        return {x.strip().lower() for x in str(raw).split(",") if x.strip()}

    async def connect(self) -> bool:
        """连接企业微信 WebSocket 网关。"""
        if not AIOHTTP_AVAILABLE:
            logger.warning("[%s] aiohttp 未安装", self.name)
            return False
        if not self._bot_id or not self._secret:
            logger.warning("[%s] WECOM_BOT_ID/SECRET 未配置", self.name)
            return False

        try:
            self._session = aiohttp.ClientSession()

            # 订阅
            subscribe_msg = {
                "cmd": CMD_SUBSCRIBE,
                "seq": 1,
                "data": {
                    "bot_id": self._bot_id,
                    "secret": self._secret,
                    "device_id": self._device_id,
                },
            }

            # 连接 WebSocket
            self._ws = await self._session.ws_connect(
                self._ws_url,
                timeout=CONNECT_TIMEOUT,
            )
            await self._ws.send_json(subscribe_msg)

            # 等待订阅确认
            resp = await asyncio.wait_for(self._ws.receive_json(), timeout=CONNECT_TIMEOUT)
            if resp.get("cmd") != CMD_SUBSCRIBE or resp.get("code", -1) != 0:
                logger.error("[%s] 订阅失败: %s", self.name, resp)
                return False

            # 启动监听和心跳
            self._listen_task = asyncio.create_task(self._listen_loop())
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            self._mark_connected()
            logger.info("[%s] 企业微信 WebSocket 已连接", self.name)
            return True

        except Exception as e:
            logger.error("[%s] 企业微信连接失败: %s", self.name, e)
            return False

    async def _listen_loop(self) -> None:
        """消息监听循环。"""
        backoff_idx = 0
        while self._running:
            try:
                if not self._ws or self._ws.closed:
                    # 重连
                    delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
                    logger.info("[%s] 企业微信 %d秒后重连...", self.name, delay)
                    await asyncio.sleep(delay)
                    backoff_idx += 1
                    self._ws = await self._session.ws_connect(self._ws_url, timeout=CONNECT_TIMEOUT)
                    # 重新订阅
                    await self._ws.send_json({
                        "cmd": CMD_SUBSCRIBE, "seq": 1,
                        "data": {"bot_id": self._bot_id, "secret": self._secret, "device_id": self._device_id},
                    })
                    await self._ws.receive_json()
                    backoff_idx = 0

                msg = await self._ws.receive_json()
                await self._handle_ws_message(msg)

            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._running:
                    return
                logger.warning("[%s] WS 监听错误: %s", self.name, e)

    async def _heartbeat_loop(self) -> None:
        """心跳保活。"""
        while self._running:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if self._ws and not self._ws.closed:
                    await self._ws.send_json({"cmd": CMD_PING, "seq": 0})
            except asyncio.CancelledError:
                return
            except Exception:
                pass

    async def _handle_ws_message(self, msg: Dict) -> None:
        """处理 WebSocket 消息。"""
        cmd = msg.get("cmd", "")
        req_id = msg.get("req_id", "")

        # 响应匹配
        if req_id in self._pending:
            future = self._pending.pop(req_id)
            if not future.done():
                future.set_result(msg)
            return

        # 回调消息
        if cmd == CMD_CALLBACK:
            await self._on_callback(msg)

    async def _on_callback(self, msg: Dict) -> None:
        """处理消息回调。"""
        data = msg.get("data", {})
        msg_id = data.get("msg_id", "")
        chat_id = data.get("chat_id", "") or data.get("from", {}).get("user_id", "")
        is_group = data.get("chat_type", "") == "group"
        sender_id = data.get("from", {}).get("user_id", "")
        sender_name = data.get("from", {}).get("name", sender_id)

        # 去重
        if msg_id in self._seen_msg_ids:
            return
        self._seen_msg_ids.add(msg_id)
        if len(self._seen_msg_ids) > 1000:
            self._seen_msg_ids.clear()

        # 权限检查
        if self._allowed_users and "*" not in self._allowed_users:
            if sender_id.lower() not in self._allowed_users:
                return
        if is_group and self._group_allowed:
            if chat_id.lower() not in self._group_allowed:
                return

        # 提取文本
        msg_type = data.get("msg_type", "text")
        text = ""
        if msg_type == "text":
            text = data.get("text", {}).get("content", "")
        elif msg_type == "markdown":
            text = data.get("markdown", {}).get("content", "")

        if not text:
            return

        event = MessageEvent(
            platform=Platform.WECOM,
            user_id=sender_id,
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            username=sender_name,
            chat_type="group" if is_group else "dm",
            raw_data=data,
        )
        await self.handle_message(event)

    async def disconnect(self) -> None:
        """断开企业微信连接。"""
        self._running = False
        self._mark_disconnected()

        for task in [self._listen_task, self._heartbeat_task]:
            if task:
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=3.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()

        self._pending.clear()
        self._seen_msg_ids.clear()
        logger.info("[%s] 已断开企业微信", self.name)

    async def send(
        self,
        chat_id: str,
        text: str,
        **kwargs,
    ) -> SendResult:
        """发送 Markdown 消息到企业微信。"""
        if not self._ws or self._ws.closed:
            return SendResult.fail("WebSocket 未连接")

        if len(text) > self.MAX_MESSAGE_LENGTH:
            text = text[:self.MAX_MESSAGE_LENGTH - 30] + "\n\n... [已截断]"

        req_id = uuid.uuid4().hex
        payload = {
            "cmd": CMD_SEND,
            "req_id": req_id,
            "data": {
                "chat_id": chat_id,
                "msg_type": "markdown",
                "markdown": {"content": text},
            },
        }

        try:
            future = asyncio.get_running_loop().create_future()
            self._pending[req_id] = future

            await self._ws.send_json(payload)

            # 等待响应（超时 15 秒）
            try:
                resp = await asyncio.wait_for(future, timeout=15.0)
                if resp.get("code", -1) == 0:
                    return SendResult.ok(resp.get("data", {}).get("msg_id", req_id))
                return SendResult.fail(f"发送失败: {resp.get('msg', '未知错误')}")
            except asyncio.TimeoutError:
                return SendResult.fail("发送超时", error_kind="network")
        except Exception as e:
            return SendResult.fail(str(e), error_kind="network")

    async def send_typing(self, chat_id: str, **kwargs) -> None:
        """企业微信不支持 typing 指示器。"""
        pass


# ---------------------------------------------------------------------------
# 自动注册
# ---------------------------------------------------------------------------
from spirit.gateway.platform_registry import platform_registry, PlatformEntry

platform_registry.register(PlatformEntry(
    platform=Platform.WECOM,
    label="企业微信 WeCom",
    adapter_factory=lambda cfg: WeComAdapter(cfg),
    check_fn=check_wecom_deps,
    required_env=["WECOM_BOT_ID", "WECOM_SECRET"],
    install_hint="pip install aiohttp httpx",
    emoji="💼",
))
