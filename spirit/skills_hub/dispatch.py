"""传输无关的 /skill 命令分发层 — Spirit Agent（Phase 4.6）。

对标 Hermes ``agent/skill_commands.py`` 的会话内技能管理 + ``tools/skills_hub.py`` 的
市场操作，但像 :mod:`spirit.goals.commands` / :mod:`spirit.moa.commands` 那样把「解析 +
派发 + 结果」从具体 UI（CLI 的 Rich console、桌宠的 WebSocket）里剥离出来：这里只吃一个
``agent`` 和一段 ``arg`` 字符串，吐一个结构化 dict。CLI 与 ws_server 各自把 dict 渲染成
自己的形式。

这样做有两个好处（对齐 goals/moa 命令层的设计动机）：

1. **可测试**——命令解析 / 市场安装 / 扫描无需真实终端或 LLM 即可单测（对齐用户
   「和 Hermes 一样的任务检查测试用例」的要求）；市场流走 :mod:`spirit.skills_hub.hub`
   的 fetch seam，离线可驱动。
2. **单一事实源**——list/reload/browse/install/uninstall/scan/bundles/info/audit 的语义
   只在这里定义一次，CLI 和桌宠不会各写一套而漂移。

命令形式::

    /skill                     列出可用技能（slash 命令面）
    /skill list|ls             同上
    /skill reload              重扫技能 + 捆绑目录，报告增删
    /skill browse [query]      浏览 / 搜索市场索引（离线走缓存）
    /skill install <id>        从市场获取并安装（隔离 → 扫描 → 安装 / 留隔离）
    /skill uninstall <name>    卸载一个市场安装的技能
    /skill scan [name]         安全扫描某技能（缺省扫全部已安装）
    /skill bundles             列出捆绑包
    /skill info <name>         技能详情（元数据 + 使用统计 + 来源）
    /skill audit               显示市场审计日志（最近若干条）
    /skill help                用法

此外，本模块导出 :data:`RESERVED_SKILL_COMMANDS`（核心 slash 命令名集合，技能自动注册
须避让）与 :func:`resolve_slash_skill_or_bundle`（把 ``/<skill-name>`` / ``/<bundle>``
解析为注入对话的调用消息，供 CLI / 网关在固定命令之外动态派发）。

返回 dict 的约定键（对齐 :mod:`spirit.moa.commands`）::

    ok          bool        命令是否成功执行（未知子命令 / 参数错误 → False）
    action      str         归一化后的动作名（list/reload/browse/install/...）
    message     str         一行主消息（给用户看）
    lines       List[str]   多行输出（技能清单、扫描报告等）
    status_line str         当前技能中心状态一行（便于 UI 常驻显示）
    response    str|None    保留（技能命令不产 LLM 响应，恒为 None）
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from spirit.skills_hub import bundles, commands, discovery, hub, provenance, usage

logger = logging.getLogger(__name__)


# 核心 slash 命令名（不含前导 ``/``）。技能 / 捆绑自动注册生成的 ``/<slug>`` 若与此集合
# 冲突，则跳过自动注册（仍可经 ``/skill <name>`` 或 ``/<name>`` 显式调用解析）——避免
# 一个名为 "help" 的技能劫持 ``/help``。对标 Hermes skill_commands 的保留名避让。
RESERVED_SKILL_COMMANDS = frozenset({
    "help", "new", "model", "status", "tools", "sessions",
    "goal", "subgoal", "moa", "skill", "skills",
    "quit", "exit", "q",
})


def _result(
    ok: bool,
    action: str,
    message: str = "",
    *,
    lines: Optional[List[str]] = None,
    status_line: str = "",
    response: Optional[str] = None,
) -> Dict[str, Any]:
    """构造统一的结果 dict（对齐 :func:`spirit.moa.commands._result`）。"""
    return {
        "ok": ok,
        "action": action,
        "message": message,
        "lines": lines or [],
        "status_line": status_line,
        "response": response,
    }


def skill_usage() -> str:
    """``/skill`` 的用法提示。"""
    return (
        "Usage: /skill list | reload | browse [query] | install <id> | "
        "uninstall <name> | scan [name] | bundles | info <name> | audit  ·  "
        "调用技能用 /<skill-name> [指令]，捆绑用 /<bundle-name> [指令]"
    )


def _status_line() -> str:
    """技能中心状态一行（可用技能数 + 捆绑数 + 已安装市场技能数）。"""
    try:
        n_skills = len(commands.get_skill_commands())
    except Exception:
        n_skills = 0
    try:
        n_bundles = len(bundles.list_bundles())
    except Exception:
        n_bundles = 0
    try:
        n_installed = len(hub.HubLockFile().list_installed())
    except Exception:
        n_installed = 0
    return f"技能中心: {n_skills} 个可用技能 · {n_bundles} 个捆绑 · {n_installed} 个市场安装"


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------

def _skill_list() -> Dict[str, Any]:
    """列出 slash 可用的技能（已施加平台 / 环境 / 禁用门）。"""
    cmds = commands.get_skill_commands()
    if not cmds:
        return _result(
            True, "list", "没有可用技能。",
            lines=[f"  把技能放进 {discovery.get_all_skills_dirs()[0] if discovery.get_all_skills_dirs() else '~/.spirit/skills'} 后 /skill reload。"],
            status_line=_status_line(),
        )
    lines: List[str] = []
    for cmd_key in sorted(cmds):
        info = cmds[cmd_key]
        desc = (info.get("description") or "").strip()
        if len(desc) > 72:
            desc = desc[:72] + "…"
        lines.append(f"  {cmd_key}  —  {desc}" if desc else f"  {cmd_key}")
    lines.append("")
    lines.append("  调用: /<skill-name> [你的指令] · 详情: /skill info <name>")
    return _result(True, "list", f"可用技能（{len(cmds)}）：", lines=lines, status_line=_status_line())


def _skill_reload() -> Dict[str, Any]:
    """重扫技能 + 捆绑目录，报告增删。"""
    skills_diff = commands.reload_skills()
    bundles_diff = bundles.reload_bundles()
    lines: List[str] = []
    added = [a["name"] for a in skills_diff.get("added", [])]
    removed = [r["name"] for r in skills_diff.get("removed", [])]
    if added:
        lines.append(f"  + 技能新增: {', '.join(added)}")
    if removed:
        lines.append(f"  - 技能移除: {', '.join(removed)}")
    b_added = [a["name"] for a in bundles_diff.get("added", [])]
    b_removed = [r["name"] for r in bundles_diff.get("removed", [])]
    if b_added:
        lines.append(f"  + 捆绑新增: {', '.join(b_added)}")
    if b_removed:
        lines.append(f"  - 捆绑移除: {', '.join(b_removed)}")
    if not lines:
        lines.append("  （无变化）")
    total = skills_diff.get("total", 0)
    return _result(
        True, "reload",
        f"✓ 已重扫：{total} 个技能 · {bundles_diff.get('total', 0)} 个捆绑。",
        lines=lines, status_line=_status_line(),
    )


def _skill_browse(query: str) -> Dict[str, Any]:
    """浏览 / 搜索市场索引（离线走缓存；无源时提示）。"""
    try:
        entries = hub.search_skills(query) if query else hub.browse_index()
    except hub.HubError as exc:
        return _result(False, "browse", f"浏览失败：{exc}", status_line=_status_line())
    if not entries:
        hint = (
            "  市场索引为空（离线或未注册源）。"
            "测试 / 离线环境可经 hub.set_fetcher / hub.register_index_source 注入。"
        )
        return _result(True, "browse", "没有可浏览的市场条目。", lines=[hint], status_line=_status_line())
    lines: List[str] = []
    for entry in entries[:50]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("identifier") or "?")
        desc = str(entry.get("description") or "").strip()
        if len(desc) > 64:
            desc = desc[:64] + "…"
        lines.append(f"  {name}  —  {desc}" if desc else f"  {name}")
    title = f"市场搜索结果（{len(entries)}）：" if query else f"市场索引（{len(entries)}）："
    lines.append("")
    lines.append("  安装: /skill install <name>")
    return _result(True, "browse", title, lines=lines, status_line=_status_line())


def _skill_install(identifier: str) -> Dict[str, Any]:
    """从市场获取并安装一个技能。"""
    ident = (identifier or "").strip()
    if not ident:
        return _result(False, "install", "用法: /skill install <identifier>", status_line=_status_line())
    ok, message = hub.install_skill(ident)
    # 安装成功后失效技能命令缓存，使新技能立即可经 /<name> 调用。
    if ok:
        commands.invalidate_skill_commands()
    return _result(ok, "install", message, status_line=_status_line())


def _skill_uninstall(name: str) -> Dict[str, Any]:
    """卸载一个市场安装的技能。"""
    target = (name or "").strip()
    if not target:
        return _result(False, "uninstall", "用法: /skill uninstall <name>", status_line=_status_line())
    ok, message = hub.uninstall_skill(target)
    if ok:
        commands.invalidate_skill_commands()
    return _result(ok, "uninstall", message, status_line=_status_line())


def _skill_scan(name: str) -> Dict[str, Any]:
    """安全扫描某技能（缺省扫全部已安装）。"""
    from spirit.tools.skills import format_scan_report, scan_skill

    target = (name or "").strip()
    if target:
        skill_dir = commands._resolve_skill_dir(discovery.normalize_skill_lookup_name(target))
        if skill_dir is None:
            return _result(False, "scan", f"技能 '{target}' 未找到。", status_line=_status_line())
        result = scan_skill(skill_dir)
        return _result(
            result.verdict != "dangerous", "scan",
            f"扫描 '{target}'：{result.verdict.upper()}",
            lines=[f"  {ln}" for ln in format_scan_report(result).splitlines()],
            status_line=_status_line(),
        )

    # 全量扫描
    lines: List[str] = []
    dangerous = 0
    metas = discovery.find_all_skills()
    for meta in metas[:50]:
        if not meta.path:
            continue
        from pathlib import Path

        result = scan_skill(Path(meta.path))
        flag = "⚠" if result.verdict == "dangerous" else ("△" if result.verdict == "caution" else "✓")
        if result.verdict == "dangerous":
            dangerous += 1
        lines.append(f"  {flag} {meta.name}: {result.verdict}（{len(result.findings)} 处发现）")
    if not lines:
        return _result(True, "scan", "没有可扫描的技能。", status_line=_status_line())
    return _result(
        dangerous == 0, "scan",
        f"已扫描 {len(lines)} 个技能（{dangerous} 个危险）。",
        lines=lines, status_line=_status_line(),
    )


def _skill_bundles_list() -> Dict[str, Any]:
    """列出捆绑包。"""
    all_bundles = bundles.list_bundles()
    if not all_bundles:
        return _result(
            True, "bundles", "没有捆绑包。",
            lines=[f"  把 YAML 放进 {discovery.get_all_skills_dirs()[0].parent / 'skill-bundles' if discovery.get_all_skills_dirs() else '~/.spirit/skill-bundles'} 即注册。"],
            status_line=_status_line(),
        )
    lines: List[str] = []
    for b in all_bundles:
        skills = ", ".join(b.get("skills", []))
        desc = (b.get("description") or "").strip()
        head = f"  /{b['slug']}  ({len(b.get('skills', []))} 技能: {skills})"
        lines.append(head)
        if desc:
            lines.append(f"      {desc}")
    return _result(True, "bundles", f"捆绑包（{len(all_bundles)}）：", lines=lines, status_line=_status_line())


def _skill_info(name: str) -> Dict[str, Any]:
    """技能详情：元数据 + 使用统计 + 来源。"""
    target = (name or "").strip()
    if not target:
        return _result(False, "info", "用法: /skill info <name>", status_line=_status_line())
    skill_dir = commands._resolve_skill_dir(discovery.normalize_skill_lookup_name(target))
    if skill_dir is None:
        return _result(False, "info", f"技能 '{target}' 未找到。", status_line=_status_line())

    from pathlib import Path

    meta = discovery.parse_skill_md(Path(skill_dir) / "SKILL.md")
    display_name = meta.name if meta else Path(skill_dir).name
    lines: List[str] = [f"  名称: {display_name}", f"  目录: {skill_dir}"]
    if meta:
        if meta.description:
            lines.append(f"  描述: {meta.description}")
        if meta.version:
            lines.append(f"  版本: {meta.version}")
        if meta.tags:
            lines.append(f"  标签: {', '.join(meta.tags)}")
        if meta.platforms:
            lines.append(f"  平台: {', '.join(meta.platforms)}")
        if meta.environments:
            lines.append(f"  环境: {', '.join(meta.environments)}")

    rec = usage.get_usage(display_name)
    if rec:
        lines.append(
            f"  使用: {rec.get('use_count', 0)} 次调用 · {rec.get('view_count', 0)} 次查看"
            f"（最近 {rec.get('last_used_at') or rec.get('last_viewed_at') or '—'}）"
        )
    else:
        lines.append("  使用: 尚无记录")

    inst = provenance.get_install(display_name)
    if inst:
        lines.append(
            f"  来源: {inst.get('source') or '?'} / {inst.get('identifier') or '?'}"
            f"（信任级 {inst.get('trust_level') or '?'}）"
        )
    else:
        lines.append("  来源: 本地 / 内置（非市场安装）")
    return _result(True, "info", f"技能 '{display_name}'：", lines=lines, status_line=_status_line())


def _skill_audit() -> Dict[str, Any]:
    """显示市场审计日志（最近 20 条）。"""
    records = hub.read_audit_log()
    if not records:
        return _result(True, "audit", "审计日志为空。", status_line=_status_line())
    lines: List[str] = []
    for rec in records[-20:]:
        ts = str(rec.get("ts") or "")
        action = str(rec.get("action") or "")
        skill = str(rec.get("skill") or "")
        verdict = str(rec.get("verdict") or "")
        tail = f" [{verdict}]" if verdict else ""
        lines.append(f"  {ts}  {action:<10} {skill}{tail}")
    return _result(True, "audit", f"市场审计日志（最近 {min(len(records), 20)} / {len(records)} 条）：",
                   lines=lines, status_line=_status_line())


# ---------------------------------------------------------------------------
# 主派发
# ---------------------------------------------------------------------------

def handle_skill_command(agent, arg: str) -> Dict[str, Any]:
    """派发 ``/skill`` 子命令（对齐 :func:`spirit.moa.commands.handle_moa_command`）。

    Args:
        agent: SpiritAgent（技能中心命令本身不依赖 agent 状态，仅为与其它命令层签名
            一致而接收；``None`` 也可用）。
        arg: ``/skill`` 之后的整段参数（可为空）。

    Returns:
        结构化结果 dict（见模块 docstring）。
    """
    arg = (arg or "").strip()
    lower = arg.lower()

    # 裸 /skill 或 /skill list|ls → 列出可用技能
    if not arg or lower in {"list", "ls"}:
        return _skill_list()

    tokens = arg.split(None, 1)
    verb = tokens[0].lower()
    rest = tokens[1].strip() if len(tokens) > 1 else ""

    if verb == "reload":
        return _skill_reload()
    if verb == "browse":
        return _skill_browse(rest)
    if verb == "install":
        return _skill_install(rest)
    if verb == "uninstall":
        return _skill_uninstall(rest)
    if verb == "scan":
        return _skill_scan(rest)
    if verb == "bundles":
        return _skill_bundles_list()
    if verb == "info":
        return _skill_info(rest)
    if verb == "audit":
        return _skill_audit()
    if verb in {"help", "-h", "--help"}:
        return _result(True, "help", skill_usage(), status_line=_status_line())

    return _result(False, "unknown", f"未知子命令: {verb}", lines=[skill_usage()], status_line=_status_line())


# ---------------------------------------------------------------------------
# 动态 slash 技能 / 捆绑解析（供 CLI / 网关在固定命令之外派发）
# ---------------------------------------------------------------------------

def resolve_slash_skill_or_bundle(command: str, rest: str = "") -> Optional[str]:
    """把 ``/<command> <rest>`` 解析为注入对话的技能 / 捆绑调用消息（解析不到 None）。

    **捆绑优先于技能**（对齐 :mod:`spirit.skills_hub.bundles` 的冲突解决约定）：名为
    ``research`` 的捆绑与同名技能并存时，``/research`` 指捆绑。

    技能侧支持**堆叠调用** ``/skill-a /skill-b do XYZ``：首个 token 解析为技能后，
    ``rest`` 里前导的、能解析为已安装技能的 ``/token`` 被一并消费（至多
    ``commands._MAX_STACKED_SKILLS`` 个），合并进同一条消息。

    Args:
        command: slash 命令名（不含前导 ``/``），如 ``"my-skill"``。
        rest: 命令之后的整段文本（用户指令，或更多堆叠的 ``/skill`` token）。

    Returns:
        完整的调用消息字符串（可直接喂给 ``agent.run_conversation``），解析不到返回 None。
    """
    cmd = (command or "").strip().lstrip("/")
    if not cmd:
        return None
    remainder = rest or ""

    # ① 捆绑优先
    bundle_key = bundles.resolve_bundle_command_key(cmd)
    if bundle_key is not None:
        built = bundles.build_bundle_invocation_message(bundle_key, user_instruction=remainder.strip())
        if built is None:
            return None
        message, _loaded, _missing = built
        return message

    # ② 技能（含堆叠）
    skill_key = commands.resolve_skill_command_key(cmd)
    if skill_key is None:
        return None
    extra_keys, remaining = commands.split_stacked_skill_commands(remainder)
    all_keys = [skill_key, *extra_keys]
    if len(all_keys) > 1:
        built = commands.build_stacked_skill_invocation_message(all_keys, user_instruction=remaining)
        if built is None:
            return None
        message, _loaded, _missing = built
        return message
    return commands.build_skill_invocation_message(skill_key, user_instruction=remaining)


__all__ = [
    "RESERVED_SKILL_COMMANDS",
    "handle_skill_command",
    "skill_usage",
    "resolve_slash_skill_or_bundle",
]
