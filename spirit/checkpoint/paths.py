"""检查点路径解析 —— Spirit Agent。

遵循 Spirit 的**按调用解析路径**约定（对齐 :mod:`spirit.profile.paths` 与
:mod:`spirit.skills_hub.paths`）：每次调用时读 ``spirit.config.SPIRIT_HOME``，
故测试 monkeypatch SPIRIT_HOME 立即反映到下游，且无导入期副作用（不建目录）。

存储布局（``<SPIRIT_HOME>/checkpoints/``）::

    checkpoints/
      blobs/<sha256>      # 内容寻址的文件内容（跨快照去重）
      index.json          # 快照索引（原子写）
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _spirit_home() -> Path:
    """按调用读取 ``spirit.config.SPIRIT_HOME``（延迟导入避免循环）。"""
    try:
        from spirit import config
        home = getattr(config, "SPIRIT_HOME", None)
        if home:
            return Path(home)
    except Exception as exc:  # pragma: no cover - 配置不可用时降级
        logger.debug("checkpoint.paths: 读取 SPIRIT_HOME 失败: %s", exc)
    return Path("~/.spirit").expanduser()


def checkpoints_root() -> Path:
    """检查点存储根目录 ``<SPIRIT_HOME>/checkpoints``。"""
    return _spirit_home() / "checkpoints"


def blobs_dir() -> Path:
    """内容寻址 blob 目录 ``<SPIRIT_HOME>/checkpoints/blobs``。"""
    return checkpoints_root() / "blobs"


def blob_path(sha256: str) -> Path:
    """单个 blob 的路径。"""
    return blobs_dir() / sha256


def index_path() -> Path:
    """快照索引 ``<SPIRIT_HOME>/checkpoints/index.json``。"""
    return checkpoints_root() / "index.json"


def ensure_dirs() -> None:
    """惰性创建存储目录（仅在真正写入前调用，非导入期）。"""
    blobs_dir().mkdir(parents=True, exist_ok=True)


__all__ = [
    "checkpoints_root",
    "blobs_dir",
    "blob_path",
    "index_path",
    "ensure_dirs",
]
