"""Google Gemini provider profile."""

from spirit.providers import register_provider
from spirit.providers.base import ProviderProfile

gemini = ProviderProfile(
    name="gemini",
    aliases=("google", "google-gemini", "google-ai-studio"),
    api_mode="gemini",
    display_name="Google Gemini",
    description="Google AI Studio（Gemini 原生 API）",
    signup_url="https://aistudio.google.com/app/apikey",
    env_vars=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    base_url="https://generativelanguage.googleapis.com/v1beta",
    supports_vision=True,
)

register_provider(gemini)
