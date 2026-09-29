"""Profile 路径解析 — Spirit Agent（Phase 4.2）。

对标 Hermes ``hermes_cli/profiles.py`` 顶部的 ``_get_profiles_root`` /
``_get_default_hermes_home`` / ``_get_active_profile_path`` / ``get_profile_dir``
等路径函数，按 Spirit 约定收拢为一个纯路径层。

设计要点（对齐 ``skills_hub/paths.py``）：**按调用解析**，而非 import 时冻结。
所有函数在调用时读取 ``spirit.config.SPIRIT_HOME`` 模块全局，故测试里
``monkeypatch.setattr(config, "SPIRIT_HOME", tmp_path)`` 会立即反映到所有下游路径。

Profile 模型（与 Hermes 一致）::

    <root>/                       默认 profile（"default"），即 SPIRIT_HOME 本身
    <root>/active_profile         粘性活跃 profile 名（纯文本，缺失=default）
    <root>/profiles/<name>/       命名 profile —— 各自独立的 HOME 目录

其中 ``<root>`` 由 :func:`default_root` 稳定解析：**即使当前 SPIRIT_HOME 已指向
某个 profile 目录（``<root>/profiles/<name>``），也会爬回 ``<root>``**，保证
``profiles_root`` 不随活跃 profile 漂移（对标 Hermes ``get_default_hermes_root``
独立于 ``get_hermes_home`` 的设计）。

激活语义：Spirit 在 import 时从环境读一次 ``SPIRIT_HOME``，故真正的进程级激活需
带新环境重启（:func:`resolve_profile_env` 给出子进程环境）；:func:`apply_active_profile`
提供**运行时进程内切换**（直接改写 ``config.SPIRIT_HOME`` 模块全局）作为便捷路径。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

__all__ = [
    "default_root",
    "profiles_root",
    "active_profile_path",
    "profile_dir",
    "resolve_profile_env",
    "apply_active_profile",
]


def _spirit_home() -> Path:
    """按调用读取 ``spirit.config.SPIRIT_HOME``（延迟导入避免包级循环）。"""
    from spirit import config as _config

    return Path(_config.SPIRIT_HOME).expanduser()


def default_root() -> Path:
    """返回稳定的 Spirit 根目录（不随活跃 profile 变化）。

    若当前 SPIRIT_HOME 恰好是某个 profile 目录（``<root>/profiles/<name>``），
    则爬回 ``<root>``；否则原样返回。这样 ``profiles_root()`` 恒等于
    ``<root>/profiles``，无论此刻哪个 profile 处于活跃态。
    """
    home = _spirit_home()
    # 仅当形如 <...>/profiles/<name> 时才上爬两级（parent.name == "profiles"）。
    if home.parent.name == "profiles" and home.name:
        return home.parent.parent
    return home


def profiles_root() -> Path:
    """命名 profile 的存放根：``<root>/profiles``。"""
    return default_root() / "profiles"


def active_profile_path() -> Path:
    """粘性活跃 profile 文件：``<root>/active_profile``。"""
    return default_root() / "active_profile"


def profile_dir(name: str) -> Path:
    """把 profile 名解析为其 HOME 目录。

    ``"default"`` → 根目录本身（向后兼容，零迁移）；其余 → ``<root>/profiles/<name>``。
    名字规范化/校验由 :mod:`spirit.profile.manager` 负责，本函数只做路径拼接。
    """
    if name == "default":
        return default_root()
    return profiles_root() / name


def resolve_profile_env(name: str, base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """返回在指定 profile 下派生子进程所需的环境（``SPIRIT_HOME`` 指向该 profile）。

    对标 Hermes ``resolve_profile_env``：不改动当前进程环境，只产出一份可交给
    ``subprocess`` 的 env 副本。``base_env`` 缺省为 ``os.environ``。
    """
    env = dict(base_env if base_env is not None else os.environ)
    env["SPIRIT_HOME"] = str(profile_dir(name))
    return env


def apply_active_profile(name: str) -> Path:
    """运行时进程内切换：把 ``config.SPIRIT_HOME`` 改写为指定 profile 目录。

    返回切换后的 HOME 路径。用于 CLI ``/profile use`` 后无需重启即在当前进程生效
    （对标 Hermes 通过重执行 + 环境注入的激活，Spirit 简化为直接改模块全局）。

    .. note:: 已在 import 时缓存了旧 SPIRIT_HOME 的下游模块（若有）不会自动感知；
       Spirit 的路径层（skills_hub/paths 等）均按调用解析，故切换后立即生效。
    """
    from spirit import config as _config

    target = profile_dir(name)
    _config.SPIRIT_HOME = target
    return target
