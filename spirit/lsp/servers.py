"""语言服务器注册表 — 每种语言对应的 LSP 服务器定义。

每个 ServerDef 知道如何：
- 按文件扩展名匹配
- 从文件路径解析项目根
- 组装启动命令（二进制、参数、环境变量、工作目录）
- 计算 LSP initializationOptions

服务器定义随包发布，但只在用户实际编辑对应语言文件时才启动。
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from spirit.lsp.workspace import nearest_root

logger = logging.getLogger("spirit.lsp.servers")

# 文件扩展名 → LSP 语言 ID
LANGUAGE_BY_EXT: Dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescriptreact",
    ".js": "javascript",
    ".jsx": "javascriptreact",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hxx": "cpp",
    ".cs": "csharp",
    ".java": "java",
    ".kt": "kotlin",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".jsonc": "jsonc",
    ".sh": "shellscript",
    ".bash": "shellscript",
    ".html": "html",
    ".css": "css",
    ".vue": "vue",
    ".svelte": "svelte",
    ".lua": "lua",
    ".php": "php",
    ".dart": "dart",
}


def language_id_for(file_path: str) -> str:
    """根据文件扩展名返回 LSP 语言 ID。"""
    from pathlib import Path
    ext = Path(file_path).suffix.lower()
    return LANGUAGE_BY_EXT.get(ext, "plaintext")


# ---------------------------------------------------------------------------
# ServerDef — 单个语言服务器的定义
# ---------------------------------------------------------------------------

@dataclass
class ServerDef:
    """语言服务器定义。"""
    server_id: str                    # 唯一标识（如 "pyright"）
    language_ids: List[str]           # 服务的语言（如 ["python"]）
    binary: str                       # 可执行文件名
    args: List[str] = field(default_factory=list)  # 启动参数
    project_markers: List[str] = field(default_factory=list)  # 项目根标记
    env_overrides: Dict[str, str] = field(default_factory=dict)  # 额外环境变量
    init_options: Dict[str, Any] = field(default_factory=dict)  # initializationOptions
    install_strategy: str = "auto"    # auto | manual | off
    install_package: str = ""         # 安装时的包名
    extra_packages: List[str] = field(default_factory=list)  # 附带安装的包

    def matches_file(self, file_path: str) -> bool:
        """检查此服务器是否服务该文件。"""
        lang = language_id_for(file_path)
        return lang in self.language_ids

    def resolve_root(self, file_path: str) -> Optional[str]:
        """解析该文件的项目根目录。"""
        if not self.project_markers:
            # 无标记 → 使用 git 根
            from spirit.lsp.workspace import find_git_worktree
            return find_git_worktree(file_path)
        return nearest_root(file_path, self.project_markers)

    def build_command(self, root: str) -> List[str]:
        """组装启动命令。"""
        return [self.binary] + self.args

    def build_env(self) -> Dict[str, str]:
        """组装环境变量。"""
        env = os.environ.copy()
        env.update(self.env_overrides)
        return env


# ---------------------------------------------------------------------------
# 内置服务器注册表
# ---------------------------------------------------------------------------

BUILTIN_SERVERS: List[ServerDef] = [
    # Python — pyright
    ServerDef(
        server_id="pyright",
        language_ids=["python"],
        binary="pyright-langserver",
        args=["--stdio"],
        project_markers=["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"],
        install_strategy="npm",
        install_package="pyright",
    ),
    # TypeScript / JavaScript
    ServerDef(
        server_id="typescript-language-server",
        language_ids=["typescript", "typescriptreact", "javascript", "javascriptreact"],
        binary="typescript-language-server",
        args=["--stdio"],
        project_markers=["tsconfig.json", "jsconfig.json", "package.json"],
        install_strategy="npm",
        install_package="typescript-language-server",
        extra_packages=["typescript"],
    ),
    # Go
    ServerDef(
        server_id="gopls",
        language_ids=["go"],
        binary="gopls",
        args=["serve"],
        project_markers=["go.mod", "go.sum"],
        install_strategy="manual",
        install_package="gopls",
    ),
    # Rust
    ServerDef(
        server_id="rust-analyzer",
        language_ids=["rust"],
        binary="rust-analyzer",
        args=[],
        project_markers=["Cargo.toml", "Cargo.lock"],
        install_strategy="manual",
        install_package="rust-analyzer",
    ),
    # YAML
    ServerDef(
        server_id="yaml-language-server",
        language_ids=["yaml"],
        binary="yaml-language-server",
        args=["--stdio"],
        project_markers=[".git"],
        install_strategy="npm",
        install_package="yaml-language-server",
    ),
    # JSON
    ServerDef(
        server_id="vscode-json-language-server",
        language_ids=["json", "jsonc"],
        binary="vscode-json-language-server",
        args=["--stdio"],
        project_markers=[".git"],
        install_strategy="npm",
        install_package="vscode-langservers-extracted",
    ),
    # HTML / CSS
    ServerDef(
        server_id="vscode-html-language-server",
        language_ids=["html"],
        binary="vscode-html-language-server",
        args=["--stdio"],
        project_markers=[".git"],
        install_strategy="npm",
        install_package="vscode-langservers-extracted",
    ),
    ServerDef(
        server_id="vscode-css-language-server",
        language_ids=["css"],
        binary="vscode-css-language-server",
        args=["--stdio"],
        project_markers=[".git"],
        install_strategy="npm",
        install_package="vscode-langservers-extracted",
    ),
    # Lua
    ServerDef(
        server_id="lua-language-server",
        language_ids=["lua"],
        binary="lua-language-server",
        args=[],
        project_markers=[".luarc.json", ".luacheckrc", ".git"],
        install_strategy="manual",
        install_package="lua-language-server",
    ),
]

# server_id → ServerDef 快速索引
_SERVER_MAP: Dict[str, ServerDef] = {s.server_id: s for s in BUILTIN_SERVERS}


def find_server_for_file(file_path: str,
                         preferred: Optional[str] = None) -> Optional[ServerDef]:
    """为文件找到合适的语言服务器。

    Args:
        file_path: 文件路径
        preferred: 优先使用的 server_id（如果匹配）

    Returns:
        匹配的 ServerDef，或 None。
    """
    if preferred:
        srv = _SERVER_MAP.get(preferred)
        if srv and srv.matches_file(file_path):
            return srv

    for srv in BUILTIN_SERVERS:
        if srv.matches_file(file_path):
            return srv

    return None


def get_server(server_id: str) -> Optional[ServerDef]:
    """按 ID 获取服务器定义。"""
    return _SERVER_MAP.get(server_id)


def is_binary_available(server_id: str) -> bool:
    """检查服务器二进制是否在 PATH 上。"""
    srv = _SERVER_MAP.get(server_id)
    if not srv:
        return False
    return shutil.which(srv.binary) is not None


__all__ = [
    "ServerDef",
    "BUILTIN_SERVERS",
    "language_id_for",
    "find_server_for_file",
    "get_server",
    "is_binary_available",
]
