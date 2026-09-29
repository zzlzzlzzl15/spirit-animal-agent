"""消息平台集成 —— Telegram 发送 + 跨平台路由。

对标 ``spirit/tools/platforms.py`` 的 ``send_message`` 段（原 ``send_message_tool.py``），
拆为：

- :class:`TelegramIntegration` —— Telegram Bot API 发送（注入式 transport，可离线测）。
- :class:`MessageRouter` —— 跨平台路由：给定若干 sender 集成，按平台名或「首个已配置」
  选择发送通道（纯逻辑，可离线测）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from spirit.integrations.base import (
    BaseIntegration,
    Category,
    IntegrationResult,
    IntegrationSpec,
)

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"

TELEGRAM_SPEC = IntegrationSpec(
    name="telegram",
    display_name="Telegram",
    category=Category.MESSAGING,
    description="Telegram Bot 消息发送。",
    emoji="📨",
    required_env=("TELEGRAM_BOT_TOKEN",),
    capabilities=("send",),
    docs_url="https://core.telegram.org/bots/api",
)


def build_telegram_url(token: str, method: str, base: str = TELEGRAM_API_BASE) -> str:
    """纯函数：构造 ``<base>/bot<token>/<method>`` 端点。"""
    return f"{base.rstrip('/')}/bot{token}/{method.lstrip('/')}"


class TelegramIntegration(BaseIntegration):
    """Telegram Bot API 集成。"""

    def __init__(self, transport=None, env=None, api_base: str = TELEGRAM_API_BASE) -> None:
        super().__init__(TELEGRAM_SPEC, transport=transport, env=env)
        self.api_base = api_base

    def send_message(self, chat_id: str, text: str) -> IntegrationResult:
        if not self.is_configured():
            return IntegrationResult.failure("TELEGRAM_BOT_TOKEN 未设置")
        if not chat_id or not text:
            return IntegrationResult.failure("chat_id 和 text 必填")
        token = self.getenv("TELEGRAM_BOT_TOKEN")
        url = build_telegram_url(token, "sendMessage", self.api_base)
        try:
            resp = self.transport.request(
                "POST", url, json_body={"chat_id": chat_id, "text": text}, timeout=15.0
            )
            if not resp.ok:
                return IntegrationResult.failure(f"Telegram 错误 ({resp.status})")
            return IntegrationResult.success({"sent": True, "platform": "telegram"})
        except Exception as exc:  # noqa: BLE001
            return IntegrationResult.failure(str(exc))


class MessageRouter:
    """跨平台消息路由：按平台名或首个已配置 sender 发送。

    senders 是 ``{platform_name: obj_with_send_message}`` 映射（如 telegram/discord）。
    路由逻辑纯离线可测（sender 用 fake 注入）。
    """

    def __init__(self, senders: Optional[Dict[str, Any]] = None) -> None:
        self._senders: Dict[str, Any] = senders or {}

    def add(self, platform: str, sender: Any) -> None:
        self._senders[platform] = sender

    def available_platforms(self) -> List[str]:
        """返回**已配置**的发送平台名（按名字排序）。"""
        ready = []
        for name, sender in self._senders.items():
            try:
                if sender.is_configured():
                    ready.append(name)
            except Exception:  # noqa: BLE001 - 单个 sender 探测失败不影响其余
                continue
        return sorted(ready)

    def send(
        self, to: str, message: str, platform: Optional[str] = None
    ) -> IntegrationResult:
        """发送消息。

        - ``platform`` 指定 → 用该平台（未注册/未配置则失败）。
        - 未指定 → 用首个已配置平台。
        """
        if not to or not message:
            return IntegrationResult.failure("to 和 message 必填")

        if platform:
            sender = self._senders.get(platform)
            if sender is None:
                return IntegrationResult.failure(f"未知平台: {platform}")
            return self._invoke(sender, to, message, platform)

        for name in self.available_platforms():
            result = self._invoke(self._senders[name], to, message, name)
            if result.ok:
                return result
        return IntegrationResult.failure(
            "没有可用的消息平台。设置 TELEGRAM_BOT_TOKEN 或 DISCORD_BOT_TOKEN。"
        )

    @staticmethod
    def _invoke(sender: Any, to: str, message: str, platform: str) -> IntegrationResult:
        try:
            result = sender.send_message(to, message)
            if isinstance(result, IntegrationResult):
                return result
            # sender 返回非结构化值时包装
            return IntegrationResult.success({"sent": True, "platform": platform})
        except Exception as exc:  # noqa: BLE001
            return IntegrationResult.failure(f"{platform}: {exc}")


__all__ = [
    "TelegramIntegration",
    "TELEGRAM_SPEC",
    "TELEGRAM_API_BASE",
    "build_telegram_url",
    "MessageRouter",
]
