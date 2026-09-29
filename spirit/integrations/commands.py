"""``/integrations`` 命令 —— 传输无关分发。

对齐 Spirit 的命令范式（见 :mod:`spirit.checkpoint.commands` /
:mod:`spirit.profile.commands`）：把命令解析与业务集中在此，返回结构化 dict
（``ok/action/message/lines``），CLI 与 ws_server 各自渲染。**绝不抛异常**。

用法::

    /integrations list [category]
    /integrations status [name]
    /integrations test <name>
    /integrations help
"""

from __future__ import annotations

import logging
import shlex
from typing import Any, Dict, List, Optional

from spirit.integrations.base import IntegrationRegistry

logger = logging.getLogger(__name__)

_VERB_ALIASES = {
    "list": "list", "ls": "list", "l": "list",
    "status": "status", "info": "status", "show": "status",
    "test": "test", "check": "test", "health": "test",
    "help": "help", "?": "help",
}


def _result(
    ok: bool, action: str, message: str = "", *,
    lines: Optional[List[str]] = None, **extra: Any,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": ok, "action": action, "message": message, "lines": lines or []}
    out.update(extra)
    return out


def _tokenize(arg: str) -> List[str]:
    try:
        return shlex.split(arg or "")
    except ValueError:
        return (arg or "").split()


def _describe_line(desc: Dict[str, Any]) -> str:
    mark = "✓" if desc["configured"] else "○"
    caps = ",".join(desc.get("capabilities") or []) or "-"
    return (
        f"{mark} {desc['emoji']} {desc['display_name']} "
        f"[{desc['name']}] ({desc['category']}) 能力:{caps}"
    )


def handle_integrations_command(
    arg: str, registry: Optional[IntegrationRegistry] = None
) -> Dict[str, Any]:
    """解析并执行 ``/integrations`` 命令，返回结构化结果 dict。"""
    if registry is None:
        from spirit.integrations import get_registry
        registry = get_registry()

    tokens = _tokenize(arg)
    if not tokens:
        tokens = ["list"]
    verb = _VERB_ALIASES.get(tokens[0].lower())
    if not verb:
        return _result(False, "unknown", f"未知子命令: {tokens[0]}")
    rest = tokens[1:]

    try:
        if verb == "list":
            return _cmd_list(registry, rest[0] if rest else None)
        if verb == "status":
            return _cmd_status(registry, rest[0] if rest else None)
        if verb == "test":
            return _cmd_test(registry, rest[0] if rest else None)
        return _cmd_help()
    except Exception as exc:  # noqa: BLE001 - 命令层绝不抛
        logger.warning("integrations 命令失败: %s", exc)
        return _result(False, verb, f"执行失败: {exc}")


# --------------------------------------------------------------------------

def _cmd_list(registry: IntegrationRegistry, category: Optional[str]) -> Dict[str, Any]:
    items = registry.list(category=category)
    if not items:
        msg = f"类别 {category} 下暂无集成" if category else "暂无集成"
        return _result(True, "list", msg, lines=[])
    lines = [_describe_line(i.describe()) for i in items]
    scope = f"（类别 {category}）" if category else ""
    ready = sum(1 for i in items if i.is_configured())
    return _result(
        True, "list", f"共 {len(items)} 个集成{scope}，{ready} 个已配置",
        lines=lines, categories=registry.categories(),
    )


def _cmd_status(registry: IntegrationRegistry, name: Optional[str]) -> Dict[str, Any]:
    if not name:
        # 概览：分组显示已配置/未配置
        items = registry.list()
        lines: List[str] = []
        for i in items:
            desc = i.describe()
            lines.append(_describe_line(desc))
            if desc["missing"]:
                lines.append(f"    缺失: {', '.join(desc['missing'])}")
        return _result(True, "status", f"{len(items)} 个集成状态", lines=lines)

    integration = registry.get(name)
    if integration is None:
        return _result(False, "status", f"未知集成: {name}")
    desc = integration.describe()
    lines = [
        f"{desc['emoji']} {desc['display_name']} [{desc['name']}]",
        f"类别: {desc['category']}",
        f"描述: {desc['description']}",
        f"能力: {', '.join(desc['capabilities']) or '-'}",
        f"已配置: {'是' if desc['configured'] else '否'}",
    ]
    if desc["missing"]:
        lines.append(f"缺失: {', '.join(desc['missing'])}")
    if desc["docs_url"]:
        lines.append(f"文档: {desc['docs_url']}")
    return _result(True, "status", f"{desc['display_name']} 状态", lines=lines, integration=desc)


def _cmd_test(registry: IntegrationRegistry, name: Optional[str]) -> Dict[str, Any]:
    if not name:
        return _result(False, "test", "test 需要集成名，如: /integrations test discord")
    integration = registry.get(name)
    if integration is None:
        return _result(False, "test", f"未知集成: {name}")
    result = integration.health_check()
    if result.ok:
        return _result(True, "test", f"✓ {name} 健康检查通过", lines=[], result=result.to_dict())
    return _result(
        False, "test", f"✗ {name}: {result.error}", lines=[], result=result.to_dict()
    )


def _cmd_help() -> Dict[str, Any]:
    lines = [
        "/integrations list [category]   列出集成（可按类别过滤）",
        "/integrations status [name]     查看集成配置状态",
        "/integrations test <name>       对某集成做健康检查",
        "/integrations help              显示本帮助",
    ]
    return _result(True, "help", "集成管理命令", lines=lines)


__all__ = ["handle_integrations_command"]
