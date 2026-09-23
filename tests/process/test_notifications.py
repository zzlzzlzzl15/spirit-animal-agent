"""format_process_notification 渲染单测。

对标 Hermes ``tests/tools/test_process_registry.py`` 里针对通知格式化的断言：
完成事件（正常/killed/lost/failed_start/非零退出/SIGTERM）、watch 匹配、
限流熔断摘要、异步委托（单任务 + 批量）。
"""

from __future__ import annotations

import time

from spirit.process.notifications import (
    _format_age,
    format_async_delegation,
    format_process_notification,
)


# =========================================================================
# 边界
# =========================================================================

class TestEdgeCases:
    def test_none_for_empty(self):
        assert format_process_notification({}) is None

    def test_none_for_non_dict(self):
        assert format_process_notification("not a dict") is None
        assert format_process_notification(None) is None


# =========================================================================
# 完成事件
# =========================================================================

class TestCompletion:
    def test_normal_exit(self):
        text = format_process_notification({
            "type": "completion", "session_id": "p1", "command": "npm test",
            "exit_code": 0, "output": "all passed",
        })
        assert "completed normally" in text
        assert "exit code 0" in text
        assert "npm test" in text
        assert "all passed" in text
        assert text.startswith("[IMPORTANT:")

    def test_nonzero_exit(self):
        text = format_process_notification({
            "type": "completion", "session_id": "p1", "command": "make",
            "exit_code": 2, "output": "boom",
        })
        assert "exited" in text
        assert "exit code 2" in text
        assert "completed normally" not in text

    def test_killed_by_us(self):
        text = format_process_notification({
            "type": "completion", "session_id": "p1", "command": "server",
            "exit_code": -15, "completion_reason": "killed",
            "termination_source": "process.kill", "output": "",
        })
        assert "terminated by process.kill" in text
        assert "SIGTERM" in text

    def test_killed_default_source(self):
        text = format_process_notification({
            "type": "completion", "session_id": "p1", "command": "server",
            "exit_code": -15, "completion_reason": "killed", "output": "",
        })
        assert "terminated by Spirit" in text

    def test_external_sigterm_not_reported_as_killed(self):
        """退出码 143 但 reason 非 killed → 报 exited（区分外部 SIGTERM）。"""
        text = format_process_notification({
            "type": "completion", "session_id": "p1", "command": "x",
            "exit_code": 143, "completion_reason": "exited", "output": "",
        })
        assert "terminated by" not in text
        assert "exited" in text
        assert "SIGTERM" in text

    def test_lost(self):
        text = format_process_notification({
            "type": "completion", "session_id": "p1", "command": "x",
            "exit_code": None, "completion_reason": "lost", "output": "",
        })
        assert "marked lost" in text

    def test_failed_start(self):
        text = format_process_notification({
            "type": "completion", "session_id": "p1", "command": "nope",
            "exit_code": -1, "completion_reason": "failed_start", "output": "err",
        })
        assert "failed to start" in text

    def test_default_type_is_completion(self):
        """缺 type 字段时按 completion 处理（向后兼容）。"""
        text = format_process_notification({
            "session_id": "p1", "command": "x", "exit_code": 0, "output": "",
        })
        assert "completed normally" in text


# =========================================================================
# watch 匹配 / 熔断摘要
# =========================================================================

class TestWatchEvents:
    def test_watch_match(self):
        text = format_process_notification({
            "type": "watch_match", "session_id": "p1", "command": "npm run dev",
            "pattern": "ready in", "output": "ready in 300ms", "suppressed": 0,
        })
        assert 'matched watch pattern "ready in"' in text
        assert "ready in 300ms" in text
        assert "suppressed" not in text

    def test_watch_match_with_suppressed(self):
        text = format_process_notification({
            "type": "watch_match", "session_id": "p1", "command": "x",
            "pattern": "tick", "output": "tick 1", "suppressed": 7,
        })
        assert "7 earlier matches were suppressed" in text

    def test_watch_disabled(self):
        text = format_process_notification({
            "type": "watch_disabled", "message": "Watch patterns disabled for p1",
        })
        assert text == "[IMPORTANT: Watch patterns disabled for p1]"

    def test_watch_overflow_tripped(self):
        text = format_process_notification({
            "type": "watch_overflow_tripped", "message": "overflow: >15 in 10s",
        })
        assert text.startswith("[IMPORTANT:")
        assert "overflow" in text

    def test_watch_overflow_released(self):
        text = format_process_notification({
            "type": "watch_overflow_released", "message": "resumed, 3 suppressed",
        })
        assert "resumed" in text

    def test_empty_message_returns_none(self):
        assert format_process_notification({"type": "watch_disabled", "message": ""}) is None


# =========================================================================
# 异步委托
# =========================================================================

class TestAsyncDelegation:
    def test_single_completed(self):
        now = time.time()
        text = format_process_notification({
            "type": "async_delegation", "delegation_id": "d1",
            "goal": "analyze logs", "status": "completed",
            "summary": "found 3 errors", "api_calls": 5,
            "duration_seconds": 12, "dispatched_at": now - 30,
            "completed_at": now, "role": "leaf", "model": "gpt-4o",
        })
        assert "[ASYNC DELEGATION COMPLETE — d1]" in text
        assert "Original goal: analyze logs" in text
        assert "--- RESULT ---" in text
        assert "found 3 errors" in text
        assert "ago)" in text

    def test_single_interrupted(self):
        text = format_process_notification({
            "type": "async_delegation", "delegation_id": "d2",
            "goal": "long task", "status": "interrupted",
            "error": "user sent a message", "summary": "partial",
        })
        assert "was interrupted before completing" in text
        assert "user sent a message" in text
        assert "Partial output:" in text
        assert "partial" in text

    def test_single_error(self):
        text = format_process_notification({
            "type": "async_delegation", "delegation_id": "d3",
            "goal": "x", "status": "error", "error": "API failed",
        })
        assert "did not complete successfully" in text
        assert "status=error" in text
        assert "API failed" in text

    def test_batch(self):
        now = time.time()
        text = format_process_notification({
            "type": "async_delegation", "delegation_id": "b1", "is_batch": True,
            "goals": ["task A", "task B"],
            "results": [
                {"task_index": 0, "status": "completed", "summary": "A done",
                 "api_calls": 2, "duration_seconds": 5},
                {"task_index": 1, "status": "error", "error": "B failed"},
            ],
            "dispatched_at": now - 20, "completed_at": now,
            "total_duration_seconds": 20, "role": "leaf", "model": "gpt-4o",
        })
        assert "[ASYNC DELEGATION BATCH COMPLETE — b1]" in text
        assert "fan-out of 2 subagent(s)" in text
        assert "TASK 1/2: task A" in text
        assert "A done" in text
        assert "TASK 2/2: task B" in text
        assert "✓" in text and "✗" in text

    def test_batch_error_no_results(self):
        text = format_process_notification({
            "type": "async_delegation", "delegation_id": "b2", "is_batch": True,
            "goals": ["x"], "results": [], "error": "whole batch died",
        })
        assert "--- ERROR ---" in text
        assert "whole batch died" in text

    def test_alias_export(self):
        """format_async_delegation 是 _format_async_delegation 的稳定别名。"""
        out = format_async_delegation({
            "delegation_id": "d9", "goal": "g", "status": "completed",
            "summary": "s",
        })
        assert "[ASYNC DELEGATION COMPLETE — d9]" in out


# =========================================================================
# _format_age
# =========================================================================

class TestFormatAge:
    def test_seconds(self):
        assert _format_age(45) == "45s"

    def test_minutes(self):
        assert _format_age(120) == "2m"
        assert _format_age(125) == "2m5s"

    def test_hours(self):
        assert _format_age(3600) == "1h"
        assert _format_age(3780) == "1h3m"

    def test_negative_clamped(self):
        assert _format_age(-10) == "0s"

    def test_invalid(self):
        assert _format_age("oops") == "?"
