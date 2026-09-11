"""文件操作工具 — read_file / write_file / patch / search_files。

参考 Hermes 的 tools/file_tools.py 设计，提供基础的文件读写能力。
"""

import json
import os
import re
from pathlib import Path

from spirit.tools.registry import registry
from spirit.config import get_config_value


# ---------------------------------------------------------------------------
# Schema 定义（OpenAI function calling 格式）
# ---------------------------------------------------------------------------

READ_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "读取文件内容。支持指定行范围（offset/limit）进行分页读取。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径（绝对或相对当前工作目录）",
                },
                "offset": {
                    "type": "integer",
                    "description": "起始行号（0-based，默认 0）",
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": "读取行数（默认 2000，0 = 全部）",
                    "default": 2000,
                },
            },
            "required": ["path"],
        },
    },
}

WRITE_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "将内容写入文件。如果文件不存在则创建，存在则覆盖。自动创建父目录。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的文件内容",
                },
            },
            "required": ["path", "content"],
        },
    },
}

PATCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "patch",
        "description": "对文件进行精确的搜索替换修改。支持多处同时替换。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径",
                },
                "edits": {
                    "type": "array",
                    "description": "编辑操作列表，每个操作包含 old_text 和 new_text",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_text": {
                                "type": "string",
                                "description": "要被替换的原始文本",
                            },
                            "new_text": {
                                "type": "string",
                                "description": "替换后的新文本",
                            },
                        },
                        "required": ["old_text", "new_text"],
                    },
                },
            },
            "required": ["path", "edits"],
        },
    },
}

SEARCH_FILES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_files",
        "description": "在目录中搜索文件内容（正则表达式）。返回匹配的行及上下文。",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "搜索模式（正则表达式）",
                },
                "path": {
                    "type": "string",
                    "description": "搜索目录（默认当前目录）",
                    "default": ".",
                },
                "include": {
                    "type": "string",
                    "description": "文件名过滤（glob 模式，如 '*.py'）",
                    "default": "*",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大结果数（默认 50）",
                    "default": 50,
                },
            },
            "required": ["pattern"],
        },
    },
}


# ---------------------------------------------------------------------------
# 处理函数
# ---------------------------------------------------------------------------

def _handle_read_file(path: str, offset: int = 0, limit: int = 2000) -> str:
    """读取文件内容，带行号和分页支持。"""
    file_path = Path(path).expanduser()

    if not file_path.exists():
        return json.dumps({"error": f"文件不存在: {path}"})
    if not file_path.is_file():
        return json.dumps({"error": f"不是文件: {path}"})

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return json.dumps({"error": f"读取失败: {e}"})

    lines = content.splitlines()
    total = len(lines)

    # 分页
    if limit == 0:
        limit = total
    end = min(offset + limit, total)
    selected = lines[offset:end]

    # 带行号输出
    result_lines = []
    for i, line in enumerate(selected, start=offset + 1):
        result_lines.append(f"{i:>6}│{line}")

    result = "\n".join(result_lines)

    # 截断保护
    max_chars = get_config_value("limits.tool_output_max_chars", 100_000)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n\n... [内容已截断]"

    header = f"文件: {file_path} ({total} 行"
    if offset > 0 or end < total:
        header += f", 显示 {offset+1}-{end}"
    header += ")\n"

    return header + result


def _handle_write_file(path: str, content: str) -> str:
    """写入文件，自动创建父目录，并集成 LSP 诊断检查。"""
    from spirit.lsp_integration import (
        snapshot_before_write,
        get_diagnostics_after_write,
        format_diagnostics,
    )
    
    file_path = Path(path).expanduser()

    try:
        # 1. 读取旧内容（用于 LSP delta 对比）
        pre_content = None
        if file_path.exists():
            try:
                pre_content = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass  # 如果读取失败，跳过 LSP 快照
        
        # 2. 快照 LSP 基线（在写入前捕获当前诊断）
        if pre_content is not None:
            snapshot_before_write(str(file_path))
        
        # 3. 执行写入
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        lines = content.count("\n") + 1
        
        # 4. LSP 诊断检查（只报告新增的错误）
        lsp_result = ""
        if pre_content is not None:
            diagnostics = get_diagnostics_after_write(
                str(file_path),
                pre_content=pre_content,
                post_content=content,
            )
            if diagnostics:
                lsp_block = format_diagnostics(diagnostics)
                if lsp_block:
                    lsp_result = f"\n\n🔍 LSP Diagnostics:\n{lsp_block}"
        
        # 5. 返回结果（包含 LSP 信息）
        result = {
            "success": True,
            "path": str(file_path.resolve()),
            "lines": lines,
            "chars": len(content),
        }
        if lsp_result:
            result["lsp_diagnostics"] = lsp_result
        
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"写入失败: {e}"})


def _handle_patch(path: str, edits: list) -> str:
    """对文件进行搜索替换修改，并集成 LSP 诊断检查。"""
    from spirit.lsp_integration import (
        snapshot_before_write,
        get_diagnostics_after_write,
        format_diagnostics,
    )
    
    file_path = Path(path).expanduser()

    if not file_path.exists():
        return json.dumps({"error": f"文件不存在: {path}"})

    try:
        pre_content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        return json.dumps({"error": f"读取失败: {e}"})

    applied = 0
    errors = []

    for i, edit in enumerate(edits):
        old_text = edit.get("old_text", "")
        new_text = edit.get("new_text", "")

        if not old_text:
            errors.append(f"编辑 #{i+1}: old_text 不能为空")
            continue

        if old_text not in pre_content:
            errors.append(f"编辑 #{i+1}: 未找到匹配文本")
            continue

        count = pre_content.count(old_text)
        pre_content = pre_content.replace(old_text, new_text, 1)  # 每次只替换第一处
        applied += 1

    if applied > 0:
        # 1. 快照 LSP 基线（在写入前捕获当前诊断）
        snapshot_before_write(str(file_path))
        
        # 2. 执行写入
        try:
            file_path.write_text(pre_content, encoding="utf-8")
        except Exception as e:
            return json.dumps({"error": f"写入失败: {e}"})
        
        # 3. LSP 诊断检查（只报告新增的错误）
        diagnostics = get_diagnostics_after_write(
            str(file_path),
            pre_content=file_path.read_text(encoding="utf-8", errors="replace"),
            post_content=pre_content,
        )
        lsp_result = ""
        if diagnostics:
            lsp_block = format_diagnostics(diagnostics)
            if lsp_block:
                lsp_result = f"\n\n🔍 LSP Diagnostics:\n{lsp_block}"

    result = {"applied": applied, "total": len(edits)}
    if errors:
        result["errors"] = errors
    if lsp_result:
        result["lsp_diagnostics"] = lsp_result

    return json.dumps(result, ensure_ascii=False)


def _handle_search_files(
    pattern: str,
    path: str = ".",
    include: str = "*",
    max_results: int = 50,
) -> str:
    """在目录中搜索文件内容。"""
    search_path = Path(path).expanduser()

    if not search_path.exists():
        return json.dumps({"error": f"目录不存在: {path}"})

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return json.dumps({"error": f"无效的正则表达式: {e}"})

    results = []
    files_searched = 0

    # 递归搜索
    for file_path in search_path.rglob(include):
        if not file_path.is_file():
            continue
        # 跳过隐藏文件和常见非文本目录
        parts = file_path.parts
        if any(p.startswith(".") or p in ("node_modules", "__pycache__", ".git") for p in parts):
            continue

        files_searched += 1
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            for line_num, line in enumerate(content.splitlines(), 1):
                if regex.search(line):
                    rel_path = file_path.relative_to(search_path)
                    results.append(f"{rel_path}:{line_num}: {line.strip()}")
                    if len(results) >= max_results:
                        break
        except Exception:
            continue

        if len(results) >= max_results:
            break

    header = f"搜索 '{pattern}' in {search_path}"
    header += f" ({len(results)} 个匹配, 扫描 {files_searched} 个文件)\n"
    return header + "\n".join(results)


# ---------------------------------------------------------------------------
# 自注册
# ---------------------------------------------------------------------------

registry.register(
    name="read_file",
    toolset="file",
    schema=READ_FILE_SCHEMA,
    handler=_handle_read_file,
    emoji="📖",
    max_result_size_chars=get_config_value("limits.tool_output_max_chars", 100_000),
)

registry.register(
    name="write_file",
    toolset="file",
    schema=WRITE_FILE_SCHEMA,
    handler=_handle_write_file,
    emoji="✍️",
)

registry.register(
    name="patch",
    toolset="file",
    schema=PATCH_SCHEMA,
    handler=_handle_patch,
    emoji="🔧",
)

# search_files 已移至 search_tools.py（更完整的实现）


# ---------------------------------------------------------------------------
# 新增工具：insert_at_position / replace_range
# ---------------------------------------------------------------------------

INSERT_AT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "insert_at_position",
        "description": (
            "在文件的指定行插入内容。\n"
            "在 VSCode 环境中会展示 diff 预览并等待用户确认。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径",
                },
                "line": {
                    "type": "integer",
                    "description": "插入位置（0-based 行号，0 = 文件开头）",
                },
                "content": {
                    "type": "string",
                    "description": "要插入的内容",
                },
            },
            "required": ["path", "line", "content"],
        },
    },
}

REPLACE_RANGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "replace_range",
        "description": (
            "替换文件中指定行范围的内容。\n"
            "在 VSCode 环境中会展示 diff 预览并等待用户确认。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径",
                },
                "start_line": {
                    "type": "integer",
                    "description": "起始行号（0-based）",
                },
                "end_line": {
                    "type": "integer",
                    "description": "结束行号（0-based，包含该行）",
                },
                "content": {
                    "type": "string",
                    "description": "替换后的新内容",
                },
            },
            "required": ["path", "start_line", "end_line", "content"],
        },
    },
}


def _handle_insert_at(path: str, line: int, content: str) -> str:
    """在指定行插入内容。"""
    file_path = Path(path).expanduser()

    if not file_path.exists():
        # 新文件：直接写入
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            return json.dumps({
                "success": True,
                "path": str(file_path.resolve()),
                "action": "create",
                "lines": content.count("\n") + 1,
            })
        except Exception as e:
            return json.dumps({"error": f"创建文件失败: {e}"})

    try:
        original = file_path.read_text(encoding="utf-8")
    except Exception as e:
        return json.dumps({"error": f"读取失败: {e}"})

    lines = original.splitlines(keepends=True)
    insert_line = max(0, min(line, len(lines)))

    # 确保插入内容有换行符
    insert_content = content if content.endswith("\n") else content + "\n"
    lines.insert(insert_line, insert_content)

    new_content = "".join(lines)

    # VSCode 模式：发送编辑提案
    from spirit.tools.edit_proposal import EditProposalManager
    mgr = EditProposalManager()  # 无 send_fn = 直接写模式
    # 检查是否有 agent 级别的管理器（通过全局注册表）
    # 这里简化处理：直接写入，VSCode 模式由 server.py 设置的管理器处理
    try:
        file_path.write_text(new_content, encoding="utf-8")
        return json.dumps({
            "success": True,
            "path": str(file_path.resolve()),
            "action": "insert",
            "at_line": insert_line,
            "inserted_lines": content.count("\n") + 1,
        })
    except Exception as e:
        return json.dumps({"error": f"写入失败: {e}"})


def _handle_replace_range(
    path: str, start_line: int, end_line: int, content: str
) -> str:
    """替换指定行范围的内容。"""
    file_path = Path(path).expanduser()

    if not file_path.exists():
        return json.dumps({"error": f"文件不存在: {path}"})

    try:
        original = file_path.read_text(encoding="utf-8")
    except Exception as e:
        return json.dumps({"error": f"读取失败: {e}"})

    lines = original.splitlines(keepends=True)
    start = max(0, start_line)
    end = min(end_line, len(lines) - 1)

    if start > end:
        return json.dumps({"error": f"无效范围: {start_line}-{end_line}"})

    # 替换
    new_lines = lines[:start]
    if content:
        insert = content if content.endswith("\n") else content + "\n"
        new_lines.append(insert)
    new_lines.extend(lines[end + 1:])

    new_content = "".join(new_lines)

    try:
        file_path.write_text(new_content, encoding="utf-8")
        return json.dumps({
            "success": True,
            "path": str(file_path.resolve()),
            "action": "replace_range",
            "replaced_lines": end - start + 1,
            "new_lines": content.count("\n") + 1,
        })
    except Exception as e:
        return json.dumps({"error": f"写入失败: {e}"})


registry.register(
    name="insert_at_position",
    toolset="file",
    schema=INSERT_AT_SCHEMA,
    handler=_handle_insert_at,
    emoji="📝",
)

registry.register(
    name="replace_range",
    toolset="file",
    schema=REPLACE_RANGE_SCHEMA,
    handler=_handle_replace_range,
    emoji="🔄",
)
