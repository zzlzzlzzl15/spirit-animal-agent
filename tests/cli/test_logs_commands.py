"""tests/cli/test_logs_commands.py — 日志查询纯函数。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from spirit.cli.logs_commands import (
    cmd_doctor,
    cmd_errors,
    cmd_events,
    parse_since,
    query_events,
    read_log_tail,
)
from spirit.logging_setup import log_event, reset_logging, setup_logging


@pytest.fixture
def populated(tmp_path):
    d = tmp_path / "logs"
    setup_logging(verbose=False, log_dir=d, force=True)
    log_event("api_error", level=logging.ERROR, reason="invalid_request",
              provider="minimax", session_id="sess-abc", iteration=9)
    log_event("compression", level=logging.INFO, before_msgs=109, after_msgs=12)
    log_event("payload_invalid", level=logging.WARNING, session_id="sess-abc")
    log_event("tool_error", level=logging.ERROR, tool="web_fetch",
              error="timeout")
    yield d
    reset_logging()


class TestParseSince:
    def test_units(self):
        assert parse_since("30s") is not None
        assert parse_since("2h") is not None
        assert parse_since("1d") is not None

    def test_none_and_garbage(self):
        assert parse_since(None) is None
        assert parse_since("not-a-time") is None


class TestQueryEvents:

    def test_filter_by_kind(self, populated):
        r = query_events(log_dir=populated, kinds=["api_error"])
        assert r["ok"]
        assert {e["kind"] for e in r["events"]} == {"api_error"}

    def test_errors_helper_defaults(self, populated):
        r = cmd_errors(log_dir=populated)
        kinds = {e["kind"] for e in r["events"]}
        assert "api_error" in kinds
        assert "tool_error" in kinds
        assert "compression" not in kinds  # INFO 被 WARNING 门槛过滤

    def test_session_filter(self, populated):
        r = query_events(log_dir=populated, session="sess-ab")
        assert r["ok"]
        assert all("sess-abc" in e.get("session_id", "") for e in r["events"])
        assert len(r["events"]) == 2

    def test_reason_filter(self, populated):
        r = query_events(log_dir=populated, reason="invalid_request")
        assert len(r["events"]) == 1
        assert r["events"][0]["reason"] == "invalid_request"

    def test_grep_filter(self, populated):
        r = query_events(log_dir=populated, grep="web_fetch")
        assert len(r["events"]) == 1
        assert r["events"][0]["kind"] == "tool_error"

    def test_events_helper_kind_csv(self, populated):
        r = cmd_events(kind="api_error,tool_error", log_dir=populated)
        assert {e["kind"] for e in r["events"]} == {"api_error", "tool_error"}

    def test_missing_file_is_ok(self, tmp_path):
        r = query_events(log_dir=tmp_path / "nope")
        assert r["ok"]
        assert r["events"] == []

    def test_limit(self, populated):
        r = query_events(log_dir=populated, limit=2)
        assert len(r["events"]) <= 2


class TestReadLogTail:

    def test_tail_lines(self, populated):
        r = read_log_tail("agent.log", lines=1, log_dir=populated)
        assert r["ok"]
        assert len(r["lines"]) == 1

    def test_missing_file(self, tmp_path):
        r = read_log_tail("nope.log", log_dir=tmp_path)
        assert r["ok"]
        assert r["lines"] == []


class TestDoctor:

    def test_report_shape(self):
        r = cmd_doctor()
        assert r["ok"]
        names = {c["name"] for c in r["checks"]}
        assert {"config", "storage", "logs", "pairing_checker", "spirit_home"} <= names

    def test_config_has_sources(self):
        r = cmd_doctor()
        config_check = next(c for c in r["checks"] if c["name"] == "config")
        values = config_check["detail"]["values"]
        keys = {row["key"] for row in values}
        assert "llm.model" in keys
        assert all(row["source"] in ("default", "yaml/env") for row in values)
