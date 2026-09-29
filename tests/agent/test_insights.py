"""spirit.agent.insights 单元测试 —— 会话分析引擎。

离线。用内存 sqlite 建 Spirit 会话 schema（sessions + messages），造数据后验证
总览/模型/平台/工具/活动/Top 会话聚合，以及时间窗口过滤、source 过滤、空库、
畸形 tool_calls、时间戳解析等 fail-soft 路径。
"""

import json
import sqlite3
import time

import pytest

from spirit.agent import insights
from spirit.agent.insights import InsightsEngine, _parse_timestamp, _bar_chart


SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'cli',
    model TEXT NOT NULL,
    system_prompt TEXT,
    cwd TEXT,
    parent_session_id TEXT,
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    end_reason TEXT,
    metadata TEXT DEFAULT '{}'
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    created_at TIMESTAMP
);
"""


def _ts(days_ago=0, hour=12):
    """返回一个距今 days_ago 天、指定小时的 SQLite 文本时间戳。"""
    t = time.time() - days_ago * 86400
    lt = time.localtime(t)
    return time.strftime("%Y-%m-%d ", lt) + f"{hour:02d}:30:00"


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _add_session(conn, sid, model="gpt-4", source="cli", days_ago=0, hour=12,
                 metadata="{}"):
    conn.execute(
        "INSERT INTO sessions (id, source, model, started_at, metadata) "
        "VALUES (?, ?, ?, ?, ?)",
        (sid, source, model, _ts(days_ago, hour), metadata),
    )


def _add_message(conn, sid, role="assistant", tool_calls=None):
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_calls) "
        "VALUES (?, ?, ?, ?)",
        (sid, role, "x", tool_calls),
    )


def _toolcall_json(name):
    return json.dumps([{"function": {"name": name}}])


# --------------------------------------------------------------------------
# 辅助函数
# --------------------------------------------------------------------------

class TestHelpers:
    def test_parse_timestamp_text(self):
        ts = _parse_timestamp("2026-09-27 12:30:00")
        assert ts is not None
        assert isinstance(ts, float)

    def test_parse_timestamp_iso_T(self):
        assert _parse_timestamp("2026-09-27T12:30:00") is not None

    def test_parse_timestamp_epoch_numeric(self):
        assert _parse_timestamp(1_700_000_000) == 1_700_000_000.0

    def test_parse_timestamp_epoch_string(self):
        assert _parse_timestamp("1700000000") == 1_700_000_000.0

    def test_parse_timestamp_invalid(self):
        assert _parse_timestamp("not-a-time") is None
        assert _parse_timestamp(None) is None
        assert _parse_timestamp("") is None

    def test_bar_chart(self):
        bars = _bar_chart([0, 5, 10], max_width=10)
        assert len(bars) == 3
        assert bars[0] == ""           # 0 → 空
        assert len(bars[2]) == 10      # 峰值 → 满宽

    def test_bar_chart_all_zero(self):
        assert _bar_chart([0, 0]) == ["", ""]

    def test_parse_tool_calls_openai_shape(self):
        raw = json.dumps([{"function": {"name": "read_file"}}])
        assert InsightsEngine._parse_tool_calls(raw) == ["read_file"]

    def test_parse_tool_calls_flat_shape(self):
        raw = json.dumps([{"name": "grep"}])
        assert InsightsEngine._parse_tool_calls(raw) == ["grep"]

    def test_parse_tool_calls_malformed(self):
        assert InsightsEngine._parse_tool_calls("{not json") == []
        assert InsightsEngine._parse_tool_calls(None) == []
        assert InsightsEngine._parse_tool_calls("") == []


# --------------------------------------------------------------------------
# 空库 / 构造
# --------------------------------------------------------------------------

class TestEmptyAndConstruction:
    def test_empty_db_returns_empty_report(self, db):
        eng = InsightsEngine(db)
        report = eng.generate(days=30)
        assert report["empty"] is True
        assert report["overview"] == {}

    def test_accepts_raw_connection(self, db):
        _add_session(db, "s1")
        eng = InsightsEngine(db)
        assert eng.generate()["empty"] is False

    def test_accepts_session_db_wrapper(self, db):
        class FakeSessionDB:
            def __init__(self, conn):
                self.conn = conn
        _add_session(db, "s1")
        eng = InsightsEngine(FakeSessionDB(db))
        assert eng.generate()["empty"] is False


# --------------------------------------------------------------------------
# 聚合
# --------------------------------------------------------------------------

class TestAggregation:
    def test_overview_counts(self, db):
        _add_session(db, "s1", model="gpt-4")
        _add_session(db, "s2", model="gpt-4")
        _add_message(db, "s1", tool_calls=_toolcall_json("read_file"))
        _add_message(db, "s1", tool_calls=_toolcall_json("grep"))
        _add_message(db, "s2")
        eng = InsightsEngine(db)
        ov = eng.generate()["overview"]
        assert ov["total_sessions"] == 2
        assert ov["total_messages"] == 3
        assert ov["total_tool_calls"] == 2
        assert ov["distinct_models"] == 1
        assert ov["avg_messages_per_session"] == 1.5

    def test_model_breakdown(self, db):
        _add_session(db, "s1", model="gpt-4")
        _add_session(db, "s2", model="gpt-4")
        _add_session(db, "s3", model="claude")
        eng = InsightsEngine(db)
        models = eng.generate()["models"]
        top = models[0]
        assert top["model"] == "gpt-4"
        assert top["sessions"] == 2
        assert top["percentage"] == pytest.approx(66.7, abs=0.1)

    def test_platform_breakdown(self, db):
        _add_session(db, "s1", source="cli")
        _add_session(db, "s2", source="telegram")
        _add_session(db, "s3", source="cli")
        eng = InsightsEngine(db)
        platforms = {p["platform"]: p["sessions"] for p in eng.generate()["platforms"]}
        assert platforms["cli"] == 2
        assert platforms["telegram"] == 1

    def test_tool_breakdown(self, db):
        _add_session(db, "s1")
        _add_message(db, "s1", tool_calls=_toolcall_json("read_file"))
        _add_message(db, "s1", tool_calls=_toolcall_json("read_file"))
        _add_message(db, "s1", tool_calls=_toolcall_json("grep"))
        eng = InsightsEngine(db)
        tools = {t["tool"]: t["count"] for t in eng.generate()["tools"]}
        assert tools["read_file"] == 2
        assert tools["grep"] == 1

    def test_activity_peak_hour(self, db):
        _add_session(db, "s1", hour=9)
        _add_session(db, "s2", hour=9)
        _add_session(db, "s3", hour=14)
        eng = InsightsEngine(db)
        activity = eng.generate()["activity"]
        assert activity["peak_hour"] == 9
        assert activity["by_hour"][9] == 2
        assert len(activity["by_hour"]) == 24

    def test_top_sessions_sorted_by_message_count(self, db):
        _add_session(db, "s1")
        _add_session(db, "s2")
        _add_message(db, "s1")
        _add_message(db, "s2")
        _add_message(db, "s2")
        _add_message(db, "s2")
        eng = InsightsEngine(db)
        top = eng.generate()["top_sessions"]
        assert top[0]["id"] == "s2"
        assert top[0]["message_count"] == 3

    def test_token_rollup_from_metadata(self, db):
        meta = json.dumps({"input_tokens": 100, "output_tokens": 50})
        _add_session(db, "s1", metadata=meta)
        eng = InsightsEngine(db)
        ov = eng.generate()["overview"]
        assert ov["input_tokens"] == 100
        assert ov["output_tokens"] == 50

    def test_malformed_metadata_ignored(self, db):
        _add_session(db, "s1", metadata="{bad json")
        eng = InsightsEngine(db)
        ov = eng.generate()["overview"]
        assert ov["input_tokens"] == 0


# --------------------------------------------------------------------------
# 过滤
# --------------------------------------------------------------------------

class TestFiltering:
    def test_source_filter(self, db):
        _add_session(db, "s1", source="cli")
        _add_session(db, "s2", source="telegram")
        eng = InsightsEngine(db)
        report = eng.generate(source="telegram")
        assert report["source_filter"] == "telegram"
        assert report["overview"]["total_sessions"] == 1

    def test_time_window_excludes_old_sessions(self, db):
        _add_session(db, "recent", days_ago=1)
        _add_session(db, "old", days_ago=100)
        eng = InsightsEngine(db)
        report = eng.generate(days=30)
        assert report["overview"]["total_sessions"] == 1

    def test_messages_of_excluded_sessions_not_counted(self, db):
        _add_session(db, "old", days_ago=100)
        _add_message(db, "old", tool_calls=_toolcall_json("grep"))
        eng = InsightsEngine(db)
        report = eng.generate(days=30)
        # old 会话被窗口排除 → 整报表为空
        assert report["empty"] is True


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------

class TestFormat:
    def test_format_empty(self, db):
        eng = InsightsEngine(db)
        out = eng.format_terminal(eng.generate(days=7))
        assert "没有会话数据" in out

    def test_format_populated(self, db):
        _add_session(db, "s1", model="gpt-4", hour=9)
        _add_message(db, "s1", tool_calls=_toolcall_json("read_file"))
        eng = InsightsEngine(db)
        out = eng.format_terminal(eng.generate())
        assert "使用洞察" in out
        assert "gpt-4" in out
        assert "read_file" in out

    def test_format_none_report(self, db):
        eng = InsightsEngine(db)
        assert "没有会话数据" in eng.format_terminal(None)


# --------------------------------------------------------------------------
# fail-soft：损坏连接
# --------------------------------------------------------------------------

class TestFailSoft:
    def test_broken_connection_degrades(self):
        class BrokenConn:
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("no such table")
        eng = InsightsEngine(BrokenConn())
        report = eng.generate()
        assert report["empty"] is True
