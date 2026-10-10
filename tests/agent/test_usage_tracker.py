"""Spirit Agent usage_tracker.py 用量追踪回归测试（v0.2 已交付未测）。

参考 hermes:
    - hermes account_usage.py + billing_usage.py

UsageStats 数据类 + UsageTracker 会话管理 + 全局单例。
"""

from __future__ import annotations

import time

import pytest

from spirit.agent.usage_tracker import (
    UsageStats,
    UsageTracker,
    get_usage_tracker,
)


@pytest.fixture(autouse=True)
def _reset_global():
    """每个测试前重置全局单例（避免污染）。"""
    get_usage_tracker().reset()
    yield
    get_usage_tracker().reset()


# ─────────────────────────── UsageStats 构造 ───────────────────────────


class TestUsageStatsConstruction:
    """UsageStats 数据类 + 默认值。"""

    def test_defaults(self):
        s = UsageStats()
        assert s.session_id == ""
        assert s.total_tokens == 0
        assert s.prompt_tokens == 0
        assert s.completion_tokens == 0
        assert s.api_calls == 0
        assert s.estimated_cost_usd == 0.0

    def test_started_at_recent(self):
        before = time.time()
        s = UsageStats()
        after = time.time()
        assert before <= s.started_at <= after

    def test_last_updated_recent(self):
        before = time.time()
        s = UsageStats()
        after = time.time()
        assert before <= s.last_updated <= after


# ─────────────────────────── UsageStats.update_from_response ───────────────────────────


class TestUpdateFromResponse:
    """update_from_response 累加 token + api_calls。"""

    def test_empty_usage_returns_early(self):
        s = UsageStats()
        s.update_from_response({})
        # 不修改任何字段
        assert s.api_calls == 0
        assert s.total_tokens == 0

    def test_none_usage_returns_early(self):
        s = UsageStats()
        s.update_from_response(None)
        assert s.api_calls == 0

    def test_basic_update(self):
        s = UsageStats()
        s.update_from_response({
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        })
        assert s.prompt_tokens == 100
        assert s.completion_tokens == 50
        assert s.total_tokens == 150
        assert s.api_calls == 1

    def test_multiple_updates_accumulate(self):
        s = UsageStats()
        s.update_from_response({
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        })
        s.update_from_response({
            "prompt_tokens": 200,
            "completion_tokens": 80,
            "total_tokens": 280,
        })
        assert s.prompt_tokens == 300
        assert s.completion_tokens == 130
        assert s.total_tokens == 430
        assert s.api_calls == 2

    def test_missing_keys_default_to_zero(self):
        """usage_data 缺字段 → 用 0。"""
        s = UsageStats()
        s.update_from_response({})
        s.update_from_response({"prompt_tokens": 50})  # 缺其他
        assert s.prompt_tokens == 50
        assert s.completion_tokens == 0
        assert s.total_tokens == 0

    def test_cost_estimation_gpt4(self):
        """成本估算：input $0.03/1K + output $0.06/1K（GPT-4 价格）。"""
        s = UsageStats()
        s.update_from_response({
            "prompt_tokens": 1000,  # 1K → $0.03
            "completion_tokens": 500,  # 0.5K → $0.03
            "total_tokens": 1500,
        })
        # 0.03 + 0.03 = 0.06
        assert abs(s.estimated_cost_usd - 0.06) < 1e-9

    def test_cost_estimation_zero(self):
        s = UsageStats()
        s.update_from_response({
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        })
        assert s.estimated_cost_usd == 0.0

    def test_last_updated_refreshed(self):
        s = UsageStats()
        before = s.last_updated
        time.sleep(0.01)
        s.update_from_response({
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        })
        # last_updated 被刷新
        assert s.last_updated > before


# ─────────────────────────── UsageStats.reset / to_dict ───────────────────────────


class TestUsageStatsResetAndToDict:
    """reset 清零 + to_dict 序列化。"""

    def test_reset_clears_all(self):
        s = UsageStats(session_id="test")
        s.update_from_response({
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        })
        s.reset()
        assert s.total_tokens == 0
        assert s.prompt_tokens == 0
        assert s.completion_tokens == 0
        assert s.api_calls == 0
        assert s.estimated_cost_usd == 0.0

    def test_reset_refreshes_timestamps(self):
        s = UsageStats()
        time.sleep(0.01)
        s.reset()
        # started_at 和 last_updated 都应刷新
        assert s.last_updated >= s.started_at

    def test_to_dict_basic(self):
        s = UsageStats(session_id="test-session")
        s.update_from_response({
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        })
        d = s.to_dict()
        assert d["session_id"] == "test-session"
        assert d["total_tokens"] == 150
        assert d["prompt_tokens"] == 100
        assert d["completion_tokens"] == 50
        assert d["api_calls"] == 1
        assert "estimated_cost_usd" in d
        assert "duration_seconds" in d

    def test_to_dict_cost_rounded_to_4(self):
        s = UsageStats()
        s.update_from_response({
            "prompt_tokens": 1,  # 极小
            "completion_tokens": 1,
            "total_tokens": 2,
        })
        d = s.to_dict()
        # cost rounded to 4 decimals
        # 0.00003 + 0.00006 = 0.00009 → 0.0001
        assert d["estimated_cost_usd"] == 0.0001

    def test_to_dict_duration_present(self):
        s = UsageStats()
        d = s.to_dict()
        assert d["duration_seconds"] >= 0.0


# ─────────────────────────── UsageTracker 构造 ───────────────────────────


class TestUsageTrackerConstruction:
    """UsageTracker 构造 + 初始状态。"""

    def test_initial_state(self):
        t = UsageTracker()
        assert t._current_stats is None
        assert t._session_history == {}

    def test_get_current_stats_no_session(self):
        t = UsageTracker()
        assert t.get_current_stats() is None


# ─────────────────────────── UsageTracker.start_session / end_session ───────────────────────────


class TestSessionLifecycle:
    """start_session / end_session 会话管理。"""

    def test_start_session_creates_stats(self):
        t = UsageTracker()
        t.start_session("session-1")
        assert t._current_stats is not None
        assert t._current_stats.session_id == "session-1"

    def test_start_session_no_previous(self):
        """无前一会话时 start 不归档任何东西。"""
        t = UsageTracker()
        t.start_session("session-1")
        assert len(t._session_history) == 0

    def test_start_session_archives_previous(self):
        """start 新 session → 旧 session 归档到 history。"""
        t = UsageTracker()
        t.start_session("session-1")
        t.update({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        t.start_session("session-2")  # 自动归档 session-1
        assert "session-1" in t._session_history
        assert t._current_stats.session_id == "session-2"

    def test_end_session_saves_to_history(self):
        t = UsageTracker()
        t.start_session("session-1")
        t.update({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        t.end_session("session-1")
        assert t._current_stats is None
        assert "session-1" in t._session_history

    def test_end_session_wrong_id_no_effect(self):
        """end_session 传的 id 不匹配 current → 不归档。"""
        t = UsageTracker()
        t.start_session("session-1")
        t.end_session("session-2")  # 不匹配
        # session-1 仍 current
        assert t._current_stats is not None
        assert t._current_stats.session_id == "session-1"

    def test_end_session_with_no_current_no_effect(self):
        """无 current session 时 end 不抛错。"""
        t = UsageTracker()
        # 不抛错
        t.end_session("anything")
        assert t._current_stats is None


# ─────────────────────────── UsageTracker.update ───────────────────────────


class TestUpdate:
    """update 调用 current_stats.update_from_response。"""

    def test_update_without_session_warns(self):
        """无 current session 时 update → log warning + return。"""
        t = UsageTracker()
        # 不抛错
        t.update({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        # current 仍 None
        assert t._current_stats is None

    def test_update_with_session(self):
        t = UsageTracker()
        t.start_session("session-1")
        t.update({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        assert t._current_stats.api_calls == 1
        assert t._current_stats.prompt_tokens == 10

    def test_update_exception_does_not_raise(self):
        """update_from_response 抛错 → tracker 不抛错（辅助任务容错）。"""
        t = UsageTracker()
        t.start_session("session-1")

        # 用一个会抛错的 usage_data → tracker 捕获
        # update_from_response 不抛错的，但强行传 None 会让 .get() 报错
        # 用一个会让 update_from_response 内部出错的输入
        bad_data = object()  # 不是 dict，.get() 会抛 AttributeError
        # 不抛错给调用者
        t.update(bad_data)  # type: ignore
        # current_stats 状态未变（api_calls 仍 0）
        assert t._current_stats.api_calls == 0


# ─────────────────────────── UsageTracker 查询 ───────────────────────────


class TestUsageTrackerQuery:
    """get_current_stats / get_session_stats / get_total_usage。"""

    def test_get_current_stats_with_session(self):
        t = UsageTracker()
        t.start_session("session-1")
        t.update({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        stats = t.get_current_stats()
        assert stats is not None
        assert stats["session_id"] == "session-1"
        assert stats["total_tokens"] == 15

    def test_get_session_stats_existing(self):
        t = UsageTracker()
        t.start_session("session-1")
        t.update({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        t.end_session("session-1")
        # 从 history 查
        stats = t.get_session_stats("session-1")
        assert stats is not None
        assert stats["total_tokens"] == 15

    def test_get_session_stats_nonexistent(self):
        t = UsageTracker()
        assert t.get_session_stats("nonexistent") is None

    def test_get_total_usage_empty(self):
        t = UsageTracker()
        total = t.get_total_usage()
        assert total["total_tokens"] == 0
        assert total["total_cost_usd"] == 0.0
        assert total["total_api_calls"] == 0
        assert total["tracked_sessions"] == 0

    def test_get_total_usage_with_history(self):
        t = UsageTracker()
        t.start_session("s1")
        t.update({"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})
        t.end_session("s1")
        t.start_session("s2")
        t.update({"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300})
        # current s2 + history s1
        total = t.get_total_usage()
        assert total["total_tokens"] == 450  # 150 + 300
        assert total["total_api_calls"] == 2
        assert total["tracked_sessions"] == 2  # 1 history + 1 current

    def test_get_total_usage_includes_current(self):
        t = UsageTracker()
        t.start_session("s1")
        t.update({"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})
        # current s1
        total = t.get_total_usage()
        assert total["total_tokens"] == 150
        assert total["tracked_sessions"] == 1


# ─────────────────────────── UsageTracker.reset ───────────────────────────


class TestReset:
    """reset 清空所有状态。"""

    def test_reset_clears_current(self):
        t = UsageTracker()
        t.start_session("s1")
        t.reset()
        assert t._current_stats is None

    def test_reset_clears_history(self):
        t = UsageTracker()
        t.start_session("s1")
        t.end_session("s1")
        assert len(t._session_history) == 1
        t.reset()
        assert t._session_history == {}


# ─────────────────────────── 全局单例 ───────────────────────────


class TestGlobalSingleton:
    """get_usage_tracker 全局单例。"""

    def test_returns_same_instance(self):
        t1 = get_usage_tracker()
        t2 = get_usage_tracker()
        assert t1 is t2

    def test_global_state_persists(self):
        t = get_usage_tracker()
        t.start_session("global-1")
        # 再次获取 → 同一个实例，state 保留
        t2 = get_usage_tracker()
        assert t2._current_stats is not None
        assert t2._current_stats.session_id == "global-1"


# ─────────────────────────── 端到端集成 ───────────────────────────


class TestEndToEndIntegration:
    """完整会话生命周期 + 多个会话累计。"""

    def test_multiple_sessions_total(self):
        t = UsageTracker()
        # session 1
        t.start_session("s1")
        t.update({"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})
        t.update({"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300})
        t.end_session("s1")
        # session 2
        t.start_session("s2")
        t.update({"prompt_tokens": 50, "completion_tokens": 25, "total_tokens": 75})
        t.end_session("s2")
        # session 3 (current)
        t.start_session("s3")
        t.update({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})

        total = t.get_total_usage()
        # 150 + 300 + 75 + 15 = 540
        assert total["total_tokens"] == 540
        assert total["total_api_calls"] == 4  # s1(2) + s2(1) + s3(1)
        assert total["tracked_sessions"] == 3  # s1, s2 history + s3 current

    def test_realistic_session_flow(self):
        """真实场景：start → 多次 update → end → 查询。"""
        t = UsageTracker()
        t.start_session("user-1-task")

        for i in range(3):
            t.update({
                "prompt_tokens": 500,
                "completion_tokens": 200,
                "total_tokens": 700,
            })

        # current stats
        current = t.get_current_stats()
        assert current["api_calls"] == 3
        assert current["total_tokens"] == 2100
        # cost = 3 * (500/1000*0.03 + 200/1000*0.06) = 3 * (0.015 + 0.012) = 3 * 0.027 = 0.081
        assert abs(current["estimated_cost_usd"] - 0.081) < 1e-4

        t.end_session("user-1-task")
        # end 后 current = None
        assert t.get_current_stats() is None
        # 但 history 有
        history_stats = t.get_session_stats("user-1-task")
        assert history_stats is not None
        assert history_stats["total_tokens"] == 2100