"""工作区与项目根解析。

职责：
1. **工作区门控** — 检测文件是否在 git 仓库内，非 git 目录不启动 LSP
2. **项目根查找** — 从文件路径向上查找最近的项目根标记
   （pyproject.toml / Cargo.toml / go.mod 等）
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable, Optional, Tuple

logger = logging.getLogger("spirit.lsp.workspace")

# 缓存: cwd → (worktree_root, is_git)
_workspace_cache: dict = {}


def normalize_path(path: str) -> str:
    """规范化路径作为稳定的缓存 key。"""
    return os.path.abspath(os.path.expanduser(path))


def find_git_worktree(start: str) -> Optional[str]:
    """从 start 向上查找 .git 标记。

    Returns:
        git 仓库根目录路径，或 None。
    """
    try:
        start_path = Path(normalize_path(start))
        if start_path.is_file():
            start_path = start_path.parent
    except (OSError, RuntimeError, ValueError):
        return None

    cached = _workspace_cache.get(str(start_path))
    if cached is not None:
        root, _is_git = cached
        return root

    cur = start_path
    for _ in range(64):  # 防止路径层级过深或循环
        git_marker = cur / ".git"
        try:
            if git_marker.exists():
                resolved = str(cur)
                _workspace_cache[str(start_path)] = (resolved, True)
                return resolved
        except OSError:
            return None

        parent = cur.parent
        if parent == cur:
            break
        cur = parent

    _workspace_cache[str(start_path)] = (None, False)
    return None


def nearest_root(start: str, markers: Iterable[str],
                  exclude_markers: Iterable[str] = ()) -> Optional[str]:
    """从 start 向上查找包含指定标记文件的最近目录。

    Args:
        start: 起始路径
        markers: 项目根标记文件名列表（如 ["pyproject.toml", "setup.py"]）
        exclude_markers: 排除标记（如 ["node_modules"]，遇到即停止）

    Returns:
        项目根目录路径，或 None。
    """
    try:
        cur = Path(normalize_path(start))
        if cur.is_file():
            cur = cur.parent
    except (OSError, RuntimeError, ValueError):
        return None

    marker_set = set(markers)
    exclude_set = set(exclude_markers)

    for _ in range(64):
        # 检查排除标记
        for em in exclude_set:
            if (cur / em).exists():
                return None

        # 检查包含标记
        for m in marker_set:
            if (cur / m).exists():
                return str(cur)

        parent = cur.parent
        if parent == cur:
            break
        cur = parent

    return None


def clear_cache() -> None:
    """清除工作区缓存（关闭时调用）。"""
    _workspace_cache.clear()


__all__ = [
    "normalize_path",
    "find_git_worktree",
    "nearest_root",
    "clear_cache",
]
