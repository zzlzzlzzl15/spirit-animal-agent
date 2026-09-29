"""Alibaba Cloud DashScope (Qwen) provider profile.

阿里云百炼 DashScope 的 OpenAI 兼容端点。注意：用户的 token-plan 端点
（``token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1``）是不同的自定义
base_url，通过 config 的 inline ``base_url`` 覆盖此默认值（inline 恒优先）。
``qwen`` 作为 alias，故 ``get_provider_profile("qwen")`` 命中此 profile。
"""

from spirit.providers import register_provider
from spirit.providers.base import ProviderProfile

alibaba = ProviderProfile(
    name="alibaba",
    aliases=("dashscope", "alibaba-cloud", "qwen", "qwen-dashscope"),
    api_mode="chat_completions",
    display_name="Alibaba Cloud (DashScope)",
    description="阿里云百炼 DashScope（Qwen，OpenAI 兼容）",
    signup_url="https://help.aliyun.com/zh/model-studio/",
    env_vars=("DASHSCOPE_API_KEY",),
    base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    supports_vision=True,
)

register_provider(alibaba)
