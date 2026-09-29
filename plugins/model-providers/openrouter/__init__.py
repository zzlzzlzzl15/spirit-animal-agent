"""OpenRouter provider profile —— build_extra_body hook 范例。

OpenRouter 是聚合器，把 provider 偏好、session_id 放进 ``extra_body``。此 profile
演示如何子类化 ``ProviderProfile`` 并覆盖 hook（对齐 Hermes 的 openrouter 插件，
取其对 Spirit 有意义的子集）。
"""

from typing import Any

from spirit.providers import register_provider
from spirit.providers.base import ProviderProfile


class OpenRouterProfile(ProviderProfile):
    """OpenRouter 聚合器 —— provider 偏好 + session_id 透传到 extra_body。"""

    def build_extra_body(
        self, *, session_id: str | None = None, **context: Any
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if session_id:
            body["session_id"] = session_id
        prefs = context.get("provider_preferences")
        if prefs:
            body["provider"] = prefs
        return body


openrouter = OpenRouterProfile(
    name="openrouter",
    aliases=("or",),
    api_mode="chat_completions",
    display_name="OpenRouter",
    description="OpenRouter —— 200+ 模型的统一 API",
    signup_url="https://openrouter.ai/keys",
    env_vars=("OPENROUTER_API_KEY", "OPENAI_API_KEY"),
    base_url="https://openrouter.ai/api/v1",
    models_url="https://openrouter.ai/api/v1/models",
    supports_vision=True,
)

register_provider(openrouter)
