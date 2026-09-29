"""记忆哈希 + 冻结快照 —— Spirit Agent Phase 6.D（docs/13 §D3 / §6.3 / §2.2 Phase 3）。

对标 RSIAgent ``explore/memory_hash.py``，为"冻结记忆复用"提供可审计、可校验的地基：

- **哈希**：对三层记忆文件（轨迹池 / 洞察库 / 技能库）的原始字节做 sha256，得到确定
  指纹。用于判断两个 wave 之间记忆是否变化、快照是否被篡改。
- **冻结快照**：把当前记忆原子复制到 ``<SPIRIT_HOME>/evolution_memory/snapshots/<id>/``，
  连同 ``manifest.json``（哈希 + 计数 + 时间）落盘。
- **校验**：:meth:`MemorySnapshot.verify` 重算哈希与 manifest 比对，保证 Phase 3 复用的
  是确定版本。
- **只读复用**（Phase 3 / D4）：:class:`FrozenMemory` 是记忆的只读门面——**关闭记忆更新**，
  任何写操作抛错，供测试时 Actor 复用冻结经验。

路径**按调用解析** ``SPIRIT_HOME``（对齐 :mod:`spirit.evolution.memory`），可注入 ``root``。
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from spirit.evolution.memory import (
    EvolutionMemory,
    Insight,
    SkillEntry,
    Trajectory,
    evolution_memory_root,
    insights_path,
    skills_path,
    trajectories_path,
)

logger = logging.getLogger(__name__)

SNAPSHOT_DIRNAME = "snapshots"
MANIFEST_NAME = "manifest.json"
# 参与哈希的三个记忆文件（固定顺序，保证确定性）
_HASH_FILES = ("trajectories.json", "insights.json", "skills.json")


# ---------------------------------------------------------------------------
# 哈希
# ---------------------------------------------------------------------------

def _hash_bytes(root: Path) -> str:
    """对 ``root`` 下三层记忆文件的原始字节做 sha256（缺失文件按空字节计）。"""
    digest = hashlib.sha256()
    for name in _HASH_FILES:
        path = root / name
        digest.update(name.encode("utf-8"))
        digest.update(b"\x00")
        try:
            if path.is_file():
                digest.update(path.read_bytes())
        except OSError as exc:  # pragma: no cover - 读失败降级为空
            logger.warning("evolution.memory_hash: 读取 %s 失败: %s", name, exc)
        digest.update(b"\x00")
    return digest.hexdigest()


def compute_hash(memory: Optional[EvolutionMemory] = None, *, root: Optional[Path] = None) -> str:
    """计算当前三层记忆的确定性哈希指纹。"""
    mem = memory or EvolutionMemory()
    base = Path(root) if root is not None else mem.root
    return _hash_bytes(base)


def snapshots_root(memory: Optional[EvolutionMemory] = None, *, root: Optional[Path] = None) -> Path:
    mem = memory or EvolutionMemory()
    base = Path(root) if root is not None else mem.root
    return base / SNAPSHOT_DIRNAME


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------

@dataclass
class MemorySnapshot:
    """一份冻结的记忆快照（含哈希 manifest，可校验、可只读复用）。"""

    snapshot_id: str
    root: Path                       # 快照所在目录（含三份 json + manifest）
    hash: str
    counts: Dict[str, int] = field(default_factory=dict)
    label: str = ""
    created_at: float = field(default_factory=time.time)

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    def to_dict(self) -> Dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "hash": self.hash,
            "counts": dict(self.counts),
            "label": self.label,
            "created_at": self.created_at,
        }

    # -- 校验 --------------------------------------------------------------
    def verify(self) -> bool:
        """重算快照目录哈希并与 manifest 记录比对（一致 → True）。"""
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("evolution.memory_hash: 读取 manifest 失败: %s", exc)
            return False
        expected = str(manifest.get("hash", ""))
        return bool(expected) and _hash_bytes(self.root) == expected

    # -- 只读复用（Phase 3）------------------------------------------------
    def open_readonly(self) -> "FrozenMemory":
        """返回指向快照目录的只读记忆门面（关闭更新）。"""
        return FrozenMemory(root=self.root)


def freeze(
    memory: Optional[EvolutionMemory] = None,
    *,
    root: Optional[Path] = None,
    label: str = "",
    snapshot_id: Optional[str] = None,
) -> MemorySnapshot:
    """把当前记忆原子冻结为一份快照并落盘，返回 :class:`MemorySnapshot`。"""
    mem = memory or EvolutionMemory()
    base = Path(root) if root is not None else mem.root
    snap_id = snapshot_id or f"snap_{uuid.uuid4().hex[:12]}"
    dest = snapshots_root(mem, root=base) / snap_id
    dest.mkdir(parents=True, exist_ok=True)
    # 复制三份记忆文件（缺失则跳过）
    for name in _HASH_FILES:
        src = base / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    counts = mem.stats()
    snap_hash = _hash_bytes(dest)
    snapshot = MemorySnapshot(
        snapshot_id=snap_id,
        root=dest,
        hash=snap_hash,
        counts=counts,
        label=label,
        created_at=time.time(),
    )
    manifest = snapshot.to_dict()
    (dest / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return snapshot


def list_snapshots(
    memory: Optional[EvolutionMemory] = None, *, root: Optional[Path] = None
) -> List[MemorySnapshot]:
    """列出所有已落盘快照（按 created_at 升序）。损坏的快照跳过。"""
    mem = memory or EvolutionMemory()
    base = Path(root) if root is not None else mem.root
    snap_dir = snapshots_root(mem, root=base)
    out: List[MemorySnapshot] = []
    if not snap_dir.is_dir():
        return out
    for child in snap_dir.iterdir():
        manifest = child / MANIFEST_NAME
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append(
            MemorySnapshot(
                snapshot_id=str(data.get("snapshot_id", child.name)),
                root=child,
                hash=str(data.get("hash", "")),
                counts=dict(data.get("counts") or {}),
                label=str(data.get("label", "")),
                created_at=float(data.get("created_at", 0.0)),
            )
        )
    out.sort(key=lambda s: s.created_at)
    return out


def load_snapshot(
    snapshot_id: str, memory: Optional[EvolutionMemory] = None, *, root: Optional[Path] = None
) -> Optional[MemorySnapshot]:
    for snap in list_snapshots(memory, root=root):
        if snap.snapshot_id == snapshot_id:
            return snap
    return None


# ---------------------------------------------------------------------------
# 只读记忆门面（Phase 3 · D4：关闭 Curriculum 与记忆更新）
# ---------------------------------------------------------------------------

class FrozenMemory:
    """冻结记忆的**只读**门面：供 Phase 3 测试时复用，任何写操作一律拒绝。

    只暴露读取接口（trajectories/insights/skills/stats），语义与
    :class:`~spirit.evolution.memory.EvolutionMemory` 一致，但底层指向快照目录。
    """

    def __init__(self, *, root: Path) -> None:
        self._root = Path(root)
        self._inner = EvolutionMemory(root=self._root)

    @property
    def root(self) -> Path:
        return self._root

    def trajectories(self, task_id: Optional[str] = None) -> List[Trajectory]:
        return self._inner.trajectories(task_id)

    def insights(self, *, active_only: bool = True, min_confidence: float = 0.0) -> List[Insight]:
        return self._inner.insights(active_only=active_only, min_confidence=min_confidence)

    def skills(self) -> List[SkillEntry]:
        return self._inner.skills()

    def stats(self) -> Dict[str, int]:
        return self._inner.stats()

    # -- 写操作：全部拒绝（记忆冻结）--------------------------------------
    def _reject(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("FrozenMemory 为只读冻结记忆，禁止写入（Phase 3 记忆复用）")

    add_trajectory = _reject
    add_insight = _reject
    add_skill = _reject
    deprecate_insight = _reject
    decay_insights = _reject
    clear = _reject


__all__ = [
    "MemorySnapshot",
    "FrozenMemory",
    "compute_hash",
    "freeze",
    "list_snapshots",
    "load_snapshot",
    "snapshots_root",
    "SNAPSHOT_DIRNAME",
    "MANIFEST_NAME",
]
