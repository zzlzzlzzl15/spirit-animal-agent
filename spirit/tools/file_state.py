"""文件变更追踪 — file_state（追踪当前会话中修改过的文件）。

参考 Hermes 的 tools/file_state.py 设计：
- 记录哪些文件被 Agent 修改过
- 提供变更摘要（用于审批、回滚决策）
- 支持检查文件是否被外部修改
"""

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FileState 追踪器
# ---------------------------------------------------------------------------

class FileStateTracker:
    """追踪文件变更状态。"""

    def __init__(self):
        # filepath → {original_hash, original_size, modified_at, ...}
        self._states: Dict[str, Dict[str, Any]] = {}

    def record_original(self, filepath: str):
        """记录文件的原始状态（在首次修改前调用）。"""
        path = str(Path(filepath).resolve())
        if path in self._states:
            return  # 已记录

        p = Path(path)
        if not p.exists():
            self._states[path] = {
                "existed": False,
                "original_hash": None,
                "original_size": 0,
                "recorded_at": time.time(),
            }
            return

        content = p.read_bytes()
        self._states[path] = {
            "existed": True,
            "original_hash": hashlib.sha256(content).hexdigest()[:16],
            "original_size": len(content),
            "recorded_at": time.time(),
        }

    def record_modified(self, filepath: str):
        """记录文件修改后的状态。"""
        path = str(Path(filepath).resolve())
        p = Path(path)

        if path not in self._states:
            self.record_original(filepath)

        if p.exists():
            content = p.read_bytes()
            self._states[path]["current_hash"] = hashlib.sha256(content).hexdigest()[:16]
            self._states[path]["current_size"] = len(content)
            self._states[path]["modified_at"] = time.time()
        else:
            self._states[path]["current_hash"] = None
            self._states[path]["current_size"] = 0
            self._states[path]["deleted"] = True

    def get_changes(self) -> List[Dict]:
        """获取所有变更记录。"""
        changes = []
        for path, state in self._states.items():
            if "current_hash" not in state:
                continue  # 只记录了原始，还没修改

            changed = state.get("original_hash") != state.get("current_hash")
            if not changed:
                continue

            change = {
                "path": path,
                "existed_before": state.get("existed", False),
                "original_size": state.get("original_size", 0),
                "current_size": state.get("current_size", 0),
                "deleted": state.get("deleted", False),
            }

            if state.get("existed") and not state.get("deleted"):
                change["size_diff"] = state["current_size"] - state["original_size"]

            changes.append(change)

        return changes

    def get_summary(self) -> str:
        """获取变更摘要文本。"""
        changes = self.get_changes()
        if not changes:
            return "当前会话没有修改任何文件"

        lines = [f"当前会话修改了 {len(changes)} 个文件："]
        for c in changes:
            path = Path(c["path"]).name
            if c.get("deleted"):
                lines.append(f"  🗑️ {path} (已删除)")
            elif not c.get("existed_before"):
                lines.append(f"  ✨ {path} (新建, {c['current_size']} bytes)")
            else:
                diff = c.get("size_diff", 0)
                sign = "+" if diff >= 0 else ""
                lines.append(f"  📝 {path} ({sign}{diff} bytes)")

        return "\n".join(lines)

    def reset(self):
        """重置追踪状态。"""
        self._states.clear()


# 全局实例
_file_state = FileStateTracker()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

FILE_STATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "file_changes",
        "description": (
            "查看当前会话中修改了哪些文件。\n\n"
            "返回变更摘要：新建/修改/删除的文件列表及大小变化。\n"
            "用于了解一次操作的影响范围。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "操作: summary（摘要）/ detail（详情）/ reset（重置）",
                    "enum": ["summary", "detail", "reset"],
                },
            },
            "required": [],
        },
    },
}


def _file_changes_impl(action: str = "summary") -> str:
    if action == "summary":
        return _file_state.get_summary()
    elif action == "detail":
        changes = _file_state.get_changes()
        return json.dumps({"changes": changes, "count": len(changes)}, ensure_ascii=False, indent=2)
    elif action == "reset":
        _file_state.reset()
        return json.dumps({"success": True, "message": "变更追踪已重置"})
    return json.dumps({"error": f"未知操作: {action}"})


registry.register(
    name="file_changes",
    toolset="safety",
    schema=FILE_STATE_SCHEMA,
    handler=_file_changes_impl,
    description="文件变更追踪",
    emoji="📊",
)
