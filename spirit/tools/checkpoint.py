"""文件快照/回滚工具 — checkpoint（自动保存 + 一键回滚）。

参考 Hermes 的 tools/checkpoint_manager.py 设计：
- 在文件修改前自动创建快照
- 支持回滚到任意快照点
- 使用 git 作为底层存储（共享对象去重）
"""

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)

# 快照存储目录
CHECKPOINT_DIR = Path.home() / ".spirit" / "checkpoints"


# ---------------------------------------------------------------------------
# 快照管理
# ---------------------------------------------------------------------------

class CheckpointManager:
    """文件快照管理器。

    使用简化版设计：直接在 ~/.spirit/checkpoints/ 下按时间戳存储文件副本。
    （Hermes 使用共享 git store，更复杂但更高效）
    """

    def __init__(self):
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        self._checkpoints: List[Dict[str, Any]] = []
        self._load_index()

    def _index_path(self) -> Path:
        return CHECKPOINT_DIR / "index.json"

    def _load_index(self):
        """加载快照索引。"""
        if self._index_path().exists():
            try:
                data = json.loads(self._index_path().read_text(encoding="utf-8"))
                self._checkpoints = data.get("checkpoints", [])
            except Exception:
                self._checkpoints = []

    def _save_index(self):
        """保存快照索引。"""
        data = {"checkpoints": self._checkpoints}
        self._index_path().write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def create(self, paths: List[str], description: str = "") -> Dict:
        """创建快照 — 保存指定文件的当前内容。"""
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        checkpoint_id = f"cp_{timestamp}"
        checkpoint_dir = CHECKPOINT_DIR / checkpoint_id
        checkpoint_dir.mkdir(exist_ok=True)

        saved_files = []
        for filepath in paths:
            src = Path(filepath).resolve()
            if not src.exists() or not src.is_file():
                continue

            # 保持相对路径结构
            rel = src.name
            dst = checkpoint_dir / rel
            try:
                shutil.copy2(str(src), str(dst))
                saved_files.append({
                    "original": str(src),
                    "backup": str(dst),
                    "size": src.stat().st_size,
                })
            except Exception as e:
                logger.warning("快照失败 %s: %s", src, e)

        if not saved_files:
            return {"success": False, "error": "没有文件被快照"}

        checkpoint = {
            "id": checkpoint_id,
            "timestamp": timestamp,
            "description": description,
            "files": saved_files,
            "file_count": len(saved_files),
        }
        self._checkpoints.append(checkpoint)
        self._save_index()

        return {"success": True, "checkpoint": checkpoint}

    def list_checkpoints(self, limit: int = 10) -> List[Dict]:
        """列出最近的快照。"""
        return list(reversed(self._checkpoints[-limit:]))

    def restore(self, checkpoint_id: str) -> Dict:
        """回滚到指定快照。"""
        checkpoint = None
        for cp in self._checkpoints:
            if cp["id"] == checkpoint_id:
                checkpoint = cp
                break

        if not checkpoint:
            return {"success": False, "error": f"快照不存在: {checkpoint_id}"}

        restored = []
        for file_info in checkpoint["files"]:
            backup = Path(file_info["backup"])
            original = Path(file_info["original"])

            if not backup.exists():
                continue

            try:
                # 先备份当前版本（安全网）
                if original.exists():
                    safety = original.with_suffix(original.suffix + ".pre-rollback")
                    shutil.copy2(str(original), str(safety))

                # 恢复
                shutil.copy2(str(backup), str(original))
                restored.append(str(original))
            except Exception as e:
                logger.warning("回滚失败 %s: %s", original, e)

        return {
            "success": True,
            "checkpoint_id": checkpoint_id,
            "restored_files": restored,
            "count": len(restored),
        }

    def cleanup(self, keep: int = 20):
        """清理旧快照，保留最近 N 个。"""
        if len(self._checkpoints) <= keep:
            return

        to_remove = self._checkpoints[:-keep]
        for cp in to_remove:
            cp_dir = CHECKPOINT_DIR / cp["id"]
            if cp_dir.exists():
                shutil.rmtree(str(cp_dir), ignore_errors=True)

        self._checkpoints = self._checkpoints[-keep:]
        self._save_index()


# 全局实例
_checkpoint_manager = CheckpointManager()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

CHECKPOINT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "checkpoint",
        "description": (
            "文件快照管理 — 修改前保存安全副本，支持一键回滚。\n\n"
            "用法：\n"
            "- action=snapshot: 为指定文件创建快照\n"
            "- action=list: 列出所有快照\n"
            "- action=restore: 回滚到指定快照\n\n"
            "在进行大规模修改前使用 snapshot，出错时可以 restore。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "操作: snapshot / list / restore",
                    "enum": ["snapshot", "list", "restore"],
                },
                "files": {
                    "type": "array",
                    "description": "要快照的文件列表（snapshot 时必填）",
                    "items": {"type": "string"},
                },
                "checkpoint_id": {
                    "type": "string",
                    "description": "快照 ID（restore 时必填）",
                },
                "description": {
                    "type": "string",
                    "description": "快照描述",
                },
            },
            "required": ["action"],
        },
    },
}


def _checkpoint_impl(
    action: str = "list",
    files: List[str] = None,
    checkpoint_id: str = None,
    description: str = "",
) -> str:
    if action == "snapshot":
        if not files:
            return json.dumps({"error": "snapshot 需要提供 files 参数"})
        result = _checkpoint_manager.create(files, description)
        return json.dumps(result, ensure_ascii=False, indent=2)

    elif action == "list":
        checkpoints = _checkpoint_manager.list_checkpoints()
        return json.dumps({
            "checkpoints": checkpoints,
            "count": len(checkpoints),
        }, ensure_ascii=False, indent=2)

    elif action == "restore":
        if not checkpoint_id:
            return json.dumps({"error": "restore 需要提供 checkpoint_id 参数"})
        result = _checkpoint_manager.restore(checkpoint_id)
        return json.dumps(result, ensure_ascii=False, indent=2)

    return json.dumps({"error": f"未知操作: {action}"})


registry.register(
    name="checkpoint",
    toolset="safety",
    schema=CHECKPOINT_SCHEMA,
    handler=_checkpoint_impl,
    description="文件快照/回滚",
    emoji="💾",
)
