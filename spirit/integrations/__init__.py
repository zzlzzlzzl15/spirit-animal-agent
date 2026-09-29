"""Spirit 外部集成框架 —— 声明式集成目录 + 注入式 HTTP seam。

对标 Hermes 散落在 ``tools/``（discord/feishu/homeassistant/x_search/send_message/
yuanbao）与 ``gateway/platforms/`` 的平台集成，按 Spirit「精简可测试子集」约定收敛为
一个**统一框架**：

- :class:`~spirit.integrations.base.IntegrationSpec` —— 声明式集成元数据（凭据/能力/类别）。
- :class:`~spirit.integrations.base.BaseIntegration` —— 统一契约（is_configured/
  missing_requirements/describe/health_check），网络经**注入式** transport seam。
- :class:`~spirit.integrations.base.IntegrationRegistry` —— 集成注册表。
- 具体集成：Discord / Home Assistant / Telegram / X-Search / 飞书 / 元宝。
- :func:`~spirit.integrations.commands.handle_integrations_command` —— ``/integrations``
  传输无关命令分发。

一站式::

    from spirit.integrations import get_registry, handle_integrations_command
    reg = get_registry()                 # 懒注册全部内置集成
    reg.get("discord").is_configured()   # 读 DISCORD_BOT_TOKEN
    handle_integrations_command("list")  # 结构化 dict

**与既有工具层的关系**：``spirit/tools/platforms.py`` 的工具注册保持不变（工具层
可后续逐步委托到本框架）；本框架提供集成的**目录/状态/健康检查**统一视图与可测内核。
"""

from __future__ import annotations

import logging
from typing import Optional

from spirit.integrations.base import (
    BaseIntegration,
    Category,
    HttpResponse,
    HttpTransport,
    IntegrationRegistry,
    IntegrationResult,
    IntegrationSpec,
)
from spirit.integrations.commands import handle_integrations_command
from spirit.integrations.discord import DiscordIntegration
from spirit.integrations.docs import FeishuDocIntegration, YuanbaoIntegration
from spirit.integrations.homeassistant import HomeAssistantIntegration
from spirit.integrations.http_client import UrllibTransport, build_url, safe_json
from spirit.integrations.messaging import MessageRouter, TelegramIntegration
from spirit.integrations.x_search import XSearchIntegration

logger = logging.getLogger(__name__)

_registry: Optional[IntegrationRegistry] = None


def build_default_registry(env=None) -> IntegrationRegistry:
    """构造并注册全部内置集成的新注册表（env 可注入以隔离测试）。"""
    reg = IntegrationRegistry()
    reg.register(DiscordIntegration(env=env))
    reg.register(HomeAssistantIntegration(env=env))
    reg.register(TelegramIntegration(env=env))
    reg.register(XSearchIntegration(env=env))
    reg.register(FeishuDocIntegration(env=env))
    reg.register(YuanbaoIntegration(env=env))
    return reg


def get_registry() -> IntegrationRegistry:
    """返回进程级惰性单例注册表（默认读 ``os.environ``）。"""
    global _registry
    if _registry is None:
        _registry = build_default_registry()
    return _registry


def reset_registry() -> None:
    """重置全局单例（测试切换 env 后可调用以清缓存）。"""
    global _registry
    _registry = None


__all__ = [
    # base
    "BaseIntegration",
    "IntegrationSpec",
    "IntegrationRegistry",
    "IntegrationResult",
    "HttpResponse",
    "HttpTransport",
    "Category",
    # http
    "UrllibTransport",
    "build_url",
    "safe_json",
    # concrete integrations
    "DiscordIntegration",
    "HomeAssistantIntegration",
    "TelegramIntegration",
    "XSearchIntegration",
    "FeishuDocIntegration",
    "YuanbaoIntegration",
    "MessageRouter",
    # registry / commands
    "build_default_registry",
    "get_registry",
    "reset_registry",
    "handle_integrations_command",
]
