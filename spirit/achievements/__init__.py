"""Spirit 成就系统 —— 从会话历史派生游戏化成就。

移植自 Hermes ``plugins/hermes-achievements``（精简可测试子集，Spirit 化）。

四个协作件：

- :mod:`spirit.achievements.definitions` —— 成就目录（metric 驱动的分级/多条件定义）。
- :mod:`spirit.achievements.metrics` —— 从会话 DB 采集 aggregate 指标。
- :mod:`spirit.achievements.engine` —— 纯函数式评估（tier / requirements / boolean）。
- :mod:`spirit.achievements.store` —— 解锁状态 JSON 持久化（``<SPIRIT_HOME>/achievements``）。

一站式入口::

    from spirit.achievements import check_achievements
    result = check_achievements(session_db)   # 采集 → 评估 → 落盘 → 返回新解锁

典型用于会话结束或 dashboard 刷新时调用；``check_achievements`` 全程 fail-soft，
绝不因成就逻辑异常而阻断主流程。
"""

import logging
from typing import Any, Dict, Optional

from spirit.achievements.definitions import (
    ACHIEVEMENTS,
    BY_ID,
    CATEGORIES,
    METRIC_LABELS,
    TIER_NAMES,
    req,
    tiers,
)
from spirit.achievements.engine import (
    AchievementEngine,
    evaluate_boolean,
    evaluate_definition,
    evaluate_requirements,
    evaluate_tiered,
)
from spirit.achievements.metrics import MetricsCollector, collect_metrics
from spirit.achievements.store import AchievementStore, achievements_dir, state_path

logger = logging.getLogger(__name__)


def check_achievements(
    db,
    *,
    store: Optional[AchievementStore] = None,
    catalog=None,
    persist: bool = True,
) -> Dict[str, Any]:
    """一站式：从会话 DB 采集指标 → 评估全部成就 → 落盘新解锁。

    Args:
        db: ``SessionDB`` 实例或裸 ``sqlite3.Connection``。
        store: 可选自定义 :class:`AchievementStore`（测试注入）。None 则用默认
            （按调用解析 SPIRIT_HOME）。
        catalog: 可选自定义成就目录。None 则用内置 :data:`ACHIEVEMENTS`。
        persist: 是否把新解锁写入 state.json（False 则只评估不落盘）。

    Returns:
        ``{"metrics": {...}, "evaluation": {...}, "newly_unlocked": [...],
        "upgraded": [...]}``。任何异常都降级为空结果（fail-soft）。
    """
    try:
        metrics = collect_metrics(db)
    except Exception as exc:  # pragma: no cover - 防御性
        logger.warning("成就指标采集失败: %s", exc)
        metrics = {}

    store = store or AchievementStore()
    engine = AchievementEngine(catalog=catalog)
    prior_unlocks = store.get_unlocks() if persist else {}

    evaluation = engine.evaluate_all(metrics, prior_unlocks=prior_unlocks)

    if persist and (evaluation["newly_unlocked"] or evaluation["upgraded"]):
        try:
            store.record_evaluation(evaluation)
        except Exception as exc:  # pragma: no cover - 防御性
            logger.warning("成就状态落盘失败: %s", exc)

    return {
        "metrics": metrics,
        "evaluation": evaluation,
        "newly_unlocked": evaluation["newly_unlocked"],
        "upgraded": evaluation["upgraded"],
    }


__all__ = [
    # 定义
    "ACHIEVEMENTS", "BY_ID", "CATEGORIES", "TIER_NAMES", "METRIC_LABELS",
    "tiers", "req",
    # 引擎
    "AchievementEngine", "evaluate_definition", "evaluate_tiered",
    "evaluate_requirements", "evaluate_boolean",
    # 指标
    "MetricsCollector", "collect_metrics",
    # 存储
    "AchievementStore", "achievements_dir", "state_path",
    # 一站式
    "check_achievements",
]
