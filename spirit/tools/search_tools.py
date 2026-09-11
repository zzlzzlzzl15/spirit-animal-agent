"""文件搜索工具 — search_files（内容搜索 / grep 替代）。

参考 Hermes 的搜索工具设计，提供基于正则的文件内容搜索。
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

from spirit.tools.registry import registry
from spirit.config import get_config_value

logger = logging.getLogger(__name__)

# 忽略的目录
IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".tox", ".mypy_cache", ".pytest_cache",
    "target", ".idea", ".vs", "vendor",
}

# 忽略的二进制扩展名
BINARY_EXTENSIONS = {
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".obj", ".o",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico", ".svg", ".webp",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv",
    ".zip", ".tar", ".gz", ".bz2", ".rar", ".7z",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".woff", ".woff2", ".ttf", ".eot",
    ".db", ".sqlite", ".sqlite3",
}

MAX_RESULTS = 100
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SEARCH_FILES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_files",
        "description": (
            "在文件内容中搜索匹配的模式（类似 grep/ripgrep）。\n\n"
            "支持正则表达式，显示匹配行及上下文。\n"
            "自动跳过二进制文件和常见忽略目录。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "搜索的正则表达式",
                },
                "path": {
                    "type": "string",
                    "description": "搜索根目录（默认当前目录）",
                },
                "file_glob": {
                    "type": "string",
                    "description": "文件名过滤（如 '*.py'、'*.ts'）",
                },
                "ignore_case": {
                    "type": "boolean",
                    "description": "忽略大小写（默认 false）",
                },
                "context_lines": {
                    "type": "integer",
                    "description": "显示上下文行数（默认 0）",
                },
                "max_results": {
                    "type": "integer",
                    "description": f"最大结果数（默认 50，最大 {MAX_RESULTS}）",
                },
            },
            "required": ["pattern"],
        },
    },
}


def _search_files_impl(
    pattern: str,
    path: str = ".",
    file_glob: str = None,
    ignore_case: bool = False,
    context_lines: int = 0,
    max_results: int = 50,
) -> str:
    """搜索文件内容。"""
    max_results = min(max(max_results, 1), MAX_RESULTS)
    context_lines = min(max(context_lines, 0), 5)

    root = Path(path).resolve()
    if not root.is_dir():
        return json.dumps({"error": f"不是目录: {path}"})

    # 编译正则
    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return json.dumps({"error": f"无效的正则表达式: {e}"})

    # 文件名过滤
    glob_match = None
    if file_glob:
        import fnmatch
        glob_match = lambda name: fnmatch.fnmatch(name.lower(), file_glob.lower())

    results = []
    files_searched = 0

    for dirpath, dirnames, filenames in os.walk(root):
        # 过滤忽略目录
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]

        for fname in filenames:
            # 文件名过滤
            if glob_match and not glob_match(fname):
                continue

            # 跳过二进制
            ext = Path(fname).suffix.lower()
            if ext in BINARY_EXTENSIONS:
                continue

            fpath = Path(dirpath) / fname

            # 跳过过大文件
            try:
                if fpath.stat().st_size > MAX_FILE_SIZE:
                    continue
            except OSError:
                continue

            files_searched += 1

            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except (OSError, UnicodeDecodeError):
                continue

            # 搜索匹配行
            for i, line in enumerate(lines):
                if regex.search(line):
                    rel_path = str(fpath.relative_to(root))

                    # 构建上下文
                    start = max(0, i - context_lines)
                    end = min(len(lines), i + context_lines + 1)
                    context = []
                    for j in range(start, end):
                        prefix = ">" if j == i else " "
                        context.append(f"{prefix} {j + 1}: {lines[j].rstrip()}")

                    results.append({
                        "file": rel_path,
                        "line": i + 1,
                        "match": line.rstrip()[:200],
                        "context": context if context_lines > 0 else None,
                    })

                    if len(results) >= max_results:
                        return json.dumps({
                            "results": results,
                            "count": len(results),
                            "files_searched": files_searched,
                            "truncated": True,
                            "pattern": pattern,
                        }, ensure_ascii=False, indent=2)

    return json.dumps({
        "results": results,
        "count": len(results),
        "files_searched": files_searched,
        "truncated": False,
        "pattern": pattern,
    }, ensure_ascii=False, indent=2)


registry.register(
    name="search_files",
    toolset="file",
    schema=SEARCH_FILES_SCHEMA,
    handler=_search_files_impl,
    description="搜索文件内容（grep）",
    emoji="🔎",
    max_result_size_chars=get_config_value("limits.search_max_chars", 20000),
)


# ---------------------------------------------------------------------------
# find_files — 文件名搜索
# ---------------------------------------------------------------------------

FIND_FILES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "find_files",
        "description": (
            "按文件名模式搜索文件（类似 find/glob）。\n\n"
            "支持通配符和正则表达式。\n"
            "返回匹配的文件路径列表。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "文件名模式（通配符如 '*.py' 或正则）",
                },
                "path": {
                    "type": "string",
                    "description": "搜索根目录（默认当前目录）",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大结果数（默认 50）",
                },
                "regex": {
                    "type": "boolean",
                    "description": "是否使用正则表达式（默认 false = 通配符）",
                },
            },
            "required": ["pattern"],
        },
    },
}


def _find_files_impl(
    pattern: str,
    path: str = ".",
    max_results: int = 50,
    regex: bool = False,
) -> str:
    """按文件名搜索。"""
    max_results = min(max(max_results, 1), 200)
    root = Path(path).resolve()

    if not root.is_dir():
        return json.dumps({"error": f"不是目录: {path}"})

    import fnmatch

    results = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]

        for fname in filenames:
            if regex:
                try:
                    if re.search(pattern, fname):
                        results.append(str(Path(dirpath, fname).relative_to(root)))
                except re.error:
                    return json.dumps({"error": f"无效的正则: {pattern}"})
            else:
                if fnmatch.fnmatch(fname.lower(), pattern.lower()):
                    results.append(str(Path(dirpath, fname).relative_to(root)))

            if len(results) >= max_results:
                break

        if len(results) >= max_results:
            break

    return json.dumps({
        "files": sorted(results),
        "count": len(results),
        "pattern": pattern,
    }, ensure_ascii=False, indent=2)


registry.register(
    name="find_files",
    toolset="file",
    schema=FIND_FILES_SCHEMA,
    handler=_find_files_impl,
    description="按文件名搜索文件",
    emoji="📂",
)
