"""LSP 服务器二进制自动安装。

尝试用合适的包管理器安装缺失的服务器二进制文件。
所有安装目标: ``<SPIRIT_HOME>/lsp/bin/``，不污染用户全局环境。

策略：
- ``auto`` — 用最佳包管理器安装（默认）
- ``manual`` — 不安装，缺失时静默跳过
- ``off`` — 同 manual

安装失败是非致命的：返回 None，工具层回退到内置语法检查。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("spirit.lsp.install")

# 安装配方: server_id → {strategy, pkg, bin, extra_pkgs}
INSTALL_RECIPES: Dict[str, Dict[str, Any]] = {
    # Python
    "pyright": {
        "strategy": "npm",
        "pkg": "pyright",
        "bin": "pyright-langserver",
    },
    # JS/TS
    "typescript-language-server": {
        "strategy": "npm",
        "pkg": "typescript-language-server",
        "bin": "typescript-language-server",
        "extra_pkgs": ["typescript"],
    },
    # YAML / JSON / HTML / CSS（打包在 vscode-langservers-extracted 中）
    "yaml-language-server": {
        "strategy": "npm",
        "pkg": "yaml-language-server",
        "bin": "yaml-language-server",
    },
    "vscode-json-language-server": {
        "strategy": "npm",
        "pkg": "vscode-langservers-extracted",
        "bin": "vscode-json-language-server",
    },
    "vscode-html-language-server": {
        "strategy": "npm",
        "pkg": "vscode-langservers-extracted",
        "bin": "vscode-html-language-server",
    },
    "vscode-css-language-server": {
        "strategy": "npm",
        "pkg": "vscode-langservers-extracted",
        "bin": "vscode-css-language-server",
    },
    # Go — 需要手动安装
    "gopls": {
        "strategy": "manual",
        "pkg": "gopls",
        "bin": "gopls",
        "hint": "go install golang.org/x/tools/gopls@latest",
    },
    # Rust — 需要手动安装
    "rust-analyzer": {
        "strategy": "manual",
        "pkg": "rust-analyzer",
        "bin": "rust-analyzer",
        "hint": "rustup component add rust-analyzer",
    },
    # Lua — 需要手动安装
    "lua-language-server": {
        "strategy": "manual",
        "pkg": "lua-language-server",
        "bin": "lua-language-server",
        "hint": "参考 https://github.com/LuaLS/lua-language-server",
    },
}

# 安装锁 — 防止并发安装同一包
_install_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _get_staging_dir() -> Path:
    """获取安装暂存目录。"""
    from spirit.config import SPIRIT_HOME
    staging = SPIRIT_HOME / "lsp" / "bin"
    staging.mkdir(parents=True, exist_ok=True)
    return staging


def _get_lock(server_id: str) -> threading.Lock:
    """获取安装锁。"""
    with _locks_guard:
        if server_id not in _install_locks:
            _install_locks[server_id] = threading.Lock()
        return _install_locks[server_id]


def _find_npm() -> Optional[str]:
    """查找 npm 可执行文件。"""
    return shutil.which("npm") or shutil.which("npx")


def _npm_install(recipe: Dict[str, Any], staging: Path) -> bool:
    """通过 npm 安装。"""
    npm = _find_npm()
    if not npm:
        logger.warning("npm 未安装，无法自动安装 LSP 服务器")
        return False

    pkg = recipe["pkg"]
    extra = recipe.get("extra_pkgs", [])
    all_pkgs = [pkg] + extra

    try:
        # 在 staging 目录初始化 node_modules
        if not (staging / "node_modules").exists():
            subprocess.run(
                [npm, "init", "-y"],
                cwd=str(staging),
                capture_output=True,
                timeout=30,
            )

        # 安装包
        cmd = [npm, "install"] + all_pkgs
        result = subprocess.run(
            cmd,
            cwd=str(staging),
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            logger.warning("npm install 失败: %s", result.stderr[:500])
            return False

        # 验证二进制存在
        bin_name = recipe["bin"]
        bin_path = staging / "node_modules" / ".bin" / bin_name
        if not bin_path.exists():
            # 也检查 node_modules/pkg/bin/
            bin_path2 = staging / "node_modules" / pkg / "bin"
            if bin_path2.exists():
                return True
            logger.warning("安装完成但未找到二进制: %s", bin_name)
            return False

        logger.info("LSP 服务器已安装: %s → %s", pkg, bin_path)
        return True

    except subprocess.TimeoutExpired:
        logger.warning("npm install 超时: %s", pkg)
        return False
    except Exception as e:
        logger.warning("npm install 异常: %s", e)
        return False


def try_install(server_id: str) -> Optional[str]:
    """尝试安装 LSP 服务器二进制。

    Args:
        server_id: 服务器 ID

    Returns:
        安装后的二进制路径，或 None（失败/不需要安装）。
    """
    recipe = INSTALL_RECIPES.get(server_id)
    if not recipe:
        logger.debug("无安装配方: %s", server_id)
        return None

    strategy = recipe.get("strategy", "manual")
    if strategy in ("manual", "off"):
        hint = recipe.get("hint", "")
        if hint:
            logger.info("LSP 服务器 %s 需手动安装: %s", server_id, hint)
        return None

    # 先检查是否已在 PATH 上
    bin_name = recipe["bin"]
    existing = shutil.which(bin_name)
    if existing:
        return existing

    # 加锁安装
    lock = _get_lock(server_id)
    with lock:
        # 再次检查（可能在等锁期间被另一个线程安装了）
        existing = shutil.which(bin_name)
        if existing:
            return existing

        staging = _get_staging_dir()

        # 检查 staging 中是否已安装
        staging_bin = staging / "node_modules" / ".bin" / bin_name
        if staging_bin.exists():
            return str(staging_bin)

        if strategy == "npm":
            success = _npm_install(recipe, staging)
            if success and staging_bin.exists():
                return str(staging_bin)

    return None


def get_resolved_binary(server_id: str) -> Optional[str]:
    """解析服务器二进制的完整路径。

    优先 PATH，然后 staging 目录。
    """
    from spirit.lsp.servers import get_server
    srv = get_server(server_id)
    if not srv:
        return None

    # 1. PATH 查找
    on_path = shutil.which(srv.binary)
    if on_path:
        return on_path

    # 2. staging 目录
    staging = _get_staging_dir()
    staging_bin = staging / "node_modules" / ".bin" / srv.binary
    if staging_bin.exists():
        return str(staging_bin)

    return None


__all__ = ["try_install", "get_resolved_binary", "INSTALL_RECIPES"]
