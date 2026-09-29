"""Spirit 检查点系统 —— 内容寻址文件快照 / 回滚。

对标 Hermes ``tools/checkpoint_manager.py``（git shadow-repo），按 Spirit「精简可测试
子集」约定改用**内容寻址 blob 去重 store**（sha256，纯标准库、无 git 依赖、可离线测）。

三个协作件：

- :mod:`spirit.checkpoint.paths` —— SPIRIT_HOME 感知的路径解析（按调用读 config）。
- :mod:`spirit.checkpoint.manager` —— :class:`CheckpointManager`：快照 CRUD / restore /
  diff / cleanup + blob 去重 + 垃圾回收。
- :mod:`spirit.checkpoint.commands` —— 传输无关的 ``/checkpoint`` 命令分发。

一站式::

    from spirit.checkpoint import get_manager, handle_checkpoint_command
    mgr = get_manager()
    mgr.create(["/path/to/file.py"], description="重构前")
    result = handle_checkpoint_command("list")

全局 manager 惰性创建（``get_manager()``），**无导入期副作用**（不建目录）。
"""

import logging
from typing import Optional

from spirit.checkpoint.commands import handle_checkpoint_command
from spirit.checkpoint.manager import CheckpointManager
from spirit.checkpoint.paths import (
    blob_path,
    blobs_dir,
    checkpoints_root,
    index_path,
)

logger = logging.getLogger(__name__)

_manager: Optional[CheckpointManager] = None


def get_manager() -> CheckpointManager:
    """返回进程级惰性单例 :class:`CheckpointManager`。"""
    global _manager
    if _manager is None:
        _manager = CheckpointManager()
    return _manager


def reset_manager() -> None:
    """重置全局单例（测试切换 SPIRIT_HOME 后可调用以清缓存）。"""
    global _manager
    _manager = None


__all__ = [
    "CheckpointManager",
    "get_manager",
    "reset_manager",
    "handle_checkpoint_command",
    "checkpoints_root",
    "blobs_dir",
    "blob_path",
    "index_path",
]
