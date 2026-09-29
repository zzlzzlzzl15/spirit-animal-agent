"""外部集成框架基座 —— Spirit Agent。

对标 Hermes 散落在 ``tools/``（discord/feishu/homeassistant/x_search/send_message）
与 ``gateway/platforms/`` 的各平台集成，按 Spirit「精简可测试子集」约定收敛为一个
**声明式 + 注入式 HTTP seam** 的集成框架：

- :class:`IntegrationSpec` —— 声明一个集成的身份、凭据需求、能力（纯数据）。
- :class:`BaseIntegration` —— 统一契约：``is_configured()`` / ``missing_requirements()``
  / ``describe()`` / ``health_check()``；网络经**注入式** :class:`HttpTransport` seam，
  故所有集成逻辑可离线单测（传 FakeTransport）。
- :class:`IntegrationRegistry` —— 集成注册表（懒发现 + 按类别列举）。
- :class:`IntegrationResult` —— 统一结构化返回（ok/data/error），命令层直接渲染。

设计遵循 Spirit 三大范式：按调用解析凭据（读注入的 env 映射，默认 ``os.environ``）、
注入式可测试 seam（transport）、传输无关命令分发（见 :mod:`spirit.integrations.commands`）。

**注意**：本框架是集成能力的**新家**；既有 ``spirit/tools/platforms.py`` 的工具注册
保持不变（工具层可后续逐步委托到此处），以避免回归。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 类别常量
# --------------------------------------------------------------------------

class Category:
    MESSAGING = "messaging"
    SMART_HOME = "smart_home"
    SOCIAL = "social"
    DOCS = "docs"
    SEARCH = "search"


# --------------------------------------------------------------------------
# 结构化返回
# --------------------------------------------------------------------------

@dataclass
class IntegrationResult:
    """统一的集成调用结果（可 JSON 化）。"""

    ok: bool
    data: Any = None
    error: Optional[str] = None

    @classmethod
    def success(cls, data: Any = None) -> "IntegrationResult":
        return cls(ok=True, data=data)

    @classmethod
    def failure(cls, error: str) -> "IntegrationResult":
        return cls(ok=False, error=error)

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "data": self.data, "error": self.error}


# --------------------------------------------------------------------------
# HTTP seam（注入式，离线可测）
# --------------------------------------------------------------------------

@dataclass
class HttpResponse:
    """传输无关的 HTTP 响应。"""

    status: int
    body: bytes = b""
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        import json
        return json.loads(self.text() or "null")


class HttpTransport(Protocol):
    """HTTP 传输接口。默认实现见 :mod:`spirit.integrations.http_client`。"""

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
        ...


# --------------------------------------------------------------------------
# 集成声明
# --------------------------------------------------------------------------

@dataclass
class IntegrationSpec:
    """一个集成的声明式元数据（纯数据，可离线断言）。"""

    name: str                       # 规范键，如 "discord"
    display_name: str               # 展示名，如 "Discord"
    category: str = Category.MESSAGING
    description: str = ""
    emoji: str = "🔌"
    # 凭据需求：全部 required_env 齐备才算 configured（或任一 any_of_env 齐备）。
    required_env: tuple = ()
    any_of_env: tuple = ()
    # 可选环境变量（用于展示/覆盖端点）。
    optional_env: tuple = ()
    # config.yaml 里的相关键（展示用）。
    config_keys: tuple = ()
    # 能力标签，如 ("read", "send", "control")。
    capabilities: tuple = ()
    # 需要额外 SDK 才可用（如飞书 lark_oapi）。
    requires_sdk: Optional[str] = None
    docs_url: str = ""


# --------------------------------------------------------------------------
# 集成基类
# --------------------------------------------------------------------------

EnvGetter = Callable[[str, str], str]


class BaseIntegration:
    """所有具体集成的基类：统一凭据解析 + 注入式 transport。"""

    spec: IntegrationSpec

    def __init__(
        self,
        spec: IntegrationSpec,
        transport: Optional[HttpTransport] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.spec = spec
        self._transport = transport
        # env 默认读 os.environ（按调用），测试可注入固定映射。
        self._env: Optional[Mapping[str, str]] = env

    # -- 凭据 / 环境 ------------------------------------------------------

    def getenv(self, key: str, default: str = "") -> str:
        """按调用读取环境变量（注入 env 优先，否则 os.environ）。"""
        source = self._env if self._env is not None else os.environ
        value = source.get(key, default)
        return value.strip() if isinstance(value, str) else value

    @property
    def transport(self) -> HttpTransport:
        """惰性解析 transport（未注入时用 stdlib urllib 实现）。"""
        if self._transport is None:
            from spirit.integrations.http_client import UrllibTransport
            self._transport = UrllibTransport()
        return self._transport

    def is_configured(self) -> bool:
        """凭据是否齐备（required_env 全有，或 any_of_env 至少一个）。"""
        if not self._sdk_available():
            return False
        if self.spec.required_env:
            if not all(self.getenv(k) for k in self.spec.required_env):
                return False
        if self.spec.any_of_env:
            if not any(self.getenv(k) for k in self.spec.any_of_env):
                return False
        return True

    def _sdk_available(self) -> bool:
        if not self.spec.requires_sdk:
            return True
        try:
            import importlib.util
            return importlib.util.find_spec(self.spec.requires_sdk) is not None
        except (ImportError, ValueError):
            return False

    def missing_requirements(self) -> List[str]:
        """返回缺失的凭据/依赖清单（用于 /integrations status 展示）。"""
        missing: List[str] = []
        if self.spec.requires_sdk and not self._sdk_available():
            missing.append(f"sdk:{self.spec.requires_sdk}")
        for key in self.spec.required_env:
            if not self.getenv(key):
                missing.append(key)
        if self.spec.any_of_env and not any(self.getenv(k) for k in self.spec.any_of_env):
            missing.append("any_of:" + "|".join(self.spec.any_of_env))
        return missing

    def describe(self) -> Dict[str, Any]:
        """返回该集成的可展示元数据 + 配置状态。"""
        return {
            "name": self.spec.name,
            "display_name": self.spec.display_name,
            "category": self.spec.category,
            "description": self.spec.description,
            "emoji": self.spec.emoji,
            "capabilities": list(self.spec.capabilities),
            "configured": self.is_configured(),
            "missing": self.missing_requirements(),
            "docs_url": self.spec.docs_url,
        }

    def health_check(self) -> IntegrationResult:
        """默认健康检查：仅校验凭据是否齐备（不发网络请求）。

        具体集成可覆盖以做轻量探活。
        """
        if self.is_configured():
            return IntegrationResult.success({"configured": True})
        return IntegrationResult.failure(
            "未配置，缺失: " + ", ".join(self.missing_requirements() or ["?"])
        )


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------

class IntegrationRegistry:
    """集成注册表：按 name 注册/查询，按类别列举。"""

    def __init__(self) -> None:
        self._items: Dict[str, BaseIntegration] = {}

    def register(self, integration: BaseIntegration) -> None:
        self._items[integration.spec.name] = integration

    def get(self, name: str) -> Optional[BaseIntegration]:
        return self._items.get(name)

    def list(self, category: Optional[str] = None) -> List[BaseIntegration]:
        items = list(self._items.values())
        if category:
            items = [i for i in items if i.spec.category == category]
        return sorted(items, key=lambda i: i.spec.name)

    def names(self) -> List[str]:
        return sorted(self._items.keys())

    def categories(self) -> List[str]:
        return sorted({i.spec.category for i in self._items.values()})

    def configured(self) -> List[BaseIntegration]:
        return [i for i in self.list() if i.is_configured()]

    def clear(self) -> None:
        self._items.clear()


__all__ = [
    "Category",
    "IntegrationResult",
    "HttpResponse",
    "HttpTransport",
    "IntegrationSpec",
    "BaseIntegration",
    "IntegrationRegistry",
]
