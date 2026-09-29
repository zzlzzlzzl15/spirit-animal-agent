"""Provider profile 基类 —— 声明式定义一个推理 provider 的一切。

移植自 Hermes ``providers/base.py``，适配 Spirit 命名空间与依赖：

一个 ``ProviderProfile`` 把某个 provider 的身份、鉴权、端点、请求期怪癖集中声明
在一处。下游各层（config 解析、transport 路由、failover 探测、模型列举）都读这些
profile，而不是各自维护一份平行数据。

Profile 是**声明式**的 —— 只描述 provider 的行为，不拥有 client 构造、凭据轮换、
流式输出（那些仍留在 ``SpiritAgent`` / transport 层）。

与 Hermes 的差异（适配点）：
- ``fetch_models()`` 用标准库 ``urllib.request.urlopen``（Spirit 无
  ``hermes_cli.urllib_security``），与 ``spirit/llm_pool.py::probe`` 同款请求方式。
- ``_profile_user_agent()`` 取 ``spirit.__version__``，缺失时回退静态串。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# “完全省略 temperature” 的哨兵（某些 provider 由服务端管理温度，不下发该字段）
OMIT_TEMPERATURE = object()


def _profile_user_agent() -> str:
    """返回 ``spirit-agent/<version>`` UA 串，带稳定回退。

    供 ``ProviderProfile.fetch_models`` 的目录探测使用，避免被默认
    ``Python-urllib/<ver>`` UA 的 WAF 挡下 403。
    """
    try:
        from spirit import __version__ as _ver  # 延迟导入：避免 import 期环依赖
        return f"spirit-agent/{_ver}"
    except Exception:
        return "spirit-agent"


@dataclass
class ProviderProfile:
    """Provider 声明档 —— 可直接实例化并覆盖字段，也可子类化加 hook。"""

    # ── 身份 ─────────────────────────────────────────────────
    name: str
    # Spirit transport 路由键：chat_completions|anthropic_messages|gemini|bedrock
    api_mode: str = "chat_completions"
    aliases: tuple = ()

    # ── 人类可读元数据 ───────────────────────────────────────
    display_name: str = ""       # 选择器/标签展示名，如 "OpenRouter"
    description: str = ""        # 选择器副标题，一行描述
    signup_url: str = ""         # 申请 key 的网址，配置向导展示

    # ── 鉴权与端点 ───────────────────────────────────────────
    env_vars: tuple = ()         # 环境变量名（API key / base_url 查找），按序取首个非空
    base_url: str = ""
    models_url: str = ""         # 显式模型目录端点；空则回退 {base_url}/models
    auth_type: str = "api_key"   # api_key|oauth_device_code|oauth_external|aws_sdk
    supports_health_check: bool = True  # False → doctor 跳过对该 provider 的 /models 探测

    # ── 视觉（多模态）支持 ───────────────────────────────────
    # provider 的 API 是否原生接受 tool-result 消息里的图片内容。
    supports_vision: bool = False
    # 是否接受 list 型 tool 消息内容（含 image_url 分部的多模态）。默认 True。
    supports_vision_tool_messages: bool = True

    # ── 模型目录 ─────────────────────────────────────────────
    # fallback_models：实时拉取失败时，/model 选择器展示的兜底清单。
    # 只应放支持工具调用的 agentic 模型。
    fallback_models: tuple = ()

    # hostname：URL→provider 反查用的基础主机名（factory._detect_provider 用）。
    # 空则由 base_url 派生。
    hostname: str = ""

    # ── client 级怪癖（构造 client 时设一次） ────────────────
    default_headers: dict[str, str] = field(default_factory=dict)

    # ── 请求级怪癖 ───────────────────────────────────────────
    # temperature：None = 用调用方默认；OMIT_TEMPERATURE = 不下发
    fixed_temperature: Any = None
    default_max_tokens: int | None = None
    default_aux_model: str = ""  # 辅助任务（压缩/视觉等）用的廉价模型；空=复用主模型

    # ── Hooks（复杂 provider 在子类里覆盖） ──────────────────

    def get_hostname(self) -> str:
        """返回 URL 反查用的基础主机名。

        显式设了 ``self.hostname`` 就用它，否则从 ``base_url`` 派生。
        例：``https://api.minimaxi.com/v1`` → ``api.minimaxi.com``
        """
        if self.hostname:
            return self.hostname
        if self.base_url:
            from urllib.parse import urlparse
            return urlparse(self.base_url).hostname or ""
        return ""

    def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Provider 特定的消息预处理。默认透传。

        调用时机：字段清洗之后、developer 角色替换之前。
        """
        return messages

    def build_extra_body(
        self, *, session_id: str | None = None, **context: Any
    ) -> dict[str, Any]:
        """Provider 特定的 extra_body 字段。默认空 dict。"""
        return {}

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """拆分到 extra_body 与顶层 api_kwargs 的 provider 特定参数。

        返回 ``(extra_body_additions, top_level_kwargs)``。默认 ``({}, {})``。
        存在此拆分是因为有的 provider 把 reasoning 配置放 extra_body，有的放顶层。
        """
        return {}, {}

    def default_vision_model(self) -> str | None:
        """返回该 provider 的默认视觉模型 id，或 None（回退主模型/聚合链）。"""
        return None

    def get_max_tokens(self, model: str | None) -> int | None:
        """返回 *model* 的默认 max_tokens 上限。

        默认返回静态字段 ``self.default_max_tokens``；子类可覆盖以按 model 变化。
        """
        return self.default_max_tokens

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """从 provider 的 models 端点实时拉取模型清单。

        返回模型 id 字符串列表；拉取失败或不支持实时列举时返回 None。

        端点解析顺序：
          1. ``self.models_url``（显式覆盖 —— 目录端点与推理端点不同时用）
          2. ``base_url``（调用方覆盖 —— 用户配置的 model.base_url）
          3. ``self.base_url + "/models"``（标准 OpenAI 兼容回退）

        默认实现在给了 api_key 时带 Bearer 鉴权，并转发 ``self.default_headers``。
        无 REST 目录的 provider（Bedrock）或 OAuth 目录（Anthropic）应覆盖此方法。
        调用方在本方法返回 None 时须回退到静态 ``fallback_models``。
        """
        effective_base = base_url or self.base_url
        url = (self.models_url or "").strip()
        if not url:
            if not effective_base:
                return None
            url = effective_base.rstrip("/") + "/models"

        import json
        import urllib.request

        req = urllib.request.Request(url)
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Accept", "application/json")
        # 某些 provider 的 WAF 会挡默认 Python-urllib UA，设一个通用 spirit UA。
        req.add_header("User-Agent", _profile_user_agent())
        for k, v in self.default_headers.items():
            req.add_header(k, v)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            items = data if isinstance(data, list) else data.get("data", [])
            return [m["id"] for m in items if isinstance(m, dict) and "id" in m]
        except Exception as exc:  # noqa: BLE001 - 目录拉取失败绝不阻断
            logger.debug("fetch_models(%s): %s", self.name, exc)
            return None


__all__ = ["ProviderProfile", "OMIT_TEMPERATURE"]
