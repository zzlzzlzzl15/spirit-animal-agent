"""成就定义目录 —— Spirit Agent。

移植自 Hermes ``plugins/hermes-achievements``（精简可测试子集，Spirit 化）。

数据模型（对齐 Hermes）：
- 每个成就有 ``kind``：``lifetime``（累计指标分级）/ ``best_session``（单会话最佳
  指标分级）/ ``multi_condition``（多指标同时达标）。
- 分级成就带 ``threshold_metric`` + ``tiers``（Copper→Olympian 五级阈值）。
- 多条件成就带 ``requirements``（[{metric, gte}, ...]）。
- ``secret`` 成就在未产生任何进度前不暴露描述。

**关键约束**：这里列出的每个 ``threshold_metric`` / ``requirement.metric`` 都必须能由
:mod:`spirit.achievements.metrics` 从 Spirit 会话 DB（sessions + messages）派生，
保证成就是可离线计算、可测试的，不依赖任何外部埋点。
"""

from typing import Any, Dict, List

TIER_NAMES = ["Copper", "Silver", "Gold", "Diamond", "Olympian"]


def tiers(values: List[int]) -> List[Dict[str, Any]]:
    """把 5 个阈值包装为 [{name, threshold}, ...]（与 TIER_NAMES 对齐）。"""
    return [
        {"name": name, "threshold": threshold}
        for name, threshold in zip(TIER_NAMES, values)
    ]


def req(metric: str, gte: int) -> Dict[str, Any]:
    """构造一个多条件需求项。"""
    return {"metric": metric, "gte": gte}


# 指标的人类可读标签（用于 dashboard / CLI 展示）
METRIC_LABELS: Dict[str, str] = {
    "session_count": "累计会话数",
    "total_messages": "累计消息数",
    "total_tool_calls": "累计工具调用数",
    "max_tool_calls_in_session": "单会话工具调用数",
    "max_messages_in_session": "单会话消息数",
    "max_distinct_tools_in_session": "单会话不同工具数",
    "distinct_model_count": "使用过的不同模型数",
    "distinct_provider_count": "使用过的不同 provider 数",
    "weekend_sessions": "周末会话数",
    "night_sessions": "深夜（00-05 点）会话数",
    "total_terminal_calls": "累计终端调用数",
    "total_file_calls": "累计文件读写/检索调用数",
    "total_web_calls": "累计 web/浏览器调用数",
    "distinct_source_count": "使用过的不同来源平台数",
}


ACHIEVEMENTS: List[Dict[str, Any]] = [
    # --- Agent 自主 ---
    {
        "id": "let_him_cook", "name": "放手一搏", "category": "Agent 自主",
        "description": "在单个会话里跑一条像样的自主工具链。",
        "kind": "best_session", "icon": "flame",
        "threshold_metric": "max_tool_calls_in_session",
        "tiers": tiers([30, 80, 200, 500, 1200]),
    },
    {
        "id": "autonomous_avalanche", "name": "自主雪崩", "category": "Agent 自主",
        "description": "跨会话累计出雪崩般的工具调用量。",
        "kind": "lifetime", "icon": "avalanche",
        "threshold_metric": "total_tool_calls",
        "tiers": tiers([200, 800, 2500, 8000, 25000]),
    },
    {
        "id": "toolchain_maxxer", "name": "工具链拉满", "category": "Agent 自主",
        "description": "在单个会话里用上足够宽的不同工具面。",
        "kind": "best_session", "icon": "nodes",
        "threshold_metric": "max_distinct_tools_in_session",
        "tiers": tiers([6, 10, 16, 24, 35]),
    },
    {
        "id": "full_send", "name": "全力开火", "category": "Agent 自主",
        "description": "终端、文件、web/浏览器在一次真实运行里全部登场。",
        "kind": "multi_condition", "icon": "rocket",
        "requirements": [
            req("total_terminal_calls", 30),
            req("total_file_calls", 30),
            req("total_web_calls", 10),
        ],
    },

    # --- Vibe Coding ---
    {
        "id": "supposed_to_be_quick", "name": "本以为很快", "category": "Vibe Coding",
        "description": "一个小需求变成一整场远征。",
        "kind": "best_session", "icon": "melting_clock",
        "threshold_metric": "max_messages_in_session",
        "tiers": tiers([40, 100, 250, 600, 1500]),
    },
    {
        "id": "one_more_small_change", "name": "再改一点点", "category": "Vibe Coding",
        "description": "累计消息量让“小改动”这个词失效。",
        "kind": "lifetime", "icon": "pencil",
        "threshold_metric": "total_messages",
        "tiers": tiers([500, 2000, 6000, 20000, 60000]),
    },

    # --- 工具精通 ---
    {
        "id": "terminal_goblin", "name": "终端哥布林", "category": "工具精通",
        "description": "在 shell 之地花了实打实的时间。",
        "kind": "lifetime", "icon": "terminal",
        "threshold_metric": "total_terminal_calls",
        "tiers": tiers([100, 400, 1200, 4000, 12000]),
    },
    {
        "id": "file_archaeologist", "name": "文件考古学家", "category": "工具精通",
        "description": "用读取与检索把文件系统翻了个遍。",
        "kind": "lifetime", "icon": "folder",
        "threshold_metric": "total_file_calls",
        "tiers": tiers([100, 400, 1200, 4000, 12000]),
    },
    {
        "id": "rabbit_hole_certified", "name": "深挖认证", "category": "工具精通",
        "description": "web 检索/浏览多到构成一场研究漩涡。",
        "kind": "lifetime", "icon": "spiral",
        "threshold_metric": "total_web_calls",
        "tiers": tiers([50, 200, 600, 2000, 6000]),
    },

    # --- 模型传说 ---
    {
        "id": "five_model_flight", "name": "五模型巡礼", "category": "模型传说",
        "description": "至少试过五种不同 LLM，而不是从一而终。",
        "kind": "lifetime", "icon": "prism",
        "threshold_metric": "distinct_model_count",
        "tiers": tiers([5, 10, 20, 40, 80]),
    },
    {
        "id": "provider_polyglot", "name": "Provider 通才", "category": "模型传说",
        "description": "跨多个 provider 使用模型。",
        "kind": "lifetime", "icon": "swap",
        "threshold_metric": "distinct_provider_count",
        "tiers": tiers([2, 3, 5, 8, 12]),
    },

    # --- 生活方式 ---
    {
        "id": "marathon_operator", "name": "马拉松操作员", "category": "生活方式",
        "description": "累计相当数量的会话。",
        "kind": "lifetime", "icon": "marathon",
        "threshold_metric": "session_count",
        "tiers": tiers([10, 50, 150, 500, 1500]),
    },
    {
        "id": "weekend_warrior", "name": "周末勇士", "category": "生活方式",
        "description": "周末运行 Spirit 多到成为一种生活方式。",
        "kind": "lifetime", "icon": "calendar",
        "threshold_metric": "weekend_sessions",
        "tiers": tiers([5, 20, 60, 200, 600]),
    },
    {
        "id": "night_shift_operator", "name": "夜班操作员", "category": "生活方式",
        "description": "在深夜（00-05 点）反复运行会话。",
        "kind": "lifetime", "icon": "moon", "secret": True,
        "threshold_metric": "night_sessions",
        "tiers": tiers([5, 20, 60, 200, 600]),
    },
    {
        "id": "omnichannel", "name": "全渠道", "category": "生活方式",
        "description": "从多个来源平台（CLI / 网关等）接入 Spirit。",
        "kind": "lifetime", "icon": "antenna",
        "threshold_metric": "distinct_source_count",
        "tiers": tiers([2, 3, 4, 6, 8]),
    },
]


# 便捷索引
BY_ID: Dict[str, Dict[str, Any]] = {a["id"]: a for a in ACHIEVEMENTS}
CATEGORIES: List[str] = sorted({a["category"] for a in ACHIEVEMENTS})


__all__ = [
    "TIER_NAMES",
    "tiers",
    "req",
    "ACHIEVEMENTS",
    "BY_ID",
    "CATEGORIES",
    "METRIC_LABELS",
]
