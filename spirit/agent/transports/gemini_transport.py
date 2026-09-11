"""Gemini Transport — Google Gemini 原生 API 适配器。

支持两种模式：
1. **OpenAI 兼容模式**（通过 base_url 走 generativelanguage.googleapis.com/v1beta/openai）
   → 自动回退到 OpenAITransport
2. **原生 Gemini API**（通过 google-genai SDK）
   → 使用本模块

消息格式转换：
- OpenAI 格式 → Gemini Content/Part 格式
- system 消息 → system_instruction
- tool_calls → functionCall
- 图片 → inlineData (base64)
- 思维内容 → thought 标记
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


class GeminiTransport(Transport):
    """Google Gemini 原生 API 适配器。

    使用 google-genai SDK（推荐）或 google-generativeai SDK。
    延迟导入 SDK，首次调用时才加载。
    """

    def __init__(self, config: TransportConfig):
        super().__init__(config)
        self._client = None
        self._model_ref = None

    @property
    def provider_name(self) -> str:
        return "gemini"

    def _get_client(self):
        """延迟初始化 Gemini 客户端。"""
        if self._client is None:
            try:
                from google import genai
                self._client = genai.Client(api_key=self.config.api_key)
            except ImportError:
                raise TransportError(
                    "google-genai 包未安装: pip install google-genai"
                )
            except Exception as e:
                raise TransportError(f"Gemini 客户端初始化失败: {e}")

        return self._client

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]] = None,
        **kwargs,
    ) -> TransportResponse:
        """同步聊天调用。"""
        client = self._get_client()

        # 构建请求参数
        system_instruction, contents = self._convert_messages(messages)
        gemini_tools = self._convert_tools(tools) if tools else None

        request_kwargs: Dict[str, Any] = {
            "model": self._resolve_model(),
            "contents": contents,
        }
        if system_instruction:
            request_kwargs["config"] = {"system_instruction": system_instruction}
        if gemini_tools:
            if "config" not in request_kwargs:
                request_kwargs["config"] = {}
            request_kwargs["config"]["tools"] = gemini_tools

        # max_tokens
        max_tokens = kwargs.get("max_tokens")
        if max_tokens:
            if "config" not in request_kwargs:
                request_kwargs["config"] = {}
            request_kwargs["config"]["max_output_tokens"] = max_tokens

        # temperature
        temperature = kwargs.get("temperature")
        if temperature is not None:
            if "config" not in request_kwargs:
                request_kwargs["config"] = {}
            request_kwargs["config"]["temperature"] = temperature

        try:
            response = client.models.generate_content(**request_kwargs)
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

        system_instruction, contents = self._convert_messages(messages)
        gemini_tools = self._convert_tools(tools) if tools else None

        request_kwargs: Dict[str, Any] = {
            "model": self._resolve_model(),
            "contents": contents,
        }
        if system_instruction:
            request_kwargs["config"] = {"system_instruction": system_instruction}
        if gemini_tools:
            if "config" not in request_kwargs:
                request_kwargs["config"] = {}
            request_kwargs["config"]["tools"] = gemini_tools

        try:
            for chunk in client.models.generate_content_stream(**request_kwargs):
                if not chunk.candidates:
                    continue

                for part in chunk.candidates[0].content.parts:
                    if part.text:
                        yield {"type": "content", "text": part.text}
                    elif part.function_call:
                        yield {
                            "type": "tool_call",
                            "id": f"gemini_{part.function_call.name}",
                            "name": part.function_call.name,
                            "args": json.dumps(dict(part.function_call.args)) if part.function_call.args else "{}",
                        }

                # 结束
                if chunk.candidates[0].finish_reason:
                    usage = {}
                    if chunk.usage_metadata:
                        usage = {
                            "prompt_tokens": chunk.usage_metadata.prompt_token_count or 0,
                            "completion_tokens": chunk.usage_metadata.candidates_token_count or 0,
                            "total_tokens": chunk.usage_metadata.total_token_count or 0,
                        }
                    yield {"type": "done", "usage": usage}
                    break

        except Exception as e:
            raise self._wrap_error(e)

    def _resolve_model(self) -> str:
        """解析模型名称（去除 provider 前缀）。"""
        model = self.config.model
        # 去除 "gemini/" 或 "google/" 前缀
        for prefix in ("gemini/", "google/", "gemini-"):
            if model.startswith(prefix) and prefix != "gemini-":
                model = model[len(prefix):]
                break
        # 确保有 gemini- 前缀
        if not model.startswith("gemini"):
            model = f"gemini-{model}"
        return model

    def _convert_messages(self, messages: List[Dict]) -> tuple:
        """将 OpenAI 格式消息转换为 Gemini Content 格式。

        Returns:
            (system_instruction, contents) 元组
        """
        system_parts = []
        contents = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                # 提取为 system_instruction
                if isinstance(content, str):
                    system_parts.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if part.get("type") == "text":
                            system_parts.append(part.get("text", ""))
                continue

            # 转换角色
            gemini_role = "user" if role == "user" else "model"

            # 转换内容
            gemini_parts = []

            if isinstance(content, str):
                if content:
                    gemini_parts.append({"text": content})
            elif isinstance(content, list):
                gemini_parts = self._convert_content_parts(content)

            # 处理工具调用（assistant 消息）
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    try:
                        args = json.loads(func.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        args = {}
                    gemini_parts.append({
                        "function_call": {
                            "name": func.get("name", ""),
                            "args": args,
                        }
                    })

            # 处理工具结果（tool 消息）
            if role == "tool":
                gemini_role = "user"
                tool_name = msg.get("name", "")
                try:
                    result = json.loads(content) if isinstance(content, str) else content
                except json.JSONDecodeError:
                    result = content
                gemini_parts = [{
                    "function_response": {
                        "name": tool_name,
                        "response": {"result": result} if not isinstance(result, dict) else result,
                    }
                }]

            if gemini_parts:
                contents.append({"role": gemini_role, "parts": gemini_parts})

        system_instruction = "\n\n".join(system_parts) if system_parts else None
        return system_instruction, contents

    def _convert_content_parts(self, content: List[Dict]) -> List[Dict]:
        """转换多模态内容部分。"""
        parts = []
        for part in content:
            ptype = part.get("type", "")
            if ptype == "text":
                text = part.get("text", "")
                if text:
                    parts.append({"text": text})
            elif ptype == "image_url":
                url = part.get("image_url", {}).get("url", "")
                if url.startswith("data:"):
                    # Base64 内联图片
                    header, _, data = url.partition(",")
                    media_type = "image/png"
                    if "image/jpeg" in header:
                        media_type = "image/jpeg"
                    elif "image/webp" in header:
                        media_type = "image/webp"
                    parts.append({
                        "inline_data": {
                            "mime_type": media_type,
                            "data": data,
                        }
                    })
        return parts

    def _convert_tools(self, tools: List[Dict]) -> List[Dict]:
        """将 OpenAI 工具定义转换为 Gemini FunctionDeclaration 格式。"""
        declarations = []
        for tool in tools:
            func = tool.get("function", {})
            params = func.get("parameters", {})
            # Gemini 需要 properties + required 格式
            declarations.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "parameters": self._convert_schema(params),
            })
        return [{"function_declarations": declarations}]

    def _convert_schema(self, schema: Dict) -> Dict:
        """确保 schema 兼容 Gemini 格式要求。"""
        if not schema:
            return {"type": "OBJECT", "properties": {}}

        result = dict(schema)
        # Gemini 用大写类型名
        type_map = {"string": "STRING", "integer": "INTEGER", "number": "NUMBER",
                    "boolean": "BOOLEAN", "array": "ARRAY", "object": "OBJECT"}
        if "type" in result:
            result["type"] = type_map.get(result["type"], result["type"].upper())

        # 递归转换 items
        if "items" in result:
            result["items"] = self._convert_schema(result["items"])

        # 递归转换 properties
        if "properties" in result:
            result["properties"] = {
                k: self._convert_schema(v)
                for k, v in result["properties"].items()
            }

        return result

    def _parse_response(self, response) -> TransportResponse:
        """解析 Gemini 响应。"""
        content = ""
        tool_calls = []
        reasoning = None

        if not response.candidates:
            return TransportResponse(
                content="",
                tool_calls=[],
                finish_reason="content_filter",
                model=self.config.model,
                raw=response,
            )

        candidate = response.candidates[0]

        for part in candidate.content.parts:
            if part.text:
                # 检查是否是思维内容
                if getattr(part, "thought", False):
                    reasoning = (reasoning or "") + part.text
                else:
                    content += part.text
            elif part.function_call:
                tool_calls.append({
                    "id": f"gemini_{part.function_call.name}",
                    "name": part.function_call.name,
                    "args": json.dumps(dict(part.function_call.args)) if part.function_call.args else "{}",
                })

        # Usage
        usage = {}
        if response.usage_metadata:
            usage = {
                "prompt_tokens": response.usage_metadata.prompt_token_count or 0,
                "completion_tokens": response.usage_metadata.candidates_token_count or 0,
                "total_tokens": response.usage_metadata.total_token_count or 0,
            }

        # Finish reason 映射
        finish_map = {
            "STOP": "stop",
            "MAX_TOKENS": "length",
            "SAFETY": "content_filter",
            "RECITATION": "content_filter",
            "OTHER": "stop",
        }
        raw_reason = str(candidate.finish_reason) if candidate.finish_reason else "STOP"
        finish_reason = finish_map.get(raw_reason, "stop")

        return TransportResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
            model=self.config.model,
            raw=response,
        )

    def _wrap_error(self, exc: Exception) -> TransportError:
        """将 Gemini 异常包装为 TransportError。"""
        err_str = str(exc).lower()

        # 速率限制
        if "429" in err_str or "rate limit" in err_str or "quota" in err_str:
            return RateLimitError(str(exc))

        # 认证
        if "401" in err_str or "403" in err_str or "api key" in err_str:
            return AuthenticationError(str(exc))

        # 服务端错误
        if any(code in err_str for code in ("500", "502", "503", "504")):
            return TransportError(str(exc), retryable=True, provider="gemini")

        return TransportError(str(exc), retryable=False, provider="gemini")


__all__ = ["GeminiTransport"]
