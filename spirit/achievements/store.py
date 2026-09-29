"""成就状态持久化 —— Spirit Agent。

把解锁状态存到 ``<SPIRIT_HOME>/achievements/state.json``。遵循 Spirit 的
**按调用解析路径**约定（对齐 :mod:`spirit.profile.paths`）：每次调用时读
``spirit.config.SPIRIT_HOME``，故测试 monkeypatch SPIRIT_HOME 立即生效。

state.json 结构::

    {
      "schema_version": 1,
      "unlocks": {
        "<achievement_id>": {"tier": "Silver", "unlocked_at": 1700000000.0,
                              "name": "...", "category": "..."}
      },
      "last_evaluated": 1700000000.0
    }

设计约定：
- **原子写**：先写 ``.tmp`` 再 ``os.replace``，避免中断损坏。
- **fail-soft**：读损坏 JSON 降级为空 state；写失败仅记日志不抛。
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1


def _spirit_home() -> Path:
    """按调用读取 ``spirit.config.SPIRIT_HOME``（延迟导入避免循环）。"""
    try:
        from spirit import config
        home = getattr(config, "SPIRIT_HOME", None)
        if home:
            return Path(home)
    except Exception as exc:  # pragma: no cover - 配置不可用时降级
        logger.debug("achievements.store: 读取 SPIRIT_HOME 失败: %s", exc)
    return Path("~/.spirit").expanduser()


def achievements_dir() -> Path:
    return _spirit_home() / "achievements"


def state_path() -> Path:
    return achievements_dir() / "state.json"


def _empty_state() -> Dict[str, Any]:
    return {"schema_version": _SCHEMA_VERSION, "unlocks": {}, "last_evaluated": None}


class AchievementStore:
    """成就解锁状态的读写。"""

    def __init__(self, root: Optional[Path] = None):
        """``root`` 可显式指定成就目录（测试用）；None 则按调用解析 SPIRIT_HOME。"""
        self._root_override = Path(root) if root else None

    def _dir(self) -> Path:
        return self._root_override if self._root_override else achievements_dir()

    def _path(self) -> Path:
        return self._dir() / "state.json"

    def load(self) -> Dict[str, Any]:
        path = self._path()
        if not path.exists():
            return _empty_state()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("成就 state 解析失败，重置为空: %s", exc)
            return _empty_state()
        if not isinstance(data, dict):
            return _empty_state()
        data.setdefault("schema_version", _SCHEMA_VERSION)
        unlocks = data.get("unlocks")
        data["unlocks"] = unlocks if isinstance(unlocks, dict) else {}
        data.setdefault("last_evaluated", None)
        return data

    def save(self, state: Dict[str, Any]) -> bool:
        path = self._path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, path)
            return True
        except Exception as exc:
            logger.warning("成就 state 写入失败: %s", exc)
            return False

    def get_unlocks(self) -> Dict[str, Any]:
        return self.load().get("unlocks", {})

    def record_evaluation(self, evaluation: Dict[str, Any]) -> Dict[str, Any]:
        """把一次 ``AchievementEngine.evaluate_all`` 的结果落盘。

        对 ``newly_unlocked`` 与 ``upgraded`` 里的每个成就，写入/更新其 tier 与
        时间戳。返回更新后的完整 state。
        """
        state = self.load()
        unlocks: Dict[str, Any] = state.setdefault("unlocks", {})
        now = time.time()

        for record in evaluation.get("newly_unlocked", []):
            unlocks[record["id"]] = {
                "tier": record.get("tier"),
                "unlocked_at": now,
                "name": record.get("name"),
                "category": record.get("category"),
                "icon": record.get("icon"),
            }
        # 升级：保留原 unlocked_at，更新 tier
        for record in evaluation.get("upgraded", []):
            existing = unlocks.get(record["id"], {})
            unlocks[record["id"]] = {
                **existing,
                "tier": record.get("tier"),
                "upgraded_at": now,
                "name": record.get("name"),
                "category": record.get("category"),
                "icon": record.get("icon"),
            }

        state["last_evaluated"] = now
        state["schema_version"] = _SCHEMA_VERSION
        self.save(state)
        return state

    def reset(self) -> None:
        """清空解锁状态（保留文件）。"""
        self.save(_empty_state())


__all__ = [
    "AchievementStore",
    "achievements_dir",
    "state_path",
]
