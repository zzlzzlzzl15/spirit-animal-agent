"""OpenAI provider profile."""

from spirit.providers import register_provider
from spirit.providers.base import ProviderProfile

openai = ProviderProfile(
    name="openai",
    api_mode="chat_completions",
    display_name="OpenAI",
    description="OpenAI 官方 API（Chat Completions）",
    signup_url="https://platform.openai.com/api-keys",
    env_vars=("OPENAI_API_KEY",),
    base_url="https://api.openai.com/v1",
    supports_vision=True,
)

register_provider(openai)
