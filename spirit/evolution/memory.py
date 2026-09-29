"""三层进化记忆 —— Spirit Agent Phase 6.A（docs/13 §六）。

自进化的本质是"把存下来的经验变成下次更好的自己"，故记忆分三层（工程铁律，
勿混用）：

============  ==============================  ==================================
层            内容                            关键字段
============  ==============================  ==================================
轨迹池        原始探索经验（动作-观察序列）    task_id / timestamp / content
洞察库        跨任务可复用的抽象结论          **confidence** / **source_task_id** / status
技能库        固化的可执行解法                name / trigger / procedure
============  ==============================  ==================================

洞察库必须带 ``confidence`` + ``source_task_id``：没置信度无法"被证伪就沉底"，
没来源出了事故查不到根（docs/13 §6.1）。

落盘布局（``<SPIRIT_HOME>/evolution_memory/``）::

    evolution_memory/
      trajectories.json   # 轨迹池
      insights.json       # 洞察库
      skills.json         # 技能库

路径**按调用解析**（对齐 :mod:`spirit.checkpoint.paths` / :mod:`spirit.profile.paths`）：
每次操作读 ``spirit.config.SPIRIT_HOME``，故测试 monkeypatch SPIRIT_HOME 立即反映，
无导入期副作用（不建目录）。也可给 :class:`EvolutionMemory` 注入 ``root`` 直接隔离。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 洞察状态
INSIGHT_ACTIVE = "active"
INSIGHT_DEPRECATED = "deprecated"


# ---------------------------------------------------------------------------
# 路径解析（按调用读 config.SPIRIT_HOME）
# ---------------------------------------------------------------------------

def _spirit_home() -> Path:
    """按调用读取 ``spirit.config.SPIRIT_HOME``（延迟导入避免循环）。"""
    try:
        from spirit import config
        home = getattr(config, "SPIRIT_HOME", None)
        if home:
            return Path(home)
    except Exception as exc:  # pragma: no cover - 配置不可用时降级
        logger.debug("evolution.memory: 读取 SPIRIT_HOME 失败: %s", exc)
    return Path("~/.spirit").expanduser()


def evolution_memory_root() -> Path:
    """进化记忆根目录 ``<SPIRIT_HOME>/evolution_memory``。"""
    return _spirit_home() / "evolution_memory"


def trajectories_path(root: Optional[Path] = None) -> Path:
    return (root or evolution_memory_root()) / "trajectories.json"


def insights_path(root: Optional[Path] = None) -> Path:
    return (root or evolution_memory_root()) / "insights.json"


def skills_path(root: Optional[Path] = None) -> Path:
    return (root or evolution_memory_root()) / "skills.json"


def _atomic_write_json(path: Path, payload: Any) -> None:
    """原子写 JSON（先写临时文件再 replace，避免半写损坏）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:  # pragma: no cover - Windows 偶发文件锁
                pass


def _read_json_list(path: Path) -> List[Dict[str, Any]]:
    """读 JSON 数组，损坏/缺失一律 fail-soft 返回空列表。"""
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
    except Exception as exc:
        logger.warning("evolution.memory: 读取 %s 失败（视为空）: %s", path.name, exc)
    return []


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# 三层记忆条目
# ---------------------------------------------------------------------------

@dataclass
class Trajectory:
    """轨迹池条目：一次探索的原始经验（动作-观察序列）。"""

    task_id: str = ""
    content: str = ""
    outcome: str = ""           # 可选：PASS/FAIL/UNVERIFIED 或自由文本
    id: str = field(default_factory=lambda: _new_id("traj"))
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "content": self.content,
            "outcome": self.outcome,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Trajectory":
        try:
            ts = float(data.get("timestamp", time.time()))
        except (TypeError, ValueError):
            ts = time.time()
        return cls(
            id=str(data.get("id") or _new_id("traj")),
            task_id=str(data.get("task_id", "") or ""),
            content=str(data.get("content", "") or ""),
            outcome=str(data.get("outcome", "") or ""),
            timestamp=ts,
        )


@dataclass
class Insight:
    """洞察库条目：跨任务可复用的抽象结论。

    ``confidence`` ∈ [0,1]，``source_task_id`` 溯源，``status`` 支持被证伪后沉底。
    """

    text: str = ""
    confidence: float = 0.5
    source_task_id: str = ""
    status: str = INSIGHT_ACTIVE
    tags: List[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: _new_id("ins"))
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        # 置信度夹到 [0,1]
        try:
            self.confidence = float(self.confidence)
        except (TypeError, ValueError):
            self.confidence = 0.5
        self.confidence = max(0.0, min(1.0, self.confidence))

    @property
    def active(self) -> bool:
        return self.status == INSIGHT_ACTIVE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "confidence": self.confidence,
            "source_task_id": self.source_task_id,
            "status": self.status,
            "tags": list(self.tags),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Insight":
        tags = data.get("tags") or []
        if not isinstance(tags, list):
            tags = [str(tags)]
        try:
            created = float(data.get("created_at", time.time()))
        except (TypeError, ValueError):
            created = time.time()
        return cls(
            id=str(data.get("id") or _new_id("ins")),
            text=str(data.get("text", "") or ""),
            confidence=data.get("confidence", 0.5),
            source_task_id=str(data.get("source_task_id", "") or ""),
            status=str(data.get("status", INSIGHT_ACTIVE) or INSIGHT_ACTIVE),
            tags=[str(t) for t in tags],
            created_at=created,
        )


@dataclass
class SkillEntry:
    """技能库条目：固化的可执行解法（后续可结晶进 ``skills_hub``，见 docs/13 §D5）。"""

    name: str = ""
    trigger: str = ""
    procedure: str = ""
    source_insight_ids: List[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: _new_id("skill"))
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "trigger": self.trigger,
            "procedure": self.procedure,
            "source_insight_ids": list(self.source_insight_ids),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillEntry":
        srcs = data.get("source_insight_ids") or []
        if not isinstance(srcs, list):
            srcs = [str(srcs)]
        try:
            created = float(data.get("created_at", time.time()))
        except (TypeError, ValueError):
            created = time.time()
        return cls(
            id=str(data.get("id") or _new_id("skill")),
            name=str(data.get("name", "") or ""),
            trigger=str(data.get("trigger", "") or ""),
            procedure=str(data.get("procedure", "") or ""),
            source_insight_ids=[str(s) for s in srcs],
            created_at=created,
        )


# ---------------------------------------------------------------------------
# 进化记忆容器
# ---------------------------------------------------------------------------

class EvolutionMemory:
    """三层进化记忆容器（轨迹池 / 洞察库 / 技能库）+ 落盘。

    ``root`` 为 None 时**按调用**解析 ``<SPIRIT_HOME>/evolution_memory``（测试可
    monkeypatch ``config.SPIRIT_HOME``）；也可注入固定 ``root`` 直接隔离。
    所有写操作即时落盘（原子写），读操作 fail-soft。
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root_override = Path(root) if root is not None else None

    # -- 路径 --------------------------------------------------------------
    @property
    def root(self) -> Path:
        return self._root_override or evolution_memory_root()

    # -- 轨迹池 ------------------------------------------------------------
    def add_trajectory(
        self, task_id: str, content: str, *, outcome: str = ""
    ) -> Trajectory:
        traj = Trajectory(task_id=task_id, content=content, outcome=outcome)
        raw = _read_json_list(trajectories_path(self.root))
        raw.append(traj.to_dict())
        _atomic_write_json(trajectories_path(self.root), raw)
        return traj

    def trajectories(self, task_id: Optional[str] = None) -> List[Trajectory]:
        raw = _read_json_list(trajectories_path(self.root))
        items = [Trajectory.from_dict(d) for d in raw]
        if task_id is not None:
            items = [t for t in items if t.task_id == task_id]
        return items

    # -- 洞察库 ------------------------------------------------------------
    def add_insight(
        self,
        text: str,
        *,
        confidence: float = 0.5,
        source_task_id: str = "",
        tags: Optional[List[str]] = None,
    ) -> Insight:
        ins = Insight(
            text=text,
            confidence=confidence,
            source_task_id=source_task_id,
            tags=list(tags or []),
        )
        raw = _read_json_list(insights_path(self.root))
        raw.append(ins.to_dict())
        _atomic_write_json(insights_path(self.root), raw)
        return ins

    def insights(
        self,
        *,
        active_only: bool = True,
        min_confidence: float = 0.0,
    ) -> List[Insight]:
        raw = _read_json_list(insights_path(self.root))
        items = [Insight.from_dict(d) for d in raw]
        if active_only:
            items = [i for i in items if i.active]
        if min_confidence > 0.0:
            items = [i for i in items if i.confidence >= min_confidence]
        return items

    def get_insight(self, insight_id: str) -> Optional[Insight]:
        for ins in self.insights(active_only=False):
            if ins.id == insight_id:
                return ins
        return None

    def deprecate_insight(self, insight_id: str) -> bool:
        """把某条洞察标记为 deprecated（被证伪后沉底，不物理删除以保审计）。"""
        raw = _read_json_list(insights_path(self.root))
        changed = False
        for d in raw:
            if d.get("id") == insight_id:
                d["status"] = INSIGHT_DEPRECATED
                changed = True
        if changed:
            _atomic_write_json(insights_path(self.root), raw)
        return changed

    def decay_insights(self, factor: float = 0.9) -> int:
        """对所有 active 洞察的 confidence 乘以衰减因子（docs/13 §9：置信度衰减）。

        返回被衰减的洞察数。``factor`` 夹到 (0,1]。
        """
        factor = max(0.0, min(1.0, float(factor)))
        raw = _read_json_list(insights_path(self.root))
        count = 0
        for d in raw:
            if d.get("status", INSIGHT_ACTIVE) == INSIGHT_ACTIVE:
                try:
                    conf = float(d.get("confidence", 0.5))
                except (TypeError, ValueError):
                    conf = 0.5
                d["confidence"] = round(max(0.0, min(1.0, conf * factor)), 6)
                count += 1
        if count:
            _atomic_write_json(insights_path(self.root), raw)
        return count

    # -- 技能库 ------------------------------------------------------------
    def add_skill(
        self,
        name: str,
        trigger: str,
        procedure: str,
        *,
        source_insight_ids: Optional[List[str]] = None,
    ) -> SkillEntry:
        skill = SkillEntry(
            name=name,
            trigger=trigger,
            procedure=procedure,
            source_insight_ids=list(source_insight_ids or []),
        )
        raw = _read_json_list(skills_path(self.root))
        raw.append(skill.to_dict())
        _atomic_write_json(skills_path(self.root), raw)
        return skill

    def skills(self) -> List[SkillEntry]:
        raw = _read_json_list(skills_path(self.root))
        return [SkillEntry.from_dict(d) for d in raw]

    # -- 汇总 / 统计 -------------------------------------------------------
    def stats(self) -> Dict[str, int]:
        return {
            "trajectories": len(self.trajectories()),
            "insights_active": len(self.insights(active_only=True)),
            "insights_total": len(self.insights(active_only=False)),
            "skills": len(self.skills()),
        }

    def clear(self) -> None:
        """清空三层记忆（删文件，测试用）。"""
        for p in (
            trajectories_path(self.root),
            insights_path(self.root),
            skills_path(self.root),
        ):
            try:
                if p.exists():
                    p.unlink()
            except OSError as exc:  # pragma: no cover
                logger.warning("evolution.memory: 删除 %s 失败: %s", p.name, exc)


__all__ = [
    "EvolutionMemory",
    "Trajectory",
    "Insight",
    "SkillEntry",
    "INSIGHT_ACTIVE",
    "INSIGHT_DEPRECATED",
    "evolution_memory_root",
    "trajectories_path",
    "insights_path",
    "skills_path",
]
