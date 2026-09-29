"""Groq provider profile."""

from spirit.providers import register_provider
from spirit.providers.base import ProviderProfile

groq = ProviderProfile(
    name="groq",
    api_mode="chat_completions",
    display_name="Groq",
    description="Groq —— 极速 LPU 推理（OpenAI 兼容）",
    signup_url="https://console.groq.com/keys",
    env_vars=("GROQ_API_KEY",),
    base_url="https://api.groq.com/openai/v1",
)

register_provider(groq)
