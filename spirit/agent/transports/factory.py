"""Transport 工厂 — 根据 Provider 创建适配器。

支持自动检测（auto 模式）和显式指定 Provider。
延迟导入各 Transport SDK，仅在需要时加载。
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Type

from spirit.agent.transports.base import Transport, TransportConfig, TransportError

logger = logging.getLogger(__name__)


# 已注册的 Transport 类
_REGISTRY: Dict[str, Type[Transport]] = {}
_discovered = False


def register_transport(provider: str, transport_class: Type[Transport]) -> None:
    """注册 Transport 类。"""
    _REGISTRY[provider.lower()] = transport_class
    logger.debug("注册 Transport: %s -> %s", provider, transport_class.__name__)


def create_transport(config: TransportConfig) -> Transport:
    """根据配置创建 Transport 实例。

    支持 auto 模式自动检测 Provider。

    Args:
        config: Transport 配置

    Returns:
        Transport 实例

    Raises:
        TransportError: 不支持的 Provider
    """
    provider = config.provider.lower()

    # auto 模式：根据 model/base_url 自动检测
    if provider == "auto":
        provider = _detect_provider(config)
        logger.info("Auto 检测 Provider: %s", provider)
        config = TransportConfig(
            provider=provider,
            api_key=config.api_key,
            base_url=config.base_url,
            model=config.model,
            timeout=config.timeout,
            streaming=config.streaming,
            tools=config.tools,
            vision=config.vision,
            extra=config.extra,
        )

    # 延迟导入并注册
    _ensure_registered()

    if provider not in _REGISTRY:
        raise TransportError(
            f"不支持的 Provider: {provider}。"
            f"支持的: {', '.join(sorted(_REGISTRY.keys()))}",
            provider=provider,
        )

    transport_class = _REGISTRY[provider]
    logger.info("创建 Transport: %s -> %s", provider, transport_class.__name__)
    return transport_class(config)


def _detect_provider(config: TransportConfig) -> str:
    """根据 model 名称和 base_url 自动检测 Provider。

    检测优先级：
    1. base_url 特征（如 api.anthropic.com → anthropic）
    2. model 名称特征（如 claude-* → anthropic, gemini-* → gemini）
    3. 默认回退到 openai
    """
    model = (config.model or "").lower().strip()
    base_url = (config.base_url or "").lower().strip()

    # ── base_url 检测 ──
    if "anthropic.com" in base_url:
        return "anthropic"
    if "generativelanguage.googleapis.com" in base_url:
        # 检查是否是 OpenAI 兼容模式
        if base_url.endswith("/openai"):
            return "openai"
        return "gemini"
    if "bedrock" in base_url or "aws" in base_url:
        return "bedrock"

    # ── model 名称检测 ──
    # Anthropic Claude
    if model.startswith(("claude", "anthropic/")):
        return "anthropic"

    # Google Gemini
    if model.startswith(("gemini", "google/", "gemini-")):
        return "gemini"

    # AWS Bedrock（通过 model 前缀）
    if model.startswith(("anthropic.claude", "amazon.titan", "meta.llama", "mistral.")):
        return "bedrock"

    # ── 默认回退 ──
    return "openai"


def _ensure_registered() -> None:
    """确保内置 Transport 已注册（延迟导入）。"""
    global _discovered
    if _discovered:
        return
    _discovered = True

    # OpenAI（含所有兼容 API）
    try:
        from spirit.agent.transports.openai_transport import OpenAITransport
        register_transport("openai", OpenAITransport)
        # OpenAI 兼容的 Provider 别名
        register_transport("azure", OpenAITransport)
        register_transport("ollama", OpenAITransport)
        register_transport("lmstudio", OpenAITransport)
        register_transport("vllm", OpenAITransport)
        register_transport("llamacpp", OpenAITransport)
        register_transport("openrouter", OpenAITransport)
        register_transport("together", OpenAITransport)
        register_transport("deepinfra", OpenAITransport)
        register_transport("groq", OpenAITransport)
        register_transport("custom", OpenAITransport)
    except ImportError:
        pass

    # Anthropic
    try:
        from spirit.agent.transports.anthropic_transport import AnthropicTransport
        register_transport("anthropic", AnthropicTransport)
    except ImportError:
        pass

    # Gemini
    try:
        from spirit.agent.transports.gemini_transport import GeminiTransport
        register_transport("gemini", GeminiTransport)
    except ImportError:
        pass

    # Bedrock
    try:
        from spirit.agent.transports.bedrock_transport import BedrockTransport
        register_transport("bedrock", BedrockTransport)
    except ImportError:
        pass


def list_providers() -> list:
    """列出所有可用的 Provider。"""
    _ensure_registered()
    return sorted(_REGISTRY.keys())


def get_transport_info() -> Dict[str, Dict]:
    """获取所有 Provider 的详细信息。"""
    _ensure_registered()
    info = {}
    for name, cls in _REGISTRY.items():
        info[name] = {
            "class": cls.__name__,
            "module": cls.__module__,
            "available": True,
        }
    return info


__all__ = [
    "create_transport",
    "register_transport",
    "list_providers",
    "get_transport_info",
]
