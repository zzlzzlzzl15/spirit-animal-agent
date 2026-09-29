"""Together AI provider profile."""

from spirit.providers import register_provider
from spirit.providers.base import ProviderProfile

together = ProviderProfile(
    name="together",
    aliases=("together-ai",),
    api_mode="chat_completions",
    display_name="Together AI",
    description="Together AI（OpenAI 兼容）",
    signup_url="https://api.together.xyz/settings/api-keys",
    env_vars=("TOGETHER_API_KEY",),
    base_url="https://api.together.xyz/v1",
)

register_provider(together)
