"""默认 HTTP 传输 —— 标准库 urllib 实现 + 纯 URL/JSON 辅助。

:mod:`spirit.integrations.base` 定义了 :class:`HttpTransport` 接口；本模块提供其
**默认实现** :class:`UrllibTransport`（无第三方依赖，对标 ``providers/base.py`` 的
``fetch_models`` 请求方式），以及两个纯函数 :func:`build_url` / :func:`safe_json`
供各集成复用（单独可测，无需网络）。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from spirit.integrations.base import HttpResponse

logger = logging.getLogger(__name__)

_MAX_BODY = 8 * 1024 * 1024  # 8 MiB 读取上限，防御超大响应


def build_url(base: str, path: str = "", params: Optional[Dict[str, Any]] = None) -> str:
    """拼接 ``base + path`` 并附加 query 参数（纯函数）。

    - ``base`` 尾部斜杠与 ``path`` 首部斜杠自动去重。
    - ``params`` 中值为 None 的键被忽略。
    """
    base = (base or "").rstrip("/")
    if path:
        if not path.startswith("/"):
            path = "/" + path
        url = base + path
    else:
        url = base
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(clean)
    return url


def safe_json(raw: Any, default: Any = None) -> Any:
    """安全解析 JSON（bytes/str），失败返回 ``default``（纯函数，绝不抛）。"""
    if raw is None:
        return default
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8", errors="replace")
        except Exception:
            return default
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return default
    return raw


class UrllibTransport:
    """基于 :mod:`urllib.request` 的默认 :class:`HttpTransport` 实现。"""

    def __init__(self, user_agent: str = "spirit-agent") -> None:
        self.user_agent = user_agent

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        json_body: Optional[Any] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 15.0,
    ) -> HttpResponse:
        full_url = build_url(url, "", params) if params else url
        data = None
        final_headers: Dict[str, str] = {"User-Agent": self.user_agent}
        if headers:
            final_headers.update(headers)
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            final_headers.setdefault("Content-Type", "application/json")

        req = urllib.request.Request(
            full_url, data=data, method=(method or "GET").upper(), headers=final_headers
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read(_MAX_BODY)
                return HttpResponse(
                    status=getattr(resp, "status", 200),
                    body=body,
                    headers={k: v for k, v in resp.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read(64 * 1024)
            except Exception:  # pragma: no cover - 读取错误体失败不致命
                pass
            return HttpResponse(status=exc.code, body=body, headers=dict(exc.headers or {}))
        except Exception as exc:  # noqa: BLE001 - 网络错误转为响应对象
            logger.debug("UrllibTransport 请求失败 %s %s: %s", method, full_url, exc)
            return HttpResponse(status=0, body=str(exc).encode("utf-8"), headers={})


__all__ = ["UrllibTransport", "build_url", "safe_json", "HttpResponse"]
