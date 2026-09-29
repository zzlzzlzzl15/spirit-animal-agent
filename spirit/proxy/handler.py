"""代理请求处理器 —— 传输无关的核心分发。

对齐 Spirit 的「传输无关命令分发」范式（见 :mod:`spirit.checkpoint.commands` /
:mod:`spirit.profile.commands`）：把 HTTP 语义（method/path/headers/body）与传输实现
解耦。:meth:`ProxyHandler.handle` 接收纯数据、返回 :class:`ProxyResponse` 纯数据，
**不触碰 socket**，故可完全离线单测。真正的 stdlib HTTP 服务在 :mod:`spirit.proxy.server`
里只是一层薄适配。

路由：
- ``GET /health`` —— 健康检查（无需鉴权）
- ``GET /v1/models`` —— 模型清单
- ``POST /v1/chat/completions`` —— 对话补全（流式 SSE / 非流式 JSON）
- ``OPTIONS *`` —— CORS 预检
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

from spirit.proxy import openai_format as fmt
from spirit.proxy.auth import is_authorized
from spirit.proxy.config import ProxyConfig, load_proxy_config
from spirit.proxy.completion import default_completion_fn

logger = logging.getLogger(__name__)

CompletionFn = Callable[..., str]

_JSON_HEADERS = {"Content-Type": "application/json"}
_SSE_HEADERS = {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
}
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
}


@dataclass
class ProxyResponse:
    """传输无关的 HTTP 响应（纯数据）。"""

    status: int
    body: bytes = b""
    headers: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, status: int, payload: Any) -> "ProxyResponse":
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = dict(_JSON_HEADERS)
        headers.update(_CORS_HEADERS)
        return cls(status=status, body=body, headers=headers)

    @classmethod
    def sse(cls, lines: List[str]) -> "ProxyResponse":
        body = "".join(lines).encode("utf-8")
        headers = dict(_SSE_HEADERS)
        headers.update(_CORS_HEADERS)
        return cls(status=200, body=body, headers=headers)


class ProxyHandler:
    """OpenAI 兼容代理的核心分发器（可注入 config + completion seam）。"""

    def __init__(
        self,
        config: Optional[ProxyConfig] = None,
        completion_fn: Optional[CompletionFn] = None,
    ) -> None:
        self.config = config if config is not None else load_proxy_config()
        self.completion_fn = completion_fn or default_completion_fn

    # ------------------------------------------------------------------
    # 顶层分发
    # ------------------------------------------------------------------

    def handle(
        self,
        method: str,
        path: str,
        headers: Optional[Dict[str, str]] = None,
        body: bytes = b"",
    ) -> ProxyResponse:
        """处理一个请求，返回 :class:`ProxyResponse`。绝不抛异常。"""
        headers = _lower_keys(headers or {})
        method = (method or "GET").upper()
        route = _normalize_path(path)

        try:
            if method == "OPTIONS":
                return ProxyResponse(status=204, body=b"", headers=dict(_CORS_HEADERS))
            if route == "/health":
                return self._handle_health()
            # /v1/* 需要鉴权（配置了 key 时）
            if not self._authorized(headers):
                return ProxyResponse.json(
                    401, fmt.build_error(
                        "无效的 API key", err_type="authentication_error",
                        code="invalid_api_key",
                    )
                )
            if route == "/v1/models":
                return self._handle_models(method)
            if route == "/v1/chat/completions":
                return self._handle_chat(method, body)
            return ProxyResponse.json(
                404, fmt.build_error(f"未知端点: {route}", code="not_found")
            )
        except Exception as exc:  # noqa: BLE001 - handler 绝不穿透异常
            logger.warning("代理请求处理失败 %s %s: %s", method, route, exc)
            return ProxyResponse.json(
                500, fmt.build_error(
                    f"内部错误: {exc}", err_type="server_error", code="internal_error"
                )
            )

    # ------------------------------------------------------------------
    # 各端点
    # ------------------------------------------------------------------

    def _authorized(self, headers: Dict[str, str]) -> bool:
        if not self.config.requires_auth():
            return True
        return is_authorized(headers.get("authorization"), self.config.api_key)

    def _handle_health(self) -> ProxyResponse:
        return ProxyResponse.json(200, {
            "status": "ok",
            "model": self.config.model,
            "auth_required": self.config.requires_auth(),
        })

    def _handle_models(self, method: str) -> ProxyResponse:
        if method != "GET":
            return ProxyResponse.json(
                405, fmt.build_error("仅支持 GET", code="method_not_allowed")
            )
        return ProxyResponse.json(200, fmt.build_models_list(self.config.model))

    def _handle_chat(self, method: str, body: bytes) -> ProxyResponse:
        if method != "POST":
            return ProxyResponse.json(
                405, fmt.build_error("仅支持 POST", code="method_not_allowed")
            )
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError) as exc:
            return ProxyResponse.json(
                400, fmt.build_error(f"请求体不是合法 JSON: {exc}", code="invalid_json")
            )
        if not isinstance(payload, dict):
            return ProxyResponse.json(
                400, fmt.build_error("请求体必须是 JSON 对象", code="invalid_json")
            )

        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            return ProxyResponse.json(
                400,
                fmt.build_error("缺少 messages 数组", param="messages",
                                code="missing_messages"),
            )

        model = self._resolve_model(payload.get("model"))
        stream = bool(payload.get("stream"))

        try:
            text = self.completion_fn(messages, model=model) or ""
        except Exception as exc:  # noqa: BLE001 - 补全失败转 OpenAI 错误体
            logger.warning("补全调用失败: %s", exc)
            return ProxyResponse.json(
                502, fmt.build_error(
                    f"上游补全失败: {exc}", err_type="server_error",
                    code="completion_failed",
                )
            )

        prompt_text = "\n".join(
            str(m.get("content", "")) for m in messages if isinstance(m, dict)
        )
        usage = fmt.build_usage(prompt_text, text)

        if stream:
            cid = fmt.new_completion_id()
            lines = fmt.build_sse_chunks(text, model=model, completion_id=cid)
            return ProxyResponse.sse(lines)

        return ProxyResponse.json(200, fmt.build_chat_completion(
            text, model=model, usage=usage,
            finish_reason="stop" if text else "length",
        ))

    def _resolve_model(self, requested: Optional[str]) -> str:
        """把客户端请求的 model 映射到服务端配置的模型（除非允许覆盖）。"""
        if self.config.allow_model_override and requested:
            return str(requested)
        return self.config.model


def _lower_keys(headers: Dict[str, str]) -> Dict[str, str]:
    return {str(k).lower(): v for k, v in headers.items()}


def _normalize_path(path: str) -> str:
    """去掉 query string，规范尾部斜杠（保留根 ``/``）。"""
    raw = urlsplit(path or "/").path
    if len(raw) > 1 and raw.endswith("/"):
        raw = raw.rstrip("/")
    return raw or "/"


__all__ = ["ProxyHandler", "ProxyResponse"]
