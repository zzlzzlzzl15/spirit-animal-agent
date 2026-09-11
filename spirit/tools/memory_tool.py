"""记忆管理工具 — memory（跨会话持久记忆）。

参考 Hermes 的 tools/memory_tool.py 设计：
- 文件持久化，存储在 ~/.spirit/memories/
- 支持 add / replace / remove / read 操作
- 分隔符 § 标记条目边界
- 字符限制防止无限膨胀
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)

# 存储路径
MEMORY_DIR = Path.home() / ".spirit" / "memories"
ENTRY_DELIMITER = "\n§\n"
MAX_MEMORY_CHARS = 5000
MAX_USER_CHARS = 3000


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------

class MemoryStore:
    """文件持久化的记忆存储。"""

    def __init__(self):
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        self.memory_file = MEMORY_DIR / "MEMORY.md"
        self.user_file = MEMORY_DIR / "USER.md"

    def _read_entries(self, filepath: Path) -> List[str]:
        if not filepath.exists():
            return []
        content = filepath.read_text(encoding="utf-8")
        entries = [e.strip() for e in content.split(ENTRY_DELIMITER) if e.strip()]
        return entries

    def _write_entries(self, filepath: Path, entries: List[str], max_chars: int):
        # 限制总字符数
        total = sum(len(e) for e in entries)
        while total > max_chars and len(entries) > 1:
            removed = entries.pop(0)
            total -= len(removed)

        content = ENTRY_DELIMITER.join(entries) + "\n"
        # 原子写入
        tmp = filepath.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(filepath)

    def add(self, store: str, content: str) -> Dict:
        """添加记忆条目。"""
        filepath = self.user_file if store == "user" else self.memory_file
        max_chars = MAX_USER_CHARS if store == "user" else MAX_MEMORY_CHARS

        entries = self._read_entries(filepath)
        entries.append(content.strip())
        self._write_entries(filepath, entries, max_chars)

        return {"success": True, "action": "add", "store": store, "count": len(entries)}

    def replace(self, store: str, old_substring: str, new_content: str) -> Dict:
        """用子串匹配替换条目。"""
        filepath = self.user_file if store == "user" else self.memory_file
        max_chars = MAX_USER_CHARS if store == "user" else MAX_MEMORY_CHARS

        entries = self._read_entries(filepath)
        found = False
        for i, entry in enumerate(entries):
            if old_substring in entry:
                entries[i] = new_content.strip()
                found = True
                break

        if not found:
            return {"success": False, "error": f"未找到包含 '{old_substring}' 的条目"}

        self._write_entries(filepath, entries, max_chars)
        return {"success": True, "action": "replace", "store": store, "count": len(entries)}

    def remove(self, store: str, substring: str) -> Dict:
        """用子串匹配删除条目。"""
        filepath = self.user_file if store == "user" else self.memory_file
        max_chars = MAX_USER_CHARS if store == "user" else MAX_MEMORY_CHARS

        entries = self._read_entries(filepath)
        original_count = len(entries)
        entries = [e for e in entries if substring not in e]

        if len(entries) == original_count:
            return {"success": False, "error": f"未找到包含 '{substring}' 的条目"}

        self._write_entries(filepath, entries, max_chars)
        return {"success": True, "action": "remove", "store": store, "count": len(entries)}

    def read(self, store: str = None) -> Dict:
        """读取记忆。"""
        if store == "user":
            return {"user_entries": self._read_entries(self.user_file)}
        elif store == "memory":
            return {"memory_entries": self._read_entries(self.memory_file)}
        else:
            return {
                "memory_entries": self._read_entries(self.memory_file),
                "user_entries": self._read_entries(self.user_file),
            }


# 全局实例
_memory_store = MemoryStore()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

MEMORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory",
        "description": (
            "管理跨会话持久记忆。\n\n"
            "两个存储区：\n"
            "- memory: Agent 的笔记（项目约定、工具技巧、环境信息）\n"
            "- user: 关于用户的信息（偏好、沟通风格、工作习惯）\n\n"
            "操作：\n"
            "- add: 添加新条目\n"
            "- replace: 用子串匹配替换条目\n"
            "- remove: 用子串匹配删除条目\n"
            "- read: 读取所有条目"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "操作: add / replace / remove / read",
                    "enum": ["add", "replace", "remove", "read"],
                },
                "store": {
                    "type": "string",
                    "description": "存储区: memory / user（默认 memory）",
                    "enum": ["memory", "user"],
                },
                "content": {
                    "type": "string",
                    "description": "新条目内容（add 时必填）",
                },
                "substring": {
                    "type": "string",
                    "description": "匹配子串（replace/remove 时必填）",
                },
                "new_content": {
                    "type": "string",
                    "description": "替换内容（replace 时必填）",
                },
            },
            "required": ["action"],
        },
    },
}


def _memory_impl(
    action: str = "read",
    store: str = "memory",
    content: str = None,
    substring: str = None,
    new_content: str = None,
) -> str:
    if action == "read":
        result = _memory_store.read(store)
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif action == "add":
        if not content:
            return json.dumps({"error": "add 操作需要 content 参数"})
        result = _memory_store.add(store, content)
        return json.dumps(result, ensure_ascii=False)

    elif action == "replace":
        if not substring or not new_content:
            return json.dumps({"error": "replace 操作需要 substring 和 new_content 参数"})
        result = _memory_store.replace(store, substring, new_content)
        return json.dumps(result, ensure_ascii=False)

    elif action == "remove":
        if not substring:
            return json.dumps({"error": "remove 操作需要 substring 参数"})
        result = _memory_store.remove(store, substring)
        return json.dumps(result, ensure_ascii=False)

    return json.dumps({"error": f"未知操作: {action}"})


registry.register(
    name="memory",
    toolset="memory",
    schema=MEMORY_SCHEMA,
    handler=_memory_impl,
    description="持久记忆管理",
    emoji="🧠",
)
