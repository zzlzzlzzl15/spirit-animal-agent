"""文档 / 专有平台集成 —— 飞书文档 + 元宝（SDK/上下文门控）。

对标 ``spirit/tools/platforms.py`` 的飞书与元宝段。这两个集成需要**额外 SDK** 或
**gateway 连接上下文**才能真正工作，故按 Spirit「优雅降级」约定：spec 声明依赖，
:func:`BaseIntegration.is_configured` 检测 SDK 可用性，未就绪时返回结构化失败而非抛错。

- :class:`FeishuDocIntegration` —— 需 ``lark_oapi`` SDK（``requires_sdk``）。
- :class:`YuanbaoIntegration` —— 需注入 gateway 上下文（``context`` 参数）。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from spirit.integrations.base import (
    BaseIntegration,
    Category,
    IntegrationResult,
    IntegrationSpec,
)

logger = logging.getLogger(__name__)

FEISHU_SPEC = IntegrationSpec(
    name="feishu",
    display_name="飞书 / Lark 文档",
    category=Category.DOCS,
    description="读取飞书/Lark 文档纯文本内容（需 lark_oapi SDK 与应用上下文）。",
    emoji="📄",
    any_of_env=("FEISHU_APP_ID", "LARK_APP_ID"),
    optional_env=("FEISHU_APP_SECRET", "LARK_APP_SECRET"),
    requires_sdk="lark_oapi",
    capabilities=("read",),
)

YUANBAO_SPEC = IntegrationSpec(
    name="yuanbao",
    display_name="元宝",
    category=Category.MESSAGING,
    description="元宝平台交互：群信息、成员查询、贴纸/私信（需 gateway 连接上下文）。",
    emoji="🪙",
    capabilities=("read", "send"),
)


class FeishuDocIntegration(BaseIntegration):
    """飞书文档读取（SDK 门控）。"""

    def __init__(self, transport=None, env=None) -> None:
        super().__init__(FEISHU_SPEC, transport=transport, env=env)

    def read_doc(self, doc_token: str) -> IntegrationResult:
        if not doc_token:
            return IntegrationResult.failure("doc_token 必填")
        if not self._sdk_available():
            return IntegrationResult.failure(
                "飞书文档读取需要 lark_oapi SDK（pip install lark-oapi）和应用上下文。"
            )
        # SDK 就绪时的真实读取属未来扩展点（需 app_id/secret 换取 tenant token）。
        return IntegrationResult.failure(
            "飞书文档读取尚未接线到 lark_oapi 客户端（当前仅声明依赖）。"
        )


class YuanbaoIntegration(BaseIntegration):
    """元宝平台交互（gateway 上下文门控）。"""

    def __init__(self, transport=None, env=None, context: Optional[Any] = None) -> None:
        super().__init__(YUANBAO_SPEC, transport=transport, env=env)
        self._context = context

    def is_configured(self) -> bool:
        """元宝依赖 gateway 注入的上下文，而非环境变量。"""
        return self._context is not None

    def missing_requirements(self):
        return [] if self._context is not None else ["gateway_context"]

    def call(self, action: str, **kwargs: Any) -> IntegrationResult:
        if not self.is_configured():
            return IntegrationResult.failure(
                "元宝平台需要 gateway 连接上下文，当前不在元宝平台环境中。"
            )
        handler = getattr(self._context, action, None)
        if not callable(handler):
            return IntegrationResult.failure(f"元宝不支持的操作: {action}")
        try:
            return IntegrationResult.success(handler(**kwargs))
        except Exception as exc:  # noqa: BLE001
            return IntegrationResult.failure(str(exc))


__all__ = [
    "FeishuDocIntegration",
    "YuanbaoIntegration",
    "FEISHU_SPEC",
    "YUANBAO_SPEC",
]
