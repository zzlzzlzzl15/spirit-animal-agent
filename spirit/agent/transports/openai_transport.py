"""OpenAI Transport — OpenAI 及兼容 API 适配器。

借鉴 Hermes ChatCompletionsTransport 设计：
- Reasoning/Thinking 支持（reasoning_content, reasoning_details）
- Gemini thinking_config 转发
- 消息清洗（去除内部字段）
- 响应验证 + 缓存统计提取
- Refusal 检测与处理
"""

from __future__ import annotations

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


class OpenAITransport(Transport):
    """OpenAI / 兼容 API 适配器。

    支持：
    - OpenAI 官方 API
    - Azure OpenAI（通过 base_url 配置）
    - 本地模型（Ollama、LM Studio 等）
    - 其他 OpenAI 兼容服务
    """

    def __init__(self, config: TransportConfig):
        super().__init__(config)
        self._client = None

    @property
    def provider_name(self) -> str:
        return "openai"

    def _get_client(self):
        """延迟初始化 OpenAI 客户端。"""
        if self._client is None:
            try:
                from openai import OpenAI, APIError, RateLimitError as RL, AuthenticationError as AE
            except ImportError:
                raise TransportError("openai 包未安装: pip install openai")

            kwargs = {
                "api_key": self.config.api_key or "dummy",
                "timeout": self.config.timeout,
            }
            if self.config.base_url:
                kwargs["base_url"] = self.config.base_url

            self._client = OpenAI(**kwargs)
            self._api_error = APIError
            self._rate_limit_error = RL
            self._auth_error = AE

        return self._client

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]] = None,
        **kwargs,
    ) -> TransportResponse:
        """同步聊天调用。"""
        client = self._get_client()
        messages = self._normalize_messages(messages)

        request_kwargs = {
            "model": self.config.model,
            "messages": messages,
        }
        if tools:
            request_kwargs["tools"] = self._normalize_tools(tools)

        # 推理配置（借鉴 Hermes reasoning_config）
        reasoning_config = kwargs.pop("reasoning_config", None) or self.config.reasoning
        if reasoning_config:
            self._apply_reasoning(request_kwargs, reasoning_config)

        request_kwargs.update(kwargs)

        try:
            response = client.chat.completions.create(**request_kwargs)
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
        messages = self._normalize_messages(messages)

        request_kwargs = {
            "model": self.config.model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            request_kwargs["tools"] = self._normalize_tools(tools)

        # 推理配置
        reasoning_config = kwargs.pop("reasoning_config", None) or self.config.reasoning
        if reasoning_config:
            self._apply_reasoning(request_kwargs, reasoning_config)

        request_kwargs.update(kwargs)

        try:
            stream = client.chat.completions.create(**request_kwargs)
        except Exception as e:
            raise self._wrap_error(e)

        # 工具调用累积
        tool_calls_buffer: Dict[int, Dict] = {}

        for chunk in stream:
            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta

            # 文本内容
            if delta.content:
                yield {"type": "content", "text": delta.content}

            # 工具调用
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_buffer:
                        tool_calls_buffer[idx] = {
                            "id": "",
                            "name": "",
                            "args": "",
                        }

                    entry = tool_calls_buffer[idx]
                    if tc_delta.id:
                        entry["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            entry["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            entry["args"] += tc_delta.function.arguments

            # 结束
            if chunk.choices[0].finish_reason:
                # 输出累积的工具调用
                for idx in sorted(tool_calls_buffer.keys()):
                    entry = tool_calls_buffer[idx]
                    yield {
                        "type": "tool_call",
                        "id": entry["id"],
                        "name": entry["name"],
                        "args": entry["args"],
                    }

                # Usage（如果 Provider 支持）
                usage = {}
                if hasattr(chunk, "usage") and chunk.usage:
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens or 0,
                        "completion_tokens": chunk.usage.completion_tokens or 0,
                        "total_tokens": chunk.usage.total_tokens or 0,
                    }
                yield {"type": "done", "usage": usage}
                break

    def _parse_response(self, response) -> TransportResponse:
        """解析 OpenAI 响应（借鉴 Hermes normalize_response）。

        增强点：
        - 推理内容分离（reasoning vs reasoning_content）
        - Gemini thought_signature 保留
        - Refusal 检测与处理
        """
        choice = response.choices[0]
        message = choice.message

        # finish_reason 容错（借鉴 Hermes: Poolside 返回整数）
        finish_reason = choice.finish_reason or "stop"
        if isinstance(finish_reason, int):
            finish_reason = str(finish_reason)

        # 工具调用（保留 provider_data，如 Gemini extra_content）
        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tc_provider_data: Dict[str, Any] = {}
                # Gemini 3 thinking 模型的 thought_signature
                extra = getattr(tc, "extra_content", None)
                if extra is None and hasattr(tc, "model_extra"):
                    extra = (tc.model_extra if isinstance(tc.model_extra, dict) else {}).get("extra_content")
                if extra is not None:
                    if hasattr(extra, "model_dump"):
                        try:
                            extra = extra.model_dump()
                        except Exception:
                            pass
                    tc_provider_data["extra_content"] = extra

                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "args": tc.function.arguments,
                    **({"provider_data": tc_provider_data} if tc_provider_data else {}),
                })

        # Usage
        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens or 0,
                "completion_tokens": response.usage.completion_tokens or 0,
                "total_tokens": response.usage.total_tokens or 0,
            }

        # 推理内容分离（借鉴 Hermes）
        reasoning = getattr(message, "reasoning", None)
        reasoning_content = getattr(message, "reasoning_content", None)
        if reasoning_content is None and hasattr(message, "model_extra"):
            model_extra = getattr(message, "model_extra", None) or {}
            if isinstance(model_extra, dict) and "reasoning_content" in model_extra:
                reasoning_content = model_extra["reasoning_content"]

        # provider_data
        provider_data: Dict[str, Any] = {}
        if reasoning_content is not None:
            provider_data["reasoning_content"] = reasoning_content
        rd = getattr(message, "reasoning_details", None)
        if rd:
            provider_data["reasoning_details"] = rd

        # Refusal 检测（借鉴 Hermes）
        content = message.content or ""
        refusal = getattr(message, "refusal", None)
        if refusal is None and hasattr(message, "model_extra"):
            _msg_extra = getattr(message, "model_extra", None) or {}
            if isinstance(_msg_extra, dict):
                refusal = _msg_extra.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            provider_data["refusal"] = refusal
            has_text = isinstance(content, str) and content.strip()
            has_tool_calls = bool(tool_calls)
            if not has_text and not has_tool_calls:
                content = refusal
                if finish_reason in (None, "stop"):
                    finish_reason = "content_filter"

        return TransportResponse(
            content=content or "",
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
            model=response.model or self.config.model,
            raw=response,
            reasoning=reasoning,
            reasoning_content=reasoning_content,
            provider_data=provider_data or None,
        )

    def _wrap_error(self, exc: Exception) -> TransportError:
        """将 OpenAI 异常包装为 TransportError。"""
        if hasattr(self, "_rate_limit_error") and isinstance(exc, self._rate_limit_error):
            retry_after = getattr(exc, "retry_after", 0)
            return RateLimitError(str(exc), retry_after=float(retry_after) if retry_after else 0)

        if hasattr(self, "_auth_error") and isinstance(exc, self._auth_error):
            return AuthenticationError(str(exc))

        if hasattr(self, "_api_error") and isinstance(exc, self._api_error):
            status = getattr(exc, "status_code", 0)
            retryable = status in (429, 500, 502, 503, 504)
            return TransportError(
                str(exc),
                retryable=retryable,
                status_code=status,
                provider="openai",
            )

        return TransportError(str(exc), retryable=False, provider="openai")

    # ------------------------------------------------------------------
    # 增强功能（借鉴 Hermes）
    # ------------------------------------------------------------------

    def _apply_reasoning(self, request_kwargs: Dict, reasoning_config: Dict) -> None:
        """应用推理配置到请求参数（借鉴 Hermes reasoning 处理）。"""
        if not isinstance(reasoning_config, dict):
            return

        if reasoning_config.get("enabled") is False:
            return

        effort = str(reasoning_config.get("effort", "medium") or "medium").strip().lower()

        # OpenAI 风格：extra_body.reasoning
        model = self.config.model.lower()
        if "gemini" in model:
            # Gemini 风格：thinking_config
            thinking_config: Dict[str, Any] = {"includeThoughts": True}
            if model.startswith("gemini-3"):
                if "flash" in model:
                    level_map = {"minimal": "low", "low": "low", "high": "high",
                                 "xhigh": "high", "max": "high", "ultra": "high"}
                    thinking_config["thinkingLevel"] = level_map.get(effort, "medium")
                elif "pro" in model:
                    thinking_config["thinkingLevel"] = "high" if effort in {"high", "xhigh", "max", "ultra"} else "low"
            if "extra_body" not in request_kwargs:
                request_kwargs["extra_body"] = {}
            request_kwargs["extra_body"]["thinking_config"] = thinking_config
        else:
            # 标准 OpenAI 风格
            if "extra_body" not in request_kwargs:
                request_kwargs["extra_body"] = {}
            request_kwargs["extra_body"]["reasoning"] = {"enabled": True, "effort": effort}

    def validate_response(self, response: Any) -> bool:
        """验证 OpenAI 响应结构（借鉴 Hermes）。"""
        if response is None:
            return False
        if not hasattr(response, "choices") or response.choices is None:
            return False
        if not response.choices:
            return False
        return True

    def extract_cache_stats(self, response: Any) -> Optional[Dict[str, int]]:
        """提取缓存统计（借鉴 Hermes：支持 OpenAI prompt_tokens_details + DeepSeek 原生字段）。"""
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) or 0 if details else 0
        written = getattr(details, "cache_write_tokens", 0) or 0 if details else 0
        if not cached:
            # DeepSeek 原生 API
            cached = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
        if cached or written:
            return {"cached_tokens": cached, "creation_tokens": written}
        return None


__all__ = ["OpenAITransport"]
