"""Home Assistant 智能家居集成 —— 实体列举/状态/服务调用（含安全护栏）。

对标 ``spirit/tools/platforms.py`` 的 HA 段（原 ``homeassistant_tool.py``），重构为
:class:`BaseIntegration` 子类。完整保留原有**安全护栏**：

- ``entity_id`` 必须匹配 ``domain.name`` 白名单正则（防注入）。
- ``domain`` / ``service`` 必须是 ``[a-z0-9_]`` 服务名。
- ``_BLOCKED_DOMAINS``（shell_command/python_script/hassio 等）**禁止调用** ——
  这些域能在 HA 侧执行任意代码，属提权风险。

网络经注入式 transport seam，护栏逻辑纯函数，全部可离线单测。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from spirit.integrations.base import (
    BaseIntegration,
    Category,
    IntegrationResult,
    IntegrationSpec,
)
from spirit.integrations.http_client import build_url

logger = logging.getLogger(__name__)

HA_DEFAULT_URL = "http://homeassistant.local:8123"

_ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")
_SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_BLOCKED_DOMAINS = frozenset({
    "shell_command", "command_line", "python_script",
    "pyscript", "hassio", "rest_command",
})

HA_SPEC = IntegrationSpec(
    name="homeassistant",
    display_name="Home Assistant",
    category=Category.SMART_HOME,
    description="Home Assistant 智能家居：列举实体、读状态、调用服务（带安全护栏）。",
    emoji="🏠",
    required_env=("HASS_TOKEN",),
    optional_env=("HASS_URL",),
    config_keys=("integrations.homeassistant_url",),
    capabilities=("read", "control"),
    docs_url="https://developers.home-assistant.io/docs/api/rest",
)


def validate_entity_id(entity_id: str) -> bool:
    """纯函数：entity_id 是否符合 ``domain.name`` 白名单格式。"""
    return bool(entity_id) and bool(_ENTITY_ID_RE.match(entity_id))


def validate_service_name(name: str) -> bool:
    """纯函数：domain/service 名是否合法。"""
    return bool(name) and bool(_SERVICE_NAME_RE.match(name))


def is_blocked_domain(domain: str) -> bool:
    """纯函数：该 domain 是否被安全策略禁止（可执行任意代码的高危域）。"""
    return domain in _BLOCKED_DOMAINS


class HomeAssistantIntegration(BaseIntegration):
    """Home Assistant REST API 集成。"""

    def __init__(self, transport=None, env=None, base_url: Optional[str] = None) -> None:
        super().__init__(HA_SPEC, transport=transport, env=env)
        self._base_url_override = base_url

    @property
    def base_url(self) -> str:
        """按调用解析 HA 地址：注入 > HASS_URL env > config.yaml > 默认。"""
        if self._base_url_override:
            return self._base_url_override.rstrip("/")
        env_url = self.getenv("HASS_URL")
        if env_url:
            return env_url.rstrip("/")
        try:
            from spirit.config import get_config_value
            cfg = get_config_value("integrations.homeassistant_url", None)
            if cfg:
                return str(cfg).rstrip("/")
        except Exception:  # pragma: no cover
            pass
        return HA_DEFAULT_URL

    def _headers(self) -> Dict[str, str]:
        token = self.getenv("HASS_TOKEN")
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _get(self, path: str):
        return self.transport.request(
            "GET", build_url(self.base_url, path), headers=self._headers()
        )

    def _post(self, path: str, body: Dict[str, Any]):
        return self.transport.request(
            "POST", build_url(self.base_url, path),
            headers=self._headers(), json_body=body,
        )

    # -- 动作 -------------------------------------------------------------

    def list_entities(self, domain: str = "", area: str = "") -> IntegrationResult:
        if not self.is_configured():
            return IntegrationResult.failure("HASS_TOKEN 未设置")
        try:
            resp = self._get("/api/states")
            if not resp.ok:
                return IntegrationResult.failure(f"HA 错误 ({resp.status})")
            states = resp.json() or []
            filtered = self._filter_states(states, domain, area)
            entities = [
                {
                    "entity_id": s.get("entity_id"),
                    "state": s.get("state"),
                    "friendly_name": (s.get("attributes") or {}).get("friendly_name", ""),
                }
                for s in filtered
            ]
            return IntegrationResult.success(
                {"count": len(entities), "entities": entities[:200]}
            )
        except Exception as exc:  # noqa: BLE001
            return IntegrationResult.failure(str(exc))

    @staticmethod
    def _filter_states(states: List[Dict], domain: str, area: str) -> List[Dict]:
        """纯函数：按 domain 前缀与 area（friendly_name 子串）过滤实体。"""
        result = states
        if domain:
            result = [
                s for s in result
                if str(s.get("entity_id", "")).startswith(f"{domain}.")
            ]
        if area:
            area_lower = area.lower()
            result = [
                s for s in result
                if area_lower in str(
                    (s.get("attributes") or {}).get("friendly_name", "")
                ).lower()
            ]
        return result

    def get_state(self, entity_id: str) -> IntegrationResult:
        if not self.is_configured():
            return IntegrationResult.failure("HASS_TOKEN 未设置")
        if not validate_entity_id(entity_id):
            return IntegrationResult.failure(f"无效的 entity_id: {entity_id}")
        try:
            resp = self._get(f"/api/states/{entity_id}")
            if not resp.ok:
                return IntegrationResult.failure(f"HA 错误 ({resp.status})")
            return IntegrationResult.success({"entity": resp.json()})
        except Exception as exc:  # noqa: BLE001
            return IntegrationResult.failure(str(exc))

    def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> IntegrationResult:
        if not self.is_configured():
            return IntegrationResult.failure("HASS_TOKEN 未设置")
        if not validate_service_name(domain) or not validate_service_name(service):
            return IntegrationResult.failure("无效的 domain 或 service 名称")
        if is_blocked_domain(domain):
            return IntegrationResult.failure(f"安全策略阻止了 {domain} 域的服务调用")
        body: Dict[str, Any] = dict(data or {})
        if entity_id:
            body["entity_id"] = entity_id
        try:
            resp = self._post(f"/api/services/{domain}/{service}", body)
            if not resp.ok:
                return IntegrationResult.failure(f"HA 服务调用失败 ({resp.status})")
            return IntegrationResult.success({"result": resp.json()})
        except Exception as exc:  # noqa: BLE001
            return IntegrationResult.failure(str(exc))


__all__ = [
    "HomeAssistantIntegration",
    "HA_SPEC",
    "HA_DEFAULT_URL",
    "validate_entity_id",
    "validate_service_name",
    "is_blocked_domain",
]
