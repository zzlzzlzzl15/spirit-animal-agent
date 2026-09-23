"""技能中心路径解析 — Spirit Agent（Phase 4.6）。

对标 Hermes ``tools/skills_hub.py`` 顶部的「按调用解析（而非 import 时冻结）」路径
设计：Hermes 用 ``_skills_dir()`` / ``_hub_dir()`` / ``_lock_file()`` 等函数 + PEP 562
``__getattr__`` 让 profile 覆盖生效；Spirit 简化为**函数式按调用解析**，理由相同——
import 时常量会在单进程多会话 / 测试 monkeypatch ``SPIRIT_HOME`` 时泄漏旧值。

解析优先级（每个目录独立）：

1. 显式环境变量覆盖（``SPIRIT_SKILLS_DIR`` / ``SPIRIT_HUB_DIR`` / ``SPIRIT_BUNDLES_DIR``）；
2. 回退到 ``spirit.config.SPIRIT_HOME``（**按调用读取模块全局**，故测试
   ``monkeypatch.setattr(config, "SPIRIT_HOME", tmp_path)`` 立即生效）。

目录布局（与 Hermes 一致）::

    <SPIRIT_HOME>/skills/                 技能根（本地安装 + 内置）
    <SPIRIT_HOME>/skills/.hub/            市场元数据根
    <SPIRIT_HOME>/skills/.hub/lock.json   已安装技能的来源锁文件
    <SPIRIT_HOME>/skills/.hub/quarantine/ 扫描前的隔离区
    <SPIRIT_HOME>/skills/.hub/audit.jsonl 安装/卸载/隔离审计日志
    <SPIRIT_HOME>/skills/.hub/taps.json   自定义 GitHub 源
    <SPIRIT_HOME>/skills/.hub/index-cache/ 市场索引缓存
    <SPIRIT_HOME>/skills/.hub/usage.json  技能使用追踪
    <SPIRIT_HOME>/skill-bundles/          技能捆绑包（/<bundle> 别名）
"""

from __future__ import annotations

import os
from pathlib import Path

# 技能扫描时跳过的目录（VCS / 依赖 / 缓存 / 市场元数据 / 渐进式披露支持区）。
# 对标 Hermes ``agent/skill_utils.EXCLUDED_SKILL_DIRS``。
EXCLUDED_SKILL_DIRS = frozenset({
    ".git", ".github", ".hub", ".archive", ".venv", "venv",
    "node_modules", "site-packages", "__pycache__", ".tox", ".nox",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".DS_Store",
})

# 渐进式披露支持区：位于技能包内部，经 ``skill_view(file=...)`` 显式加载，
# 不是独立技能的发现根。对标 Hermes ``skill_utils.SKILL_SUPPORT_DIRS``。
SKILL_SUPPORT_DIRS = frozenset({"references", "templates", "assets", "scripts", "examples"})


def spirit_home() -> Path:
    """返回 SPIRIT_HOME（按调用读取 ``spirit.config.SPIRIT_HOME`` 模块全局）。

    延迟导入 ``spirit.config`` 避免包级循环；属性在**调用时**读取，故测试对
    ``config.SPIRIT_HOME`` 的 monkeypatch 会立即反映到所有下游路径函数。
    """
    from spirit import config as _config

    return Path(_config.SPIRIT_HOME).expanduser()


def _env_dir(var: str) -> Path | None:
    raw = os.environ.get(var)
    if raw and raw.strip():
        return Path(raw).expanduser()
    return None


def skills_dir() -> Path:
    """技能根目录（``SPIRIT_SKILLS_DIR`` 覆盖 → ``<home>/skills``）。"""
    return _env_dir("SPIRIT_SKILLS_DIR") or (spirit_home() / "skills")


def hub_dir() -> Path:
    """市场元数据根（``SPIRIT_HUB_DIR`` 覆盖 → ``<skills>/.hub``）。"""
    return _env_dir("SPIRIT_HUB_DIR") or (skills_dir() / ".hub")


def lock_file() -> Path:
    """已安装技能锁文件 ``<hub>/lock.json``。"""
    return hub_dir() / "lock.json"


def quarantine_dir() -> Path:
    """扫描前隔离区 ``<hub>/quarantine``。"""
    return hub_dir() / "quarantine"


def audit_log() -> Path:
    """审计日志 ``<hub>/audit.jsonl``（每行一条 JSON）。"""
    return hub_dir() / "audit.jsonl"


def taps_file() -> Path:
    """自定义源 ``<hub>/taps.json``。"""
    return hub_dir() / "taps.json"


def index_cache_dir() -> Path:
    """市场索引缓存目录 ``<hub>/index-cache``。"""
    return hub_dir() / "index-cache"


def usage_file() -> Path:
    """技能使用追踪 ``<hub>/usage.json``。"""
    return hub_dir() / "usage.json"


def provenance_file() -> Path:
    """技能安装来源记录 ``<hub>/provenance.json``。"""
    return hub_dir() / "provenance.json"


def bundles_dir() -> Path:
    """技能捆绑包目录（``SPIRIT_BUNDLES_DIR`` 覆盖 → ``<home>/skill-bundles``）。

    对标 Hermes ``agent/skill_bundles._bundles_dir`` 的 ``HERMES_BUNDLES_DIR`` 覆盖。
    """
    return _env_dir("SPIRIT_BUNDLES_DIR") or (spirit_home() / "skill-bundles")


__all__ = [
    "EXCLUDED_SKILL_DIRS",
    "SKILL_SUPPORT_DIRS",
    "spirit_home",
    "skills_dir",
    "hub_dir",
    "lock_file",
    "quarantine_dir",
    "audit_log",
    "taps_file",
    "index_cache_dir",
    "usage_file",
    "provenance_file",
    "bundles_dir",
]
