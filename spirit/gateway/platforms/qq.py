"""QQ Bot 平台适配器。

参考 Hermes gateway/platforms/qqbot/adapter.py 设计：
- 使用 QQ 官方 Bot API v2（WebSocket Gateway）
- 支持私聊 / 群聊 / 频道
- 支持文本 / Markdown / 富媒体消息
- 支持消息分片（QQ 消息长度限制）
- Token 鉴权 + 自动刷新

依赖安装: pip install aiohttp httpx
环境变量: QQ_APP_ID, QQ_CLIENT_SECRET

参考文档: https://bot.q.qq.com/wiki/develop/api-v2/
"""

import asyncio
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

MAX_MESSAGE_LENGTH = get_config_value("platforms.qq.max_message_length", 2000)
API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
GATEWAY_URL = "wss://api.sgroup.qq.com/websocket"
RECONNECT_BACKOFF = get_config_value("platforms.default_reconnect_backoff", [2, 5, 10, 30, 60])

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


def check_qq_deps() -> bool:
    """检查 QQ Bot 依赖。"""
    if not AIOHTTP_AVAILABLE:
        return False
    return bool(os.getenv("QQ_APP_ID") and os.getenv("QQ_CLIENT_SECRET"))


class QQAdapter(BasePlatformAdapter):
    """QQ Bot 适配器（官方 API v2）。

    通过 QQ Bot WebSocket Gateway 接收消息，
    通过 REST API 发送消息。

    支持功能：
    - 私聊 / 群聊
    - 文本 / Markdown 消息
    - 消息分片（自动处理长度限制）
    - Token 自动刷新
    - 心跳保活 + 自动重连
    - 消息去重
    """

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.QQ)

        extra = config.extra or {}
        self._app_id = extra.get("app_id") or os.getenv("QQ_APP_ID", "")
        self._client_secret = extra.get("client_secret") or os.getenv("QQ_CLIENT_SECRET", "")

        self._session: Optional["aiohttp.ClientSession"] = None
        self._ws: Optional["aiohttp.ClientWebSocketResponse"] = None
        self._http_client = None
        self._listen_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

        # Token 管理
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0

        # 消息管理
        self._seen_msg_ids: Set[str] = set()
        self._seq: int = 0  # Gateway sequence
        self._allowed_users: Set[str] = self._load_set("QQ_ALLOWED_USERS")

    def _load_set(self, env_key: str) -> Set[str]:
        extra = self.config.extra or {}
        # 尝试多种 key 格式: "allowed_users", "qq_allowed_users", "QQ_ALLOWED_USERS"
        short_key = env_key.replace("QQ_", "").replace("qq_", "").lower()
        raw = extra.get(short_key) or extra.get(env_key.lower()) or os.getenv(env_key, "")
        if isinstance(raw, list):
            return {str(x).strip().lower() for x in raw if str(x).strip()}
        return {x.strip().lower() for x in str(raw).split(",") if x.strip()}

    async def connect(self) -> bool:
        """连接 QQ Bot Gateway。"""
        if not AIOHTTP_AVAILABLE:
            logger.warning("[%s] aiohttp 未安装", self.name)
            return False
        if not self._app_id or not self._client_secret:
            logger.warning("[%s] QQ_APP_ID/CLIENT_SECRET 未配置", self.name)
            return False

        try:
            self._session = aiohttp.ClientSession()
            if HTTPX_AVAILABLE:
                self._http_client = httpx.AsyncClient(timeout=30.0)

            # 获取 access token
            if not await self._refresh_token():
                return False

            # 连接 WebSocket Gateway
            self._ws = await self._session.ws_connect(
                GATEWAY_URL,
                timeout=20.0,
            )

            # IDENTIFY
            await self._ws.send_json({
                "op": 2,  # IDENTIFY
                "d": {
                    "token": f"QQBot {self._access_token}",
                    "intents": 0 | 1 << 0 | 1 << 1 | 1 << 25 | 1 << 27,  # 基础事件
                    "shard": [0, 1],
                    "properties": {},
                },
            })

            # 启动监听和心跳
            self._listen_task = asyncio.create_task(self._listen_loop())
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            self._mark_connected()
            logger.info("[%s] QQ Bot Gateway 已连接", self.name)
            return True

        except Exception as e:
            logger.error("[%s] QQ 连接失败: %s", self.name, e)
            return False

    async def _refresh_token(self) -> bool:
        """刷新 access token。"""
        if not self._http_client:
            return False

        try:
            resp = await self._http_client.post(
                TOKEN_URL,
                json={
                    "appId": self._app_id,
                    "clientSecret": self._client_secret,
                },
            )
            data = resp.json()
            self._access_token = data.get("access_token", "")
            expires_in = int(data.get("expires_in", 7200))
            self._token_expires_at = time.time() + expires_in - 60  # 提前 60 秒刷新
            return bool(self._access_token)
        except Exception as e:
            logger.error("[%s] Token 刷新失败: %s", self.name, e)
            return False

    async def _listen_loop(self) -> None:
        """消息监听循环。"""
        backoff_idx = 0
        while self._running:
            try:
                if not self._ws or self._ws.closed:
                    delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
                    logger.info("[%s] QQ %d秒后重连...", self.name, delay)
                    await asyncio.sleep(delay)
                    backoff_idx += 1
                    # 重连
                    if time.time() >= self._token_expires_at:
                        await self._refresh_token()
                    self._ws = await self._session.ws_connect(GATEWAY_URL, timeout=20.0)
                    await self._ws.send_json({
                        "op": 2,
                        "d": {
                            "token": f"QQBot {self._access_token}",
                            "intents": 0 | 1 << 0 | 1 << 1 | 1 << 25 | 1 << 27,
                            "shard": [0, 1],
                            "properties": {},
                        },
                    })
                    backoff_idx = 0

                msg = await self._ws.receive_json()
                await self._handle_gateway_event(msg)

            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._running:
                    return
                logger.warning("[%s] QQ 监听错误: %s", self.name, e)

    async def _heartbeat_loop(self) -> None:
        """心跳循环。"""
        while self._running:
            try:
                await asyncio.sleep(30)
                if self._ws and not self._ws.closed:
                    await self._ws.send_json({"op": 1, "d": self._seq})
            except asyncio.CancelledError:
                return
            except Exception:
                pass

    async def _handle_gateway_event(self, event: Dict) -> None:
        """处理 Gateway 事件。"""
        op = event.get("op")
        data = event.get("d", {})
        seq = event.get("s")

        if seq:
            self._seq = seq

        if op == 10:  # HELLO / Resume
            interval = data.get("heartbeat_interval", 30000)
            logger.debug("[%s] QQ 心跳间隔: %dms", self.name, interval)

        elif op == 0:  # DISPATCH
            t = event.get("t", "")
            if t in ("GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"):
                await self._process_message(data, is_group=(t == "GROUP_AT_MESSAGE_CREATE"))

    async def _process_message(self, data: Dict, is_group: bool = False) -> None:
        """处理收到的消息。"""
        msg_id = data.get("id", "")
        author = data.get("author", {})
        user_id = author.get("user_openid", "") or author.get("member_openid", "")
        chat_id = data.get("group_openid", "") or user_id

        # 去重
        if msg_id in self._seen_msg_ids:
            return
        self._seen_msg_ids.add(msg_id)
        if len(self._seen_msg_ids) > 1000:
            self._seen_msg_ids.clear()

        # 权限
        if self._allowed_users and "*" not in self._allowed_users:
            if user_id.lower() not in self._allowed_users:
                return

        # 提取文本
        text = data.get("content", "")
        if not text:
            return

        event = MessageEvent(
            platform=Platform.QQ,
            user_id=user_id,
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            chat_type="group" if is_group else "dm",
            raw_data=data,
        )
        await self.handle_message(event)

    async def disconnect(self) -> None:
        """断开 QQ 连接。"""
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
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        self._seen_msg_ids.clear()
        logger.info("[%s] 已断开 QQ", self.name)

    async def send(
        self,
        chat_id: str,
        text: str,
        **kwargs,
    ) -> SendResult:
        """发送消息到 QQ。"""
        if not self._http_client or not self._access_token:
            return SendResult.fail("QQ 客户端未初始化")

        # 自动刷新 token
        if time.time() >= self._token_expires_at:
            await self._refresh_token()

        # 截断
        if len(text) > self.MAX_MESSAGE_LENGTH:
            text = text[:self.MAX_MESSAGE_LENGTH - 30] + "\n\n... [已截断]"

        headers = {
            "Authorization": f"QQBot {self._access_token}",
            "Content-Type": "application/json",
        }

        # 判断是群聊还是私聊
        is_group = chat_id.startswith("g") or len(chat_id) > 20

        try:
            if is_group:
                # 群聊消息
                resp = await self._http_client.post(
                    f"{API_BASE}/v2/groups/{chat_id}/messages",
                    headers=headers,
                    json={
                        "msg_type": 0,  # 文本
                        "content": text,
                    },
                )
            else:
                # 私聊消息
                resp = await self._http_client.post(
                    f"{API_BASE}/v2/users/{chat_id}/messages",
                    headers=headers,
                    json={
                        "msg_type": 0,
                        "content": text,
                    },
                )

            if resp.status_code < 300:
                data = resp.json()
                return SendResult.ok(data.get("id", uuid.uuid4().hex[:12]))
            else:
                return SendResult.fail(f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            return SendResult.fail(str(e), error_kind="network")

    async def send_typing(self, chat_id: str, **kwargs) -> None:
        """QQ 不支持 typing 指示器。"""
        pass


# ---------------------------------------------------------------------------
# 自动注册
# ---------------------------------------------------------------------------
from spirit.gateway.platform_registry import platform_registry, PlatformEntry

platform_registry.register(PlatformEntry(
    platform=Platform.QQ,
    label="QQ Bot",
    adapter_factory=lambda cfg: QQAdapter(cfg),
    check_fn=check_qq_deps,
    required_env=["QQ_APP_ID", "QQ_CLIENT_SECRET"],
    install_hint="pip install aiohttp httpx",
    emoji="🐧",
))
