"""文件快照/回滚工具 — checkpoint（自动保存 + 一键回滚）。

**本文件现为薄包装**：真实实现已提升到 :mod:`spirit.checkpoint`（内容寻址 blob 去重
store + SPIRIT_HOME 感知 + 可测试）。此处仅保留工具注册与 schema，并委托给
:func:`spirit.checkpoint.get_manager`。

相比旧实现的改进（见 spirit/checkpoint/manager.py 文档）：
- SPIRIT_HOME 感知（按调用解析，可 monkeypatch 测试）。
- 无导入期副作用（不再在 import 时建目录 / 建全局实例）。
- 内容寻址去重，避免旧实现按 basename 平铺导致的同名冲突。
- 新增 diff / cleanup / stats 动作。
"""

import json
import logging
from typing import List

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)


def _manager():
    """惰性获取全局 CheckpointManager（委托到 spirit.checkpoint）。"""
    from spirit.checkpoint import get_manager
    return get_manager()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

CHECKPOINT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "checkpoint",
        "description": (
            "文件快照管理 — 修改前保存安全副本，支持一键回滚。\n\n"
            "用法：\n"
            "- action=snapshot: 为指定文件创建快照\n"
            "- action=list: 列出所有快照\n"
            "- action=restore: 回滚到指定快照\n"
            "- action=diff: 对比快照与文件当前状态\n"
            "- action=cleanup: 清理旧快照（保留最近 keep 个）\n"
            "- action=stats: 查看快照存储统计\n\n"
            "在进行大规模修改前使用 snapshot，出错时可以 restore。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "操作: snapshot / list / restore / diff / cleanup / stats",
                    "enum": ["snapshot", "list", "restore", "diff", "cleanup", "stats"],
                },
                "files": {
                    "type": "array",
                    "description": "要快照的文件列表（snapshot 时必填）",
                    "items": {"type": "string"},
                },
                "checkpoint_id": {
                    "type": "string",
                    "description": "快照 ID（restore / diff 时必填）",
                },
                "description": {
                    "type": "string",
                    "description": "快照描述",
                },
                "scope": {
                    "type": "string",
                    "description": "作用域标签（如工作目录），默认 global",
                },
                "keep": {
                    "type": "integer",
                    "description": "cleanup 时保留的最近快照数，默认 20",
                },
            },
            "required": ["action"],
        },
    },
}


def _checkpoint_impl(
    action: str = "list",
    files: List[str] = None,
    checkpoint_id: str = None,
    description: str = "",
    scope: str = "global",
    keep: int = 20,
) -> str:
    mgr = _manager()

    if action == "snapshot":
        if not files:
            return json.dumps({"error": "snapshot 需要提供 files 参数"}, ensure_ascii=False)
        result = mgr.create(files, description=description, scope=scope or "global")
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif action == "list":
        checkpoints = mgr.list_checkpoints(scope=scope if scope != "global" else None)
        return json.dumps({
            "checkpoints": checkpoints,
            "count": len(checkpoints),
        }, ensure_ascii=False, indent=2)

    elif action == "restore":
        if not checkpoint_id:
            return json.dumps({"error": "restore 需要提供 checkpoint_id 参数"}, ensure_ascii=False)
        result = mgr.restore(checkpoint_id)
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif action == "diff":
        if not checkpoint_id:
            return json.dumps({"error": "diff 需要提供 checkpoint_id 参数"}, ensure_ascii=False)
        result = mgr.diff(checkpoint_id)
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif action == "cleanup":
        result = mgr.cleanup(keep=keep, scope=scope if scope != "global" else None)
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif action == "stats":
        return json.dumps(mgr.store_stats(), ensure_ascii=False, indent=2)

    return json.dumps({"error": f"未知操作: {action}"}, ensure_ascii=False)


registry.register(
    name="checkpoint",
    toolset="safety",
    schema=CHECKPOINT_SCHEMA,
    handler=_checkpoint_impl,
    description="文件快照/回滚",
    emoji="💾",
)
