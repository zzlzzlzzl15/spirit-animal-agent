"""LSP 集成辅助模块 — 为 Spirit Agent 的终端工具提供 LSP 支持。

Spirit Agent 通过终端执行 shell 命令来写入文件（如 `echo "content" > file.txt`），
这与 Hermes 直接在 Python 中调用 write_file() 不同。

本模块提供辅助函数，在终端命令执行前后拦截，实现：
1. 写前快照（snapshot_baseline）- 捕获当前 LSP 诊断
2. 写后诊断（get_diagnostics）- 获取新的 LSP 诊断
3. Delta 过滤 - 只报告新增的错误

使用方式：
    from spirit.lsp_integration import snapshot_before_write, get_diagnostics_after_write
    
    # 执行写入命令前
    pre_content = read_file(path)
    snapshot_before_write(path)
    
    # 执行写入命令（通过 terminal_tool.execute）
    result = terminal.execute(f'cat > {path}', input=new_content)
    
    # 执行写入命令后
    diags = get_diagnostics_after_write(path, pre_content=pre_content, post_content=new_content)
    if diags:
        print("新增的 LSP 错误:", diags)
"""

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _get_lsp_service():
    """获取 LSP 服务单例。失败时返回 None。"""
    try:
        from spirit.lsp import get_service
        return get_service()
    except Exception as e:
        logger.debug("Failed to get LSP service: %s", e)
        return None


def snapshot_before_write(file_path: str) -> bool:
    """在写入文件前捕获 LSP 诊断基线。
    
    Args:
        file_path: 要写入的文件路径
        
    Returns:
        True 如果成功捕获基线，False 如果 LSP 不可用或失败
    """
    svc = _get_lsp_service()
    if svc is None:
        return False
    
    try:
        abs_path = os.path.abspath(file_path)
        if not svc.enabled_for(abs_path):
            logger.debug("LSP not enabled for %s", abs_path)
            return False
        
        svc.snapshot_baseline(abs_path)
        logger.debug("Snapshot baseline for %s", abs_path)
        return True
    except Exception as e:
        logger.debug("Failed to snapshot baseline for %s: %s", file_path, e)
        return False


def get_diagnostics_after_write(
    file_path: str,
    *,
    pre_content: Optional[str] = None,
    post_content: Optional[str] = None,
    delta: bool = True,
) -> List[Dict[str, Any]]:
    """在写入文件后获取 LSP 诊断。
    
    Args:
        file_path: 已写入的文件路径
        pre_content: 写入前的文件内容（用于构建 line_shift map）
        post_content: 写入后的文件内容（用于构建 line_shift map）
        delta: 是否启用 delta 过滤（默认 True）
        
    Returns:
        新增的诊断列表（如果 delta=True），或所有诊断（如果 delta=False）
    """
    svc = _get_lsp_service()
    if svc is None:
        return []
    
    try:
        abs_path = os.path.abspath(file_path)
        if not svc.enabled_for(abs_path):
            logger.debug("LSP not enabled for %s", abs_path)
            return []
        
        # 构建 line_shift map（如果有 pre/post content）
        line_shift = None
        if pre_content is not None and post_content is not None and pre_content != post_content:
            try:
                from spirit.lsp.range_shift import build_line_shift
                line_shift = build_line_shift(pre_content, post_content)
                logger.debug("Built line_shift map for %s", abs_path)
            except Exception as e:
                logger.debug("Failed to build line_shift map: %s", e)
                line_shift = None
        
        # 获取诊断
        diagnostics = svc.get_diagnostics_sync(
            abs_path,
            delta=delta,
            line_shift=line_shift,
        )
        
        if diagnostics:
            logger.info("Found %d new diagnostic(s) for %s", len(diagnostics), abs_path)
        else:
            logger.debug("No new diagnostics for %s", abs_path)
        
        return diagnostics
    except Exception as e:
        logger.debug("Failed to get diagnostics for %s: %s", file_path, e)
        return []


def format_diagnostics(diagnostics: List[Dict[str, Any]]) -> str:
    """格式化诊断列表为可读字符串。
    
    Args:
        diagnostics: 诊断列表
        
    Returns:
        格式化的诊断字符串，如果没有诊断则返回空字符串
    """
    if not diagnostics:
        return ""
    
    lines = ["<lsp_diagnostics>"]
    for i, diag in enumerate(diagnostics, 1):
        severity_map = {1: "ERROR", 2: "WARNING", 3: "INFO", 4: "HINT"}
        severity = severity_map.get(diag.get("severity", 0), "UNKNOWN")
        message = diag.get("message", "Unknown error")
        source = diag.get("source", "LSP")
        code = diag.get("code")
        
        rng = diag.get("range", {})
        start = rng.get("start", {})
        end = rng.get("end", {})
        start_line = start.get("line", 0) + 1  # LSP uses 0-indexed
        start_char = start.get("character", 0)
        
        line_info = f"[Line {start_line}:{start_char}]"
        code_info = f" [{code}]" if code else ""
        
        lines.append(f"{i}. {severity}{code_info} {line_info}: {message}")
        if source:
            lines.append(f"   Source: {source}")
    
    lines.append("</lsp_diagnostics>")
    return "\n".join(lines)


def check_write_with_lsp(
    file_path: str,
    pre_content: Optional[str],
    post_content: Optional[str],
) -> str:
    """完整的 LSP 检查流程：快照 → 获取诊断 → 格式化。
    
    这是最方便的入口函数，封装了完整的 LSP 集成流程。
    
    Args:
        file_path: 文件路径
        pre_content: 写入前的内容
        post_content: 写入后的内容
        
    Returns:
        格式化的诊断字符串，如果没有新增诊断则返回空字符串
    """
    # 1. 快照基线（如果还没有）
    snapshot_before_write(file_path)
    
    # 2. 获取诊断
    diagnostics = get_diagnostics_after_write(
        file_path,
        pre_content=pre_content,
        post_content=post_content,
        delta=True,
    )
    
    # 3. 格式化
    return format_diagnostics(diagnostics)


__all__ = [
    "snapshot_before_write",
    "get_diagnostics_after_write",
    "format_diagnostics",
    "check_write_with_lsp",
]
