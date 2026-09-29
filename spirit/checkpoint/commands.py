"""``/checkpoint`` 命令 —— 传输无关分发。

对齐 :mod:`spirit.profile.commands` 与 :mod:`spirit.goals.commands` 范式：把命令解析
与业务逻辑集中在此，返回结构化 dict（``ok/action/message/lines``），CLI 与 ws_server
各自渲染。不含任何传输/IO 细节，便于离线单测。

用法::

    /checkpoint snapshot <file...> [--desc "..."] [--scope work]
    /checkpoint list [--scope work] [--limit N]
    /checkpoint restore <id>
    /checkpoint diff <id>
    /checkpoint delete <id>
    /checkpoint cleanup [--keep N] [--scope work]
    /checkpoint stats
    /checkpoint latest [--scope work]
"""

import logging
from typing import Any, Dict, List, Optional

from spirit.checkpoint.manager import CheckpointManager

logger = logging.getLogger(__name__)

_VERB_ALIASES = {
    "snapshot": "snapshot", "create": "snapshot", "save": "snapshot", "s": "snapshot",
    "list": "list", "ls": "list", "l": "list",
    "restore": "restore", "rollback": "restore", "revert": "restore", "r": "restore",
    "diff": "diff", "d": "diff",
    "delete": "delete", "rm": "delete", "remove": "delete",
    "cleanup": "cleanup", "clean": "cleanup", "prune": "cleanup",
    "stats": "stats", "status": "stats",
    "latest": "latest", "last": "latest",
}


def _result(
    ok: bool,
    action: str,
    message: str = "",
    *,
    lines: Optional[List[str]] = None,
    **extra: Any,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "ok": ok,
        "action": action,
        "message": message,
        "lines": lines or [],
    }
    out.update(extra)
    return out


def _parse_flags(tokens: List[str]) -> tuple:
    """分离位置参数与旗标。``--desc``/``--scope`` 吃值；``--keep``/``--limit`` 吃整数。"""
    positional: List[str] = []
    flags: Dict[str, Any] = {}
    value_flags = {"--desc", "--description", "--scope"}
    int_flags = {"--keep", "--limit"}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in value_flags:
            if i + 1 < len(tokens):
                flags[tok.lstrip("-")] = tokens[i + 1]
                i += 2
                continue
            i += 1
            continue
        if tok in int_flags:
            if i + 1 < len(tokens):
                try:
                    flags[tok.lstrip("-")] = int(tokens[i + 1])
                except ValueError:
                    flags[tok.lstrip("-")] = None
                i += 2
                continue
            i += 1
            continue
        if tok.startswith("--"):
            flags[tok.lstrip("-")] = True
            i += 1
            continue
        positional.append(tok)
        i += 1
    # 归一化别名
    if "description" in flags and "desc" not in flags:
        flags["desc"] = flags.pop("description")
    return positional, flags


def _tokenize(arg: str) -> List[str]:
    import shlex
    try:
        return shlex.split(arg or "")
    except ValueError:
        return (arg or "").split()


def handle_checkpoint_command(
    arg: str, manager: Optional[CheckpointManager] = None
) -> Dict[str, Any]:
    """解析并执行 ``/checkpoint`` 命令，返回结构化结果 dict。"""
    manager = manager or CheckpointManager()
    tokens = _tokenize(arg)
    if not tokens:
        return _result(False, "help", "用法: /checkpoint <snapshot|list|restore|diff|delete|cleanup|stats|latest> ...")

    verb_raw = tokens[0].lower()
    verb = _VERB_ALIASES.get(verb_raw)
    if not verb:
        return _result(False, "unknown", f"未知子命令: {verb_raw}")

    positional, flags = _parse_flags(tokens[1:])

    try:
        if verb == "snapshot":
            return _cmd_snapshot(manager, positional, flags)
        if verb == "list":
            return _cmd_list(manager, flags)
        if verb == "restore":
            return _cmd_restore(manager, positional)
        if verb == "diff":
            return _cmd_diff(manager, positional)
        if verb == "delete":
            return _cmd_delete(manager, positional)
        if verb == "cleanup":
            return _cmd_cleanup(manager, flags)
        if verb == "stats":
            return _cmd_stats(manager)
        if verb == "latest":
            return _cmd_latest(manager, flags)
    except Exception as exc:  # 防御性：命令层绝不抛
        logger.warning("checkpoint 命令失败: %s", exc)
        return _result(False, verb, f"执行失败: {exc}")

    return _result(False, "unknown", f"未处理的子命令: {verb}")


# --------------------------------------------------------------------------
# 子命令实现
# --------------------------------------------------------------------------

def _cmd_snapshot(manager, positional, flags) -> Dict[str, Any]:
    if not positional:
        return _result(False, "snapshot", "snapshot 需要至少一个文件路径")
    desc = flags.get("desc", "") or ""
    scope = flags.get("scope", "global") or "global"
    res = manager.create(positional, description=desc, scope=scope)
    if not res.get("success"):
        return _result(False, "snapshot", res.get("error", "快照失败"))
    cp = res["checkpoint"]
    lines = [f"id: {cp['id']}", f"文件数: {cp['file_count']}", f"作用域: {cp['scope']}"]
    if desc:
        lines.append(f"描述: {desc}")
    return _result(True, "snapshot", f"已创建快照 {cp['id']}", lines=lines, checkpoint=cp)


def _cmd_list(manager, flags) -> Dict[str, Any]:
    scope = flags.get("scope")
    limit = flags.get("limit", 20) or 20
    cps = manager.list_checkpoints(scope=scope, limit=limit)
    if not cps:
        return _result(True, "list", "暂无快照", lines=[])
    lines = []
    for cp in cps:
        desc = cp.get("description") or ""
        lines.append(
            f"{cp['id']}  [{cp.get('scope', 'global')}]  "
            f"{cp.get('file_count', 0)} 文件  {desc}"
        )
    return _result(True, "list", f"共 {len(cps)} 个快照", lines=lines, checkpoints=cps)


def _cmd_restore(manager, positional) -> Dict[str, Any]:
    if not positional:
        return _result(False, "restore", "restore 需要快照 id")
    res = manager.restore(positional[0])
    if not res.get("success"):
        return _result(False, "restore", res.get("error", "回滚失败"))
    lines = [f"已恢复 {res['count']} 个文件"]
    lines.extend(f"  ✓ {p}" for p in res.get("restored_files", []))
    if res.get("skipped_files"):
        lines.extend(f"  ⚠ 跳过 {p}" for p in res["skipped_files"])
    return _result(True, "restore", f"已回滚到 {positional[0]}", lines=lines, **{
        "restored_files": res.get("restored_files", []),
    })


def _cmd_diff(manager, positional) -> Dict[str, Any]:
    if not positional:
        return _result(False, "diff", "diff 需要快照 id")
    res = manager.diff(positional[0])
    if not res.get("success"):
        return _result(False, "diff", res.get("error", "diff 失败"))
    lines = []
    if res.get("modified"):
        lines.append(f"已修改 ({len(res['modified'])}):")
        lines.extend(f"  ~ {p}" for p in res["modified"])
    if res.get("missing"):
        lines.append(f"已丢失 ({len(res['missing'])}):")
        lines.extend(f"  ✗ {p}" for p in res["missing"])
    lines.append(f"未变: {len(res.get('unchanged', []))}")
    msg = "有变更" if res.get("changed") else "无变更"
    return _result(True, "diff", msg, lines=lines, diff=res)


def _cmd_delete(manager, positional) -> Dict[str, Any]:
    if not positional:
        return _result(False, "delete", "delete 需要快照 id")
    res = manager.delete(positional[0])
    if not res.get("success"):
        return _result(False, "delete", res.get("error", "删除失败"))
    return _result(True, "delete", f"已删除快照 {positional[0]}")


def _cmd_cleanup(manager, flags) -> Dict[str, Any]:
    keep = flags.get("keep", 20)
    if keep is None:
        keep = 20
    scope = flags.get("scope")
    res = manager.cleanup(keep=keep, scope=scope)
    return _result(
        True, "cleanup",
        f"已清理 {res['removed']} 个旧快照，保留 {res['kept']} 个",
    )


def _cmd_stats(manager) -> Dict[str, Any]:
    stats = manager.store_stats()
    lines = [
        f"快照数: {stats['checkpoint_count']}",
        f"blob 数: {stats['blob_count']}",
        f"总字节: {stats['total_bytes']}",
    ]
    return _result(True, "stats", "检查点存储统计", lines=lines, stats=stats)


def _cmd_latest(manager, flags) -> Dict[str, Any]:
    scope = flags.get("scope")
    cp = manager.latest(scope=scope)
    if not cp:
        return _result(True, "latest", "暂无快照", lines=[])
    lines = [f"id: {cp['id']}", f"文件数: {cp.get('file_count', 0)}"]
    if cp.get("description"):
        lines.append(f"描述: {cp['description']}")
    return _result(True, "latest", f"最新快照 {cp['id']}", lines=lines, checkpoint=cp)


__all__ = ["handle_checkpoint_command"]
