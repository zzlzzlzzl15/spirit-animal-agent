"""X/Twitter 搜索集成 —— 经 xAI API 的带引用搜索。

对标 ``spirit/tools/platforms.py`` 的 ``x_search`` 段（原 ``x_search_tool.py``），
重构为 :class:`BaseIntegration` 子类。payload 构造与响应解析为纯函数，网络经注入式
transport seam，可离线单测。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from spirit.integrations.base import (
    BaseIntegration,
    Category,
    IntegrationResult,
    IntegrationSpec,
)

logger = logging.getLogger(__name__)

XAI_DEFAULT_BASE = "https://api.x.ai/v1"
XAI_DEFAULT_MODEL = "grok-4.20-reasoning"

X_SEARCH_SPEC = IntegrationSpec(
    name="x_search",
    display_name="X / Twitter Search",
    category=Category.SEARCH,
    description="搜索 X/Twitter 内容（经 xAI API，带引用）。",
    emoji="🐦",
    required_env=("XAI_API_KEY",),
    optional_env=("XAI_BASE_URL", "X_SEARCH_MODEL"),
    capabilities=("search", "read"),
)


def build_search_payload(
    query: str,
    model: str = XAI_DEFAULT_MODEL,
    *,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    handles: Optional[List[str]] = None,
    max_results: int = 10,
) -> Dict[str, Any]:
    """纯函数：构造 xAI chat/completions + x_search 工具的请求体。"""
    content = query
    extras: List[str] = []
    if from_date:
        extras.append(f"from:{from_date}")
    if to_date:
        extras.append(f"to:{to_date}")
    if handles:
        extras.append("handles:" + ",".join(handles))
    if extras:
        content = query + "\n" + " ".join(extras)
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "tools": [{"type": "x_search"}],
        "max_results": max_results,
    }


def parse_search_response(payload: Any) -> Dict[str, Any]:
    """纯函数：从 xAI 响应体提取 answer + citations（对缺失字段容错）。"""
    if not isinstance(payload, dict):
        return {"answer": "", "citations": []}
    choices = payload.get("choices") or [{}]
    message = (choices[0] or {}).get("message", {}) if choices else {}
    answer = message.get("content", "") if isinstance(message, dict) else ""
    return {"answer": answer or "", "citations": payload.get("citations", []) or []}


class XSearchIntegration(BaseIntegration):
    """X/Twitter 搜索集成（xAI）。"""

    def __init__(self, transport=None, env=None, api_base: Optional[str] = None) -> None:
        super().__init__(X_SEARCH_SPEC, transport=transport, env=env)
        self._api_base_override = api_base

    @property
    def api_base(self) -> str:
        return (self._api_base_override or self.getenv("XAI_BASE_URL") or XAI_DEFAULT_BASE).rstrip("/")

    @property
    def model(self) -> str:
        return self.getenv("X_SEARCH_MODEL") or XAI_DEFAULT_MODEL

    def search(
        self,
        query: str,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        handles: Optional[List[str]] = None,
        max_results: int = 10,
    ) -> IntegrationResult:
        if not self.is_configured():
            return IntegrationResult.failure("XAI_API_KEY 未设置")
        if not query:
            return IntegrationResult.failure("query 不能为空")
        api_key = self.getenv("XAI_API_KEY")
        payload = build_search_payload(
            query, self.model, from_date=from_date, to_date=to_date,
            handles=handles, max_results=max_results,
        )
        url = f"{self.api_base}/chat/completions"
        try:
            resp = self.transport.request(
                "POST", url,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json_body=payload, timeout=180.0,
            )
            if not resp.ok:
                return IntegrationResult.failure(f"xAI API 错误: {resp.status}")
            return IntegrationResult.success(parse_search_response(resp.json()))
        except Exception as exc:  # noqa: BLE001
            return IntegrationResult.failure(str(exc))


__all__ = [
    "XSearchIntegration",
    "X_SEARCH_SPEC",
    "build_search_payload",
    "parse_search_response",
    "XAI_DEFAULT_BASE",
    "XAI_DEFAULT_MODEL",
]
