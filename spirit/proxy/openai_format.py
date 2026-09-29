"""OpenAI 兼容线格式构造器 —— 纯函数，无 IO。

对标 Hermes ``.plans/openai-api-server.md`` 的 Chat Completions / Models / SSE 响应
格式，按 Spirit 精简为一组**纯构造函数**：输入文本/元数据，输出可直接 ``json.dumps``
的 dict（或 SSE 字符串）。不含任何 HTTP/agent 依赖，便于离线单测。

覆盖：
- :func:`build_chat_completion` —— 非流式 ``chat.completion`` 响应
- :func:`build_models_list` —— ``GET /v1/models`` 响应
- :func:`build_sse_chunks` —— 流式 ``chat.completion.chunk`` SSE 序列（含 ``[DONE]``）
- :func:`build_error` —— OpenAI 风格错误体
- :func:`estimate_tokens` —— 粗略 token 估算（无 tokenizer 依赖）
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional


def new_completion_id() -> str:
    """生成 ``chatcmpl-<hex>`` 风格的响应 id。"""
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def estimate_tokens(text: str) -> int:
    """粗略 token 估算（无 tokenizer 依赖）：约每 4 字符 1 token，至少 1。"""
    if not text:
        return 0
    return max(1, len(text) // 4)


def build_usage(prompt_text: str, completion_text: str) -> Dict[str, int]:
    """构造 ``usage`` 段（prompt/completion/total）。"""
    p = estimate_tokens(prompt_text)
    c = estimate_tokens(completion_text)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def build_chat_completion(
    text: str,
    *,
    model: str,
    completion_id: Optional[str] = None,
    created: Optional[int] = None,
    finish_reason: str = "stop",
    usage: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """构造非流式 ``chat.completion`` 响应体。"""
    return {
        "id": completion_id or new_completion_id(),
        "object": "chat.completion",
        "created": created if created is not None else int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def build_models_list(model: str, *, created: Optional[int] = None) -> Dict[str, Any]:
    """构造 ``GET /v1/models`` 响应体（把 Spirit agent 暴露为单个模型）。"""
    return {
        "object": "list",
        "data": [
            {
                "id": model,
                "object": "model",
                "created": created if created is not None else int(time.time()),
                "owned_by": "spirit-agent",
            }
        ],
    }


def _chunk(
    completion_id: str,
    model: str,
    created: int,
    delta: Dict[str, Any],
    finish_reason: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def sse_line(payload: Dict[str, Any]) -> str:
    """把一个 chunk dict 序列化为单条 SSE ``data:`` 行（含结尾空行）。"""
    import json
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def build_sse_chunks(
    text: str,
    *,
    model: str,
    completion_id: Optional[str] = None,
    created: Optional[int] = None,
    chunk_size: int = 0,
    finish_reason: str = "stop",
) -> List[str]:
    """构造流式响应的完整 SSE 文本行序列（含 role 首块、内容块、finish 块、``[DONE]``）。

    ``chunk_size <= 0`` 时整段文本作为单个内容块下发（MVP 策略，兼容所有前端）。
    """
    cid = completion_id or new_completion_id()
    ts = created if created is not None else int(time.time())
    lines: List[str] = [sse_line(_chunk(cid, model, ts, {"role": "assistant"}))]

    if chunk_size and chunk_size > 0:
        pieces = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)] or [""]
    else:
        pieces = [text] if text else []
    for piece in pieces:
        lines.append(sse_line(_chunk(cid, model, ts, {"content": piece})))

    lines.append(sse_line(_chunk(cid, model, ts, {}, finish_reason=finish_reason)))
    lines.append("data: [DONE]\n\n")
    return lines


def build_error(
    message: str,
    *,
    err_type: str = "invalid_request_error",
    code: Optional[str] = None,
    param: Optional[str] = None,
) -> Dict[str, Any]:
    """构造 OpenAI 风格错误响应体 ``{"error": {...}}``。"""
    return {
        "error": {
            "message": message,
            "type": err_type,
            "param": param,
            "code": code,
        }
    }


__all__ = [
    "new_completion_id",
    "estimate_tokens",
    "build_usage",
    "build_chat_completion",
    "build_models_list",
    "build_sse_chunks",
    "sse_line",
    "build_error",
]
