"""LSP JSON-RPC 2.0 协议帧。

LSP 线格式：

    Content-Length: <bytes>\r\n
    \r\n
    <utf-8 JSON body>

消息体是 JSON-RPC 2.0 信封：request / response / notification。

职责：
- encode_message: 将 dict 编码为 Content-Length 帧格式
- read_message: 从 asyncio StreamReader 读取一帧消息
- make_request / make_response / make_notification: 构造 JSON-RPC 信封
- 错误码常量
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger("spirit.lsp.protocol")

# LSP 错误码
ERROR_REQUEST_CANCELLED = -32800
ERROR_CONTENT_MODIFIED = -32801
ERROR_METHOD_NOT_FOUND = -32601


class LSPProtocolError(Exception):
    """协议层违规（帧损坏、流意外关闭等）。"""


class LSPRequestError(Exception):
    """服务器返回 JSON-RPC 错误响应。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"LSP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


# ---------------------------------------------------------------------------
# 编码
# ---------------------------------------------------------------------------

def encode_message(obj: dict) -> bytes:
    """将 JSON-RPC 信封编码为 Content-Length 帧字节串。"""
    body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

async def read_message(reader: asyncio.StreamReader) -> Optional[dict]:
    """从流中读取一帧 Content-Length 消息。

    Returns:
        解析后的 JSON dict，或 None（流正常关闭）。

    Raises:
        LSPProtocolError: 帧格式损坏。
    """
    headers: dict = {}
    while True:
        try:
            line = await reader.readuntil(b"\r\n")
        except asyncio.IncompleteReadError:
            return None  # 服务器关闭了 stdout
        except asyncio.LimitOverrunError:
            raise LSPProtocolError("Header 行过长")

        text = line.decode("ascii", errors="replace").strip()
        if not text:
            break  # 空行 = header 结束

        if ":" in text:
            key, _, value = text.partition(":")
            headers[key.strip().lower()] = value.strip()

    # 读取 body
    content_length = int(headers.get("content-length", 0))
    if content_length <= 0:
        raise LSPProtocolError(f"无效的 Content-Length: {content_length}")

    body = await reader.readexactly(content_length)
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise LSPProtocolError(f"JSON 解析失败: {e}")


# ---------------------------------------------------------------------------
# 信封构造
# ---------------------------------------------------------------------------

def make_request(method: str, params: Any, request_id: int) -> dict:
    """构造 JSON-RPC 2.0 请求。"""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }


def make_response(request_id: int, result: Any) -> dict:
    """构造 JSON-RPC 2.0 成功响应。"""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }


def make_error_response(request_id: int, code: int, message: str) -> dict:
    """构造 JSON-RPC 2.0 错误响应。"""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def make_notification(method: str, params: Any) -> dict:
    """构造 JSON-RPC 2.0 通知（无 id，不期望响应）。"""
    return {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
    }


# ---------------------------------------------------------------------------
# 消息分类
# ---------------------------------------------------------------------------

def classify_message(msg: dict) -> str:
    """分类消息类型。

    Returns:
        "request" | "response" | "notification"
    """
    if "id" in msg and "method" in msg:
        return "request"
    if "id" in msg:
        return "response"
    if "method" in msg:
        return "notification"
    return "unknown"
