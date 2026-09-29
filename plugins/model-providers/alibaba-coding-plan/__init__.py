"""Alibaba Cloud Coding Plan provider profile.

与标准 ``alibaba`` profile 分开，因为它命中不同端点
（coding-intl.dashscope.aliyuncs.com）且使用专用的 API key 档位。
"""

from spirit.providers import register_provider
from spirit.providers.base import ProviderProfile

alibaba_coding_plan = ProviderProfile(
    name="alibaba-coding-plan",
    aliases=("alibaba_coding", "alibaba-coding", "dashscope-coding"),
    api_mode="chat_completions",
    display_name="Alibaba Cloud (Coding Plan)",
    description="阿里云百炼 Coding Plan（专用编码档位）",
    signup_url="https://help.aliyun.com/zh/model-studio/",
    env_vars=("ALIBABA_CODING_PLAN_API_KEY", "DASHSCOPE_API_KEY"),
    base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
)

register_provider(alibaba_coding_plan)
