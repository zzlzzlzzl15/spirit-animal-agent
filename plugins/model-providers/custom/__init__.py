"""Custom / 本地（Ollama 等）provider profile。

覆盖任何注册为 ``provider="custom"`` 的端点，包括本地 Ollama 实例与 OpenAI 兼容的
推理端点（vLLM、llama.cpp、LM Studio、Azure OpenAI，以及用户自定义的 token-plan
类端点）。关键怪癖：
  - ``reasoning_config`` disabled → 顶层 ``reasoning_effort="none"`` + ``extra_body.think=False``
  - ``reasoning_config`` enabled + effort → 顶层 ``reasoning_effort``
    （GLM/ARK 等 OpenAI 兼容推理 API 期望的原生格式；未设则省略，用服务端默认）

``base_url`` 留空 —— 由用户配置（inline ``base_url`` 或 ``PROVIDER_BASE_URLS`` 里
的本地默认，如 ollama → http://127.0.0.1:11434/v1）。
"""

from typing import Any

from spirit.providers import register_provider
from spirit.providers.base import ProviderProfile


class CustomProfile(ProviderProfile):
    """Custom/本地 provider —— think=false 与 reasoning_effort 支持。"""

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        **ctx: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        if reasoning_config and isinstance(reasoning_config, dict):
            _effort = (reasoning_config.get("effort") or "").strip().lower()
            _enabled = reasoning_config.get("enabled", True)
            if _effort == "none" or _enabled is False:
                # Ollama 的 /v1/chat/completions 忽略 extra_body.think，但认顶层
                # reasoning_effort，故两者都发；不认识它们的端点会直接忽略。
                top_level["reasoning_effort"] = "none"
                extra_body["think"] = False
            elif _effort:
                top_level["reasoning_effort"] = _effort

        return extra_body, top_level

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Custom/本地：base_url 由用户配置；设了才拉取。"""
        if not (base_url or self.base_url):
            return None
        return super().fetch_models(api_key=api_key, base_url=base_url, timeout=timeout)


custom = CustomProfile(
    name="custom",
    aliases=(
        "ollama",
        "local",
        "vllm",
        "llamacpp",
        "llama.cpp",
        "llama-cpp",
        "lmstudio",
        "azure",
    ),
    api_mode="chat_completions",
    display_name="Custom / Local",
    description="自定义 OpenAI 兼容端点（Ollama/vLLM/llama.cpp/LM Studio/Azure 等）",
    env_vars=(),  # 无固定 key —— 自定义端点
    base_url="",  # 用户配置
    # 不设此值则不下发 max_tokens，Ollama 会回退到内部 num_predict=128 而截断响应。
    # 这只是用户未设 model.max_tokens 时的下限，故给足而非给小。
    default_max_tokens=65536,
)

register_provider(custom)
