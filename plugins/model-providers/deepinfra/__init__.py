"""DeepInfra provider profile."""

from spirit.providers import register_provider
from spirit.providers.base import ProviderProfile

deepinfra = ProviderProfile(
    name="deepinfra",
    api_mode="chat_completions",
    display_name="DeepInfra",
    description="DeepInfra（OpenAI 兼容）",
    signup_url="https://deepinfra.com/dash/api_keys",
    env_vars=("DEEPINFRA_API_KEY",),
    base_url="https://api.deepinfra.com/v1/openai",
)

register_provider(deepinfra)
