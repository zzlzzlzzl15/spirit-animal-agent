"""Spirit Gateway — 多平台消息网关。

参考 Hermes 的 gateway 架构设计：
- 统一网关框架连接多种消息平台
- 平台适配器模式（Telegram/Discord/Webhook 等）
- 会话上下文管理（知道消息来自哪里）
- 消息投递路由（cron 输出到合适渠道）
- 平台特定工具集（不同平台不同能力）

核心组件：
- GatewayConfig: 网关配置
- SessionContext/SessionStore: 会话上下文
- PlatformAdapter: 平台适配器基类
- PlatformRegistry: 适配器注册表
- DeliveryRouter: 消息投递路由
- GatewayRunner: 网关生命周期管理
"""

from spirit.gateway.config import (
    GatewayConfig,
    PlatformConfig,
    HomeChannel,
    SessionResetPolicy,
    load_gateway_config,
)
from spirit.gateway.session import (
    SessionContext,
    SessionStore,
    SessionSource,
    build_session_context_prompt,
)
from spirit.gateway.platform_registry import (
    PlatformEntry,
    PlatformRegistry,
    platform_registry,
)
from spirit.gateway.delivery import DeliveryRouter, DeliveryTarget, DeliveryResult
from spirit.gateway.runner import GatewayRunner, GatewayState

__all__ = [
    # Config
    "GatewayConfig",
    "PlatformConfig",
    "HomeChannel",
    "SessionResetPolicy",
    "load_gateway_config",
    # Session
    "SessionContext",
    "SessionStore",
    "SessionSource",
    "build_session_context_prompt",
    # Registry
    "PlatformEntry",
    "PlatformRegistry",
    "platform_registry",
    # Delivery
    "DeliveryRouter",
    "DeliveryTarget",
    "DeliveryResult",
    # Runner
    "GatewayRunner",
    "GatewayState",
]
