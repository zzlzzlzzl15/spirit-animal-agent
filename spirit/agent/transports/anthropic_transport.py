"""Anthropic Transport — Claude 适配器。

借鉴 Hermes AnthropicTransport 设计：
- Thinking 块处理（thinking / redacted_thinking）
- reasoning_details 保留用于回放
- anthropic_content_blocks 顺序保留（用于签名验证）
- Cache 统计提取
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterator, List, Optional

from spirit.config import get_config_value
from spirit.agent.transports.base import (
    Transport,
    TransportConfig,
    TransportError,
    TransportResponse,
    RateLimitError,
    AuthenticationError,
)

logger = logging.getLogger(__name__)


class AnthropicTransport(Transport):
    """Anthropic Claude 适配器。

    消息格式转换：
    - OpenAI 格式 -> Anthropic 格式
    - system 消息提取为顶层 system 参数
    - tool_calls 格式转换
    """

    def __init__(self, config: TransportConfig):
        super().__init__(config)
        self._client = None

    @property
    def provider_name(self) -> str:
        return "anthropic"

    def _get_client(self):
        """延迟初始化 Anthropic 客户端。"""
        if self._client is None:
            try:
                import anthropic
            except ImportError:
                raise TransportError("anthropic 包未安装: pip install anthropic")

            self._client = anthropic.Anthropic(
                api_key=self.config.api_key,
                timeout=self.config.timeout,
            )
            self._anthropic = anthropic

        return self._client

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]] = None,
        **kwargs,
    ) -> TransportResponse:
        """同步聊天调用。"""
        client = self._get_client()
        system, messages = self._extract_system(messages)
        messages = self._convert_messages(messages)

        request_kwargs = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": kwargs.pop("max_tokens", get_config_value("llm.anthropic_default_max_tokens", 4096)),
        }
        if system:
            request_kwargs["system"] = system
        if tools:
            request_kwargs["tools"] = self._convert_tools(tools)
        request_kwargs.update(kwargs)

        try:
            response = client.messages.create(**request_kwargs)
        except Exception as e:
            raise self._wrap_error(e)

        return self._parse_response(response)

    def chat_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]] = None,
        **kwargs,
    ) -> Iterator[Dict[str, Any]]:
        """流式聊天调用。"""
        client = self._get_client()
        system, messages = self._extract_system(messages)
        messages = self._convert_messages(messages)

        request_kwargs = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": kwargs.pop("max_tokens", get_config_value("llm.anthropic_default_max_tokens", 4096)),
        }
        if system:
            request_kwargs["system"] = system
        if tools:
            request_kwargs["tools"] = self._convert_tools(tools)
        request_kwargs.update(kwargs)

        try:
            with client.messages.stream(**request_kwargs) as stream:
                for event in stream:
                    if event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            yield {"type": "content", "text": event.delta.text}
                    elif event.type == "message_stop":
                        yield {"type": "done", "usage": {}}
        except Exception as e:
            raise self._wrap_error(e)

    def _extract_system(self, messages: List[Dict]) -> tuple:
        """提取 system 消息为顶层参数。"""
        system_parts = []
        filtered = []
        for msg in messages:
            if msg.get("role") == "system":
                system_parts.append(msg.get("content", ""))
            else:
                filtered.append(msg)
        return "\n\n".join(system_parts), filtered

    def _convert_messages(self, messages: List[Dict]) -> List[Dict]:
        """将 OpenAI 格式消息转换为 Anthropic 格式。"""
        converted = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            # 角色映射
            if role == "assistant":
                role = "assistant"
            elif role == "tool":
                role = "user"  # Anthropic 工具结果用 user 角色

            # 内容处理
            if isinstance(content, str):
                new_msg = {"role": role, "content": content}
            elif isinstance(content, list):
                # 多模态内容
                new_msg = {"role": role, "content": self._convert_content(content)}
            else:
                new_msg = {"role": role, "content": str(content)}

            # 工具调用
            if msg.get("tool_calls"):
                new_msg["content"] = self._convert_tool_calls(msg)

            converted.append(new_msg)

        return converted

    def _convert_content(self, content: List[Dict]) -> List[Dict]:
        """转换多模态内容。"""
        result = []
        for part in content:
            if part.get("type") == "text":
                result.append({"type": "text", "text": part.get("text", "")})
            elif part.get("type") == "image_url":
                # 图片转换
                url = part.get("image_url", {}).get("url", "")
                if url.startswith("data:"):
                    # Base64
                    result.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": url.split(",", 1)[1] if "," in url else url,
                        },
                    })
        return result

    def _convert_tool_calls(self, msg: Dict) -> List[Dict]:
        """转换工具调用消息。"""
        content = []
        if msg.get("content"):
            content.append({"type": "text", "text": msg["content"]})

        for tc in msg.get("tool_calls", []):
            content.append({
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": tc.get("function", {}).get("name", ""),
                "input": json.loads(tc.get("function", {}).get("args", "{}")),
            })

        return content

    def _convert_tools(self, tools: List[Dict]) -> List[Dict]:
        """转换工具定义格式。"""
        converted = []
        for tool in tools:
            func = tool.get("function", {})
            converted.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {}),
            })
        return converted

    def _parse_response(self, response) -> TransportResponse:
        """解析 Anthropic 响应（借鉴 Hermes normalize_response）。

        增强点：
        - Thinking 块分离为 reasoning
        - reasoning_details 保留用于回放
        - anthropic_content_blocks 顺序保留（签名思考 + 工具交错场景）
        """
        content = ""
        tool_calls = []
        reasoning_parts = []
        reasoning_details = []
        ordered_blocks = []  # 顺序保留内容块

        for block in response.content:
            # 转换为纯 dict 用于 reasoning_details
            block_dict = self._block_to_dict(block)
            clean_block = None
            if isinstance(block_dict, dict):
                clean_block = self._sanitize_replay_block(block_dict)
                if clean_block is not None:
                    ordered_blocks.append(clean_block)

            if block.type == "text":
                content += block.text
            elif block.type in ("thinking", "redacted_thinking"):
                if block.type == "thinking":
                    reasoning_parts.append(block.thinking)
                # 保留 reasoning_details 用于回放
                if isinstance(clean_block, dict):
                    reasoning_details.append(clean_block)
                elif isinstance(block_dict, dict):
                    reasoning_details.append(block_dict)
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "args": json.dumps(block.input),
                })

        usage = {}
        if hasattr(response, "usage"):
            usage = {
                "prompt_tokens": response.usage.input_tokens or 0,
                "completion_tokens": response.usage.output_tokens or 0,
                "total_tokens": (response.usage.input_tokens or 0) + (response.usage.output_tokens or 0),
            }

        # Finish reason 映射
        finish_map = {
            "end_turn": "stop",
            "tool_use": "tool_calls",
            "max_tokens": "length",
            "stop_sequence": "stop",
            "refusal": "content_filter",
        }
        finish_reason = finish_map.get(response.stop_reason, "stop")

        # Provider data
        provider_data: Dict[str, Any] = {}
        if reasoning_details:
            provider_data["reasoning_details"] = reasoning_details

        # 仅在签名思考 + 工具交错时保留 ordered blocks
        has_signed_thinking = any(
            isinstance(b, dict)
            and b.get("type") in ("thinking", "redacted_thinking")
            and (b.get("signature") or b.get("data"))
            for b in ordered_blocks
        )
        has_tool_use = any(
            isinstance(b, dict) and b.get("type") == "tool_use"
            for b in ordered_blocks
        )
        if has_signed_thinking and has_tool_use:
            provider_data["anthropic_content_blocks"] = ordered_blocks

        reasoning_text = "\n\n".join(reasoning_parts) if reasoning_parts else None

        return TransportResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
            model=response.model or self.config.model,
            raw=response,
            reasoning=reasoning_text,
            provider_data=provider_data or None,
        )

    def _wrap_error(self, exc: Exception) -> TransportError:
        """将 Anthropic 异常包装为 TransportError。"""
        if hasattr(self, "_anthropic"):
            if isinstance(exc, self._anthropic.RateLimitError):
                return RateLimitError(str(exc))
            if isinstance(exc, self._anthropic.AuthenticationError):
                return AuthenticationError(str(exc))
            if isinstance(exc, self._anthropic.APIError):
                status = getattr(exc, "status_code", 0)
                retryable = status in (429, 500, 502, 503, 504)
                return TransportError(str(exc), retryable=retryable, status_code=status, provider="anthropic")

        return TransportError(str(exc), retryable=False, provider="anthropic")

    # ------------------------------------------------------------------
    # 增强功能（借鉴 Hermes）
    # ------------------------------------------------------------------

    def validate_response(self, response: Any) -> bool:
        """验证 Anthropic 响应结构（借鉴 Hermes）。

        空 content 列表在以下场景是合法的：
        - end_turn: 模型无更多内容
        - refusal: 模型拒绝响应
        """
        if response is None:
            return False
        content_blocks = getattr(response, "content", None)
        if not isinstance(content_blocks, list):
            return False
        if not content_blocks:
            return getattr(response, "stop_reason", None) in {"end_turn", "refusal"}
        return True

    def extract_cache_stats(self, response: Any) -> Optional[Dict[str, int]]:
        """提取 Anthropic 缓存统计（借鉴 Hermes）。"""
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        written = getattr(usage, "cache_creation_input_tokens", 0) or 0
        if cached or written:
            return {"cached_tokens": cached, "creation_tokens": written}
        return None

    @staticmethod
    def _block_to_dict(block) -> Dict:
        """将 Anthropic content block 转换为纯 dict。"""
        if hasattr(block, "model_dump"):
            try:
                return block.model_dump()
            except Exception:
                pass
        if hasattr(block, "__dict__"):
            return {k: v for k, v in block.__dict__.items() if not k.startswith("_")}
        return {}

    @staticmethod
    def _sanitize_replay_block(block_dict: Dict) -> Optional[Dict]:
        """清洗 replay 块，去除 SDK 内部字段（借鉴 Hermes _sanitize_replay_block）。

        防止 SDK 输出字段（parsed_output, caller 等）持久化后泄漏回请求。
        """
        if not isinstance(block_dict, dict):
            return None
        clean = dict(block_dict)
        # 去除 SDK 内部字段
        for key in ("parsed_output", "caller", "citations"):
            clean.pop(key, None)
        return clean


__all__ = ["AnthropicTransport"]
