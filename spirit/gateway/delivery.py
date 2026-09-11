"""消息投递路由。

参考 Hermes 的 gateway/delivery.py：
- 根据目标路由消息到合适的平台
- 支持显式目标（如 "telegram:123456789"）
- 支持主频道回退
- 支持来源回传（回到消息来源平台）
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from spirit.gateway.config import GatewayConfig, Platform, PlatformConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class DeliveryTarget:
    """投递目标。

    参考 Hermes 的 DeliveryTarget：
    - platform: 目标平台
    - chat_id: 目标聊天 ID
    - thread_id: 目标线程 ID（可选）
    """
    platform: Platform
    chat_id: str
    thread_id: Optional[str] = None

    @classmethod
    def parse(cls, target_str: str) -> Optional["DeliveryTarget"]:
        """解析目标字符串。

        格式：platform:chat_id[:thread_id]
        示例：telegram:123456789, discord:channel_id:thread_id
        """
        parts = target_str.split(":")
        if len(parts) < 2:
            return None

        try:
            platform = Platform(parts[0].lower())
        except ValueError:
            logger.warning("未知平台: %s", parts[0])
            return None

        chat_id = parts[1]
        thread_id = parts[2] if len(parts) > 2 else None

        return cls(platform=platform, chat_id=chat_id, thread_id=thread_id)


@dataclass
class DeliveryResult:
    """投递结果。

    参考 Hermes 的 SendResult。
    """
    success: bool
    target: DeliveryTarget
    message_id: Optional[str] = None
    error: Optional[str] = None
    error_kind: Optional[str] = None  # rate_limit / auth / not_found / network
    truncated: bool = False

    @classmethod
    def ok(cls, target: DeliveryTarget, message_id: str = None) -> "DeliveryResult":
        """创建成功结果。"""
        return cls(success=True, target=target, message_id=message_id)

    @classmethod
    def fail(
        cls,
        target: DeliveryTarget,
        error: str,
        error_kind: str = None,
    ) -> "DeliveryResult":
        """创建失败结果。"""
        return cls(success=False, target=target, error=error, error_kind=error_kind)


# ---------------------------------------------------------------------------
# 投递路由器
# ---------------------------------------------------------------------------

class DeliveryRouter:
    """消息投递路由器。

    参考 Hermes 的 DeliveryRouter：
    - 解析投递目标
    - 路由到合适的平台适配器
    - 处理长消息分片
    - 追踪死信目标

    Usage:
        router = DeliveryRouter(config, adapters)
        result = await router.deliver("telegram:123456", "Hello!")
    """

    def __init__(
        self,
        config: GatewayConfig,
        adapters: Dict[Platform, Any] = None,
    ):
        """初始化路由器。

        Args:
            config: 网关配置
            adapters: 平台适配器字典 {Platform: adapter}
        """
        self.config = config
        self._adapters = adapters or {}

        # 死信目标追踪（发送失败的目标）
        self._dead_targets: Dict[str, datetime] = {}

    def set_adapter(self, platform: Platform, adapter: Any):
        """设置平台适配器。"""
        self._adapters[platform] = adapter

    def remove_adapter(self, platform: Platform):
        """移除平台适配器。"""
        self._adapters.pop(platform, None)

    async def deliver(
        self,
        target: str | DeliveryTarget,
        text: str,
        *,
        reply_to: str = None,
        media_url: str = None,
        metadata: Dict[str, Any] = None,
    ) -> DeliveryResult:
        """投递消息到目标。

        Args:
            target: 投递目标（字符串或 DeliveryTarget）
            text: 消息文本
            reply_to: 回复的消息 ID
            media_url: 媒体 URL
            metadata: 额外元数据

        Returns:
            DeliveryResult
        """
        # 解析目标
        if isinstance(target, str):
            parsed = DeliveryTarget.parse(target)
            if parsed is None:
                return DeliveryResult.fail(
                    target=DeliveryTarget(platform=Platform.CLI, chat_id=""),
                    error=f"无法解析目标: {target}",
                )
            target = parsed

        # 检查死信
        target_key = f"{target.platform.value}:{target.chat_id}"
        if target_key in self._dead_targets:
            dead_time = self._dead_targets[target_key]
            # 10 分钟后重试
            if (datetime.now() - dead_time).total_seconds() < 600:
                return DeliveryResult.fail(
                    target=target,
                    error="目标已标记为死信",
                    error_kind="dead_target",
                )
            else:
                # 移除死信标记，重试
                del self._dead_targets[target_key]

        # 获取适配器
        adapter = self._adapters.get(target.platform)
        if adapter is None:
            return DeliveryResult.fail(
                target=target,
                error=f"平台 {target.platform.value} 无可用适配器",
                error_kind="no_adapter",
            )

        # 获取平台配置
        platform_config = self.config.get_platform_config(target.platform)
        max_length = platform_config.max_message_length if platform_config else 4096

        # 长消息分片
        if len(text) > max_length:
            return await self._deliver_chunked(
                adapter=adapter,
                target=target,
                text=text,
                max_length=max_length,
                reply_to=reply_to,
                metadata=metadata,
            )

        # 直接发送
        try:
            result = await adapter.send(
                chat_id=target.chat_id,
                text=text,
                thread_id=target.thread_id,
                reply_to=reply_to,
                media_url=media_url,
                metadata=metadata,
            )
            if isinstance(result, dict):
                if result.get("success") is False:
                    error = result.get("error", "未知错误")
                    error_kind = result.get("error_kind")
                    # 标记死信
                    if error_kind in ("not_found", "auth"):
                        self._dead_targets[target_key] = datetime.now()
                    return DeliveryResult.fail(target, error, error_kind)
                return DeliveryResult.ok(target, result.get("message_id"))
            return DeliveryResult.ok(target)
        except Exception as e:
            logger.error("投递失败 [%s]: %s", target_key, e)
            return DeliveryResult.fail(target, str(e), "network")

    async def deliver_to_home(
        self,
        platform: Platform,
        text: str,
        **kwargs,
    ) -> DeliveryResult:
        """投递到平台主频道。

        Args:
            platform: 目标平台
            text: 消息文本
            **kwargs: 传递给 deliver 的参数
        """
        config = self.config.get_platform_config(platform)
        if config is None or config.home_channel is None:
            return DeliveryResult.fail(
                target=DeliveryTarget(platform=platform, chat_id=""),
                error=f"平台 {platform.value} 未配置主频道",
            )

        target = DeliveryTarget(
            platform=platform,
            chat_id=config.home_channel.chat_id,
            thread_id=config.home_channel.thread_id,
        )
        return await self.deliver(target, text, **kwargs)

    async def broadcast(
        self,
        text: str,
        platforms: List[Platform] = None,
        **kwargs,
    ) -> List[DeliveryResult]:
        """广播消息到多个平台。

        Args:
            text: 消息文本
            platforms: 目标平台列表（None=所有已连接平台）
            **kwargs: 传递给 deliver 的参数
        """
        if platforms is None:
            platforms = self.config.get_connected_platforms()

        results = []
        for platform in platforms:
            result = await self.deliver_to_home(platform, text, **kwargs)
            results.append(result)

        return results

    async def _deliver_chunked(
        self,
        adapter: Any,
        target: DeliveryTarget,
        text: str,
        max_length: int,
        reply_to: str = None,
        metadata: Dict[str, Any] = None,
    ) -> DeliveryResult:
        """分片发送长消息。"""
        chunks = self._split_message(text, max_length)
        last_result = None

        for i, chunk in enumerate(chunks):
            # 只有第一个 chunk 使用 reply_to
            chunk_reply = reply_to if i == 0 else None
            try:
                result = await adapter.send(
                    chat_id=target.chat_id,
                    text=chunk,
                    thread_id=target.thread_id,
                    reply_to=chunk_reply,
                    metadata=metadata,
                )
                if isinstance(result, dict) and result.get("success") is False:
                    return DeliveryResult.fail(
                        target,
                        result.get("error", "分片发送失败"),
                    )
                last_result = result
            except Exception as e:
                return DeliveryResult.fail(target, str(e), "network")

        return DeliveryResult.ok(target, truncated=len(chunks) > 1)

    def _split_message(self, text: str, max_length: int) -> List[str]:
        """将消息分割成多个块。

        优先在换行处分割，避免截断单词。
        """
        if len(text) <= max_length:
            return [text]

        chunks = []
        remaining = text
        footer_template = "\n\n(第 {n}/{total} 部分)"

        while remaining:
            # 计算可用长度（考虑 footer）
            total_estimated = len(chunks) + 2  # 估计总块数
            footer = footer_template.format(n=len(chunks) + 1, total=total_estimated)
            available = max_length - len(footer) - 10  # 留点余量

            if available <= 0:
                # 极端情况：max_length 太小，强制分割
                available = max(10, max_length)

            if len(remaining) <= available:
                chunks.append(remaining)
                break

            # 寻找分割点
            split_at = remaining.rfind("\n", 0, available)
            if split_at < available // 2:
                # 没找到合适的换行，按空格分割
                split_at = remaining.rfind(" ", 0, available)
            if split_at <= 0:
                # 硬分割
                split_at = min(available, len(remaining) - 1)

            # 确保至少前进一个字符，防止无限循环
            split_at = max(1, split_at)

            chunks.append(remaining[:split_at].rstrip())
            new_remaining = remaining[split_at:].lstrip()

            # 安全检查：如果没有进展，强制截断
            if len(new_remaining) >= len(remaining):
                new_remaining = remaining[split_at:]

            remaining = new_remaining

        # 更新 footer
        total = len(chunks)
        if total > 1:
            chunks = [
                chunk + footer_template.format(n=i + 1, total=total)
                for i, chunk in enumerate(chunks)
            ]

        return chunks

    def mark_dead(self, platform: Platform, chat_id: str):
        """手动标记死信目标。"""
        key = f"{platform.value}:{chat_id}"
        self._dead_targets[key] = datetime.now()
        logger.info("标记死信目标: %s", key)

    def clear_dead(self, platform: Platform = None):
        """清除死信标记。"""
        if platform:
            prefix = f"{platform.value}:"
            self._dead_targets = {
                k: v for k, v in self._dead_targets.items()
                if not k.startswith(prefix)
            }
        else:
            self._dead_targets.clear()

    @property
    def dead_targets(self) -> List[str]:
        """获取所有死信目标。"""
        return list(self._dead_targets.keys())
