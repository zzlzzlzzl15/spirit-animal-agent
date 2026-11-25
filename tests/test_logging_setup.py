"""tests/test_logging_setup.py — 统一日志系统（v0.3.5）。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from spirit.logging_setup import (
    get_events_path,
    get_log_dir,
    log_event,
    reset_logging,
    setup_logging,
)


@pytest.fixture
def log_dir(tmp_path):
    d = tmp_path / "logs"
    setup_logging(verbose=True, log_dir=d, force=True)
    yield d
    reset_logging()


class TestSetupLogging:

    def test_creates_three_targets(self, log_dir):
        logging.getLogger("spirit.x").info("hi")
        logging.getLogger("spirit.x").error("boom")
        assert (log_dir / "agent.log").exists()
        assert (log_dir / "errors.log").exists()
        # events.jsonl 只在 log_event 时才创建
        log_event("probe", level=logging.INFO)
        assert (log_dir / "events.jsonl").exists()

    def test_idempotent(self, log_dir):
        spirit = logging.getLogger("spirit")
        n_before = len(spirit.handlers)
        setup_logging(verbose=True, log_dir=log_dir)  # 再次调用
        assert len(spirit.handlers) == n_before

    def test_errors_log_only_errors(self, log_dir):
        logging.getLogger("spirit.a").info("info-only")
        logging.getLogger("spirit.b").error("err-only")
        errors = (log_dir / "errors.log").read_text(encoding="utf-8")
        assert "err-only" in errors
        assert "info-only" not in errors

    def test_agent_log_has_info(self, log_dir):
        logging.getLogger("spirit.a").info("info-line-xyz")
        assert "info-line-xyz" in (log_dir / "agent.log").read_text(encoding="utf-8")


class TestLogEvent:

    def test_event_written_as_jsonl(self, log_dir):
        log_event("api_error", level=logging.ERROR, reason="timeout", retry=2)
        lines = (log_dir / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
        ev = json.loads(lines[-1])
        assert ev["kind"] == "api_error"
        assert ev["level"] == "ERROR"
        assert ev["reason"] == "timeout"
        assert ev["retry"] == 2
        assert "ts" in ev and "pid" in ev

    def test_secret_redacted(self, log_dir):
        log_event("provider_switch", message="key sk-cp-abcdef1234567890_secret tail")
        ev = json.loads((log_dir / "events.jsonl").read_text().strip().splitlines()[-1])
        assert "abcdef1234567890_secret" not in ev["message"]

    def test_long_field_truncated(self, log_dir):
        big = "x" * 5000
        ev0 = log_event("compression", detail=big)
        assert len(ev0["detail"]) < 5000
        assert ev0["detail"].endswith(")")

    def test_exception_traceback(self, log_dir):
        try:
            raise ValueError("boom-value")
        except ValueError as exc:
            ev = log_event("api_error", level=logging.ERROR, exc=exc)
        assert ev["exc_type"] == "ValueError"
        assert "boom-value" in ev["exc"]
        assert "ValueError" in ev["traceback"]

    def test_paths_follow_log_dir(self, tmp_path):
        d = tmp_path / "custom"
        assert get_log_dir(d) == d
        assert get_events_path(d) == d / "events.jsonl"
