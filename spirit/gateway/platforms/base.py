"""平台适配器基类。

参考 Hermes 的 gateway/platforms/base.py：
- BasePlatformAdapter: 所有平台适配器的抽象基类
- SendResult: 发送结果
- MessageEvent: 接收到的消息事件
- 通用功能：消息格式化、分片、打字指示器等
"""

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from spirit.gateway.config import Platform, PlatformConfig
from spirit.gateway.session import SessionSource
from spirit.config import get_config_value

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class SendResult:
    """发送结果。

    参考 Hermes 的 SendResult。
    """
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    error_kind: Optional[str] = None  # rate_limit / auth / not_found / network
    truncated: bool = False

    @classmethod
    def ok(cls, message_id: str = None) -> "SendResult":
        return cls(success=True, message_id=message_id)

    @classmethod
    def fail(cls, error: str, error_kind: str = None) -> "SendResult":
        return cls(success=False, error=error, error_kind=error_kind)


@dataclass
class MessageEvent:
    """接收到的消息事件。

    参考 Hermes 的消息事件结构。
    """
    # 来源信息
    platform: Platform
    user_id: str
    chat_id: str
    message_id: Optional[str] = None
    thread_id: Optional[str] = None

    # 消息内容
    text: str = ""
    media_url: Optional[str] = None
    media_type: Optional[str] = None  # image / audio / video / document

    # 用户信息
    username: Optional[str] = None
    display_name: Optional[str] = None
    chat_type: str = "dm"  # dm / group / forum / channel

    # 元数据
    reply_to_message_id: Optional[str] = None
    is_command: bool = False
    timestamp: datetime = field(default_factory=datetime.now)
    raw_data: Dict[str, Any] = field(default_factory=dict)

    def to_source(self) -> SessionSource:
        """转换为 SessionSource。"""
        return SessionSource(
            platform=self.platform,
            user_id=self.user_id,
            chat_id=self.chat_id,
            thread_id=self.thread_id,
            message_id=self.message_id,
            chat_type=self.chat_type,
            username=self.username,
            display_name=self.display_name,
        )

    @property
    def is_from_bot(self) -> bool:
        """检查消息是否来自机器人自身。"""
        return False  # 子类可覆盖


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------

class BasePlatformAdapter(ABC):
    """平台适配器抽象基类。

    参考 Hermes 的 BasePlatformAdapter：
    - 所有平台适配器必须继承此类
    - 提供通用功能（消息格式化、分片等）
    - 定义必须实现的抽象方法

    子类必须实现：
    - connect(): 连接到平台
    - disconnect(): 断开连接
    - send(): 发送文本消息
    - send_typing(): 发送打字指示器
    """

    def __init__(self, config: PlatformConfig, platform: Platform):
        """初始化适配器。

        Args:
            config: 平台配置
            platform: 平台类型
        """
        self.config = config
        self.platform = platform
        self.token = config.token

        # 状态
        self._connected = False
        self._running = False

        # 回调
        self._on_message: Optional[Callable] = None
        self._on_status: Optional[Callable] = None

        # 打字指示器任务
        self._typing_tasks: Dict[str, asyncio.Task] = {}

        # 消息长度限制
        self.max_message_length = config.max_message_length

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """适配器显示名称。"""
        return self.platform.value

    # ------------------------------------------------------------------
    # 状态管理
    # ------------------------------------------------------------------

    def _mark_connected(self) -> None:
        """标记为已连接。"""
        self._connected = True
        self._running = True

    def _mark_disconnected(self) -> None:
        """标记为已断开。"""
        self._connected = False
        self._running = False

    # ------------------------------------------------------------------
    # 消息处理
    # ------------------------------------------------------------------

    async def handle_message(self, event: MessageEvent) -> None:
        """将消息事件转发到已注册的回调。

        Args:
            event: 消息事件
        """
        if self._on_message:
            try:
                await self._on_message(event)
            except Exception as e:
                logger.error("[%s] 消息回调异常: %s", self.name, e)

    # ------------------------------------------------------------------
    # 抽象方法（必须实现）
    # ------------------------------------------------------------------

    @abstractmethod
    async def connect(self) -> bool:
        """连接到平台。

        Returns:
            True 如果连接成功
        """
        ...

    @abstractmethod
    async def disconnect(self):
        """断开连接，清理资源。"""
        ...

    @abstractmethod
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
        """发送消息。

        Args:
            chat_id: 目标聊天 ID
            text: 消息文本
            thread_id: 线程 ID（可选）
            reply_to: 回复的消息 ID（可选）
            media_url: 媒体 URL（可选）
            metadata: 额外元数据

        Returns:
            {"success": bool, "message_id": str, "error": str, ...}
        """
        ...

    @abstractmethod
    async def send_typing(self, chat_id: str, thread_id: str = None):
        """发送打字指示器。"""
        ...

    # ------------------------------------------------------------------
    # 可选覆盖方法
    # ------------------------------------------------------------------

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: str = None,
        thread_id: str = None,
    ) -> Dict[str, Any]:
        """发送图片（默认调用 send）。"""
        return await self.send(
            chat_id=chat_id,
            text=caption or "",
            thread_id=thread_id,
            media_url=image_url,
        )

    async def send_file(
        self,
        chat_id: str,
        file_path: str,
        caption: str = None,
        thread_id: str = None,
    ) -> Dict[str, Any]:
        """发送文件（默认调用 send）。"""
        return await self.send(
            chat_id=chat_id,
            text=caption or f"[文件: {file_path}]",
            thread_id=thread_id,
        )

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        new_text: str,
        thread_id: str = None,
    ) -> Dict[str, Any]:
        """编辑消息（默认不支持）。"""
        return {"success": False, "error": "编辑消息不支持"}

    async def delete_message(
        self,
        chat_id: str,
        message_id: str,
        thread_id: str = None,
    ) -> Dict[str, Any]:
        """删除消息（默认不支持）。"""
        return {"success": False, "error": "删除消息不支持"}

    # ------------------------------------------------------------------
    # 回调管理
    # ------------------------------------------------------------------

    def set_message_handler(self, handler: Callable):
        """设置消息接收回调。

        回调签名: async def handler(event: MessageEvent) -> Optional[str]
        """
        self._on_message = handler

    def set_status_handler(self, handler: Callable):
        """设置状态通知回调。"""
        self._on_status = handler

    # ------------------------------------------------------------------
    # 通用功能
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """是否已连接。"""
        return self._connected

    def format_message(self, text: str, max_length: int = None) -> str:
        """格式化消息文本。

        - 截断超长消息
        - 转义平台特定字符
        """
        if max_length is None:
            max_length = self.max_message_length

        if max_length and len(text) > max_length:
            text = text[:max_length - 20] + "\n\n...(消息已截断)"

        return text

    def split_message(self, text: str, max_length: int = None) -> List[str]:
        """将长消息分割成多个块。"""
        if max_length is None:
            max_length = self.max_message_length

        if not max_length or len(text) <= max_length:
            return [text]

        chunks = []
        remaining = text

        while remaining:
            if len(remaining) <= max_length:
                chunks.append(remaining)
                break

            # 寻找分割点
            split_at = remaining.rfind("\n", 0, max_length)
            if split_at < max_length // 2:
                split_at = remaining.rfind(" ", 0, max_length)
            if split_at < 0:
                split_at = max_length

            chunks.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip()

        return chunks

    async def send_long_message(
        self,
        chat_id: str,
        text: str,
        thread_id: str = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        """发送长消息（自动分片）。"""
        chunks = self.split_message(text)
        results = []

        for i, chunk in enumerate(chunks):
            result = await self.send(
                chat_id=chat_id,
                text=chunk,
                thread_id=thread_id,
                **kwargs,
            )
            results.append(result)
            # 分片之间稍作延迟，避免速率限制
            if i < len(chunks) - 1:
                await asyncio.sleep(get_config_value("platforms.chunk_delay", 0.5))

        return results

    async def _keep_typing(
        self,
        chat_id: str,
        thread_id: str = None,
        interval: float = 4.0,
    ):
        """持续发送打字指示器。

        参考 Hermes 的 _keep_typing 机制。
        """
        key = f"{chat_id}:{thread_id or ''}"
        try:
            while self._running and key in self._typing_tasks:
                await self.send_typing(chat_id, thread_id)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("打字指示器错误: %s", e)

    def start_typing(self, chat_id: str, thread_id: str = None):
        """开始打字指示器。"""
        key = f"{chat_id}:{thread_id or ''}"
        if key in self._typing_tasks:
            return

        task = asyncio.create_task(self._keep_typing(chat_id, thread_id))
        self._typing_tasks[key] = task

    def stop_typing(self, chat_id: str, thread_id: str = None):
        """停止打字指示器。"""
        key = f"{chat_id}:{thread_id or ''}"
        task = self._typing_tasks.pop(key, None)
        if task:
            task.cancel()

    def _notify_status(self, message: str):
        """发送状态通知。"""
        if self._on_status:
            try:
                self._on_status(message)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def strip_mentions(text: str, bot_username: str = None) -> str:
        """移除 @mention。"""
        if bot_username:
            text = re.sub(rf"@{bot_username}\b", "", text, flags=re.IGNORECASE)
        return text.strip()

    @staticmethod
    def is_command(text: str) -> bool:
        """检查是否是命令（以 / 开头）。"""
        return text.strip().startswith("/")

    @staticmethod
    def parse_command(text: str) -> tuple:
        """解析命令。

        Returns:
            (command, args) 如 ("/start", "arg1 arg2")
        """
        parts = text.strip().split(maxsplit=1)
        command = parts[0] if parts else ""
        args = parts[1] if len(parts) > 1 else ""
        return command, args
