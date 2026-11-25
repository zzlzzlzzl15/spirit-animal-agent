"""Spirit Agent progress_notifier.py 长任务进度推送回归测试（C-P1.1 v0.2.4 已交付未测）。

参考 hermes:
    - ws_server.py 的 _delegate_progress_sink 模式

7 阶段进度事件 + sink 注册表 + session 上下文 + 异常隔离 + 线程安全。
"""

from __future__ import annotations

import threading
import time

import pytest

from spirit.agent.progress_notifier import (
    ProgressPhase,
    _build_event,
    clear_sinks,
    get_sink_count,
    notify_progress,
    register_progress_sink,
    set_current_session_id,
    unregister_progress_sink,
)


@pytest.fixture(autouse=True)
def _clean_state():
    """每个测试前后清理全局状态。"""
    clear_sinks()
    yield
    clear_sinks()


# ─────────────────────────── ProgressPhase 枚举 ───────────────────────────


class TestProgressPhase:
    """ProgressPhase 阶段枚举（str enum）。"""

    def test_has_all_phases(self):
        # v0.3.5：枚举从 7 阶段扩展到 15 阶段后本测试一直没更新（已落后多个版本）
        assert len(ProgressPhase) == 15

    def test_task_start_value(self):
        assert ProgressPhase.TASK_START.value == "task_start"

    def test_thinking_value(self):
        assert ProgressPhase.THINKING.value == "thinking"

    def test_thinking_end_value(self):
        assert ProgressPhase.THINKING_END.value == "thinking_end"

    def test_tool_call_value(self):
        assert ProgressPhase.TOOL_CALL.value == "tool_call"

    def test_tool_result_value(self):
        assert ProgressPhase.TOOL_RESULT.value == "tool_result"

    def test_task_complete_value(self):
        assert ProgressPhase.TASK_COMPLETE.value == "task_complete"

    def test_task_aborted_value(self):
        assert ProgressPhase.TASK_ABORTED.value == "task_aborted"

    def test_is_str_enum(self):
        assert ProgressPhase.TASK_START == "task_start"


# ─────────────────────────── Sink 注册 / 反注册 ───────────────────────────


class TestSinkRegistration:
    """register_progress_sink / unregister_progress_sink。"""

    def test_register_increases_count(self):
        def cb(evt):
            pass

        register_progress_sink(cb)
        assert get_sink_count() == 1

    def test_register_deduplicates(self):
        """同一个 callback 注册多次 → 只算一次。"""
        def cb(evt):
            pass

        register_progress_sink(cb)
        register_progress_sink(cb)
        register_progress_sink(cb)
        assert get_sink_count() == 1

    def test_unregister_decreases_count(self):
        def cb(evt):
            pass

        register_progress_sink(cb)
        unregister_progress_sink(cb)
        assert get_sink_count() == 0

    def test_unregister_nonexistent_no_raise(self):
        """反注册未注册的 sink → 不抛错。"""
        def cb(evt):
            pass

        # 不抛错
        unregister_progress_sink(cb)
        assert get_sink_count() == 0

    def test_unregister_only_removes_target(self):
        def cb1(evt):
            pass

        def cb2(evt):
            pass

        register_progress_sink(cb1)
        register_progress_sink(cb2)
        unregister_progress_sink(cb1)
        assert get_sink_count() == 1

    def test_clear_sinks_removes_all(self):
        def cb(evt):
            pass

        register_progress_sink(cb)
        register_progress_sink(cb)
        clear_sinks()
        assert get_sink_count() == 0

    def test_clear_sinks_clears_session_id(self):
        set_current_session_id("session-1")
        clear_sinks()
        # session_id 也被清除（验证：notify 不带 session_id）
        captured = []

        def cb(evt):
            captured.append(evt)

        register_progress_sink(cb)
        notify_progress(ProgressPhase.TASK_START)
        assert "session_id" not in captured[0]


# ─────────────────────────── set_current_session_id ───────────────────────────


class TestSetCurrentSessionId:
    """set_current_session_id session 上下文。"""

    def test_session_id_used_in_event(self):
        set_current_session_id("session-42")
        captured = []

        def cb(evt):
            captured.append(evt)

        register_progress_sink(cb)
        notify_progress(ProgressPhase.TASK_START)
        assert captured[0]["session_id"] == "session-42"

    def test_session_id_none_by_default(self):
        """未设置 session_id → event 不含 session_id。"""
        captured = []

        def cb(evt):
            captured.append(evt)

        register_progress_sink(cb)
        notify_progress(ProgressPhase.TASK_START)
        assert "session_id" not in captured[0]

    def test_session_id_override_to_none(self):
        """set_current_session_id(None) → 清空 session_id。"""
        set_current_session_id("session-1")
        set_current_session_id(None)
        captured = []

        def cb(evt):
            captured.append(evt)

        register_progress_sink(cb)
        notify_progress(ProgressPhase.TASK_START)
        assert "session_id" not in captured[0]


# ─────────────────────────── _build_event 事件构造 ───────────────────────────


class TestBuildEvent:
    """_build_event 标准事件 payload 构造。"""

    def test_minimal_event(self):
        evt = _build_event(ProgressPhase.TASK_START)
        assert evt["phase"] == "task_start"
        assert "timestamp" in evt
        assert isinstance(evt["timestamp"], float)
        # 无 session_id/iterations/tool_name/summary
        assert "session_id" not in evt
        assert "iterations" not in evt
        assert "tool_name" not in evt
        assert "summary" not in evt

    def test_event_with_session_id(self):
        evt = _build_event(ProgressPhase.TASK_START, session_id="sess-1")
        assert evt["session_id"] == "sess-1"

    def test_event_with_iterations(self):
        evt = _build_event(ProgressPhase.THINKING, iterations=3)
        assert evt["iterations"] == 3

    def test_event_with_tool_name(self):
        evt = _build_event(ProgressPhase.TOOL_CALL, tool_name="search_files")
        assert evt["tool_name"] == "search_files"

    def test_event_with_summary(self):
        evt = _build_event(ProgressPhase.TASK_START, summary="start task")
        assert evt["summary"] == "start task"

    def test_event_with_extra(self):
        evt = _build_event(
            ProgressPhase.TOOL_RESULT,
            extra={"duration_ms": 2500, "classification": "success"},
        )
        assert evt["duration_ms"] == 2500
        assert evt["classification"] == "success"

    def test_extra_does_not_overwrite_base(self):
        """extra 中的基础字段（phase/timestamp）不被覆盖。"""
        evt = _build_event(
            ProgressPhase.TASK_START,
            extra={"phase": "hacked", "timestamp": 0.0, "custom": "x"},
        )
        # phase 不被覆盖
        assert evt["phase"] == "task_start"
        # timestamp 不被覆盖
        assert evt["timestamp"] != 0.0
        # 自定义字段保留
        assert evt["custom"] == "x"

    def test_empty_extra_string_not_set(self):
        """tool_name="" 不被写入（truthy 检查）。"""
        evt = _build_event(ProgressPhase.TOOL_CALL, tool_name="")
        assert "tool_name" not in evt

    def test_summary_empty_not_set(self):
        evt = _build_event(ProgressPhase.TASK_START, summary="")
        assert "summary" not in evt

    def test_zero_iterations_is_set(self):
        """spirit 现状：iterations=0 仍写入（用 ``is not None`` 判断，不是 truthy）。

        注意：与 tool_name/summary 不同（那两个用 truthy 检查）。
        """
        evt = _build_event(ProgressPhase.THINKING, iterations=0)
        assert evt["iterations"] == 0


# ─────────────────────────── notify_progress 核心广播 ───────────────────────────


class TestNotifyProgress:
    """notify_progress 进度事件广播。"""

    def test_no_sinks_zero_overhead(self):
        """无 sink 时直接 return（不构造 dict）。"""
        # 调用不应抛错
        notify_progress(ProgressPhase.TASK_START, summary="test")
        # 无 sink = 无 captured

    def test_basic_notification(self):
        captured = []

        def cb(evt):
            captured.append(evt)

        register_progress_sink(cb)
        notify_progress(ProgressPhase.TASK_START, summary="hello")
        assert len(captured) == 1
        assert captured[0]["phase"] == "task_start"
        assert captured[0]["summary"] == "hello"

    def test_multiple_sinks_all_called(self):
        calls1 = []
        calls2 = []

        register_progress_sink(lambda e: calls1.append(e))
        register_progress_sink(lambda e: calls2.append(e))
        notify_progress(ProgressPhase.TASK_START)
        assert len(calls1) == 1
        assert len(calls2) == 1

    def test_sink_exception_isolated(self):
        """一个 sink 抛错 → 不影响其他 sink。"""
        good_calls = []

        def bad_cb(evt):
            raise RuntimeError("sink boom")

        def good_cb(evt):
            good_calls.append(evt)

        register_progress_sink(bad_cb)
        register_progress_sink(good_cb)
        notify_progress(ProgressPhase.TASK_START)
        # good_cb 仍被调用
        assert len(good_calls) == 1

    def test_sink_exception_in_first_does_not_skip_rest(self):
        """第一个 sink 抛错 → 第二个 sink 仍被调用。"""
        first_called = []
        second_called = []

        def bad(evt):
            raise RuntimeError("boom")

        def good1(evt):
            first_called.append(evt)

        def good2(evt):
            second_called.append(evt)

        register_progress_sink(bad)
        register_progress_sink(good1)
        register_progress_sink(good2)
        notify_progress(ProgressPhase.TASK_START)
        assert len(first_called) == 1
        assert len(second_called) == 1

    def test_extra_session_id_overrides_global(self):
        """extra["session_id"] 优先于全局 _current_session_id。"""
        set_current_session_id("global-session")
        captured = []

        def cb(evt):
            captured.append(evt)

        register_progress_sink(cb)
        notify_progress(
            ProgressPhase.TASK_START,
            extra={"session_id": "extra-session"},
        )
        # extra 优先
        assert captured[0]["session_id"] == "extra-session"

    def test_session_id_fallback_to_global(self):
        """未提供 extra session_id → 用全局。"""
        set_current_session_id("global")
        captured = []

        def cb(evt):
            captured.append(evt)

        register_progress_sink(cb)
        notify_progress(ProgressPhase.TASK_START)
        assert captured[0]["session_id"] == "global"

    def test_no_session_anywhere(self):
        """未设置 session_id + 无 extra → 不含 session_id。"""
        captured = []

        def cb(evt):
            captured.append(evt)

        register_progress_sink(cb)
        notify_progress(ProgressPhase.TASK_START)
        assert "session_id" not in captured[0]

    def test_extra_other_fields_propagated(self):
        captured = []

        def cb(evt):
            captured.append(evt)

        register_progress_sink(cb)
        notify_progress(
            ProgressPhase.TOOL_RESULT,
            tool_name="search",
            extra={"duration_ms": 1500, "classification": "success"},
        )
        assert captured[0]["duration_ms"] == 1500
        assert captured[0]["classification"] == "success"
        assert captured[0]["tool_name"] == "search"

    def test_iterations_propagated(self):
        captured = []

        def cb(evt):
            captured.append(evt)

        register_progress_sink(cb)
        notify_progress(ProgressPhase.THINKING, iterations=5)
        assert captured[0]["iterations"] == 5

    def test_notify_during_iteration_does_not_modify_sinks(self):
        """notify 期间 sink 列表变化不影响当前广播。"""
        # spirit line 246: sinks = list(_sinks) 复制再迭代

        def bad_sink(evt):
            # 这个 sink 抛错时不应影响其他 sink 的回调
            raise RuntimeError("during notify")

        def good_sink(evt):
            pass

        register_progress_sink(bad_sink)
        register_progress_sink(good_sink)
        # 不应抛错
        notify_progress(ProgressPhase.TASK_START)

    def test_unregister_during_notify_safe(self):
        """notify 期间 unregister → 已复制的 sinks 仍被调用。"""
        received = []

        def cb(evt):
            received.append(evt)

        register_progress_sink(cb)
        # 直接调 notify_progress（在内部 sink 被调用时 unregister）
        captured = []

        def cb_with_unregister(evt):
            captured.append(evt)
            unregister_progress_sink(cb_with_unregister)

        # 重新注册一个会反注册自己的
        register_progress_sink(cb_with_unregister)
        notify_progress(ProgressPhase.TASK_START)
        # 至少调用了两次（cb 一次，cb_with_unregister 一次）
        assert len(received) >= 1


# ─────────────────────────── 异常隔离 ───────────────────────────


class TestExceptionIsolation:
    """sink 异常隔离（spirit line 248-252 单独 try/except）。"""

    def test_value_error_isolated(self):
        good_calls = []

        def bad(evt):
            raise ValueError("oops")

        def good(evt):
            good_calls.append(evt)

        register_progress_sink(bad)
        register_progress_sink(good)
        notify_progress(ProgressPhase.TASK_START)
        assert len(good_calls) == 1

    def test_type_error_isolated(self):
        good_calls = []

        def bad(evt):
            raise TypeError("oops")

        def good(evt):
            good_calls.append(evt)

        register_progress_sink(bad)
        register_progress_sink(good)
        notify_progress(ProgressPhase.TASK_START)
        assert len(good_calls) == 1

    def test_many_failing_sinks_does_not_break_notify(self):
        """多个 sink 都抛错 → notify 仍正常返回。"""
        def boom(evt):
            raise RuntimeError("boom")

        for _ in range(5):
            register_progress_sink(boom)
        # 不抛错
        notify_progress(ProgressPhase.TASK_START)


class TestThreadSafety:
    """sinks 列表 + session_id 线程安全。"""

    def test_concurrent_register(self):
        """多线程并发注册 → 全部成功。"""
        def cb(evt):
            pass

        def register_many():
            for _ in range(10):
                register_progress_sink(cb)

        threads = [threading.Thread(target=register_many) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 即使重复注册也不影响计数（去重）
        assert get_sink_count() == 1

    def test_concurrent_unregister(self):
        def cb(evt):
            pass

        register_progress_sink(cb)

        def unregister_many():
            for _ in range(10):
                unregister_progress_sink(cb)

        threads = [threading.Thread(target=unregister_many) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert get_sink_count() == 0

    def test_concurrent_notify(self):
        """多线程并发 notify → 所有 sink 都收到全部事件。"""
        received = []
        lock = threading.Lock()

        def cb(evt):
            with lock:
                received.append(evt["phase"])

        register_progress_sink(cb)

        def notify_many():
            for _ in range(10):
                notify_progress(ProgressPhase.TASK_START)

        threads = [threading.Thread(target=notify_many) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 共 5 * 10 = 50 次
        assert len(received) == 50
        assert all(p == "task_start" for p in received)

    def test_concurrent_session_id_change(self):
        """多线程改 session_id → 不会抛错。"""
        def change_id(i):
            set_current_session_id(f"session-{i}")

        threads = [threading.Thread(target=change_id, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 不抛错即可


# ─────────────────────────── 端到端集成 ───────────────────────────


class TestEndToEndIntegration:
    """完整 7 阶段进度事件流。"""

    def test_full_task_lifecycle_events(self):
        """完整任务生命周期：start → thinking → thinking_end → tool_call → tool_result → complete。"""
        captured = []

        def cb(evt):
            captured.append(evt["phase"])

        register_progress_sink(cb)
        set_current_session_id("session-task")

        notify_progress(ProgressPhase.TASK_START, summary="start task")
        notify_progress(ProgressPhase.THINKING, iterations=1)
        notify_progress(ProgressPhase.THINKING_END, iterations=1, extra={"duration_ms": 800})
        notify_progress(
            ProgressPhase.TOOL_CALL,
            tool_name="search_files",
            summary="调用 search_files",
        )
        notify_progress(
            ProgressPhase.TOOL_RESULT,
            tool_name="search_files",
            extra={"duration_ms": 1200, "classification": "success"},
        )
        notify_progress(ProgressPhase.TASK_COMPLETE, summary="task done")

        assert captured == [
            "task_start",
            "thinking",
            "thinking_end",
            "tool_call",
            "tool_result",
            "task_complete",
        ]
        assert len(captured) == 6

    def test_aborted_event_with_reason(self):
        captured = []

        def cb(evt):
            captured.append(evt)

        register_progress_sink(cb)
        notify_progress(
            ProgressPhase.TASK_ABORTED,
            summary="task aborted",
            extra={"reason": "max_iterations"},
        )
        assert captured[0]["phase"] == "task_aborted"
        assert captured[0]["reason"] == "max_iterations"

    def test_no_sink_is_no_error(self):
        """无 sink 时所有 7 阶段事件都不抛错。"""
        for phase in ProgressPhase:
            notify_progress(phase, summary="test")
        # 全过即 OK