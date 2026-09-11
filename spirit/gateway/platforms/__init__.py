"""平台适配器包。"""

from spirit.gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    MessageEvent,
)

# 导入各平台适配器以触发自动注册
# 核心平台
from spirit.gateway.platforms import telegram  # noqa: F401
from spirit.gateway.platforms import discord  # noqa: F401

# 扩展平台
from spirit.gateway.platforms import slack  # noqa: F401
from spirit.gateway.platforms import dingtalk  # noqa: F401
from spirit.gateway.platforms import feishu  # noqa: F401
from spirit.gateway.platforms import wecom  # noqa: F401
from spirit.gateway.platforms import qq  # noqa: F401

__all__ = [
    "BasePlatformAdapter",
    "SendResult",
    "MessageEvent",
]
