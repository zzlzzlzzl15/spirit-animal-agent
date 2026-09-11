"""Transport 基类 — 定义统一接口。

借鉴 Hermes ProviderTransport 设计：
- 统一响应归一化（NormalizedResponse 模式）
- 推理内容分离（reasoning vs reasoning_content）
- provider_data 携带协议特定元数据
- 响应验证 + 缓存统计提取
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class TransportConfig:
    """Transport 配置。"""

    provider: str = "openai"
    api_key: str = ""
    base_url: str = ""
    model: str = "gpt-4o"

    # 超时
    timeout: float = 60.0

    # 特性开关
    streaming: bool = True
    tools: bool = True
    vision: bool = False

    # 推理配置（借鉴 Hermes reasoning_config）
    reasoning: Optional[Dict[str, Any]] = None

    # 额外参数
    extra: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------

class TransportError(Exception):
    """Transport 层统一错误。"""

    def __init__(
        self,
        message: str,
        retryable: bool = False,
        status_code: int = 0,
        provider: str = "",
    ):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status_code = status_code
        self.provider = provider


class RateLimitError(TransportError):
    """速率限制错误。"""

    def __init__(self, message: str = "Rate limit exceeded", retry_after: float = 0):
        super().__init__(message, retryable=True)
        self.retry_after = retry_after


class AuthenticationError(TransportError):
    """认证错误。"""

    def __init__(self, message: str = "Authentication failed"):
        super().__init__(message, retryable=False)


# ---------------------------------------------------------------------------
# 响应类型（借鉴 Hermes NormalizedResponse / ToolCall / Usage）
# ---------------------------------------------------------------------------

@dataclass
class ToolCallData:
    """归一化工具调用。

    借鉴 Hermes ToolCall 设计：
    - id: 协议规范 ID（用于 tool_call_id 回放）
    - name: 工具名称
    - arguments: JSON 字符串参数
    - provider_data: 协议特定元数据（如 Gemini thought_signature）
    """

    id: Optional[str] = None
    name: str = ""
    arguments: str = ""  # JSON 字符串
    provider_data: Optional[Dict[str, Any]] = field(default=None, repr=False)

    # 向后兼容：tc.function.name / tc.function.arguments
    @property
    def type(self) -> str:
        return "function"

    @property
    def function(self) -> ToolCallData:
        return self


@dataclass
class UsageData:
    """归一化 Token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0


@dataclass
class TransportResponse:
    """统一响应格式（借鉴 Hermes NormalizedResponse）。"""

    content: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    usage: Dict[str, int] = field(default_factory=dict)
    finish_reason: str = ""
    model: str = ""
    raw: Any = None  # 原始响应对象

    # ── 推理内容（借鉴 Hermes reasoning 分离） ──
    reasoning: Optional[str] = None  # 推理文本
    reasoning_content: Optional[str] = None  # DeepSeek/Moonshot 风格

    # ── 协议特定元数据 ──
    # Anthropic: {"reasoning_details": [...], "anthropic_content_blocks": [...]}
    # Codex:     {"codex_reasoning_items": [...], "codex_message_items": [...]}
    # Gemini:    {"extra_content": {"google": {"thought_signature": "..."}}}
    provider_data: Optional[Dict[str, Any]] = field(default=None, repr=False)

    # ── 向后兼容属性 ──
    @property
    def reasoning_details(self):
        pd = self.provider_data or {}
        return pd.get("reasoning_details")

    @property
    def refusal(self) -> Optional[str]:
        pd = self.provider_data or {}
        return pd.get("refusal")


def build_tool_call(
    id: Optional[str],
    name: str,
    arguments: Any,
    **provider_fields: Any,
) -> ToolCallData:
    """构建 ToolCallData，自动序列化 arguments。"""
    args_str = json.dumps(arguments) if isinstance(arguments, dict) else str(arguments)
    pd = dict(provider_fields) if provider_fields else None
    return ToolCallData(id=id, name=name, arguments=args_str, provider_data=pd)


def map_finish_reason(reason: Optional[str], mapping: Dict[str, str]) -> str:
    """将 Provider 特定的停止原因映射为归一化格式。"""
    if reason is None:
        return "stop"
    return mapping.get(reason, "stop")


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------

class Transport(ABC):
    """Transport 抽象基类 — 所有 Provider 适配器必须实现。"""

    def __init__(self, config: TransportConfig):
        self.config = config
        self._client: Any = None

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Provider 名称。"""
        ...

    @abstractmethod
    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]] = None,
        **kwargs,
    ) -> TransportResponse:
        """同步聊天调用。"""
        ...

    @abstractmethod
    def chat_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]] = None,
        **kwargs,
    ) -> Iterator[Dict[str, Any]]:
        """流式聊天调用 — 产出增量事件。

        产出格式：
        - {"type": "content", "text": "..."}
        - {"type": "tool_call", "id": "...", "name": "...", "args": {...}}
        - {"type": "done", "usage": {...}}
        """
        ...

    def close(self) -> None:
        """关闭连接（可选实现）。"""
        pass

    def validate_response(self, response: Any) -> bool:
        """验证原始响应结构是否有效（子类可覆盖）。

        返回 True 表示有效，False 表示响应应被视为无效。
        借鉴 Hermes ProviderTransport.validate_response。
        """
        return True

    def extract_cache_stats(self, response: Any) -> Optional[Dict[str, int]]:
        """提取 Provider 特定的缓存命中/创建统计（子类可覆盖）。

        返回 {"cached_tokens": N, "creation_tokens": N} 或 None。
        借鉴 Hermes ProviderTransport.extract_cache_stats。
        """
        return None

    def map_finish_reason(self, raw_reason: str) -> str:
        """将 Provider 停止原因映射为 OpenAI 等价格式（子类可覆盖）。"""
        return raw_reason

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _normalize_messages(self, messages: List[Dict]) -> List[Dict]:
        """消息格式归一化（子类可覆盖）。

        借鉴 Hermes convert_messages：清洗内部字段，防止泄漏到 Provider。
        """
        return self._sanitize_messages(messages)

    def _normalize_tools(self, tools: List[Dict]) -> List[Dict]:
        """工具定义归一化（子类可覆盖）。"""
        return tools

    def _wrap_error(self, exc: Exception) -> TransportError:
        """将 Provider 异常包装为 TransportError。"""
        return TransportError(str(exc), retryable=False)

    @staticmethod
    def _sanitize_messages(messages: List[Dict]) -> List[Dict]:
        """清洗消息中的内部字段（借鉴 Hermes convert_messages 清洗逻辑）。

        去除以下字段防止 Provider 报 400：
        - _ 前缀的内部标记键
        - tool_name（仅 FTS 索引用）
        - codex_reasoning_items / codex_message_items
        - tool_calls 中的 call_id / response_item_id
        """
        needs_sanitize = False
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if any(
                k in msg
                for k in ("codex_reasoning_items", "codex_message_items",
                          "tool_name", "effect_disposition", "timestamp")
            ):
                needs_sanitize = True
                break
            if any(isinstance(k, str) and k.startswith("_") for k in msg):
                needs_sanitize = True
                break
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict) and (
                        "call_id" in tc or "response_item_id" in tc
                    ):
                        needs_sanitize = True
                        break
                if needs_sanitize:
                    break

        if not needs_sanitize:
            return messages

        sanitized = list(messages)
        for msg_idx, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue

            copied_msg: Optional[Dict] = None

            def mutable_msg():
                nonlocal copied_msg
                if copied_msg is None:
                    copied_msg = dict(msg)
                    sanitized[msg_idx] = copied_msg
                return copied_msg

            # 去除内部键
            internal_keys = [k for k in msg if isinstance(k, str) and (
                k.startswith("_") or k in (
                    "codex_reasoning_items", "codex_message_items",
                    "tool_name", "effect_disposition", "timestamp",
                )
            )]
            if internal_keys:
                out = mutable_msg()
                for key in internal_keys:
                    out.pop(key, None)

            # 清洗 tool_calls
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                copied_tcs: Optional[List] = None
                for tc_idx, tc in enumerate(tool_calls):
                    if isinstance(tc, dict) and (
                        "call_id" in tc or "response_item_id" in tc
                    ):
                        if copied_tcs is None:
                            copied_tcs = list(tool_calls)
                        copied_tc = dict(tc)
                        copied_tc.pop("call_id", None)
                        copied_tc.pop("response_item_id", None)
                        copied_tcs[tc_idx] = copied_tc
                if copied_tcs is not None:
                    mutable_msg()["tool_calls"] = copied_tcs

        return sanitized


__all__ = [
    "Transport",
    "TransportConfig",
    "TransportError",
    "TransportResponse",
    "ToolCallData",
    "UsageData",
    "RateLimitError",
    "AuthenticationError",
    "build_tool_call",
    "map_finish_reason",
]
