"""技能使用追踪 — Spirit Agent（Phase 4.6）。

对标 Hermes ``tools/skill_usage.py`` 的核心：每次技能被主动使用（载入 prompt 路径 / 从
助手轮次引用）时 ``bump_use``，被查看时 ``bump_view``。计数与时间戳存 ``<hub>/usage.json``，
供 Curator 生命周期管理（哪些技能常用 / 长期不用）与 ``/skill`` 列表排序消费。

与 Hermes 的差异：Hermes 的 skill_usage 还含内置技能保护 / 归档 / 抑制清单等 Curator
配套逻辑（依赖 curator.py）；Spirit 只移植**追踪核心**（计数 + 时间戳 + 活跃度聚合），
Curator 后台维护留待后续。存储用一把进程内锁串行化读改写，避免并发会话丢计数。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from spirit.skills_hub import paths

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> Dict[str, Any]:
    path = paths.usage_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write(data: Dict[str, Any]) -> None:
    path = paths.usage_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.debug("无法写入技能使用记录: %s", exc)


def _mutate(skill_name: str, apply_fn: Callable[[Dict[str, Any]], None]) -> None:
    """读-改-写单个技能记录（进程内锁串行化）。``apply_fn`` 就地修改记录 dict。"""
    name = (skill_name or "").strip()
    if not name:
        return
    with _lock:
        data = _read()
        record = data.get(name)
        if not isinstance(record, dict):
            record = {"first_seen_at": _now_iso()}
        apply_fn(record)
        data[name] = record
        _write(data)


def bump_use(skill_name: str) -> None:
    """递增 ``use_count`` 并刷新 ``last_used_at``（技能被主动使用时调用）。"""
    def _apply(rec: Dict[str, Any]) -> None:
        rec["use_count"] = int(rec.get("use_count") or 0) + 1
        rec["last_used_at"] = _now_iso()
    _mutate(skill_name, _apply)


def bump_view(skill_name: str) -> None:
    """递增 ``view_count`` 并刷新 ``last_viewed_at``（技能被 skill_view 查看时调用）。"""
    def _apply(rec: Dict[str, Any]) -> None:
        rec["view_count"] = int(rec.get("view_count") or 0) + 1
        rec["last_viewed_at"] = _now_iso()
    _mutate(skill_name, _apply)


def get_usage(skill_name: str) -> Optional[Dict[str, Any]]:
    """返回单个技能的使用记录（无则 None）。"""
    return _read().get((skill_name or "").strip())


def all_usage() -> Dict[str, Any]:
    """返回全部使用记录（``{name: record}``）。"""
    return _read()


def activity_count(record: Dict[str, Any]) -> int:
    """记录的总活动次数（use + view + patch 计数之和）。"""
    if not isinstance(record, dict):
        return 0
    total = 0
    for key in ("use_count", "view_count", "patch_count"):
        try:
            total += int(record.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return total


def latest_activity_at(record: Dict[str, Any]) -> Optional[str]:
    """记录中最近一次活动时间戳（各 ``last_*_at`` 的最大值；无则 None）。"""
    if not isinstance(record, dict):
        return None
    stamps = []
    for key in ("last_used_at", "last_viewed_at", "last_patched_at"):
        value = record.get(key)
        if isinstance(value, str) and value:
            stamps.append(value)
    return max(stamps) if stamps else None


__all__ = [
    "bump_use",
    "bump_view",
    "get_usage",
    "all_usage",
    "activity_count",
    "latest_activity_at",
]
