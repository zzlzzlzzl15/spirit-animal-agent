"""Bedrock Transport — AWS Bedrock 适配器。

通过 boto3 调用 AWS Bedrock 的 Converse API，支持：
- Claude (Anthropic) 模型
- Titan 模型
- Llama 模型
- Mistral 模型

消息格式转换：
- OpenAI 格式 → Bedrock Converse API 格式
- system 消息 → system 参数
- tool_calls → toolUse content block
- 工具结果 → toolResult content block
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterator, List, Optional

from spirit.agent.transports.base import (
    Transport,
    TransportConfig,
    TransportError,
    TransportResponse,
    RateLimitError,
    AuthenticationError,
)

logger = logging.getLogger(__name__)


class BedrockTransport(Transport):
    """AWS Bedrock Converse API 适配器。

    使用 boto3 SDK，延迟导入。
    支持 Bedrock 上所有模型（Claude/Titan/Llama/Mistral）。
    """

    def __init__(self, config: TransportConfig):
        super().__init__(config)
        self._client = None

    @property
    def provider_name(self) -> str:
        return "bedrock"

    def _get_client(self):
        """延迟初始化 Bedrock 客户端。"""
        if self._client is None:
            try:
                import boto3
            except ImportError:
                raise TransportError(
                    "boto3 包未安装: pip install boto3"
                )

            # 从配置或环境变量获取 AWS 参数
            region = self.config.extra.get("region", "")
            if not region:
                import os
                region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

            kwargs: Dict[str, Any] = {"region_name": region}

            # 可选的凭证
            if self.config.extra.get("aws_access_key_id"):
                kwargs["aws_access_key_id"] = self.config.extra["aws_access_key_id"]
                kwargs["aws_secret_access_key"] = self.config.extra.get("aws_secret_access_key", "")
                if self.config.extra.get("aws_session_token"):
                    kwargs["aws_session_token"] = self.config.extra["aws_session_token"]

            # 自定义 endpoint（用于测试或 VPC 端点）
            if self.config.base_url:
                kwargs["endpoint_url"] = self.config.base_url

            self._client = boto3.client("bedrock-runtime", **kwargs)

        return self._client

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]] = None,
        **kwargs,
    ) -> TransportResponse:
        """同步聊天调用（Converse API）。"""
        client = self._get_client()

        system_messages, bedrock_messages = self._convert_messages(messages)
        bedrock_tools = self._convert_tools(tools) if tools else None

        request_kwargs: Dict[str, Any] = {
            "modelId": self._resolve_model_id(),
            "messages": bedrock_messages,
        }

        # 系统消息
        if system_messages:
            request_kwargs["system"] = system_messages

        # 工具
        if bedrock_tools:
            request_kwargs["toolConfig"] = bedrock_tools

        # 推理配置
        inference_config: Dict[str, Any] = {}
        max_tokens = kwargs.get("max_tokens")
        if max_tokens:
            inference_config["maxTokens"] = max_tokens
        temperature = kwargs.get("temperature")
        if temperature is not None:
            inference_config["temperature"] = temperature
        top_p = kwargs.get("top_p")
        if top_p is not None:
            inference_config["topP"] = top_p

        if inference_config:
            request_kwargs["inferenceConfig"] = inference_config

        try:
            response = client.converse(**request_kwargs)
        except Exception as e:
            raise self._wrap_error(e)

        return self._parse_response(response)

    def chat_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]] = None,
        **kwargs,
    ) -> Iterator[Dict[str, Any]]:
        """流式聊天调用（ConverseStream API）。"""
        client = self._get_client()

        system_messages, bedrock_messages = self._convert_messages(messages)
        bedrock_tools = self._convert_tools(tools) if tools else None

        request_kwargs: Dict[str, Any] = {
            "modelId": self._resolve_model_id(),
            "messages": bedrock_messages,
        }

        if system_messages:
            request_kwargs["system"] = system_messages
        if bedrock_tools:
            request_kwargs["toolConfig"] = bedrock_tools

        inference_config: Dict[str, Any] = {}
        max_tokens = kwargs.get("max_tokens")
        if max_tokens:
            inference_config["maxTokens"] = max_tokens
        temperature = kwargs.get("temperature")
        if temperature is not None:
            inference_config["temperature"] = temperature

        if inference_config:
            request_kwargs["inferenceConfig"] = inference_config

        try:
            response = client.converse_stream(**request_kwargs)
            stream = response.get("stream", {})

            tool_calls_buffer: Dict[int, Dict] = {}

            for event in stream:
                # 文本内容
                if "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"]["delta"]
                    if "text" in delta:
                        yield {"type": "content", "text": delta["text"]}
                    elif "toolUse" in delta:
                        idx = event["contentBlockDelta"]["contentBlockIndex"]
                        if idx not in tool_calls_buffer:
                            tool_calls_buffer[idx] = {"id": "", "name": "", "args": ""}
                        tool_input = delta["toolUse"]["input"]
                        tool_calls_buffer[idx]["args"] += tool_input

                # 工具调用开始
                elif "contentBlockStart" in event:
                    start = event["contentBlockStart"]["start"]
                    if "toolUse" in start:
                        idx = event["contentBlockStart"]["contentBlockIndex"]
                        tool_calls_buffer[idx] = {
                            "id": start["toolUse"].get("toolUseId", ""),
                            "name": start["toolUse"].get("name", ""),
                            "args": "",
                        }

                # 结束
                elif "messageStop" in event:
                    for idx in sorted(tool_calls_buffer.keys()):
                        entry = tool_calls_buffer[idx]
                        yield {
                            "type": "tool_call",
                            "id": entry["id"],
                            "name": entry["name"],
                            "args": entry["args"],
                        }
                    yield {"type": "done", "usage": {}}

                # Usage（metadata 事件）
                elif "metadata" in event:
                    usage_data = event["metadata"].get("usage", {})
                    if usage_data:
                        yield {
                            "type": "usage",
                            "usage": {
                                "prompt_tokens": usage_data.get("inputTokens", 0),
                                "completion_tokens": usage_data.get("outputTokens", 0),
                                "total_tokens": usage_data.get("totalTokens", 0),
                            },
                        }

        except Exception as e:
            raise self._wrap_error(e)

    def _resolve_model_id(self) -> str:
        """解析 Bedrock modelId。

        支持短名称自动映射：
        - "claude-3-5-sonnet" → "anthropic.claude-3-5-sonnet-20241022-v2:0"
        - "claude-3-haiku" → "anthropic.claude-3-haiku-20240307-v1:0"
        """
        model = self.config.model

        # 如果已经是完整 modelId（包含 "."），直接使用
        if "." in model:
            return model

        # 短名称映射
        MODEL_MAP = {
            "claude-3-5-sonnet": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "claude-3-5-haiku": "anthropic.claude-3-5-haiku-20241022-v1:0",
            "claude-3-sonnet": "anthropic.claude-3-sonnet-20240229-v1:0",
            "claude-3-haiku": "anthropic.claude-3-haiku-20240307-v1:0",
            "claude-3-opus": "anthropic.claude-3-opus-20240229-v1:0",
            "titan-text-express": "amazon.titan-text-express-v1",
            "titan-text-lite": "amazon.titan-text-lite-v1",
            "llama-3-70b": "meta.llama3-70b-instruct-v1:0",
            "llama-3-8b": "meta.llama3-8b-instruct-v1:0",
            "mistral-large": "mistral.mistral-large-2402-v1:0",
            "mistral-small": "mistral.mistral-small-2402-v1:0",
        }

        return MODEL_MAP.get(model, model)

    def _convert_messages(self, messages: List[Dict]) -> tuple:
        """将 OpenAI 格式消息转换为 Bedrock Converse 格式。

        Returns:
            (system_list, messages_list) 元组
        """
        system_list = []
        bedrock_messages = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                # 提取为 system 参数
                if isinstance(content, str) and content:
                    system_list.append({"text": content})
                elif isinstance(content, list):
                    for part in content:
                        if part.get("type") == "text" and part.get("text"):
                            system_list.append({"text": part["text"]})
                continue

            # 转换角色
            bedrock_role = "assistant" if role == "assistant" else "user"

            # 构建 content blocks
            blocks = []

            if isinstance(content, str):
                if content:
                    blocks.append({"text": content})
            elif isinstance(content, list):
                blocks = self._convert_content_blocks(content)

            # 工具调用（assistant 消息）
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    try:
                        tool_input = json.loads(func.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        tool_input = {}
                    blocks.append({
                        "toolUse": {
                            "toolUseId": tc.get("id", ""),
                            "name": func.get("name", ""),
                            "input": tool_input,
                        }
                    })

            # 工具结果（tool 消息）
            if role == "tool":
                bedrock_role = "user"
                tool_name = msg.get("name", "")
                try:
                    result_content = json.loads(content) if isinstance(content, str) else content
                except json.JSONDecodeError:
                    result_content = content

                blocks = [{
                    "toolResult": {
                        "toolUseId": msg.get("tool_call_id", ""),
                        "content": [{"text": json.dumps(result_content) if not isinstance(result_content, str) else result_content}],
                    }
                }]

            if blocks:
                bedrock_messages.append({"role": bedrock_role, "content": blocks})

        return system_list, bedrock_messages

    def _convert_content_blocks(self, content: List[Dict]) -> List[Dict]:
        """转换多模态内容为 Bedrock content blocks。"""
        blocks = []
        for part in content:
            ptype = part.get("type", "")
            if ptype == "text":
                text = part.get("text", "")
                if text:
                    blocks.append({"text": text})
            elif ptype == "image_url":
                url = part.get("image_url", {}).get("url", "")
                if url.startswith("data:"):
                    header, _, data = url.partition(",")
                    media_type = "image/png"
                    if "image/jpeg" in header:
                        media_type = "image/jpeg"
                    blocks.append({
                        "image": {
                            "format": media_type.split("/")[1],
                            "source": {"bytes": data},
                        }
                    })
        return blocks

    def _convert_tools(self, tools: List[Dict]) -> Dict:
        """将 OpenAI 工具定义转换为 Bedrock toolConfig 格式。"""
        bedrock_tools = []
        for tool in tools:
            func = tool.get("function", {})
            bedrock_tools.append({
                "toolSpec": {
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "inputSchema": {
                        "json": func.get("parameters", {"type": "object", "properties": {}}),
                    },
                }
            })
        return {"tools": bedrock_tools}

    def _parse_response(self, response) -> TransportResponse:
        """解析 Bedrock Converse 响应。"""
        content = ""
        tool_calls = []
        stop_reason = response.get("stopReason", "")

        for block in response.get("output", {}).get("message", {}).get("content", []):
            if "text" in block:
                content += block["text"]
            elif "toolUse" in block:
                tool_use = block["toolUse"]
                tool_calls.append({
                    "id": tool_use.get("toolUseId", ""),
                    "name": tool_use.get("name", ""),
                    "args": json.dumps(tool_use.get("input", {})),
                })

        # Usage
        usage_data = response.get("usage", {})
        usage = {
            "prompt_tokens": usage_data.get("inputTokens", 0),
            "completion_tokens": usage_data.get("outputTokens", 0),
            "total_tokens": usage_data.get("totalTokens", 0),
        }

        # Finish reason 映射
        finish_map = {
            "end_turn": "stop",
            "tool_use": "tool_calls",
            "max_tokens": "length",
            "stop_sequence": "stop",
            "content_filtered": "content_filter",
        }
        finish_reason = finish_map.get(stop_reason, "stop")

        return TransportResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
            model=self.config.model,
            raw=response,
        )

    def _wrap_error(self, exc: Exception) -> TransportError:
        """将 Bedrock/boto3 异常包装为 TransportError。"""
        err_str = str(exc)

        # 速率限制
        if "ThrottlingException" in err_str or "TooManyRequestsException" in err_str:
            return RateLimitError(err_str)

        # 认证
        if "UnauthorizedException" in err_str or "AccessDeniedException" in err_str:
            return AuthenticationError(err_str)

        # 服务端错误
        if any(code in err_str for code in ("ServiceUnavailableException", "InternalServerException")):
            return TransportError(err_str, retryable=True, provider="bedrock")

        # 模型不存在
        if "ResourceNotFoundException" in err_str:
            return TransportError(err_str, retryable=False, provider="bedrock")

        return TransportError(err_str, retryable=False, provider="bedrock")


__all__ = ["BedrockTransport"]
