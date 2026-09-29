"""传输无关的 /profile 命令分发层 — Spirit Agent（Phase 4.2）。

对标 Hermes ``hermes_cli/profiles.py`` 的 CLI 子命令（create/use/delete/rename/
list/describe），但把"解析 + 派发 + 结果"从具体 UI（CLI 的 Rich console、桌宠的
WebSocket）里剥离出来：这里只吃一个 ``agent``（describe 需要它的 LLM）和一段 ``arg``
字符串，吐一个结构化 dict。CLI 与 ws_server 各自把 dict 渲染成自己的形式。

对齐 :func:`spirit.goals.commands.handle_goal_command` 的两个好处：
1. **可测试**——命令解析/状态流转无需真实终端即可单测。
2. **单一事实源**——list/create/use/delete/rename/describe 的语义只定义一次。

返回 dict 的约定键::

    ok          bool        命令是否成功执行（未知子命令/参数错误 → False）
    action      str         归一化后的动作名（list/create/use/delete/rename/describe/current）
    message     str         一行主消息（给用户看）
    lines       List[str]   多行输出（profile 列表、详情等）
    active      str         操作后的活跃 profile 名（便于 UI 常驻显示）
    restart_hint bool       use 后是否建议重启子进程以彻底激活（进程内已即时切换）

``use`` 会：① 写粘性活跃 profile（``set_active_profile``）；② 进程内即时切换
``config.SPIRIT_HOME``（``apply_active_profile``）。但 import 时已缓存旧 HOME 的
组件（如已建立的 client）不会自动感知，故给出 ``restart_hint`` 提示彻底激活需重启。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from spirit.profile import manager as _manager

logger = logging.getLogger(__name__)


def _result(
    ok: bool,
    action: str,
    message: str = "",
    *,
    lines: Optional[List[str]] = None,
    active: str = "",
    restart_hint: bool = False,
) -> Dict[str, Any]:
    """构造统一的结果 dict。"""
    return {
        "ok": ok,
        "action": action,
        "message": message,
        "lines": lines or [],
        "active": active or _safe_active_name(),
        "restart_hint": restart_hint,
    }


def _safe_active_name() -> str:
    """读当前活跃 profile 名，任何异常降级为空串（绝不因命令层探测拖垮渲染）。"""
    try:
        return _manager.get_active_profile()
    except Exception:  # pragma: no cover - 防御
        return ""


def _format_profile_line(p: _manager.ProfileInfo) -> str:
    """把一个 ProfileInfo 渲染成一行（list 用）。"""
    marks = []
    if p.is_default:
        marks.append("default")
    if p.is_active:
        marks.append("active")
    mark = f" [{', '.join(marks)}]" if marks else ""
    bits = [f"{p.name}{mark}"]
    if p.model:
        bits.append(f"model={p.model}")
    if p.provider:
        bits.append(f"provider={p.provider}")
    bits.append(f"skills={p.skill_count}")
    if p.has_env:
        bits.append("env✓")
    line = "  " + " · ".join(bits)
    if p.description:
        auto = "（自动）" if p.description_auto else ""
        line += f"\n      {p.description}{auto}"
    return line


# ──────────────────────────────────────────────────────────────────────
# /profile 主入口
# ──────────────────────────────────────────────────────────────────────

def handle_profile_command(agent, arg: str) -> Dict[str, Any]:
    """派发 ``/profile`` 子命令。

    形式（对齐 Hermes profile CLI）::

        /profile                       列出所有 profile（等价 list）
        /profile list                  同上
        /profile current               显示当前活跃 profile
        /profile create <name> [--from <src>] [--clone-all] [--clone-config] [--desc "..."]
        /profile use <name>            设为活跃（进程内即时切换 + 粘性）
        /profile delete <name> [--yes] 删除
        /profile rename <old> <new>    重命名
        /profile describe <name> [--overwrite]  用辅助 LLM 自动生成描述
        /profile info <name>           单个 profile 详情

    Args:
        agent: SpiritAgent（仅 ``describe`` 需要它的 LLM；其余子命令可传 None）。
        arg: ``/profile`` 之后的整段参数（可为空）。

    Returns:
        结构化结果 dict（见模块 docstring）。
    """
    arg = (arg or "").strip()
    if not arg:
        return _cmd_list()

    tokens = arg.split()
    verb = tokens[0].lower()
    rest = tokens[1:]

    if verb in ("list", "ls"):
        return _cmd_list()
    if verb in ("current", "whoami"):
        return _cmd_current()
    if verb in ("create", "new", "add"):
        return _cmd_create(rest)
    if verb in ("use", "switch", "activate"):
        return _cmd_use(rest)
    if verb in ("delete", "rm", "remove"):
        return _cmd_delete(rest)
    if verb in ("rename", "mv"):
        return _cmd_rename(rest)
    if verb in ("describe", "desc"):
        return _cmd_describe(agent, rest)
    if verb in ("info", "show"):
        return _cmd_info(rest)

    return _result(False, "unknown", f"未知子命令: {verb}。用 /profile list 查看可用 profile。")


# ──────────────────────────────────────────────────────────────────────
# 子命令实现
# ──────────────────────────────────────────────────────────────────────

def _cmd_list() -> Dict[str, Any]:
    try:
        profiles = _manager.list_profiles()
    except Exception as exc:
        return _result(False, "list", f"列举 profile 失败: {exc}")
    if not profiles:
        return _result(True, "list", "尚无 profile。", lines=["  用 /profile create <name> 创建一个。"])
    lines = [_format_profile_line(p) for p in profiles]
    active = _safe_active_name()
    return _result(True, "list", f"共 {len(profiles)} 个 profile（活跃: {active}）：", lines=lines, active=active)


def _cmd_current() -> Dict[str, Any]:
    active = _safe_active_name()
    try:
        home = _manager._paths.default_root() if active == "default" else _manager.get_profile_dir(active)
    except Exception:  # pragma: no cover - 防御
        home = None
    lines = [f"  HOME: {home}"] if home else []
    return _result(True, "current", f"当前活跃 profile: {active}", lines=lines, active=active)


def _parse_flags(rest: List[str]) -> Dict[str, Any]:
    """把位置参数与 ``--flag`` / ``--key value`` 分离。

    返回 ``{"positional": [...], "flags": {name: value_or_True}}``。
    ``--clone-all`` / ``--clone-config`` / ``--overwrite`` / ``--yes`` 为布尔旗标；
    ``--from`` / ``--desc`` 吃后一个 token 作值。
    """
    positional: List[str] = []
    flags: Dict[str, Any] = {}
    _BOOL_FLAGS = {"clone-all", "clone_all", "clone-config", "clone_config", "overwrite", "yes", "y"}
    _VALUE_FLAGS = {"from", "desc", "description"}
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok.startswith("--"):
            key = tok[2:].strip()
            if key in _BOOL_FLAGS:
                flags[key] = True
            elif key in _VALUE_FLAGS:
                if i + 1 < len(rest):
                    flags[key] = rest[i + 1]
                    i += 1
                else:
                    flags[key] = ""
            else:
                flags[key] = True
        else:
            positional.append(tok)
        i += 1
    return {"positional": positional, "flags": flags}


def _cmd_create(rest: List[str]) -> Dict[str, Any]:
    parsed = _parse_flags(rest)
    pos = parsed["positional"]
    flags = parsed["flags"]
    if not pos:
        return _result(False, "create", "用法: /profile create <name> [--from <src>] [--clone-all|--clone-config] [--desc \"...\"]")
    name = pos[0]
    clone_from = flags.get("from")
    clone_all = bool(flags.get("clone-all") or flags.get("clone_all"))
    clone_config = bool(flags.get("clone-config") or flags.get("clone_config"))
    description = flags.get("desc") or flags.get("description")
    try:
        path = _manager.create_profile(
            name,
            clone_from=clone_from,
            clone_all=clone_all,
            clone_config=clone_config,
            description=description,
        )
    except (ValueError, FileExistsError, FileNotFoundError) as exc:
        return _result(False, "create", f"创建失败: {exc}")
    src = f"（克隆自 {clone_from or 'active'}）" if (clone_from or clone_all or clone_config) else ""
    return _result(True, "create", f"✓ 已创建 profile '{_manager.normalize_profile_name(name)}'{src}: {path}")


def _cmd_use(rest: List[str]) -> Dict[str, Any]:
    if not rest:
        return _result(False, "use", "用法: /profile use <name>")
    name = rest[0]
    try:
        canon = _manager.normalize_profile_name(name)
        _manager.validate_profile_name(canon)
        if canon != "default" and not _manager.profile_exists(canon):
            raise FileNotFoundError(f"profile '{canon}' 不存在。先用 /profile create {canon} 创建")
        _manager.set_active_profile(canon)
        from spirit.profile import paths as _paths
        new_home = _paths.apply_active_profile(canon)
    except (ValueError, FileNotFoundError) as exc:
        return _result(False, "use", f"切换失败: {exc}")
    lines = [f"  HOME → {new_home}", "  提示: 已在当前进程即时切换；子进程/重启后按粘性 active 生效。"]
    return _result(True, "use", f"▶ 已切换到 profile '{canon}'", lines=lines, active=canon, restart_hint=True)


def _cmd_delete(rest: List[str]) -> Dict[str, Any]:
    parsed = _parse_flags(rest)
    pos = parsed["positional"]
    if not pos:
        return _result(False, "delete", "用法: /profile delete <name> [--yes]")
    name = pos[0]
    # 命令层不交互；缺 --yes 时要求上层确认。这里对非 default 直接删（对齐 CLI 二次确认交给 UI）。
    try:
        path = _manager.delete_profile(name, yes=bool(parsed["flags"].get("yes")))
    except (ValueError, FileNotFoundError) as exc:
        return _result(False, "delete", f"删除失败: {exc}")
    except OSError as exc:
        return _result(False, "delete", f"删除失败（目录占用？）: {exc}")
    return _result(True, "delete", f"✓ 已删除 profile '{_manager.normalize_profile_name(name)}': {path}")


def _cmd_rename(rest: List[str]) -> Dict[str, Any]:
    if len(rest) < 2:
        return _result(False, "rename", "用法: /profile rename <old> <new>")
    old, new = rest[0], rest[1]
    try:
        path = _manager.rename_profile(old, new)
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        return _result(False, "rename", f"重命名失败: {exc}")
    except OSError as exc:
        return _result(False, "rename", f"重命名失败: {exc}")
    return _result(
        True, "rename",
        f"✓ 已重命名 '{_manager.normalize_profile_name(old)}' → '{_manager.normalize_profile_name(new)}': {path}",
    )


def _cmd_info(rest: List[str]) -> Dict[str, Any]:
    if not rest:
        return _result(False, "info", "用法: /profile info <name>")
    name = rest[0]
    try:
        canon = _manager.normalize_profile_name(name)
        if not _manager.profile_exists(canon):
            return _result(False, "info", f"profile '{canon}' 不存在。")
        path = _manager.get_profile_dir(canon)
        model, provider = _manager._read_config_model(path)
        meta = _manager.read_profile_meta(path)
        skills = _manager._count_skills(path)
    except Exception as exc:  # pragma: no cover - 防御
        return _result(False, "info", f"读取失败: {exc}")
    lines = [
        f"  路径: {path}",
        f"  模型: {model or '(未设)'}" + (f" · provider={provider}" if provider else ""),
        f"  技能数: {skills}",
        f"  .env: {'有' if (path / '.env').exists() else '无'}",
        f"  描述: {meta.get('description') or '(无)'}" + ("（自动）" if meta.get("description_auto") else ""),
    ]
    return _result(True, "info", f"profile '{canon}'：", lines=lines)


def _cmd_describe(agent, rest: List[str]) -> Dict[str, Any]:
    parsed = _parse_flags(rest)
    pos = parsed["positional"]
    if not pos:
        return _result(False, "describe", "用法: /profile describe <name> [--overwrite]")
    name = pos[0]
    overwrite = bool(parsed["flags"].get("overwrite"))

    # 从 agent 构造注入式 llm_caller（复用 goals.judge.build_agent_llm_caller）。
    llm_caller = None
    if agent is not None:
        try:
            from spirit.goals.judge import build_agent_llm_caller
            llm_caller = build_agent_llm_caller(agent)
        except Exception as exc:  # pragma: no cover - 防御
            logger.debug("describe: 构造 llm_caller 失败: %s", exc)
            llm_caller = None

    try:
        from spirit.profile.describer import describe_profile
        outcome = describe_profile(name, llm_caller=llm_caller, overwrite=overwrite)
    except Exception as exc:  # pragma: no cover - describe_profile 自身不抛，这是双保险
        return _result(False, "describe", f"描述失败: {exc}")

    if not outcome.ok:
        return _result(False, "describe", f"✗ {outcome.profile_name}: {outcome.reason}")
    return _result(
        True, "describe",
        f"✓ 已为 '{outcome.profile_name}' 生成描述（自动，可手改确认）：",
        lines=[f"  {outcome.description}"],
    )


__all__ = [
    "handle_profile_command",
]
