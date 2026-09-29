"""成就指标采集 —— 从 Spirit 会话 DB 派生 aggregate。

把 :mod:`spirit.achievements.definitions` 里引用的每个 metric 从 sessions + messages
两张表算出来，产出一份纯 dict，喂给 :class:`spirit.achievements.engine.AchievementEngine`。

设计约定：
- **可注入连接**：接受 ``SessionDB``（取 ``.conn``）或裸 ``sqlite3.Connection``。
- **fail-soft**：查询/解析异常降级为 0，绝不抛（成就永不阻断主流程）。
- 复用 ``spirit.agent.insights`` 的时间戳解析与 tool_calls 解析，避免重复。
"""

import logging
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional

from spirit.agent.insights import InsightsEngine, _parse_timestamp

logger = logging.getLogger(__name__)

# 工具名 → 桶（terminal / file / web）分类。用子串匹配，容忍命名差异。
_TERMINAL_HINTS = ("terminal", "run_command", "bash", "shell", "process", "exec")
_FILE_HINTS = (
    "read_file", "write_file", "write", "edit", "search_files", "search",
    "grep", "glob", "patch", "str_replace", "multiedit", "file",
)
_WEB_HINTS = (
    "web_search", "browser", "fetch", "extract", "x_search", "web",
    "scrape", "url",
)


def _classify_tool(name: str) -> Optional[str]:
    """把一个工具名归类到 terminal / file / web（无匹配返回 None）。

    顺序敏感：先判 terminal（最具体），再 web，再 file（file 的 'search' 等
    提示词较宽，放最后以免吞掉 web_search）。
    """
    low = (name or "").lower()
    if not low:
        return None
    if any(h in low for h in _TERMINAL_HINTS):
        return "terminal"
    if any(h in low for h in _WEB_HINTS):
        return "web"
    if any(h in low for h in _FILE_HINTS):
        return "file"
    return None


# 从模型名粗略推断 provider（ Spirit 会话 schema 无独立 provider 列）。
_PROVIDER_RULES = [
    ("gpt", "openai"), ("o1", "openai"), ("o3", "openai"), ("chatgpt", "openai"),
    ("claude", "anthropic"),
    ("gemini", "google"), ("bard", "google"),
    ("minimax", "minimax"),
    ("deepseek", "deepseek"),
    ("qwen", "dashscope"), ("通义", "dashscope"),
    ("glm", "zhipu"), ("chatglm", "zhipu"),
    ("llama", "meta"), ("mistral", "mistral"), ("grok", "xai"),
    ("moonshot", "moonshot"), ("kimi", "moonshot"),
]


def _infer_provider(model: str) -> str:
    low = (model or "").lower()
    for needle, provider in _PROVIDER_RULES:
        if needle in low:
            return provider
    return "other"


class MetricsCollector:
    """从会话 DB 采集成就指标。"""

    def __init__(self, db):
        conn = getattr(db, "conn", None) or getattr(db, "_conn", None) or db
        self._conn = conn

    def _fetch_sessions(self) -> List[Dict[str, Any]]:
        try:
            cur = self._conn.execute(
                "SELECT id, source, model, started_at FROM sessions"
            )
            return [dict(row) for row in cur.fetchall()]
        except Exception as exc:
            logger.debug("achievements.metrics: 读取 sessions 失败: %s", exc)
            return []

    def _fetch_messages(self) -> List[Dict[str, Any]]:
        try:
            cur = self._conn.execute(
                "SELECT session_id, role, tool_calls FROM messages"
            )
            return [dict(row) for row in cur.fetchall()]
        except Exception as exc:
            logger.debug("achievements.metrics: 读取 messages 失败: %s", exc)
            return []

    def collect(self) -> Dict[str, int]:
        """采集全部指标，返回 ``{metric_name: int_value}``。"""
        sessions = self._fetch_sessions()
        messages = self._fetch_messages()

        session_count = len(sessions)
        total_messages = len(messages)

        models = {s.get("model") for s in sessions if s.get("model")}
        providers = {_infer_provider(m) for m in models}
        sources = {s.get("source") for s in sessions if s.get("source")}

        weekend_sessions = 0
        night_sessions = 0
        for s in sessions:
            ts = _parse_timestamp(s.get("started_at"))
            if ts is None:
                continue
            dt = datetime.fromtimestamp(ts)
            if dt.weekday() >= 5:  # 5=周六, 6=周日
                weekend_sessions += 1
            if 0 <= dt.hour < 5:
                night_sessions += 1

        # 逐会话工具聚合
        per_session_tool_calls: Counter = Counter()
        per_session_distinct_tools: Dict[str, set] = {}
        total_tool_calls = 0
        bucket_calls: Counter = Counter()

        for m in messages:
            sid = m.get("session_id")
            names = InsightsEngine._parse_tool_calls(m.get("tool_calls"))
            if names:
                per_session_tool_calls[sid] += len(names)
                per_session_distinct_tools.setdefault(sid, set()).update(names)
                total_tool_calls += len(names)
                for name in names:
                    bucket = _classify_tool(name)
                    if bucket:
                        bucket_calls[bucket] += 1

        max_tool_calls_in_session = (
            max(per_session_tool_calls.values()) if per_session_tool_calls else 0
        )
        max_distinct_tools_in_session = (
            max((len(v) for v in per_session_distinct_tools.values()), default=0)
        )

        # 单会话最大消息数
        per_session_messages: Counter = Counter()
        for m in messages:
            per_session_messages[m.get("session_id")] += 1
        max_messages_in_session = (
            max(per_session_messages.values()) if per_session_messages else 0
        )

        return {
            "session_count": session_count,
            "total_messages": total_messages,
            "total_tool_calls": total_tool_calls,
            "max_tool_calls_in_session": max_tool_calls_in_session,
            "max_messages_in_session": max_messages_in_session,
            "max_distinct_tools_in_session": max_distinct_tools_in_session,
            "distinct_model_count": len(models),
            "distinct_provider_count": len(providers),
            "distinct_source_count": len(sources),
            "weekend_sessions": weekend_sessions,
            "night_sessions": night_sessions,
            "total_terminal_calls": bucket_calls.get("terminal", 0),
            "total_file_calls": bucket_calls.get("file", 0),
            "total_web_calls": bucket_calls.get("web", 0),
        }


def collect_metrics(db) -> Dict[str, int]:
    """便捷函数：从一个 SessionDB / 裸连接采集成就指标。"""
    return MetricsCollector(db).collect()


__all__ = ["MetricsCollector", "collect_metrics"]
