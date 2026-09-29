"""Discord 集成 —— 服务器/频道/成员/消息读取 + 发送。

对标 ``spirit/tools/platforms.py`` 的 Discord 段（原 ``discord_tool.py``），重构为
:class:`BaseIntegration` 子类：网络经**注入式 transport** seam，故 list/fetch/send
的请求构造与响应解析全部可离线单测（传 FakeTransport）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from spirit.integrations.base import (
    BaseIntegration,
    Category,
    HttpResponse,
    IntegrationResult,
    IntegrationSpec,
)

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"

DISCORD_SPEC = IntegrationSpec(
    name="discord",
    display_name="Discord",
    category=Category.MESSAGING,
    description="Discord 服务器交互：列出服务器/频道/成员、读取消息、发送消息。",
    emoji="💬",
    required_env=("DISCORD_BOT_TOKEN",),
    capabilities=("read", "send"),
    docs_url="https://discord.com/developers/docs/intro",
)


class DiscordIntegration(BaseIntegration):
    """Discord Bot API 集成。"""

    def __init__(self, transport=None, env=None, api_base: str = DISCORD_API_BASE) -> None:
        super().__init__(DISCORD_SPEC, transport=transport, env=env)
        self.api_base = api_base.rstrip("/")

    # -- 底层请求 ---------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        token = self.getenv("DISCORD_BOT_TOKEN")
        return {"Authorization": f"Bot {token}", "Content-Type": "application/json"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        timeout: float = 15.0,
    ) -> HttpResponse:
        url = f"{self.api_base}{path}"
        return self.transport.request(
            method, url, headers=self._headers(), json_body=body,
            params=params, timeout=timeout,
        )

    @staticmethod
    def _wrap(resp: HttpResponse, key: str, limit: Optional[int] = None) -> IntegrationResult:
        if resp.status == 204:
            return IntegrationResult.success({key: None})
        if not resp.ok:
            return IntegrationResult.failure(
                f"Discord API 错误 ({resp.status}): {resp.text()[:300]}"
            )
        data = resp.json()
        if isinstance(data, list) and limit is not None:
            data = data[:limit]
        return IntegrationResult.success({key: data})

    # -- 具体动作 ---------------------------------------------------------

    def list_guilds(self, limit: int = 50) -> IntegrationResult:
        if not self.is_configured():
            return IntegrationResult.failure("DISCORD_BOT_TOKEN 未设置")
        try:
            resp = self._request("GET", "/users/@me/guilds")
            return self._wrap(resp, "guilds", limit)
        except Exception as exc:  # noqa: BLE001
            return IntegrationResult.failure(str(exc))

    def list_channels(self, guild_id: str, limit: int = 50) -> IntegrationResult:
        if not self.is_configured():
            return IntegrationResult.failure("DISCORD_BOT_TOKEN 未设置")
        if not guild_id:
            return IntegrationResult.failure("guild_id 必填")
        try:
            resp = self._request("GET", f"/guilds/{guild_id}/channels")
            return self._wrap(resp, "channels", limit)
        except Exception as exc:  # noqa: BLE001
            return IntegrationResult.failure(str(exc))

    def list_members(self, guild_id: str, limit: int = 50) -> IntegrationResult:
        if not self.is_configured():
            return IntegrationResult.failure("DISCORD_BOT_TOKEN 未设置")
        if not guild_id:
            return IntegrationResult.failure("guild_id 必填")
        try:
            resp = self._request(
                "GET", f"/guilds/{guild_id}/members",
                params={"limit": min(limit, 1000)},
            )
            return self._wrap(resp, "members")
        except Exception as exc:  # noqa: BLE001
            return IntegrationResult.failure(str(exc))

    def fetch_messages(self, channel_id: str, limit: int = 50) -> IntegrationResult:
        if not self.is_configured():
            return IntegrationResult.failure("DISCORD_BOT_TOKEN 未设置")
        if not channel_id:
            return IntegrationResult.failure("channel_id 必填")
        try:
            resp = self._request(
                "GET", f"/channels/{channel_id}/messages",
                params={"limit": min(limit, 100)},
            )
            return self._wrap(resp, "messages")
        except Exception as exc:  # noqa: BLE001
            return IntegrationResult.failure(str(exc))

    def send_message(self, channel_id: str, content: str) -> IntegrationResult:
        if not self.is_configured():
            return IntegrationResult.failure("DISCORD_BOT_TOKEN 未设置")
        if not channel_id or not content:
            return IntegrationResult.failure("channel_id 和 content 必填")
        try:
            resp = self._request(
                "POST", f"/channels/{channel_id}/messages", body={"content": content}
            )
            if not resp.ok:
                return IntegrationResult.failure(
                    f"Discord 发送失败 ({resp.status}): {resp.text()[:200]}"
                )
            return IntegrationResult.success({"sent": True, "channel_id": channel_id})
        except Exception as exc:  # noqa: BLE001
            return IntegrationResult.failure(str(exc))


__all__ = ["DiscordIntegration", "DISCORD_SPEC", "DISCORD_API_BASE"]
