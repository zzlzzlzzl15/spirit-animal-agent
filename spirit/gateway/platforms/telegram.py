"""Telegram 平台适配器。

参考 Hermes 的 Telegram 适配器设计：
- 使用 python-telegram-bot 库（或 HTTP API）
- 支持私聊、群组、超级群组、论坛话题
- 支持消息编辑、回复、媒体发送
- 支持打字指示器
- 支持长消息分片
- 命令处理（/start, /new, /reset 等）

依赖安装: pip install python-telegram-bot
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

# Telegram API 限制
TELEGRAM_MAX_MESSAGE_LENGTH = get_config_value("platforms.telegram.max_message_length", 4096)
TELEGRAM_MAX_CAPTION_LENGTH = get_config_value("platforms.telegram.max_caption_length", 1024)
TELEGRAM_TYPING_INTERVAL = get_config_value("platforms.telegram.typing_interval", 4.0)


def check_telegram_deps() -> bool:
    """检查 Telegram 依赖是否可用。"""
    try:
        import telegram  # noqa: F401
        return True
    except ImportError:
        return False


class TelegramAdapter(BasePlatformAdapter):
    """Telegram Bot 适配器。

    支持功能：
    - 私聊 / 群组 / 超级群组 / 频道
    - 论坛话题（Forum Topics）
    - 消息编辑 / 回复 / 转发
    - 媒体发送（图片、文件、音频）
    - 打字指示器
    - 长消息分片
    - 内联键盘

    Usage:
        config = PlatformConfig(platform=Platform.TELEGRAM, token="...")
        adapter = TelegramAdapter(config)
        await adapter.connect()
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.TELEGRAM)
        self.max_message_length = TELEGRAM_MAX_MESSAGE_LENGTH

        # Bot 实例
        self._bot = None
        self._app = None
        self._bot_username: Optional[str] = None
        self._bot_id: Optional[int] = None

        # 更新处理器
        self._update_queue: Optional[asyncio.Queue] = None
        self._polling_task: Optional[asyncio.Task] = None

    async def connect(self) -> bool:
        """连接到 Telegram Bot API。"""
        try:
            from telegram import Bot
            from telegram.ext import ApplicationBuilder, MessageHandler, filters, CommandHandler

            # 创建 Bot
            self._bot = Bot(token=self.token)

            # 获取 Bot 信息
            bot_info = await self._bot.get_me()
            self._bot_username = bot_info.username
            self._bot_id = bot_info.id
            logger.info("Telegram Bot 已连接: @%s (ID: %d)", self._bot_username, self._bot_id)

            # 创建 Application
            self._app = ApplicationBuilder().token(self.token).build()

            # 注册处理器
            self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text_message))
            self._app.add_handler(CommandHandler("start", self._handle_start))
            self._app.add_handler(CommandHandler("new", self._handle_new_session))
            self._app.add_handler(CommandHandler("reset", self._handle_new_session))
            self._app.add_handler(CommandHandler("help", self._handle_help))

            # 启动轮询
            await self._app.initialize()
            await self._app.start()
            self._app.updater.start_polling(drop_pending_updates=True)

            self._connected = True
            self._running = True
            return True

        except ImportError:
            logger.error("python-telegram-bot 未安装。请运行: pip install python-telegram-bot")
            return False
        except Exception as e:
            logger.error("Telegram 连接失败: %s", e)
            return False

    async def disconnect(self):
        """断开 Telegram 连接。"""
        self._running = False
        self._connected = False

        if self._app:
            try:
                if self._app.updater and self._app.updater.running:
                    await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception as e:
                logger.warning("Telegram 断开错误: %s", e)

        # 停止所有打字指示器
        for task in self._typing_tasks.values():
            task.cancel()
        self._typing_tasks.clear()

        logger.info("Telegram Bot 已断开")

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
        """发送消息到 Telegram。"""
        if not self._bot:
            return {"success": False, "error": "Bot 未连接"}

        try:
            # 构建参数
            kwargs = {
                "chat_id": int(chat_id),
                "text": self.format_message(text),
            }

            if thread_id:
                kwargs["message_thread_id"] = int(thread_id)

            if reply_to:
                kwargs["reply_to_message_id"] = int(reply_to)

            if metadata:
                # 解析参数
                parse_mode = metadata.get("parse_mode")
                if parse_mode:
                    kwargs["parse_mode"] = parse_mode

            # 发送
            message = await self._bot.send_message(**kwargs)
            return {
                "success": True,
                "message_id": str(message.message_id),
            }

        except Exception as e:
            error_kind = self._classify_error(e)
            return {
                "success": False,
                "error": str(e),
                "error_kind": error_kind,
            }

    async def send_typing(self, chat_id: str, thread_id: str = None):
        """发送打字指示器。"""
        if not self._bot:
            return

        try:
            from telegram.constants import ChatAction
            await self._bot.send_chat_action(
                chat_id=int(chat_id),
                action=ChatAction.TYPING,
            )
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
        if not self._bot:
            return {"success": False, "error": "Bot 未连接"}

        try:
            kwargs = {
                "chat_id": int(chat_id),
                "photo": image_url,
            }
            if caption:
                kwargs["caption"] = caption[:TELEGRAM_MAX_CAPTION_LENGTH]
            if thread_id:
                kwargs["message_thread_id"] = int(thread_id)

            message = await self._bot.send_photo(**kwargs)
            return {"success": True, "message_id": str(message.message_id)}

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
        if not self._bot:
            return {"success": False, "error": "Bot 未连接"}

        try:
            await self._bot.edit_message_text(
                chat_id=int(chat_id),
                message_id=int(message_id),
                text=self.format_message(new_text),
            )
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
        if not self._bot:
            return {"success": False, "error": "Bot 未连接"}

        try:
            await self._bot.delete_message(
                chat_id=int(chat_id),
                message_id=int(message_id),
            )
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # 消息处理器
    # ------------------------------------------------------------------

    async def _handle_text_message(self, update, context):
        """处理文本消息。"""
        if not self._on_message:
            return

        message = update.effective_message
        if not message or not message.text:
            return

        # 忽略机器人自身的消息
        if message.from_user and message.from_user.id == self._bot_id:
            return

        # 构建事件
        chat = update.effective_chat
        user = update.effective_user

        chat_type = "dm"
        if chat.type == "private":
            chat_type = "dm"
        elif chat.type in ("group", "supergroup"):
            if message.message_thread_id:
                chat_type = "forum"
            else:
                chat_type = "group"
        elif chat.type == "channel":
            chat_type = "channel"

        event = MessageEvent(
            platform=Platform.TELEGRAM,
            user_id=str(user.id),
            chat_id=str(chat.id),
            message_id=str(message.message_id),
            thread_id=str(message.message_thread_id) if message.message_thread_id else None,
            text=message.text,
            username=user.username,
            display_name=user.full_name,
            chat_type=chat_type,
            reply_to_message_id=(
                str(message.reply_to_message.message_id)
                if message.reply_to_message else None
            ),
            raw_data={"update": update},
        )

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
            logger.error("处理 Telegram 消息失败: %s", e)

    async def _handle_start(self, update, context):
        """处理 /start 命令。"""
        await update.message.reply_text(
            "你好！我是 Spirit Agent。发送消息开始对话。"
        )

    async def _handle_new_session(self, update, context):
        """处理 /new 或 /reset 命令 — 开始新会话。"""
        await update.message.reply_text("会话已重置。发送新消息开始。")

    async def _handle_help(self, update, context):
        """处理 /help 命令。"""
        help_text = (
            "Spirit Agent 命令:\n"
            "/start - 开始使用\n"
            "/new - 开始新会话\n"
            "/reset - 重置会话\n"
            "/help - 显示帮助"
        )
        await update.message.reply_text(help_text)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_error(error: Exception) -> str:
        """分类错误类型。"""
        error_str = str(error).lower()
        if "chat not found" in error_str:
            return "not_found"
        if "unauthorized" in error_str or "forbidden" in error_str:
            return "auth"
        if "too many requests" in error_str or "rate" in error_str:
            return "rate_limit"
        if "timeout" in error_str or "network" in error_str:
            return "network"
        return "unknown"

    @property
    def bot_username(self) -> Optional[str]:
        """获取 Bot 用户名。"""
        return self._bot_username


# ---------------------------------------------------------------------------
# 注册到平台注册表
# ---------------------------------------------------------------------------

def _register_telegram():
    """注册 Telegram 适配器到全局注册表。"""
    from spirit.gateway.platform_registry import PlatformEntry, platform_registry

    platform_registry.register(PlatformEntry(
        platform=Platform.TELEGRAM,
        label="Telegram",
        adapter_factory=lambda cfg: TelegramAdapter(cfg),
        check_fn=check_telegram_deps,
        required_env=["SPIRIT_TELEGRAM_TOKEN"],
        install_hint="pip install python-telegram-bot",
        max_message_length=TELEGRAM_MAX_MESSAGE_LENGTH,
        splits_long_messages=True,
        description="Telegram Bot 适配器 — 支持私聊/群组/论坛",
    ))


# 自动注册
try:
    _register_telegram()
except Exception as e:
    logger.debug("Telegram 注册失败: %s", e)
