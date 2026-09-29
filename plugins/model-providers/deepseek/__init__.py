"""DeepSeek provider profile."""

from spirit.providers import register_provider
from spirit.providers.base import ProviderProfile

deepseek = ProviderProfile(
    name="deepseek",
    aliases=("deepseek-chat",),
    api_mode="chat_completions",
    display_name="DeepSeek",
    description="DeepSeek —— 原生 DeepSeek API",
    signup_url="https://platform.deepseek.com/",
    env_vars=("DEEPSEEK_API_KEY",),
    base_url="https://api.deepseek.com/v1",
    default_aux_model="deepseek-chat",
)

register_provider(deepseek)
