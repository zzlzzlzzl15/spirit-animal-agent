"""任务追踪工具 — todo（计划与进度管理）。

参考 Hermes 的 tools/todo_tool.py 设计：
- 单工具：提供 todos 参数写入，省略则读取
- 每次调用返回完整列表
- 支持 merge 模式（增量更新）
"""

import json
from typing import Any, Dict, List, Optional

from spirit.tools.registry import registry


# ---------------------------------------------------------------------------
# TodoStore（内存存储，每个 Agent 会话一个实例）
# ---------------------------------------------------------------------------

VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}
MAX_ITEMS = 100
MAX_CONTENT_CHARS = 2000


class TodoStore:
    """内存中的任务列表。"""

    def __init__(self):
        self._items: List[Dict[str, str]] = []

    def write(self, todos: List[Dict[str, Any]], merge: bool = False) -> List[Dict[str, str]]:
        if not merge:
            self._items = [self._validate(t) for t in todos[:MAX_ITEMS]]
        else:
            existing = {item["id"]: item for item in self._items}
            for t in todos:
                item_id = str(t.get("id", "")).strip()
                if not item_id:
                    continue
                if item_id in existing:
                    if "content" in t and t["content"]:
                        existing[item_id]["content"] = str(t["content"])[:MAX_CONTENT_CHARS]
                    if "status" in t and t["status"]:
                        status = str(t["status"]).strip().lower()
                        if status in VALID_STATUSES:
                            existing[item_id]["status"] = status
                else:
                    validated = self._validate(t)
                    existing[validated["id"]] = validated
                    self._items.append(validated)
            # 重建保持顺序
            seen = set()
            rebuilt = []
            for item in self._items:
                if item["id"] not in seen:
                    rebuilt.append(existing.get(item["id"], item))
                    seen.add(item["id"])
            self._items = rebuilt[:MAX_ITEMS]
        return self.read()

    def read(self) -> List[Dict[str, str]]:
        return [item.copy() for item in self._items]

    def _validate(self, t: Dict[str, Any]) -> Dict[str, str]:
        return {
            "id": str(t.get("id", "")).strip()[:100],
            "content": str(t.get("content", "")).strip()[:MAX_CONTENT_CHARS],
            "status": str(t.get("status", "pending")).strip().lower()
            if str(t.get("status", "pending")).strip().lower() in VALID_STATUSES
            else "pending",
        }


# 全局 store（简化版：单进程共享）
_todo_store = TodoStore()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

TODO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "todo",
        "description": (
            "管理任务列表 — 分解复杂任务、追踪进度。\n\n"
            "用法：\n"
            "- 提供 todos 参数 → 写入/更新任务\n"
            "- 省略 todos → 读取当前任务列表\n"
            "- merge=true → 增量更新（按 id 匹配）\n"
            "- merge=false → 替换整个列表\n\n"
            "状态值：pending / in_progress / completed / cancelled\n"
            "每个任务需要 id（唯一标识）、content（描述）、status（状态）"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "任务列表 [{id, content, status}]",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "唯一标识"},
                            "content": {"type": "string", "description": "任务描述"},
                            "status": {
                                "type": "string",
                                "description": "状态: pending/in_progress/completed/cancelled",
                            },
                        },
                        "required": ["id", "content"],
                    },
                },
                "merge": {
                    "type": "boolean",
                    "description": "是否增量更新（默认 false = 替换）",
                },
            },
            "required": [],
        },
    },
}


def _todo_impl(todos: List[Dict] = None, merge: bool = False) -> str:
    if todos is not None:
        result = _todo_store.write(todos, merge=merge)
    else:
        result = _todo_store.read()

    # 格式化输出
    if not result:
        return json.dumps({"todos": [], "message": "任务列表为空"})

    markers = {
        "completed": "[x]",
        "in_progress": "[>]",
        "pending": "[ ]",
        "cancelled": "[~]",
    }

    formatted = []
    for item in result:
        marker = markers.get(item["status"], "[?]")
        formatted.append(f"{marker} {item['id']}. {item['content']} ({item['status']})")

    return json.dumps({
        "todos": result,
        "display": "\n".join(formatted),
        "summary": {
            "total": len(result),
            "completed": len([t for t in result if t["status"] == "completed"]),
            "in_progress": len([t for t in result if t["status"] == "in_progress"]),
            "pending": len([t for t in result if t["status"] == "pending"]),
        },
    }, ensure_ascii=False, indent=2)


registry.register(
    name="todo",
    toolset="planning",
    schema=TODO_SCHEMA,
    handler=_todo_impl,
    description="任务追踪",
    emoji="📋",
)
