"""watch 模式限流 / strike 熔断 / 全局熔断单测。

对标 Hermes ``tests/tools/test_process_registry.py`` 里的 watch 相关用例。
直接驱动 ``_check_watch_patterns`` 与 ``_global_watch_admit``，并 monkeypatch
``spirit.process.registry`` 命名空间里的 WATCH_* 常量（它们经 ``from ... import``
绑定到该模块），做确定性的限流/熔断断言，无需真实计时。
"""

from __future__ import annotations

import time

from spirit.process import registry as registry_mod

from .conftest import _make_session


def _drain_types(registry) -> list:
    types = []
    while not registry.completion_queue.empty():
        types.append(registry.completion_queue.get_nowait().get("type"))
    return types


# =========================================================================
# 每会话匹配 + 限流
# =========================================================================

class TestWatchMatching:
    def test_first_match_emits(self, registry):
        s = _make_session(sid="w1", watch_patterns=["ready"])
        registry._running[s.id] = s
        registry._check_watch_patterns(s, "server ready now")
        evt = registry.completion_queue.get_nowait()
        assert evt["type"] == "watch_match"
        assert evt["pattern"] == "ready"
        assert s._watch_hits == 1
        assert s._watch_cooldown_until > 0

    def test_no_match_no_event(self, registry):
        s = _make_session(sid="w2", watch_patterns=["ready"])
        registry._running[s.id] = s
        registry._check_watch_patterns(s, "nothing to see here")
        assert registry.completion_queue.empty()
        assert s._watch_hits == 0

    def test_no_patterns_no_event(self, registry):
        s = _make_session(sid="w3", watch_patterns=[])
        registry._running[s.id] = s
        registry._check_watch_patterns(s, "ready")
        assert registry.completion_queue.empty()

    def test_match_within_cooldown_suppressed(self, registry):
        """冷却窗口内的第二次匹配被丢弃，记一次 strike，不发事件。"""
        s = _make_session(sid="w4", watch_patterns=["ready"])
        registry._running[s.id] = s
        registry._check_watch_patterns(s, "ready 1")   # 发一条，开冷却窗口
        _drain_types(registry)
        registry._check_watch_patterns(s, "ready 2")   # 仍在冷却 → 抑制
        assert registry.completion_queue.empty()
        assert s._watch_suppressed >= 1
        assert s._watch_consecutive_strikes == 1
        assert s._watch_hits == 1  # 未新增投递

    def test_multiple_in_same_window_count_one_strike(self, registry):
        """同一冷却窗口内多次抑制只记一次 strike（candidate 守卫）。"""
        s = _make_session(sid="w5", watch_patterns=["ready"])
        registry._running[s.id] = s
        registry._check_watch_patterns(s, "ready 1")
        _drain_types(registry)
        registry._check_watch_patterns(s, "ready 2")
        registry._check_watch_patterns(s, "ready 3")
        registry._check_watch_patterns(s, "ready 4")
        assert s._watch_consecutive_strikes == 1
        assert s._watch_suppressed >= 3

    def test_exited_session_suppressed(self, registry):
        """进程已退出后的 chunk 是噪音 → 不再投递 watch 匹配。"""
        s = _make_session(sid="w6", watch_patterns=["ready"], exited=True)
        registry._finished[s.id] = s
        registry._check_watch_patterns(s, "ready late")
        assert registry.completion_queue.empty()


# =========================================================================
# strike 熔断 → 降级为 notify_on_complete
# =========================================================================

class TestWatchStrikeDisable:
    def test_strike_limit_disables_watch(self, registry, monkeypatch):
        monkeypatch.setattr(registry_mod, "WATCH_STRIKE_LIMIT", 1)
        s = _make_session(sid="d1", watch_patterns=["ready"])
        registry._running[s.id] = s
        registry._check_watch_patterns(s, "ready 1")   # 发一条
        _drain_types(registry)
        registry._check_watch_patterns(s, "ready 2")   # 抑制 → strike 1 → 达上限 → 关闭
        assert s._watch_disabled is True
        assert s.notify_on_complete is True
        assert "watch_disabled" in _drain_types(registry)

    def test_disabled_watch_ignores_further_matches(self, registry, monkeypatch):
        monkeypatch.setattr(registry_mod, "WATCH_STRIKE_LIMIT", 1)
        s = _make_session(sid="d2", watch_patterns=["ready"])
        registry._running[s.id] = s
        registry._check_watch_patterns(s, "ready 1")
        registry._check_watch_patterns(s, "ready 2")   # 触发关闭
        _drain_types(registry)
        registry._check_watch_patterns(s, "ready 3")   # 已关闭 → 早退，无事件
        assert registry.completion_queue.empty()

    def test_healthy_window_resets_strikes(self, registry):
        """冷却干净地过去（无抑制）→ 连续 strike 计数归零。"""
        s = _make_session(sid="d3", watch_patterns=["ready"])
        registry._running[s.id] = s
        registry._check_watch_patterns(s, "ready 1")   # emit
        _drain_types(registry)
        registry._check_watch_patterns(s, "ready 2")   # 抑制 → strike 1
        assert s._watch_consecutive_strikes == 1
        # 模拟冷却干净地过去：candidate 已随窗口清理，cooldown 到期
        s._watch_strike_candidate = False
        s._watch_cooldown_until = time.time() - 1
        registry._check_watch_patterns(s, "ready 3")   # 健康窗口 → 重置 strikes
        assert s._watch_consecutive_strikes == 0
        assert s._watch_disabled is False


# =========================================================================
# 全局熔断（跨会话二级保险）
# =========================================================================

class TestGlobalWatchBreaker:
    def test_admit_under_limit(self, registry, monkeypatch):
        monkeypatch.setattr(registry_mod, "WATCH_GLOBAL_MAX_PER_WINDOW", 5)
        monkeypatch.setattr(registry_mod, "WATCH_GLOBAL_WINDOW_SECONDS", 10)
        now = 1000.0
        for _ in range(5):
            assert registry._global_watch_admit(now) is True

    def test_trips_over_limit(self, registry, monkeypatch):
        monkeypatch.setattr(registry_mod, "WATCH_GLOBAL_MAX_PER_WINDOW", 2)
        monkeypatch.setattr(registry_mod, "WATCH_GLOBAL_WINDOW_SECONDS", 10)
        monkeypatch.setattr(registry_mod, "WATCH_GLOBAL_COOLDOWN_SECONDS", 30)
        now = 1000.0
        assert registry._global_watch_admit(now) is True    # hits 1
        assert registry._global_watch_admit(now) is True    # hits 2
        assert registry._global_watch_admit(now) is False   # 超限 → 熔断
        assert "watch_overflow_tripped" in _drain_types(registry)

    def test_suppressed_during_cooldown(self, registry, monkeypatch):
        monkeypatch.setattr(registry_mod, "WATCH_GLOBAL_MAX_PER_WINDOW", 1)
        monkeypatch.setattr(registry_mod, "WATCH_GLOBAL_WINDOW_SECONDS", 10)
        monkeypatch.setattr(registry_mod, "WATCH_GLOBAL_COOLDOWN_SECONDS", 30)
        now = 1000.0
        assert registry._global_watch_admit(now) is True
        assert registry._global_watch_admit(now) is False   # 熔断
        _drain_types(registry)
        assert registry._global_watch_admit(now + 5) is False  # 冷却期丢弃
        assert registry._global_watch_suppressed_during_trip >= 1

    def test_released_after_cooldown(self, registry, monkeypatch):
        monkeypatch.setattr(registry_mod, "WATCH_GLOBAL_MAX_PER_WINDOW", 1)
        monkeypatch.setattr(registry_mod, "WATCH_GLOBAL_WINDOW_SECONDS", 10)
        monkeypatch.setattr(registry_mod, "WATCH_GLOBAL_COOLDOWN_SECONDS", 30)
        now = 1000.0
        registry._global_watch_admit(now)        # hits 1
        registry._global_watch_admit(now)        # 熔断（tripped_until=1030）
        registry._global_watch_admit(now + 5)    # 冷却期抑制
        _drain_types(registry)
        # 冷却到期 → 放行并发一条释放摘要
        assert registry._global_watch_admit(now + 31) is True
        assert "watch_overflow_released" in _drain_types(registry)

    def test_window_rolls_over(self, registry, monkeypatch):
        """超过窗口时长后计数重置，重新放行（滚动检查在 hits 检查之前）。"""
        monkeypatch.setattr(registry_mod, "WATCH_GLOBAL_MAX_PER_WINDOW", 1)
        monkeypatch.setattr(registry_mod, "WATCH_GLOBAL_WINDOW_SECONDS", 10)
        now = 1000.0
        assert registry._global_watch_admit(now) is True       # 窗口内 hits=1
        # 时间自然超过窗口 → 先滚动重置 hits，再放行（不会误触熔断）
        assert registry._global_watch_admit(now + 11) is True
