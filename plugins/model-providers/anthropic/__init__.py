"""Native Anthropic provider profile."""

from spirit.providers import register_provider
from spirit.providers.base import ProviderProfile

anthropic = ProviderProfile(
    name="anthropic",
    aliases=("claude",),
    api_mode="anthropic_messages",
    display_name="Anthropic",
    description="Anthropic Claude（Messages API）",
    signup_url="https://console.anthropic.com/settings/keys",
    env_vars=("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN"),
    # 与 Spirit 现有 PROVIDER_BASE_URLS["anthropic"] 保持一致（含 /v1）
    base_url="https://api.anthropic.com/v1",
    supports_vision=True,
)

register_provider(anthropic)
