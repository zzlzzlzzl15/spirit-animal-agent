"""高级文件操作工具 — list_dir / move_file / delete_file / create_directory / file_info。

参考 Hermes 的 tools/file_operations.py 设计，提供完整的文件系统操作能力。
"""

import json
import os
import shutil
import stat
from pathlib import Path

from spirit.tools.registry import registry


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------

LIST_DIR_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_dir",
        "description": (
            "列出目录内容。显示文件和子目录，包含文件大小和类型信息。\n"
            "支持递归列出（max_depth 控制深度）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "目录路径（默认当前目录）",
                },
                "show_hidden": {
                    "type": "boolean",
                    "description": "是否显示隐藏文件（默认 false）",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "递归深度（默认 1，即只列当前层）",
                },
            },
            "required": [],
        },
    },
}


def _format_size(size: int) -> str:
    """人类可读的文件大小。"""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{size}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _list_dir_impl(
    path: str = ".",
    show_hidden: bool = False,
    max_depth: int = 1,
) -> str:
    target = Path(path).resolve()
    if not target.exists():
        return json.dumps({"error": f"路径不存在: {path}"})
    if not target.is_dir():
        return json.dumps({"error": f"不是目录: {path}"})

    entries = []
    max_depth = min(max_depth, 5)  # 安全限制

    def _scan(dir_path: Path, depth: int):
        if depth > max_depth:
            return
        try:
            items = sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            entries.append({"name": str(dir_path) + "/<permission denied>", "type": "error"})
            return

        for item in items:
            if not show_hidden and item.name.startswith("."):
                continue

            entry = {"name": item.name}
            if item.is_dir():
                entry["type"] = "directory"
                entries.append(entry)
                if depth < max_depth:
                    _scan(item, depth + 1)
            elif item.is_file():
                try:
                    size = item.stat().st_size
                    entry["type"] = "file"
                    entry["size"] = _format_size(size)
                except OSError:
                    entry["type"] = "file"
                    entry["size"] = "?"
                entries.append(entry)

    _scan(target, 0)
    return json.dumps({
        "path": str(target),
        "entries": entries,
        "count": len([e for e in entries if e.get("type") != "error"]),
    }, ensure_ascii=False, indent=2)


registry.register(
    name="list_dir",
    toolset="file",
    schema=LIST_DIR_SCHEMA,
    handler=_list_dir_impl,
    description="列出目录内容",
    emoji="📂",
)


# ---------------------------------------------------------------------------
# move_file
# ---------------------------------------------------------------------------

MOVE_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "move_file",
        "description": "移动或重命名文件/目录。",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "源路径"},
                "destination": {"type": "string", "description": "目标路径"},
            },
            "required": ["source", "destination"],
        },
    },
}


def _move_file_impl(source: str, destination: str) -> str:
    src = Path(source).resolve()
    dst = Path(destination).resolve()

    if not src.exists():
        return json.dumps({"error": f"源路径不存在: {source}"})
    if dst.exists():
        return json.dumps({"error": f"目标路径已存在: {destination}"})

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return json.dumps({
            "success": True,
            "source": str(src),
            "destination": str(dst),
        })
    except Exception as e:
        return json.dumps({"error": f"移动失败: {e}"})


registry.register(
    name="move_file",
    toolset="file",
    schema=MOVE_FILE_SCHEMA,
    handler=_move_file_impl,
    description="移动或重命名文件/目录",
    emoji="📦",
)


# ---------------------------------------------------------------------------
# delete_file
# ---------------------------------------------------------------------------

DELETE_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delete_file",
        "description": "删除文件或空目录。（谨慎使用）",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要删除的路径"},
                "recursive": {
                    "type": "boolean",
                    "description": "是否递归删除目录（默认 false）",
                },
            },
            "required": ["path"],
        },
    },
}


def _delete_file_impl(path: str, recursive: bool = False) -> str:
    target = Path(path).resolve()

    if not target.exists():
        return json.dumps({"error": f"路径不存在: {path}"})

    # 安全检查：不允许删除家目录级别以上的路径
    home = Path.home().resolve()
    if target == home or not str(target).startswith(str(home)):
        if target in (home, home.parent) or len(str(target)) <= len(str(home)):
            return json.dumps({"error": "拒绝删除系统关键路径"})

    try:
        if target.is_dir():
            if recursive:
                shutil.rmtree(str(target))
            else:
                target.rmdir()  # 只删空目录
        else:
            target.unlink()

        return json.dumps({"success": True, "deleted": str(target)})
    except OSError as e:
        if "not empty" in str(e).lower() and not recursive:
            return json.dumps({
                "error": "目录非空，设置 recursive=true 以递归删除",
                "path": str(target),
            })
        return json.dumps({"error": f"删除失败: {e}"})


registry.register(
    name="delete_file",
    toolset="file",
    schema=DELETE_FILE_SCHEMA,
    handler=_delete_file_impl,
    description="删除文件或空目录",
    emoji="🗑️",
)


# ---------------------------------------------------------------------------
# create_directory
# ---------------------------------------------------------------------------

CREATE_DIR_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_directory",
        "description": "创建目录（支持递归创建多级目录）。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径"},
            },
            "required": ["path"],
        },
    },
}


def _create_directory_impl(path: str) -> str:
    target = Path(path).resolve()
    try:
        target.mkdir(parents=True, exist_ok=True)
        return json.dumps({"success": True, "path": str(target)})
    except Exception as e:
        return json.dumps({"error": f"创建目录失败: {e}"})


registry.register(
    name="create_directory",
    toolset="file",
    schema=CREATE_DIR_SCHEMA,
    handler=_create_directory_impl,
    description="创建目录",
    emoji="📁",
)


# ---------------------------------------------------------------------------
# file_info
# ---------------------------------------------------------------------------

FILE_INFO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "file_info",
        "description": "获取文件/目录的详细信息（大小、修改时间、权限等）。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
            },
            "required": ["path"],
        },
    },
}


def _file_info_impl(path: str) -> str:
    import time

    target = Path(path).resolve()
    if not target.exists():
        return json.dumps({"error": f"路径不存在: {path}"})

    try:
        st = target.stat()
        info = {
            "path": str(target),
            "name": target.name,
            "type": "directory" if target.is_dir() else "file",
            "size": _format_size(st.st_size),
            "size_bytes": st.st_size,
            "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_ctime)),
            "permissions": stat.filemode(st.st_mode),
        }
        if target.is_file():
            info["extension"] = target.suffix
            # 行数统计
            try:
                with open(target, "r", encoding="utf-8", errors="replace") as f:
                    lines = sum(1 for _ in f)
                info["lines"] = lines
            except Exception:
                pass

        return json.dumps(info, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": f"获取信息失败: {e}"})


registry.register(
    name="file_info",
    toolset="file",
    schema=FILE_INFO_SCHEMA,
    handler=_file_info_impl,
    description="获取文件详细信息",
    emoji="📋",
)
