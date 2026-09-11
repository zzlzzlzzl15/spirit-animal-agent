"""Memora 自动保存钩子 — 文件生成时自动存入知识库。

监听工具执行结果，当检测到文件生成/导出操作时，
自动将文件内容上传到 Memora 知识库。

触发条件：
- 文件扩展名为 .md / .txt / .html / .json
- 文件大小 > 100 bytes（过滤空文件）
- 非临时目录（排除 /tmp, .cache, __pycache__ 等）

集成到 HookManager，监听 after_tool_execute 事件。
"""

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from spirit.hooks.hook_manager import HookEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

# 值得保存到知识库的文件扩展名
SAVABLE_EXTENSIONS: Set[str] = {
    ".md", ".txt", ".html", ".json", ".rst", ".csv", ".yaml", ".yml",
}

# 排除的目录模式（使用路径分隔符确保匹配完整目录段）
EXCLUDED_DIR_PATTERNS: List[str] = [
    "/tmp/", "\\tmp\\", "/tmp\\", "\\tmp/",
    ".cache", "__pycache__", ".git", "node_modules",
    ".spirit/cache", ".spirit/tmp",
]

from spirit.config import get_config_value

# 最小/最大文件大小（bytes，来自集中式配置）
MIN_FILE_SIZE = get_config_value("memora.min_file_size", 100)

# 最大文件大小（bytes）— 过大的文件不自动保存
MAX_FILE_SIZE = get_config_value("memora.max_file_size", 5 * 1024 * 1024)  # 5MB

# 需要监听的文件生成工具
TARGET_TOOLS: Set[str] = {
    "export_to_file",
    "create_file",
    "write_file",
    "write_content",
    "save_file",
    "session_recap",
}

# 自动去重：已保存过的文件路径（避免重复上传）
_saved_paths: Set[str] = set()
_saved_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 自动保存逻辑
# ---------------------------------------------------------------------------

def _should_save_file(file_path: str) -> bool:
    """判断文件是否值得自动保存到知识库。"""
    if not file_path:
        return False

    path = Path(file_path)

    # 扩展名检查
    if path.suffix.lower() not in SAVABLE_EXTENSIONS:
        return False

    # 文件存在性
    if not path.exists():
        return False

    # 文件大小检查
    try:
        size = path.stat().st_size
        if size < MIN_FILE_SIZE or size > MAX_FILE_SIZE:
            return False
    except OSError:
        return False

    # 排除目录检查
    path_str = str(path).lower()
    for pattern in EXCLUDED_DIR_PATTERNS:
        if pattern.lower() in path_str:
            return False

    # 去重检查
    with _saved_lock:
        resolved = str(path.resolve())
        if resolved in _saved_paths:
            return False
        _saved_paths.add(resolved)

    return True


def _extract_file_path(tool_name: str, args: Dict, result: str) -> Optional[str]:
    """从工具调用参数或结果中提取文件路径。"""
    # 1. 从参数中直接提取
    for key in ("file_path", "path", "output_path", "filename"):
        if key in args and args[key]:
            return str(args[key])

    # 2. 从结果字符串中提取（尝试 JSON 解析）
    if result:
        try:
            import json
            data = json.loads(result)
            if isinstance(data, dict):
                for key in ("file_path", "path", "output_path", "filename", "file"):
                    if key in data and data[key]:
                        return str(data[key])
        except (json.JSONDecodeError, TypeError):
            pass

        # 3. 尝试从结果文本中提取路径模式
        import re
        # 匹配 "saved to /path/to/file" 或 "文件已保存: /path"
        patterns = [
            r"(?:saved to|保存[到:])\s+['\"]?([^\s'\"]+\.(?:md|txt|html|json|rst|csv))['\"]?",
            r"['\"]((?:/[^\"]+|[A-Z]:\\[^\"]+)\.(?:md|txt|html|json|rst|csv))['\"]",
        ]
        for pattern in patterns:
            match = re.search(pattern, result, re.IGNORECASE)
            if match:
                return match.group(1)

    return None


def _generate_title(file_path: str) -> str:
    """从文件路径生成知识库标题。"""
    path = Path(file_path)
    return f"[Auto] {path.stem}"


def _generate_tags(file_path: str) -> List[str]:
    """根据文件属性生成标签。"""
    path = Path(file_path)
    tags = ["spirit-agent", "auto-save"]

    # 按扩展名分类
    ext_tags = {
        ".md": "markdown",
        ".txt": "text",
        ".html": "html",
        ".json": "data",
        ".csv": "data",
        ".yaml": "config",
        ".yml": "config",
    }
    tag = ext_tags.get(path.suffix.lower())
    if tag:
        tags.append(tag)

    return tags


# ---------------------------------------------------------------------------
# 钩子处理器
# ---------------------------------------------------------------------------

def on_tool_complete(
    tool_name: str = "",
    args: Dict = None,
    result: str = "",
    **kwargs,
) -> None:
    """after_tool_execute 事件处理器。

    检查工具执行结果，如果是文件生成操作则异步上传到 Memora。
    """
    args = args or {}

    # 快速过滤：非目标工具跳过
    if tool_name not in TARGET_TOOLS:
        return

    # 提取文件路径
    file_path = _extract_file_path(tool_name, args, result)
    if not file_path:
        return

    # 检查是否值得保存
    if not _should_save_file(file_path):
        return

    # 异步上传（不阻塞主流程）
    _schedule_upload(file_path)


def _schedule_upload(file_path: str) -> None:
    """调度异步上传到 Memora。"""
    def _upload_worker():
        """在后台线程中执行上传。"""
        try:
            from spirit.skills.memora_client import get_memora_client

            async def _do_upload():
                client = get_memora_client()
                if not await client.health_check():
                    logger.debug("Memora 不可用，跳过自动保存: %s", file_path)
                    return

                path = Path(file_path)
                title = _generate_title(file_path)
                tags = _generate_tags(file_path)

                if path.suffix.lower() in {".md", ".txt", ".rst", ".csv", ".yaml", ".yml"}:
                    # 纯文本文件直接读取内容保存
                    try:
                        content = path.read_text(encoding="utf-8", errors="replace")
                        result = await client.create_text(title, content, tags=tags)
                    except Exception as e:
                        logger.warning("自动保存文本失败: %s — %s", file_path, e)
                        return
                else:
                    # 二进制文件（HTML 等）上传文件
                    result = await client.upload_file(file_path, title=title, tags=tags)

                if result.get("status") == "success":
                    logger.info(
                        "自动保存到知识库: %s → %s",
                        file_path, result.get("document_id", "?"),
                    )
                else:
                    logger.warning(
                        "自动保存失败: %s — %s",
                        file_path, result.get("error", "未知错误"),
                    )

            asyncio.run(_do_upload())
        except Exception as e:
            logger.debug("自动保存线程异常: %s", e)

    thread = threading.Thread(
        target=_upload_worker,
        name=f"memora-autosave-{Path(file_path).name[:20]}",
        daemon=True,
    )
    thread.start()


# ---------------------------------------------------------------------------
# 注册到 HookManager
# ---------------------------------------------------------------------------

def register_auto_save_hook(hook_manager) -> None:
    """将自动保存钩子注册到 HookManager。

    Args:
        hook_manager: HookManager 实例
    """
    hook_manager.register(
        event=HookEvent.AFTER_TOOL_EXECUTE,
        handler=on_tool_complete,
        priority=200,  # 低优先级，在其他处理器之后执行
    )
    logger.debug("Memora 自动保存钩子已注册")


def clear_saved_paths() -> None:
    """清除已保存路径记录（用于测试）。"""
    with _saved_lock:
        _saved_paths.clear()
