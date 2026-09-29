"""本地代理服务器配置 —— Spirit Agent。

对标 Hermes ``.plans/openai-api-server.md`` 的 ``api_server`` 配置段，按 Spirit
「精简可测试子集」约定裁剪为一个 :class:`ProxyConfig` 数据类 + 解析函数。

解析优先级（高→低）：
1. 环境变量 ``SPIRIT_PROXY_ENABLED`` / ``_HOST`` / ``_PORT`` / ``_KEY`` / ``_MODEL``
2. config.yaml 的 ``proxy.*`` 段（经 :func:`spirit.config.get_config_value`）
3. 内置默认值（localhost-only、禁用鉴权时仅本地、模型 ``spirit-agent``）

**无导入期副作用**：默认值只在调用 :func:`load_proxy_config` 时解析。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8642
DEFAULT_MODEL = "spirit-agent"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _config_or(env_name: str, cfg_key: str, default, caster=None):
    """env > config.yaml(proxy.*) > default，可选类型转换。"""
    raw = os.getenv(env_name)
    if raw is None or raw == "":
        try:
            from spirit import config as spirit_config
            raw = spirit_config.get_config_value(cfg_key, None)
        except Exception as exc:  # pragma: no cover - 配置不可用时降级
            logger.debug("读取 %s 失败: %s", cfg_key, exc)
            raw = None
    if raw is None or raw == "":
        return default
    if caster is not None:
        try:
            return caster(raw)
        except (ValueError, TypeError):
            return default
    return raw


@dataclass
class ProxyConfig:
    """OpenAI 兼容代理服务器的运行配置。"""

    enabled: bool = False
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    # 鉴权 key；None/空 = 不校验（仅建议 localhost 绑定时无鉴权）。
    api_key: Optional[str] = None
    # 对外暴露的模型名（客户端请求任意 model 都映射到它）。
    model: str = DEFAULT_MODEL
    # 是否允许客户端用 "model" 字段覆盖服务端配置的模型。
    allow_model_override: bool = False
    # 单次请求最大并发（0 = 不限）。
    max_concurrent: int = 0

    def requires_auth(self) -> bool:
        """配置了 api_key 时才要求鉴权。"""
        return bool(self.api_key)

    def is_local_only(self) -> bool:
        return self.host in {"127.0.0.1", "localhost", "::1"}


def load_proxy_config() -> ProxyConfig:
    """按 env > config.yaml > 默认 的顺序解析代理配置。"""
    return ProxyConfig(
        enabled=_env_bool("SPIRIT_PROXY_ENABLED", False),
        host=_config_or("SPIRIT_PROXY_HOST", "proxy.host", DEFAULT_HOST),
        port=_config_or("SPIRIT_PROXY_PORT", "proxy.port", DEFAULT_PORT, int),
        api_key=_config_or("SPIRIT_PROXY_KEY", "proxy.key", None),
        model=_config_or("SPIRIT_PROXY_MODEL", "proxy.model", DEFAULT_MODEL),
        allow_model_override=_env_bool("SPIRIT_PROXY_MODEL_OVERRIDE", False),
        max_concurrent=_config_or(
            "SPIRIT_PROXY_MAX_CONCURRENT", "proxy.max_concurrent", 0, int
        ),
    )


__all__ = [
    "ProxyConfig",
    "load_proxy_config",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_MODEL",
]
