"""网关配置管理。

参考 Hermes 的 gateway/config.py：
- 平台配置（Telegram/Discord 等）
- 主频道配置
- 会话重置策略
- 投递偏好
"""

import logging
import os
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 默认配置目录
DEFAULT_CONFIG_DIR = Path.home() / ".spirit"


# ---------------------------------------------------------------------------
# 枚举类型
# ---------------------------------------------------------------------------

class Platform(str, Enum):
    """支持的消息平台。"""
    CLI = "cli"
    API = "api"
    WEB = "web"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    WEBHOOK = "webhook"
    SLACK = "slack"
    WECHAT = "wechat"
    WECOM = "wecom"
    DINGTALK = "dingtalk"
    FEISHU = "feishu"
    QQ = "qq"


from spirit.config import get_config_value


class ResetPolicy(str, Enum):
    """会话重置策略。"""
    MANUAL = "manual"           # 仅手动重置（/new 或 /reset）
    TIME_BASED = "time_based"   # 超时自动重置
    MESSAGE_COUNT = "message_count"  # 达到消息数上限自动重置
    TOKEN_BUDGET = "token_budget"    # Token 预算耗尽时重置


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class HomeChannel:
    """平台主频道 — 默认消息收发位置。

    参考 Hermes 的 HomeChannel：
    - chat_id: 频道/群组/私聊 ID
    - thread_id: 可选的线程/话题 ID（Telegram Forum 等）
    """
    chat_id: str
    thread_id: Optional[str] = None
    chat_type: str = "dm"  # dm / group / forum / channel


@dataclass
class SessionResetPolicy:
    """会话重置策略配置。

    参考 Hermes 的 SessionResetPolicy：
    - policy: 重置触发条件
    - idle_timeout_minutes: 空闲超时（分钟）
    - max_messages: 最大消息数
    - max_tokens: 最大 Token 数
    """
    policy: ResetPolicy = ResetPolicy.MANUAL
    idle_timeout_minutes: int = get_config_value("session_reset.idle_timeout_minutes", 60)
    max_messages: int = get_config_value("session_reset.max_messages", 500)
    max_tokens: int = get_config_value("session_reset.max_tokens", 100_000)


@dataclass
class PlatformConfig:
    """单个平台的配置。

    参考 Hermes 的 PlatformConfig：
    - platform: 平台类型
    - enabled: 是否启用
    - token: API Token / Bot Token
    - home_channel: 主频道
    - allowed_users: 允许的用户列表（空=全部允许）
    - extra: 平台特定配置
    """
    platform: Platform
    enabled: bool = False
    token: str = ""
    home_channel: Optional[HomeChannel] = None
    allowed_users: List[str] = field(default_factory=list)
    allow_all_users: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    # 会话重置策略
    reset_policy: SessionResetPolicy = field(default_factory=SessionResetPolicy)

    # 消息限制
    max_message_length: int = 4096  # 单条消息最大长度

    # 代理配置
    proxy: Optional[str] = None

    def is_user_allowed(self, user_id: str) -> bool:
        """检查用户是否被允许。"""
        if self.allow_all_users:
            return True
        if not self.allowed_users:
            return True  # 未配置白名单 = 全部允许
        return user_id in self.allowed_users


@dataclass
class GatewayConfig:
    """网关全局配置。

    参考 Hermes 的 GatewayConfig：
    - platforms: 各平台配置
    - agent_cache_size: Agent 缓存上限
    - auto_start: 是否自动启动
    """
    # 平台配置
    platforms: Dict[Platform, PlatformConfig] = field(default_factory=dict)

    # Agent 缓存
    agent_cache_size: int = get_config_value("gateway.agent_cache_size", 32)
    agent_idle_ttl_seconds: float = get_config_value("gateway.agent_idle_ttl_seconds", 3600.0)

    # 网关行为
    auto_start: bool = True
    graceful_shutdown_timeout: float = get_config_value("gateway.graceful_shutdown_timeout", 10.0)

    # 状态通知
    status_enabled: bool = True
    status_debounce_seconds: float = get_config_value("gateway.status_debounce_seconds", 2.0)

    # 数据目录
    data_dir: Path = field(default_factory=lambda: DEFAULT_CONFIG_DIR)
    sessions_dir: Optional[Path] = None

    def __post_init__(self):
        if self.sessions_dir is None:
            self.sessions_dir = self.data_dir / "sessions"

    def get_platform_config(self, platform: Platform) -> Optional[PlatformConfig]:
        """获取指定平台的配置。"""
        return self.platforms.get(platform)

    def get_enabled_platforms(self) -> List[Platform]:
        """获取所有已启用的平台。"""
        return [
            p for p, cfg in self.platforms.items()
            if cfg.enabled and cfg.token
        ]

    def get_connected_platforms(self) -> List[Platform]:
        """获取已连接的平台（启用且有主频道）。"""
        return [
            p for p, cfg in self.platforms.items()
            if cfg.enabled and cfg.token and cfg.home_channel
        ]


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

def load_gateway_config(config_path: Path = None) -> GatewayConfig:
    """从配置文件加载网关配置。

    优先级：环境变量 > 配置文件 > 默认值

    Args:
        config_path: 配置文件路径，默认为 ~/.spirit/gateway.json

    Returns:
        GatewayConfig 实例
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_DIR / "gateway.json"

    config = GatewayConfig()

    # 尝试从文件加载
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            config = _parse_config_dict(data)
            logger.info("已加载网关配置: %s", config_path)
        except Exception as e:
            logger.warning("加载网关配置失败: %s, 使用默认配置", e)

    # 环境变量覆盖
    config = _apply_env_overrides(config)

    return config


def _parse_config_dict(data: Dict[str, Any]) -> GatewayConfig:
    """解析配置字典为 GatewayConfig。"""
    config = GatewayConfig()

    # 解析平台配置
    for platform_name, platform_data in data.get("platforms", {}).items():
        try:
            platform = Platform(platform_name.lower())
        except ValueError:
            logger.warning("未知平台: %s, 跳过", platform_name)
            continue

        # 解析主频道
        home_channel = None
        if "home_channel" in platform_data:
            hc_data = platform_data["home_channel"]
            home_channel = HomeChannel(
                chat_id=hc_data.get("chat_id", ""),
                thread_id=hc_data.get("thread_id"),
                chat_type=hc_data.get("chat_type", "dm"),
            )

        # 解析重置策略
        reset_policy = SessionResetPolicy()
        if "reset_policy" in platform_data:
            rp_data = platform_data["reset_policy"]
            try:
                reset_policy.policy = ResetPolicy(rp_data.get("policy", "manual"))
            except ValueError:
                pass
            reset_policy.idle_timeout_minutes = rp_data.get("idle_timeout_minutes", 60)
            reset_policy.max_messages = rp_data.get("max_messages", 500)
            reset_policy.max_tokens = rp_data.get("max_tokens", 100_000)

        platform_config = PlatformConfig(
            platform=platform,
            enabled=platform_data.get("enabled", False),
            token=platform_data.get("token", ""),
            home_channel=home_channel,
            allowed_users=platform_data.get("allowed_users", []),
            allow_all_users=platform_data.get("allow_all_users", False),
            extra=platform_data.get("extra", {}),
            reset_policy=reset_policy,
            max_message_length=platform_data.get("max_message_length", 4096),
            proxy=platform_data.get("proxy"),
        )
        config.platforms[platform] = platform_config

    # 全局配置
    config.agent_cache_size = data.get("agent_cache_size", 32)
    config.auto_start = data.get("auto_start", True)
    config.graceful_shutdown_timeout = data.get("graceful_shutdown_timeout", 10.0)

    if "data_dir" in data:
        config.data_dir = Path(data["data_dir"])
        config.sessions_dir = config.data_dir / "sessions"

    return config


def _apply_env_overrides(config: GatewayConfig) -> GatewayConfig:
    """应用环境变量覆盖配置。

    支持的环境变量：
    - SPIRIT_TELEGRAM_TOKEN: Telegram Bot Token
    - SPIRIT_DISCORD_TOKEN: Discord Bot Token
    - SPIRIT_AGENT_CACHE_SIZE: Agent 缓存大小
    """
    # Telegram
    telegram_token = os.getenv("SPIRIT_TELEGRAM_TOKEN")
    if telegram_token:
        if Platform.TELEGRAM not in config.platforms:
            config.platforms[Platform.TELEGRAM] = PlatformConfig(
                platform=Platform.TELEGRAM,
            )
        config.platforms[Platform.TELEGRAM].token = telegram_token
        config.platforms[Platform.TELEGRAM].enabled = True

    # Discord
    discord_token = os.getenv("SPIRIT_DISCORD_TOKEN")
    if discord_token:
        if Platform.DISCORD not in config.platforms:
            config.platforms[Platform.DISCORD] = PlatformConfig(
                platform=Platform.DISCORD,
            )
        config.platforms[Platform.DISCORD].token = discord_token
        config.platforms[Platform.DISCORD].enabled = True

    # Agent 缓存大小
    cache_size = os.getenv("SPIRIT_AGENT_CACHE_SIZE")
    if cache_size:
        try:
            config.agent_cache_size = int(cache_size)
        except ValueError:
            pass

    return config


def save_gateway_config(config: GatewayConfig, config_path: Path = None):
    """保存网关配置到文件。

    Args:
        config: 网关配置
        config_path: 配置文件路径
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_DIR / "gateway.json"

    config_path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "platforms": {},
        "agent_cache_size": config.agent_cache_size,
        "auto_start": config.auto_start,
        "graceful_shutdown_timeout": config.graceful_shutdown_timeout,
        "data_dir": str(config.data_dir),
    }

    for platform, pcfg in config.platforms.items():
        platform_data = {
            "enabled": pcfg.enabled,
            "token": pcfg.token,  # 注意：生产环境应使用密钥管理
            "allowed_users": pcfg.allowed_users,
            "allow_all_users": pcfg.allow_all_users,
            "max_message_length": pcfg.max_message_length,
            "reset_policy": {
                "policy": pcfg.reset_policy.policy.value,
                "idle_timeout_minutes": pcfg.reset_policy.idle_timeout_minutes,
                "max_messages": pcfg.reset_policy.max_messages,
                "max_tokens": pcfg.reset_policy.max_tokens,
            },
            "extra": pcfg.extra,
        }
        if pcfg.home_channel:
            platform_data["home_channel"] = {
                "chat_id": pcfg.home_channel.chat_id,
                "thread_id": pcfg.home_channel.thread_id,
                "chat_type": pcfg.home_channel.chat_type,
            }
        if pcfg.proxy:
            platform_data["proxy"] = pcfg.proxy
        data["platforms"][platform.value] = platform_data

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    logger.info("已保存网关配置: %s", config_path)
