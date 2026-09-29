"""spirit.achievements.metrics 单元测试 —— 指标采集。"""

import json
import sqlite3

import pytest

from spirit.achievements.metrics import (
    MetricsCollector,
    _classify_tool,
    _infer_provider,
    collect_metrics,
)

from tests.achievements.conftest import add_message, add_session, toolcall


# 2026-09-26 是周六，2026-09-24 是周四（系统时间 2026-09-27 为周日）
SATURDAY_2AM = "2026-09-26 02:00:00"
THURSDAY_2PM = "2026-09-24 14:00:00"


class TestClassifyTool:
    @pytest.mark.parametrize("name,bucket", [
        ("terminal", "terminal"),
        ("run_command", "terminal"),
        ("bash", "terminal"),
        ("process_poll", "terminal"),
        ("read_file", "file"),
        ("grep", "file"),
        ("str_replace_editor", "file"),
        ("web_search", "web"),
        ("browser_navigate", "web"),
        ("fetch_url", "web"),
        ("unknown_thing", None),
        ("", None),
    ])
    def test_classification(self, name, bucket):
        assert _classify_tool(name) == bucket

    def test_web_search_not_file(self):
        # web_search 含 'search'，但必须先归 web 而非 file
        assert _classify_tool("web_search") == "web"


class TestInferProvider:
    @pytest.mark.parametrize("model,provider", [
        ("gpt-4-turbo", "openai"),
        ("gpt-4o", "openai"),
        ("claude-3-opus", "anthropic"),
        ("gemini-1.5-pro", "google"),
        ("MiniMax-M3", "minimax"),
        ("deepseek-chat", "deepseek"),
        ("qwen-max", "dashscope"),
        ("glm-4", "zhipu"),
        ("some-local-model", "other"),
    ])
    def test_provider_rules(self, model, provider):
        assert _infer_provider(model) == provider

    def test_empty_model(self):
        assert _infer_provider("") == "other"


class TestCollect:
    def test_empty_db_all_zero(self, db):
        m = collect_metrics(db)
        assert m["session_count"] == 0
        assert m["total_tool_calls"] == 0
        assert m["distinct_model_count"] == 0

    def test_session_and_message_counts(self, db):
        add_session(db, "s1")
        add_session(db, "s2")
        add_message(db, "s1")
        add_message(db, "s1")
        add_message(db, "s2")
        m = collect_metrics(db)
        assert m["session_count"] == 2
        assert m["total_messages"] == 3
        assert m["max_messages_in_session"] == 2

    def test_tool_call_aggregation(self, db):
        add_session(db, "s1")
        add_message(db, "s1", tool_calls=toolcall(["read_file", "grep"]))
        add_message(db, "s1", tool_calls=toolcall(["terminal"]))
        m = collect_metrics(db)
        assert m["total_tool_calls"] == 3
        assert m["max_tool_calls_in_session"] == 3
        assert m["max_distinct_tools_in_session"] == 3
        assert m["total_file_calls"] == 2
        assert m["total_terminal_calls"] == 1

    def test_distinct_models_and_providers(self, db):
        add_session(db, "s1", model="gpt-4")
        add_session(db, "s2", model="claude-3-opus")
        add_session(db, "s3", model="gpt-4o")
        m = collect_metrics(db)
        assert m["distinct_model_count"] == 3
        # gpt-4 + gpt-4o → openai，claude → anthropic ⇒ 2 providers
        assert m["distinct_provider_count"] == 2

    def test_distinct_sources(self, db):
        add_session(db, "s1", source="cli")
        add_session(db, "s2", source="telegram")
        m = collect_metrics(db)
        assert m["distinct_source_count"] == 2

    def test_weekend_and_night_sessions(self, db):
        add_session(db, "sat_night", started_at=SATURDAY_2AM)   # 周末 + 深夜
        add_session(db, "thu_day", started_at=THURSDAY_2PM)     # 都不是
        m = collect_metrics(db)
        assert m["weekend_sessions"] == 1
        assert m["night_sessions"] == 1

    def test_accepts_session_db_wrapper(self, db):
        class FakeSessionDB:
            def __init__(self, conn):
                self.conn = conn
        add_session(db, "s1")
        m = MetricsCollector(FakeSessionDB(db)).collect()
        assert m["session_count"] == 1

    def test_broken_connection_fails_soft(self):
        class BrokenConn:
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("boom")
        m = collect_metrics(BrokenConn())
        assert m["session_count"] == 0
        assert m["total_tool_calls"] == 0

    def test_malformed_tool_calls_ignored(self, db):
        add_session(db, "s1")
        add_message(db, "s1", tool_calls="{not valid json")
        m = collect_metrics(db)
        assert m["total_tool_calls"] == 0
        assert m["total_messages"] == 1
