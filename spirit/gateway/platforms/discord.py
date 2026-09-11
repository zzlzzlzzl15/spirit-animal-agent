"""Discord 平台适配器。

参考 Hermes 的 Discord 适配器设计：
- 使用 discord.py 库
- 支持 DM、服务器频道、线程
- 支持消息编辑、回复、嵌入
- 支持打字指示器
- 支持长消息分片
- 斜杠命令支持

依赖安装: pip install discord.py
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from spirit.gateway.config import Platform, PlatformConfig
from spirit.gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    SendResult,
)

logger = logging.getLogger(__name__)

from spirit.config import get_config_value

# Discord API 限制
DISCORD_MAX_MESSAGE_LENGTH = get_config_value("platforms.discord.max_message_length", 2000)
DISCORD_MAX_EMBED_LENGTH = get_config_value("platforms.discord.max_embed_length", 4096)
DISCORD_TYPING_INTERVAL = get_config_value("platforms.discord.typing_interval", 5.0)


def check_discord_deps() -> bool:
    """检查 Discord 依赖是否可用。"""
    try:
        import discord  # noqa: F401
        return True
    except ImportError:
        return False


class DiscordAdapter(BasePlatformAdapter):
    """Discord Bot 适配器。

    支持功能：
    - DM / 服务器频道 / 线程
    - 消息编辑 / 回复
    - 嵌入消息（Embed）
    - 打字指示器
    - 长消息分片
    - 斜杠命令

    Usage:
        config = PlatformConfig(platform=Platform.DISCORD, token="...")
        adapter = DiscordAdapter(config)
        await adapter.connect()
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.DISCORD)
        self.max_message_length = DISCORD_MAX_MESSAGE_LENGTH

        # Bot 实例
        self._client = None
        self._bot_user: Optional[str] = None
        self._bot_id: Optional[int] = None

        # 频道缓存
        self._channel_cache: Dict[str, Any] = {}

        # 事件循环引用
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def connect(self) -> bool:
        """连接到 Discord。"""
        try:
            import discord

            # 设置 Intents
            intents = discord.Intents.default()
            intents.message_content = True
            intents.messages = True
            intents.guilds = True

            # 创建 Client
            self._client = discord.Client(intents=intents)

            # 注册事件
            @self._client.event
            async def on_ready():
                self._bot_user = str(self._client.user)
                self._bot_id = self._client.user.id
                logger.info("Discord Bot 已连接: %s (ID: %d)", self._bot_user, self._bot_id)
                self._connected = True

            @self._client.event
            async def on_message(message):
                await self._handle_message(message)

            # 启动 Bot（在后台任务中）
            self._loop = asyncio.get_event_loop()
            self._bot_task = asyncio.create_task(self._client.start(self.token))

            # 等待连接就绪
            for _ in range(30):
                if self._connected:
                    break
                await asyncio.sleep(0.5)
            else:
                logger.error("Discord 连接超时")
                return False

            self._running = True
            return True

        except ImportError:
            logger.error("discord.py 未安装。请运行: pip install discord.py")
            return False
        except Exception as e:
            logger.error("Discord 连接失败: %s", e)
            return False

    async def disconnect(self):
        """断开 Discord 连接。"""
        self._running = False
        self._connected = False

        if self._client and not self._client.is_closed():
            await self._client.close()

        # 停止所有打字指示器
        for task in self._typing_tasks.values():
            task.cancel()
        self._typing_tasks.clear()

        logger.info("Discord Bot 已断开")

    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        thread_id: str = None,
        reply_to: str = None,
        media_url: str = None,
        metadata: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """发送消息到 Discord。"""
        if not self._client:
            return {"success": False, "error": "Client 未连接"}

        try:
            import discord

            # 获取频道
            channel = await self._get_channel(chat_id, thread_id)
            if channel is None:
                return {"success": False, "error": f"频道 {chat_id} 未找到", "error_kind": "not_found"}

            # 构建消息参数
            kwargs = {}

            # 回复
            if reply_to:
                try:
                    ref_message = await channel.fetch_message(int(reply_to))
                    kwargs["reference"] = ref_message
                except Exception:
                    pass

            # 嵌入消息（如果指定）
            if metadata and metadata.get("embed"):
                embed_data = metadata["embed"]
                embed = discord.Embed(
                    title=embed_data.get("title"),
                    description=embed_data.get("description", text)[:DISCORD_MAX_EMBED_LENGTH],
                    color=embed_data.get("color", 0x3498db),
                )
                kwargs["embed"] = embed
                message = await channel.send(**kwargs)
            else:
                # 普通文本消息
                formatted = self.format_message(text)
                message = await channel.send(formatted, **kwargs)

            return {"success": True, "message_id": str(message.id)}

        except Exception as e:
            error_kind = self._classify_error(e)
            return {"success": False, "error": str(e), "error_kind": error_kind}

    async def send_typing(self, chat_id: str, thread_id: str = None):
        """发送打字指示器。"""
        if not self._client:
            return

        try:
            channel = await self._get_channel(chat_id, thread_id)
            if channel:
                await channel.typing()
        except Exception as e:
            logger.debug("发送打字指示器失败: %s", e)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: str = None,
        thread_id: str = None,
    ) -> Dict[str, Any]:
        """发送图片。"""
        if not self._client:
            return {"success": False, "error": "Client 未连接"}

        try:
            import discord

            channel = await self._get_channel(chat_id, thread_id)
            if channel is None:
                return {"success": False, "error": "频道未找到"}

            # 使用嵌入显示图片
            embed = discord.Embed()
            embed.set_image(url=image_url)
            if caption:
                embed.description = caption[:DISCORD_MAX_EMBED_LENGTH]

            message = await channel.send(embed=embed)
            return {"success": True, "message_id": str(message.id)}

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        new_text: str,
        thread_id: str = None,
    ) -> Dict[str, Any]:
        """编辑消息。"""
        if not self._client:
            return {"success": False, "error": "Client 未连接"}

        try:
            channel = await self._get_channel(chat_id, thread_id)
            if channel is None:
                return {"success": False, "error": "频道未找到"}

            message = await channel.fetch_message(int(message_id))
            await message.edit(content=self.format_message(new_text))
            return {"success": True}

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def delete_message(
        self,
        chat_id: str,
        message_id: str,
        thread_id: str = None,
    ) -> Dict[str, Any]:
        """删除消息。"""
        if not self._client:
            return {"success": False, "error": "Client 未连接"}

        try:
            channel = await self._get_channel(chat_id, thread_id)
            if channel is None:
                return {"success": False, "error": "频道未找到"}

            message = await channel.fetch_message(int(message_id))
            await message.delete()
            return {"success": True}

        except Exception as e:
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # 消息处理器
    # ------------------------------------------------------------------

    async def _handle_message(self, message):
        """处理 Discord 消息。"""
        import discord

        if not self._on_message:
            return

        # 忽略机器人自身的消息
        if message.author == self._client.user:
            return

        # 忽略机器人消息
        if message.author.bot:
            return

        # 确定聊天类型
        if isinstance(message.channel, discord.DMChannel):
            chat_type = "dm"
            chat_id = str(message.channel.id)
        elif isinstance(message.channel, discord.Thread):
            chat_type = "forum" if message.channel.parent and hasattr(message.channel.parent, 'is_forum') and message.channel.parent.is_forum() else "group"
            chat_id = str(message.channel.parent.id) if message.channel.parent else str(message.channel.id)
        else:
            chat_type = "group"
            chat_id = str(message.channel.id)

        # 构建事件
        event = MessageEvent(
            platform=Platform.DISCORD,
            user_id=str(message.author.id),
            chat_id=chat_id,
            message_id=str(message.id),
            thread_id=str(message.channel.id) if isinstance(message.channel, discord.Thread) else None,
            text=message.content,
            username=message.author.name,
            display_name=message.author.display_name,
            chat_type=chat_type,
            reply_to_message_id=(
                str(message.reference.message_id)
                if message.reference else None
            ),
            raw_data={"message": message},
        )

        # 检查是否提及 Bot
        if self._client.user and self._client.user.mentioned_in(message):
            # 移除 @mention
            event.text = self.strip_mentions(message.content, self._client.user.name)

        # 调用回调
        try:
            response = await self._on_message(event)
            if response:
                await self.send(
                    chat_id=event.chat_id,
                    text=response,
                    thread_id=event.thread_id,
                    reply_to=event.message_id,
                )
        except Exception as e:
            logger.error("处理 Discord 消息失败: %s", e)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    async def _get_channel(self, chat_id: str, thread_id: str = None):
        """获取频道对象（带缓存）。"""
        import discord

        cache_key = f"{chat_id}:{thread_id}" if thread_id else chat_id

        if cache_key in self._channel_cache:
            return self._channel_cache[cache_key]

        try:
            if thread_id:
                # 获取线程
                channel = self._client.get_channel(int(thread_id))
                if channel is None:
                    channel = await self._client.fetch_channel(int(thread_id))
            else:
                # 获取频道
                channel = self._client.get_channel(int(chat_id))
                if channel is None:
                    channel = await self._client.fetch_channel(int(chat_id))

            if channel:
                self._channel_cache[cache_key] = channel
            return channel

        except Exception as e:
            logger.debug("获取频道失败 %s: %s", chat_id, e)
            return None

    @staticmethod
    def _classify_error(error: Exception) -> str:
        """分类错误类型。"""
        error_str = str(error).lower()
        if "unknown channel" in error_str or "unknown message" in error_str:
            return "not_found"
        if "missing permissions" in error_str or "forbidden" in error_str:
            return "auth"
        if "rate limit" in error_str or "429" in error_str:
            return "rate_limit"
        if "timeout" in error_str or "connection" in error_str:
            return "network"
        return "unknown"


# ---------------------------------------------------------------------------
# 注册到平台注册表
# ---------------------------------------------------------------------------

def _register_discord():
    """注册 Discord 适配器到全局注册表。"""
    from spirit.gateway.platform_registry import PlatformEntry, platform_registry

    platform_registry.register(PlatformEntry(
        platform=Platform.DISCORD,
        label="Discord",
        adapter_factory=lambda cfg: DiscordAdapter(cfg),
        check_fn=check_discord_deps,
        required_env=["SPIRIT_DISCORD_TOKEN"],
        install_hint="pip install discord.py",
        max_message_length=DISCORD_MAX_MESSAGE_LENGTH,
        splits_long_messages=True,
        description="Discord Bot 适配器 — 支持 DM/频道/线程",
    ))


# 自动注册
try:
    _register_discord()
except Exception as e:
    logger.debug("Discord 注册失败: %s", e)
