"""MiniMax provider profile.

Spirit 走 MiniMax 的 OpenAI 兼容端点（``https://api.minimaxi.com/v1``），故
``api_mode="chat_completions"``（有意区别于 Hermes 默认的 anthropic_messages）——
这样 transport 工厂会把它路由到 OpenAITransport，根治此前 "minimax" 未注册导致的
``create_transport`` 抛错。
"""

from spirit.providers import register_provider
from spirit.providers.base import ProviderProfile

minimax = ProviderProfile(
    name="minimax",
    aliases=("mini-max", "minimax-cn", "minimax-china", "minimax_cn"),
    api_mode="chat_completions",
    display_name="MiniMax",
    description="MiniMax（OpenAI 兼容 /v1，MiniMax-M3 等）",
    signup_url="https://platform.minimaxi.com/",
    env_vars=("MINIMAX_API_KEY", "MINIMAX_CN_API_KEY"),
    base_url="https://api.minimaxi.com/v1",
    default_aux_model="MiniMax-M3",
)

register_provider(minimax)
