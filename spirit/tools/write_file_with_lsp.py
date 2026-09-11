"""带 LSP 检查的文件写入工具 — write_file_with_lsp。

参考 Hermes 的设计，但适配 Spirit Agent 的架构：
- 写入文件后自动进行 LSP 诊断检查
- 只报告新增的错误（delta 过滤）
- 支持 range shift（避免误报移动的错误）

与 Hermes 的区别：
- Hermes: LSP 内置在 file_operations.write_file() 中
- Spirit: 独立的工具，通过 terminal 或 Python 写入
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

WRITE_FILE_WITH_LSP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_file_with_lsp",
        "description": (
            "写入文件并进行 LSP 诊断检查。\n"
            "如果检测到语法或语义错误，会返回详细的错误信息。\n"
            "支持多种编程语言：Python, TypeScript, Go, Rust, YAML, JSON, HTML, CSS, Lua 等。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径（绝对或相对路径）",
                },
                "content": {
                    "type": "string",
                    "description": "文件内容",
                },
                "create_parents": {
                    "type": "boolean",
                    "description": "是否创建父目录（默认 true）",
                },
            },
            "required": ["path", "content"],
        },
    },
}


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def _read_file(path: str) -> str:
    """读取文件内容，如果文件不存在则返回空字符串。"""
    if not os.path.exists(path):
        return ""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        logger.debug("Failed to read %s: %s", path, e)
        return ""


def _ensure_parent_dirs(path: str) -> bool:
    """确保父目录存在。"""
    parent = Path(path).parent
    if not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
            return True
        except Exception as e:
            logger.error("Failed to create parent dirs for %s: %s", path, e)
            return False
    return True


def _write_file_atomic(path: str, content: str) -> tuple[bool, Optional[str]]:
    """原子写入文件（先写临时文件再重命名）。
    
    Returns:
        (success, error_message)
    """
    temp_path = path + ".tmp"
    try:
        # 写入临时文件
        with open(temp_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        # 重命名为目标文件
        os.replace(temp_path, path)
        return True, None
    except Exception as e:
        # 清理临时文件
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except:
                pass
        return False, str(e)


# ---------------------------------------------------------------------------
# Tool Implementation
# ---------------------------------------------------------------------------

@registry.register(WRITE_FILE_WITH_LSP_SCHEMA)
def write_file_with_lsp(
    path: str,
    content: str,
    create_parents: bool = True,
) -> str:
    """写入文件并进行 LSP 检查。
    
    Args:
        path: 文件路径
        content: 文件内容
        create_parents: 是否创建父目录
        
    Returns:
        格式化的结果，包含写入状态和 LSP 诊断信息
    """
    # 1. 验证路径
    abs_path = os.path.abspath(path)
    
    # 2. 创建父目录
    if create_parents and not _ensure_parent_dirs(abs_path):
        return json.dumps({
            "success": False,
            "error": f"无法创建父目录: {Path(abs_path).parent}"
        })
    
    # 3. 读取旧内容（用于 delta 过滤）
    old_content = _read_file(abs_path)
    
    # 4. 快照 LSP 基线（如果 LSP 可用）
    try:
        from spirit.lsp_integration import snapshot_before_write
        snapshot_before_write(abs_path)
        logger.debug("Snapped LSP baseline for %s", abs_path)
    except Exception as e:
        logger.debug("LSP snapshot failed (may be inactive): %s", e)
    
    # 5. 写入文件
    success, error = _write_file_atomic(abs_path, content)
    if not success:
        return json.dumps({
            "success": False,
            "error": f"写入失败: {error}"
        })
    
    # 6. 获取 LSP 诊断
    lsp_output = ""
    try:
        from spirit.lsp_integration import check_write_with_lsp
        lsp_output = check_write_with_lsp(abs_path, old_content, content)
        if lsp_output:
            logger.info("LSP found diagnostics for %s", abs_path)
    except Exception as e:
        logger.debug("LSP check failed (may be inactive): %s", e)
        lsp_output = ""
    
    # 7. 构建结果
    result = {
        "success": True,
        "path": abs_path,
        "bytes_written": len(content.encode('utf-8')),
    }
    
    if lsp_output:
        result["lsp_diagnostics"] = lsp_output
        result["message"] = "文件已写入，但检测到 LSP 错误"
    else:
        result["message"] = "文件已成功写入，无 LSP 错误"
    
    return json.dumps(result, ensure_ascii=False, indent=2)


__all__ = ["write_file_with_lsp"]
