"""Transport 层 — 多 LLM Provider 适配。

设计目标：
- 统一接口：所有 Provider 遵循相同协议
- 延迟导入：按需加载 Provider SDK
- 错误归一化：统一错误类型
- 自动检测：auto 模式根据 model/base_url 自动选择 Transport

支持的 Provider（15+）：
- openai: OpenAI 官方 API
- anthropic: Anthropic Claude
- gemini: Google Gemini 原生 API
- bedrock: AWS Bedrock (Claude/Titan/Llama/Mistral)
- azure: Azure OpenAI（OpenAI 兼容）
- ollama: Ollama 本地模型（OpenAI 兼容）
- lmstudio: LM Studio 本地模型（OpenAI 兼容）
- vllm: vLLM 推理引擎（OpenAI 兼容）
- openrouter: OpenRouter 多模型路由（OpenAI 兼容）
- together: Together AI（OpenAI 兼容）
- deepinfra: DeepInfra（OpenAI 兼容）
- groq: Groq 快速推理（OpenAI 兼容）
- custom: 自定义 OpenAI 兼容 API
- auto: 自动检测 Provider

用法：
    from spirit.agent.transports import create_transport, TransportConfig

    config = TransportConfig(provider="auto", model="claude-3-5-sonnet", api_key="...")
    transport = create_transport(config)
    response = transport.chat(messages=[{"role": "user", "content": "Hello"}])
"""

from spirit.agent.transports.base import (
    Transport,
    TransportConfig,
    TransportError,
    TransportResponse,
    ToolCallData,
    UsageData,
    RateLimitError,
    AuthenticationError,
    build_tool_call,
    map_finish_reason,
)
from spirit.agent.transports.factory import (
    create_transport,
    register_transport,
    list_providers,
    get_transport_info,
)

__all__ = [
    # 基类
    "Transport",
    "TransportConfig",
    "TransportError",
    "TransportResponse",
    "ToolCallData",
    "UsageData",
    "RateLimitError",
    "AuthenticationError",
    # 工具函数
    "build_tool_call",
    "map_finish_reason",
    # 工厂
    "create_transport",
    "register_transport",
    "list_providers",
    "get_transport_info",
]
