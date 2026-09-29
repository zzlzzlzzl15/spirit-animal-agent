"""Autonomy 沙箱护栏 —— Spirit Agent Phase 7.A（自主探索的安全边界）。

自主探索循环（:mod:`spirit.autonomy.explorer`）在无人值守下运行，必须把**所有写操作**
限制在一个隔离沙箱目录内，绝不允许触碰主代码库或用户文件。本模块提供该护栏：

- **路径 containment**：任何相对路径在写入前都被解析并校验是否落在沙箱根内；
  越界（``..``、绝对路径、符号链接逃逸）一律抛 :class:`SandboxViolation`；
- **读写列三接口**：``write_text`` / ``read_text`` / ``list``，全部以沙箱根为基准；
- **按调用解析根目录**：默认根 = ``SPIRIT_HOME/autonomy``，每次调用读
  ``spirit.config.SPIRIT_HOME``（与 evolution 记忆同范式，无导入期副作用，可注入 ``root``）。

铁律：沙箱只约束**写**；读主代码库是允许的（探索需要读），但写永远只能在沙箱内。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# 沙箱根目录名（位于 SPIRIT_HOME 之下）
SANDBOX_DIRNAME = "autonomy"


class SandboxViolation(PermissionError):
    """自主写操作试图越出沙箱根目录。"""


def _resolve_root(root: Optional[os.PathLike | str] = None) -> Path:
    """解析沙箱根：优先注入 ``root``，否则 ``SPIRIT_HOME/autonomy``（按调用读取）。"""
    if root is not None:
        return Path(root)
    try:
        from spirit.config import SPIRIT_HOME
        base = Path(SPIRIT_HOME)
    except Exception as exc:  # pragma: no cover - 配置不可用时降级到 cwd
        logger.debug("autonomy.sandbox: 读取 SPIRIT_HOME 失败，降级 cwd: %s", exc)
        base = Path.cwd()
    return base / SANDBOX_DIRNAME


class Sandbox:
    """把自主写操作限制在单一隔离目录内的护栏。"""

    def __init__(self, *, root: Optional[os.PathLike | str] = None) -> None:
        self._injected_root = root

    # -- 根目录 -----------------------------------------------------------
    @property
    def root(self) -> Path:
        """沙箱根目录（惰性创建）。"""
        path = _resolve_root(self._injected_root)
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- 路径 containment -------------------------------------------------
    def resolve(self, rel: os.PathLike | str) -> Path:
        """把相对路径解析为沙箱内绝对路径；越界抛 :class:`SandboxViolation`。"""
        base = self.root.resolve()
        candidate = (base / str(rel)).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            raise SandboxViolation(
                f"自主写操作越出沙箱: {rel!r} → {candidate}（根={base}）"
            ) from None
        return candidate

    def _is_within(self, rel: os.PathLike | str) -> bool:
        try:
            self.resolve(rel)
            return True
        except SandboxViolation:
            return False

    # -- 写（受护栏）------------------------------------------------------
    def write_text(self, rel: os.PathLike | str, text: str) -> Path:
        """在沙箱内写文本文件（自动建父目录）；越界抛 SandboxViolation。"""
        target = self.resolve(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    # -- 读 / 列 ----------------------------------------------------------
    def read_text(self, rel: os.PathLike | str) -> str:
        target = self.resolve(rel)
        return target.read_text(encoding="utf-8")

    def exists(self, rel: os.PathLike | str) -> bool:
        return self.resolve(rel).exists()

    def list(self, rel: os.PathLike | str = "") -> List[str]:
        """列出沙箱内某目录下的相对路径（递归，文件）。"""
        base = self.resolve(rel)
        if not base.exists():
            return []
        if base.is_file():
            return [str(rel)]
        out: List[str] = []
        for path in sorted(base.rglob("*")):
            if path.is_file():
                out.append(path.relative_to(self.root.resolve()).as_posix())
        return out


__all__ = ["Sandbox", "SandboxViolation", "SANDBOX_DIRNAME"]
