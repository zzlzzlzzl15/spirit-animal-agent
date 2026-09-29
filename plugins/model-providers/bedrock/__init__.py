"""AWS Bedrock provider profile."""

from spirit.providers import register_provider
from spirit.providers.base import ProviderProfile


class BedrockProfile(ProviderProfile):
    """AWS Bedrock —— 无 REST /v1/models 端点，模型列举走 AWS SDK。"""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Bedrock 模型列举需 AWS SDK，而非 REST 调用。"""
        return None


bedrock = BedrockProfile(
    name="bedrock",
    aliases=("aws", "aws-bedrock", "amazon-bedrock", "amazon"),
    api_mode="bedrock",
    display_name="AWS Bedrock",
    description="AWS Bedrock（Claude/Titan/Llama/Mistral）",
    env_vars=(),  # AWS SDK 凭据 —— 非环境变量 key
    base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
    auth_type="aws_sdk",
)

register_provider(bedrock)
