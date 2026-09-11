"""平台适配器注册表。

参考 Hermes 的 gateway/platform_registry.py：
- 允许平台适配器自注册
- 网关通过注册表发现和实例化适配器
- 支持内置适配器和插件适配器
- 提供依赖检查和配置验证

Usage (注册):
    from spirit.gateway.platform_registry import platform_registry, PlatformEntry

    platform_registry.register(PlatformEntry(
        name="telegram",
        label="Telegram",
        adapter_factory=lambda cfg: TelegramAdapter(cfg),
        check_fn=check_telegram_deps,
    ))

Usage (查找):
    adapter = platform_registry.create_adapter("telegram", platform_config)
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from spirit.gateway.config import Platform, PlatformConfig

logger = logging.getLogger(__name__)


@dataclass
class PlatformEntry:
    """平台注册条目。

    参考 Hermes 的 PlatformEntry：
    - name: 平台标识符
    - label: 显示名称
    - adapter_factory: 适配器工厂函数
    - check_fn: 依赖检查函数
    - validate_config: 配置验证函数
    - required_env: 必需的环境变量
    - install_hint: 安装提示
    """
    # 平台标识（对应 Platform 枚举）
    platform: Platform

    # 显示名称
    label: str

    # 适配器工厂：接收 PlatformConfig，返回适配器实例
    adapter_factory: Callable[[PlatformConfig], Any]

    # 依赖检查：返回 True 表示依赖已就绪
    check_fn: Callable[[], bool] = field(default=lambda: True)

    # 配置验证：给定 PlatformConfig，是否配置正确
    validate_config: Optional[Callable[[PlatformConfig], bool]] = None

    # 连接状态检查
    is_connected: Optional[Callable[[PlatformConfig], bool]] = None

    # 必需的环境变量
    required_env: List[str] = field(default_factory=list)

    # 安装提示（check_fn 返回 False 时显示）
    install_hint: str = ""

    # 消息长度限制（0=无限制）
    max_message_length: int = 4096

    # 是否支持长消息分片
    splits_long_messages: bool = False

    # 来源：builtin / plugin
    source: str = "builtin"

    # 描述
    description: str = ""

    # 平台图标 emoji
    emoji: str = ""


class PlatformRegistry:
    """平台适配器注册表。

    管理所有已注册的平台适配器，提供：
    - 注册/注销适配器
    - 创建适配器实例
    - 依赖检查
    - 状态查询
    """

    def __init__(self):
        self._entries: Dict[Platform, PlatformEntry] = {}
        self._adapters: Dict[Platform, Any] = {}  # 缓存已创建的适配器

    def register(self, entry: PlatformEntry) -> None:
        """注册平台适配器。

        Args:
            entry: 平台注册条目

        Raises:
            ValueError: 平台已注册
        """
        if entry.platform in self._entries:
            logger.warning(
                "平台 %s 已注册，将被覆盖",
                entry.platform.value,
            )
        self._entries[entry.platform] = entry
        logger.debug("注册平台: %s (%s)", entry.platform.value, entry.label)

    def unregister(self, platform: Platform) -> None:
        """注销平台适配器。"""
        self._entries.pop(platform, None)
        self._adapters.pop(platform, None)
        logger.debug("注销平台: %s", platform.value)

    def get_entry(self, platform: Platform) -> Optional[PlatformEntry]:
        """获取平台注册条目。"""
        return self._entries.get(platform)

    def create_adapter(
        self,
        platform: Platform,
        config: PlatformConfig,
    ) -> Any:
        """创建平台适配器实例。

        Args:
            platform: 平台类型
            config: 平台配置

        Returns:
            适配器实例

        Raises:
            ValueError: 平台未注册或配置无效
        """
        entry = self._entries.get(platform)
        if entry is None:
            raise ValueError(f"平台 {platform.value} 未注册")

        # 检查依赖
        if not entry.check_fn():
            raise RuntimeError(
                f"平台 {platform.value} 依赖未就绪。{entry.install_hint}"
            )

        # 验证配置
        if entry.validate_config and not entry.validate_config(config):
            raise ValueError(f"平台 {platform.value} 配置无效")

        # 创建适配器（优先使用缓存）
        if platform in self._adapters:
            return self._adapters[platform]

        adapter = entry.adapter_factory(config)
        self._adapters[platform] = adapter
        return adapter

    def get_adapter(self, platform: Platform) -> Optional[Any]:
        """获取已创建的适配器（不创建新的）。"""
        return self._adapters.get(platform)

    def check_requirements(self, platform: Platform) -> bool:
        """检查平台依赖是否就绪。"""
        entry = self._entries.get(platform)
        if entry is None:
            return False
        return entry.check_fn()

    def list_registered(self) -> List[PlatformEntry]:
        """列出所有已注册的平台。"""
        return list(self._entries.values())

    def list_available(self) -> List[PlatformEntry]:
        """列出所有依赖就绪的平台。"""
        return [
            entry for entry in self._entries.values()
            if entry.check_fn()
        ]

    def list_connected(self, configs: Dict[Platform, PlatformConfig]) -> List[PlatformEntry]:
        """列出所有已连接的平台。

        Args:
            configs: 平台配置字典
        """
        result = []
        for entry in self._entries.values():
            config = configs.get(entry.platform)
            if config is None:
                continue
            if not config.enabled or not config.token:
                continue
            if entry.is_connected:
                if entry.is_connected(config):
                    result.append(entry)
            elif entry.validate_config:
                if entry.validate_config(config):
                    result.append(entry)
            else:
                result.append(entry)
        return result

    def get_status(self, configs: Dict[Platform, PlatformConfig]) -> Dict[str, Dict[str, Any]]:
        """获取所有平台的状态。

        Returns:
            {platform_name: {label, status, connected, ...}}
        """
        status = {}
        for entry in self._entries.values():
            config = configs.get(entry.platform, PlatformConfig(platform=entry.platform))
            deps_ready = entry.check_fn()
            is_enabled = config.enabled and bool(config.token)

            if not deps_ready:
                state = "missing_deps"
            elif not is_enabled:
                state = "disabled"
            elif entry.platform in self._adapters:
                state = "connected"
            else:
                state = "ready"

            status[entry.platform.value] = {
                "label": entry.label,
                "state": state,
                "deps_ready": deps_ready,
                "enabled": is_enabled,
                "install_hint": entry.install_hint if not deps_ready else "",
                "source": entry.source,
            }
        return status


# ---------------------------------------------------------------------------
# 全局注册表实例
# ---------------------------------------------------------------------------

platform_registry = PlatformRegistry()


def register_platform(entry: PlatformEntry) -> None:
    """注册平台到全局注册表。"""
    platform_registry.register(entry)
