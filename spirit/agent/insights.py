"""会话历史分析引擎 —— 从 sessions/messages 派生使用洞察。

移植自 Hermes ``agent/insights.py``（按 Spirit 精简会话 schema 重新设计）。

Hermes 的 sessions 表含 token/cost/billing 列；Spirit 的 schema 更精简
（``sessions(id, source, model, cwd, started_at, ended_at, end_reason, metadata)`` +
``messages(session_id, role, content, tool_calls, created_at)``），故本引擎从这两张表
直接聚合：会话/消息总量、模型分布、平台（source）分布、工具调用分布、按小时活动
模式、Top 会话。成本估算不在 Spirit schema 内，若 ``metadata`` JSON 携带
``input_tokens``/``output_tokens`` 则做粗略汇总，否则跳过。

设计约定：
- **可注入 db**：接受 ``SessionDB``（用其 ``.conn``）或裸 ``sqlite3.Connection``
  （须设 ``row_factory = sqlite3.Row``）。测试用内存库即可，完全离线。
- **fail-soft**：任何查询/解析异常都降级为空结果，绝不抛。
"""

import json
import logging
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _bar_chart(values: List[int], max_width: int = 20) -> List[str]:
    """从数值生成简单横向条形图字符串。"""
    peak = max(values) if values else 1
    if peak == 0:
        return ["" for _ in values]
    return ["█" * max(1, int(v / peak * max_width)) if v > 0 else "" for v in values]


def _parse_timestamp(value: Any) -> Optional[float]:
    """把 sessions.started_at 解析为 epoch 秒。

    Spirit 用 ``TIMESTAMP DEFAULT CURRENT_TIMESTAMP``，存为文本
    ``"YYYY-MM-DD HH:MM:SS"``（UTC）；也兼容直接存 epoch 数值的情况。
    解析失败返回 None（调用方据此跳过该行的时间过滤/分桶）。
    """
    if value is None:
        return None
    # 已是数值（int/float epoch）
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    # 纯数字字符串当作 epoch
    try:
        return float(text)
    except ValueError:
        pass
    # SQLite CURRENT_TIMESTAMP 格式（含 T 分隔或空格分隔，可能带毫秒/时区）
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S.%f",
    ):
        try:
            return datetime.strptime(text[:26], fmt).timestamp()
        except ValueError:
            continue
    # 最后尝试 fromisoformat（Python 3.7+，容忍时区偏移）
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


class InsightsEngine:
    """分析会话历史并产出使用洞察。

    直接基于 ``SessionDB`` 实例（或裸 sqlite3 连接）查询会话与消息数据。
    """

    def __init__(self, db):
        """用一个 SessionDB 实例（或裸连接）初始化。

        Args:
            db: ``SessionDB`` 实例（取其 ``.conn``），或已设 ``row_factory`` 的
                裸 ``sqlite3.Connection``。
        """
        self.db = db
        conn = getattr(db, "conn", None) or getattr(db, "_conn", None) or db
        self._conn = conn

    # ------------------------------------------------------------------
    # 数据聚合
    # ------------------------------------------------------------------

    def _fetch_sessions(self) -> List[Dict[str, Any]]:
        try:
            cur = self._conn.execute(
                "SELECT id, source, model, cwd, started_at, ended_at, "
                "end_reason, metadata FROM sessions"
            )
            return [dict(row) for row in cur.fetchall()]
        except Exception as exc:
            logger.debug("insights: 读取 sessions 失败: %s", exc)
            return []

    def _fetch_messages(self) -> List[Dict[str, Any]]:
        try:
            cur = self._conn.execute(
                "SELECT session_id, role, tool_calls, created_at FROM messages"
            )
            return [dict(row) for row in cur.fetchall()]
        except Exception as exc:
            logger.debug("insights: 读取 messages 失败: %s", exc)
            return []

    @staticmethod
    def _filter_by_window(
        sessions: List[Dict[str, Any]], cutoff: float
    ) -> List[Dict[str, Any]]:
        """保留 started_at >= cutoff 的会话；无法解析时间的会话保守保留。"""
        out = []
        for s in sessions:
            ts = _parse_timestamp(s.get("started_at"))
            if ts is None or ts >= cutoff:
                out.append(s)
        return out

    @staticmethod
    def _parse_tool_calls(raw: Any) -> List[str]:
        """从 messages.tool_calls（JSON 文本）提取工具名列表。

        兼容两种形状：``[{"function": {"name": ...}}]``（OpenAI 风格）与
        ``[{"name": ...}]``。畸形/空值降级为空列表。
        """
        if not raw:
            return []
        if isinstance(raw, list):
            data = raw
        else:
            try:
                data = json.loads(raw)
            except Exception:
                return []
        if not isinstance(data, list):
            return []
        names = []
        for item in data:
            if not isinstance(item, dict):
                continue
            fn = item.get("function")
            if isinstance(fn, dict) and fn.get("name"):
                names.append(str(fn["name"]))
            elif item.get("name"):
                names.append(str(item["name"]))
        return names

    # ------------------------------------------------------------------
    # 报表生成
    # ------------------------------------------------------------------

    def generate(self, days: int = 30, source: Optional[str] = None) -> Dict[str, Any]:
        """生成完整洞察报表。

        Args:
            days: 回溯天数（默认 30）。
            source: 可选，按来源平台过滤。

        Returns:
            含全部计算洞察的 dict；无数据时 ``empty=True``。
        """
        cutoff = time.time() - (days * 86400)

        sessions = self._fetch_sessions()
        if source:
            sessions = [s for s in sessions if (s.get("source") or "") == source]
        sessions = self._filter_by_window(sessions, cutoff)
        messages = self._fetch_messages()

        if not sessions:
            return {
                "days": days,
                "source_filter": source,
                "empty": True,
                "overview": {},
                "models": [],
                "platforms": [],
                "tools": [],
                "activity": {},
                "top_sessions": [],
            }

        session_ids = {s.get("id") for s in sessions}
        # 仅统计属于窗口内会话的消息
        msgs = [m for m in messages if m.get("session_id") in session_ids]

        models = self._compute_model_breakdown(sessions)
        platforms = self._compute_platform_breakdown(sessions)
        tools = self._compute_tool_breakdown(msgs)
        activity = self._compute_activity_patterns(sessions)
        top_sessions = self._compute_top_sessions(sessions, msgs)
        overview = self._compute_overview(sessions, msgs, models, tools)

        return {
            "days": days,
            "source_filter": source,
            "empty": False,
            "generated_at": time.time(),
            "overview": overview,
            "models": models,
            "platforms": platforms,
            "tools": tools,
            "activity": activity,
            "top_sessions": top_sessions,
        }

    def _compute_overview(
        self,
        sessions: List[Dict[str, Any]],
        msgs: List[Dict[str, Any]],
        models: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        total_sessions = len(sessions)
        total_messages = len(msgs)
        tool_calls = sum(t["count"] for t in tools)
        distinct_models = len({m["model"] for m in models if m.get("model")})

        # 活跃天数（按 started_at 的日期去重）
        days = set()
        for s in sessions:
            ts = _parse_timestamp(s.get("started_at"))
            if ts is not None:
                days.add(datetime.fromtimestamp(ts).date().isoformat())

        # 粗略 token 汇总（若 metadata 携带）
        input_tokens = output_tokens = 0
        for s in sessions:
            meta = s.get("metadata")
            if not meta:
                continue
            try:
                md = json.loads(meta) if isinstance(meta, str) else meta
            except Exception:
                continue
            if isinstance(md, dict):
                input_tokens += int(md.get("input_tokens") or 0)
                output_tokens += int(md.get("output_tokens") or 0)

        return {
            "total_sessions": total_sessions,
            "total_messages": total_messages,
            "total_tool_calls": tool_calls,
            "distinct_models": distinct_models,
            "active_days": len(days),
            "avg_messages_per_session": (
                round(total_messages / total_sessions, 1) if total_sessions else 0.0
            ),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }

    def _compute_model_breakdown(
        self, sessions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        counts: Counter = Counter()
        for s in sessions:
            counts[s.get("model") or "(unknown)"] += 1
        total = sum(counts.values()) or 1
        rows = [
            {
                "model": model,
                "sessions": cnt,
                "percentage": round(cnt / total * 100, 1),
            }
            for model, cnt in counts.most_common()
        ]
        return rows

    def _compute_platform_breakdown(
        self, sessions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        counts: Counter = Counter()
        for s in sessions:
            counts[s.get("source") or "(unknown)"] += 1
        total = sum(counts.values()) or 1
        return [
            {
                "platform": platform,
                "sessions": cnt,
                "percentage": round(cnt / total * 100, 1),
            }
            for platform, cnt in counts.most_common()
        ]

    def _compute_tool_breakdown(
        self, msgs: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        counts: Counter = Counter()
        for m in msgs:
            for name in self._parse_tool_calls(m.get("tool_calls")):
                counts[name] += 1
        total = sum(counts.values()) or 1
        return [
            {
                "tool": tool,
                "count": cnt,
                "percentage": round(cnt / total * 100, 1),
            }
            for tool, cnt in counts.most_common()
        ]

    def _compute_activity_patterns(
        self, sessions: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """按小时（0-23）统计会话启动分布，并给出最活跃小时。"""
        by_hour = [0] * 24
        by_weekday = [0] * 7
        for s in sessions:
            ts = _parse_timestamp(s.get("started_at"))
            if ts is None:
                continue
            dt = datetime.fromtimestamp(ts)
            by_hour[dt.hour] += 1
            by_weekday[dt.weekday()] += 1
        peak_hour = max(range(24), key=lambda h: by_hour[h]) if any(by_hour) else None
        return {
            "by_hour": by_hour,
            "by_weekday": by_weekday,
            "peak_hour": peak_hour,
            "hour_bars": _bar_chart(by_hour),
        }

    def _compute_top_sessions(
        self, sessions: List[Dict[str, Any]], msgs: List[Dict[str, Any]], limit: int = 5
    ) -> List[Dict[str, Any]]:
        msg_counts: Counter = Counter()
        tool_counts: Counter = Counter()
        for m in msgs:
            sid = m.get("session_id")
            msg_counts[sid] += 1
            tool_counts[sid] += len(self._parse_tool_calls(m.get("tool_calls")))
        rows = []
        for s in sessions:
            sid = s.get("id")
            rows.append({
                "id": sid,
                "model": s.get("model") or "(unknown)",
                "source": s.get("source") or "(unknown)",
                "message_count": msg_counts.get(sid, 0),
                "tool_call_count": tool_counts.get(sid, 0),
                "started_at": s.get("started_at"),
            })
        rows.sort(key=lambda r: r["message_count"], reverse=True)
        return rows[:limit]

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    def format_terminal(self, report: Dict[str, Any]) -> str:
        """把报表渲染为终端可读文本。"""
        if not report or report.get("empty"):
            days = report.get("days", 30) if report else 30
            return f"最近 {days} 天没有会话数据。"

        ov = report.get("overview", {})
        lines: List[str] = []
        days = report.get("days", 30)
        src = report.get("source_filter")
        scope = f"（来源：{src}）" if src else ""
        lines.append(f"📊 Spirit 使用洞察 · 最近 {days} 天{scope}")
        lines.append("═" * 44)
        lines.append(
            f"  会话 {ov.get('total_sessions', 0)} · "
            f"消息 {ov.get('total_messages', 0)} · "
            f"工具调用 {ov.get('total_tool_calls', 0)}"
        )
        lines.append(
            f"  活跃天数 {ov.get('active_days', 0)} · "
            f"模型 {ov.get('distinct_models', 0)} 种 · "
            f"均消息/会话 {ov.get('avg_messages_per_session', 0)}"
        )

        models = report.get("models", [])
        if models:
            lines.append("")
            lines.append("模型分布：")
            for m in models[:8]:
                lines.append(
                    f"  {m['model']:<28} {m['sessions']:>4}  ({m['percentage']}%)"
                )

        tools = report.get("tools", [])
        if tools:
            lines.append("")
            lines.append("工具使用 Top：")
            for t in tools[:10]:
                lines.append(
                    f"  {t['tool']:<28} {t['count']:>4}  ({t['percentage']}%)"
                )

        platforms = report.get("platforms", [])
        if platforms:
            lines.append("")
            lines.append("平台分布：")
            for p in platforms[:8]:
                lines.append(
                    f"  {p['platform']:<28} {p['sessions']:>4}  ({p['percentage']}%)"
                )

        activity = report.get("activity", {})
        peak = activity.get("peak_hour")
        if peak is not None:
            lines.append("")
            lines.append(f"最活跃时段：{peak:02d}:00")

        top = report.get("top_sessions", [])
        if top:
            lines.append("")
            lines.append("Top 会话（按消息数）：")
            for s in top:
                sid = str(s.get("id") or "")[:12]
                lines.append(
                    f"  {sid:<14} {s['model']:<20} "
                    f"msg={s['message_count']:<4} tool={s['tool_call_count']}"
                )

        return "\n".join(lines)


__all__ = ["InsightsEngine"]
