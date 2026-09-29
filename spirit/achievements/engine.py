"""成就评估引擎 —— Spirit Agent。

移植自 Hermes ``plugins/hermes-achievements`` 的评估语义（精简可测试子集）。

引擎是**纯函数式**的：给定一份 aggregate 指标 dict，对目录里每个成就算出
``{unlocked, discovered, state, tier, progress, next_tier, next_threshold,
progress_pct}``。是否“新解锁”由调用方拿旧 state 比对（见 :meth:`evaluate_all`）。

不依赖 DB、不依赖 IO —— 便于离线单测。指标采集见 :mod:`spirit.achievements.metrics`。
"""

import math
from typing import Any, Dict, List, Optional

from spirit.achievements import definitions


def evaluate_tiered(definition: Dict[str, Any], aggregate: Dict[str, Any]) -> Dict[str, Any]:
    """评估一个分级成就（lifetime / best_session）。

    取阈值 ≤ progress 的最高 tier 为当前 tier；unlocked = 至少达成一个 tier。
    progress_pct 在“当前 tier 阈值 → 下一 tier 阈值”区间内线性计算，封顶 99
    （全达成时 100）。
    """
    metric = definition.get("threshold_metric", "")
    progress = int(aggregate.get(metric, 0) or 0)
    tiers_list = sorted(definition.get("tiers", []), key=lambda t: t["threshold"])
    achieved = [t for t in tiers_list if progress >= t["threshold"]]
    next_tiers = [t for t in tiers_list if progress < t["threshold"]]
    tier = achieved[-1]["name"] if achieved else None
    next_tier = next_tiers[0]["name"] if next_tiers else None
    next_threshold = (
        next_tiers[0]["threshold"] if next_tiers
        else (tiers_list[-1]["threshold"] if tiers_list else 1)
    )
    current_threshold = achieved[-1]["threshold"] if achieved else 0
    denom = max(1, next_threshold - current_threshold)
    pct = (
        100 if not next_tiers and achieved
        else max(0, min(99, math.floor(((progress - current_threshold) / denom) * 100)))
    )
    unlocked = bool(achieved)
    discovered = bool(progress > 0)
    state = (
        "unlocked" if unlocked
        else ("secret" if definition.get("secret") and not discovered else "discovered")
    )
    return {
        "unlocked": unlocked,
        "discovered": discovered or not definition.get("secret"),
        "state": state,
        "tier": tier,
        "progress": progress,
        "next_tier": next_tier,
        "next_threshold": next_threshold,
        "progress_pct": pct,
    }


def evaluate_requirements(definition: Dict[str, Any], aggregate: Dict[str, Any]) -> Dict[str, Any]:
    """评估一个多条件成就：所有 requirement 的 metric ≥ gte 才解锁。

    progress_pct 为各 requirement 完成度（value/threshold，封顶 1.0）的平均。
    """
    requirements = definition.get("requirements", [])
    if not requirements:
        return {
            "unlocked": False,
            "discovered": not definition.get("secret"),
            "state": "secret" if definition.get("secret") else "discovered",
            "tier": None, "progress": 0, "next_tier": None,
            "next_threshold": 1, "progress_pct": 0,
        }
    parts: List[float] = []
    any_progress = False
    complete = True
    for requirement in requirements:
        value = int(aggregate.get(requirement["metric"], 0) or 0)
        threshold = int(requirement.get("gte", 1))
        any_progress = any_progress or value > 0
        complete = complete and value >= threshold
        parts.append(min(1.0, value / max(1, threshold)))
    pct = math.floor((sum(parts) / len(parts)) * 100)
    state = (
        "unlocked" if complete
        else ("secret" if definition.get("secret") and not any_progress else "discovered")
    )
    return {
        "unlocked": complete,
        "discovered": any_progress or not definition.get("secret"),
        "state": state,
        "tier": None,
        "progress": pct,
        "next_tier": None,
        "next_threshold": 100,
        "progress_pct": 100 if complete else min(99, pct),
    }


def evaluate_boolean(definition: Dict[str, Any], aggregate: Dict[str, Any]) -> Dict[str, Any]:
    """评估一个布尔成就（向后兼容；新目录避免使用简单布尔）。"""
    unlocked = bool(aggregate.get(definition.get("metric")))
    return {
        "unlocked": unlocked,
        "discovered": True,
        "state": "unlocked" if unlocked else "discovered",
        "tier": None,
        "progress": 1 if unlocked else 0,
        "next_tier": None,
        "next_threshold": 1,
        "progress_pct": 100 if unlocked else 0,
    }


def evaluate_definition(definition: Dict[str, Any], aggregate: Dict[str, Any]) -> Dict[str, Any]:
    """按 ``kind`` 分派到对应评估器。"""
    kind = definition.get("kind")
    if kind == "multi_condition" or definition.get("requirements"):
        return evaluate_requirements(definition, aggregate)
    if definition.get("threshold_metric") or kind in ("lifetime", "best_session"):
        return evaluate_tiered(definition, aggregate)
    return evaluate_boolean(definition, aggregate)


def _tier_rank(tier_name: Optional[str]) -> int:
    """tier 名 → 序号（0=未解锁）。用于判断“升级”。"""
    if not tier_name:
        return 0
    try:
        return definitions.TIER_NAMES.index(tier_name) + 1
    except ValueError:
        return 0


class AchievementEngine:
    """对整份目录做评估，并（可选）与既有 state 比对得出新解锁/升级。"""

    def __init__(self, catalog: Optional[List[Dict[str, Any]]] = None):
        self.catalog = catalog if catalog is not None else definitions.ACHIEVEMENTS

    def evaluate_all(
        self, aggregate: Dict[str, Any], prior_unlocks: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """评估全部成就。

        Args:
            aggregate: 指标 dict（见 metrics.collect_metrics）。
            prior_unlocks: 既有解锁状态 ``{achievement_id: {"tier": ...}}``；用于
                计算 ``newly_unlocked`` / ``upgraded``。None 视为空。

        Returns:
            ``{"results": {id: eval_result}, "newly_unlocked": [...],
            "upgraded": [...], "unlocked_count": int, "total": int}``。
        """
        prior = prior_unlocks or {}
        results: Dict[str, Any] = {}
        newly_unlocked: List[Dict[str, Any]] = []
        upgraded: List[Dict[str, Any]] = []

        for definition in self.catalog:
            aid = definition["id"]
            res = evaluate_definition(definition, aggregate)
            results[aid] = res

            if res["unlocked"]:
                before = prior.get(aid) or {}
                before_rank = _tier_rank(before.get("tier"))
                now_rank = _tier_rank(res.get("tier"))
                record = {
                    "id": aid,
                    "name": definition.get("name"),
                    "category": definition.get("category"),
                    "icon": definition.get("icon"),
                    "tier": res.get("tier"),
                }
                if not before or before_rank == 0:
                    newly_unlocked.append(record)
                elif now_rank > before_rank:
                    upgraded.append(record)

        unlocked_count = sum(1 for r in results.values() if r["unlocked"])
        return {
            "results": results,
            "newly_unlocked": newly_unlocked,
            "upgraded": upgraded,
            "unlocked_count": unlocked_count,
            "total": len(self.catalog),
        }

    def progress(self, aggregate: Dict[str, Any]) -> List[Dict[str, Any]]:
        """返回每个成就的进度视图（含定义元信息），供 dashboard / CLI 渲染。"""
        out = []
        for definition in self.catalog:
            res = evaluate_definition(definition, aggregate)
            secret_hidden = res["state"] == "secret"
            out.append({
                "id": definition["id"],
                "name": "???" if secret_hidden else definition.get("name"),
                "category": definition.get("category"),
                "description": "" if secret_hidden else definition.get("description", ""),
                "icon": definition.get("icon"),
                "kind": definition.get("kind"),
                **res,
            })
        return out


__all__ = [
    "evaluate_tiered",
    "evaluate_requirements",
    "evaluate_boolean",
    "evaluate_definition",
    "AchievementEngine",
]
