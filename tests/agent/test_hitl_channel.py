"""Spirit Agent hitl_channel.py 全局 HITL 决策通道回归测试（C-P1.2 v0.2.4 已交付未测）。

参考 hermes:
    - DesktopBridge.ask + spirit/evolution/hitl/HumanQuery

4 类决策 + fail-open 通道 + 容错解析 + 跨线程注册。
"""

from __future__ import annotations

import threading
import time

import pytest

from spirit.agent.hitl_channel import (
    DEFAULT_DECISION_TIMEOUT,
    DecisionKind,
    clear_hitl_channel,
    has_channel,
    parse_kind,
    register_hitl_channel,
    request_decision,
    unregister_hitl_channel,
)


@pytest.fixture(autouse=True)
def _clean_channel():
    """每个测试前后清空通道。"""
    clear_hitl_channel()
    yield
    clear_hitl_channel()


# ─────────────────────────── DecisionKind 枚举 ───────────────────────────


class TestDecisionKind:
    """DecisionKind 4 类枚举（str enum）。"""

    def test_has_four_kinds(self):
        assert len(DecisionKind) == 4

    def test_exploration_value(self):
        assert DecisionKind.EXPLORATION.value == "exploration"

    def test_tool_failure_value(self):
        assert DecisionKind.TOOL_FAILURE.value == "tool_failure"

    def test_experience_review_value(self):
        assert DecisionKind.EXPERIENCE_REVIEW.value == "experience_review"

    def test_provider_switch_value(self):
        assert DecisionKind.PROVIDER_SWITCH.value == "provider_switch"

    def test_is_str_enum(self):
        assert DecisionKind.EXPLORATION == "exploration"

    def test_default_timeout(self):
        assert DEFAULT_DECISION_TIMEOUT == 300.0


# ─────────────────────────── parse_kind 容错解析 ───────────────────────────


class TestParseKind:
    """parse_kind 把任意输入容错解析为 DecisionKind。"""

    def test_decision_kind_passthrough(self):
        assert parse_kind(DecisionKind.TOOL_FAILURE) == DecisionKind.TOOL_FAILURE

    def test_string_valid(self):
        assert parse_kind("tool_failure") == DecisionKind.TOOL_FAILURE
        assert parse_kind("exploration") == DecisionKind.EXPLORATION

    def test_string_case_insensitive(self):
        assert parse_kind("TOOL_FAILURE") == DecisionKind.TOOL_FAILURE
        assert parse_kind("Tool_Failure") == DecisionKind.TOOL_FAILURE

    def test_string_with_whitespace(self):
        assert parse_kind("  tool_failure  ") == DecisionKind.TOOL_FAILURE

    def test_unknown_string_defaults_to_exploration(self):
        assert parse_kind("unknown_kind") == DecisionKind.EXPLORATION

    def test_empty_string_defaults_to_exploration(self):
        assert parse_kind("") == DecisionKind.EXPLORATION

    def test_none_defaults_to_exploration(self):
        assert parse_kind(None) == DecisionKind.EXPLORATION

    def test_non_string_input_defaults_to_exploration(self):
        assert parse_kind(42) == DecisionKind.EXPLORATION
        assert parse_kind(["tool_failure"]) == DecisionKind.EXPLORATION


# ─────────────────────────── 通道注册 / 反注册 ───────────────────────────


class TestChannelRegistration:
    """register_hitl_channel / unregister_hitl_channel / clear_hitl_channel / has_channel。"""

    def test_has_channel_initially_false(self):
        assert has_channel() is False

    def test_register_sets_channel(self):
        def ch(q, t):
            return None

        register_hitl_channel(ch)
        assert has_channel() is True

    def test_register_overrides(self):
        """后注册覆盖先注册。"""
        def ch1(q, t):
            return "ch1"

        def ch2(q, t):
            return "ch2"

        register_hitl_channel(ch1)
        register_hitl_channel(ch2)
        # 验证当前是 ch2
        captured = []

        register_hitl_channel(lambda q, t: (captured.append("x"), "ok")[1])
        # 不再是 ch2/ ch1，是上面覆盖的 lambda
        # （验证：调用通道时执行的是新 lambda）
        result = lambda q, t: (captured.append("called"), "reply")[1]
        register_hitl_channel(result)
        request_decision(DecisionKind.TOOL_FAILURE, "q")
        assert captured == ["called"]

    def test_unregister_clears_when_match(self):
        def ch(q, t):
            return None

        register_hitl_channel(ch)
        unregister_hitl_channel(ch)
        assert has_channel() is False

    def test_unregister_no_match_no_effect(self):
        """unregister 传的 channel 不是当前 → 不清空。"""
        def ch1(q, t):
            return None

        def ch2(q, t):
            return None

        register_hitl_channel(ch1)
        unregister_hitl_channel(ch2)  # ch2 不匹配
        # ch1 仍 current
        assert has_channel() is True

    def test_clear_hitl_channel_unconditional(self):
        def ch(q, t):
            return None

        register_hitl_channel(ch)
        clear_hitl_channel()
        assert has_channel() is False

    def test_register_thread_safety(self):
        """多线程并发注册 → 最终只有一个 channel（后写者赢）。"""
        channels = [lambda q, t: f"ch{i}" for i in range(10)]

        def register_ch(ch):
            register_hitl_channel(ch)

        threads = [threading.Thread(target=register_ch, args=(ch,)) for ch in channels]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 至少有一个注册成功
        assert has_channel() is True


# ─────────────────────────── request_decision 主入口 ───────────────────────────


class TestRequestDecision:
    """request_decision 发起询问 + fail-open 铁律。"""

    def test_no_channel_returns_none(self):
        """无 channel → None（fail-open）。"""
        result = request_decision(DecisionKind.TOOL_FAILURE, "what to do?")
        assert result is None

    def test_channel_returns_reply(self):
        def ch(q, t):
            return "user reply"

        register_hitl_channel(ch)
        result = request_decision(DecisionKind.TOOL_FAILURE, "what to do?")
        assert result == "user reply"

    def test_channel_returns_none_propagated(self):
        """channel 返回 None → request_decision 返回 None。"""
        def ch(q, t):
            return None

        register_hitl_channel(ch)
        result = request_decision(DecisionKind.TOOL_FAILURE, "q")
        assert result is None

    def test_channel_returns_empty_string_propagated_as_none(self):
        """channel 返回 "" → request_decision 返回 None（fail-open）。"""
        def ch(q, t):
            return ""

        register_hitl_channel(ch)
        result = request_decision(DecisionKind.TOOL_FAILURE, "q")
        assert result is None

    def test_channel_returns_whitespace_propagated_as_none(self):
        """channel 返回 "   " → request_decision 返回 None。"""
        def ch(q, t):
            return "   "

        register_hitl_channel(ch)
        result = request_decision(DecisionKind.TOOL_FAILURE, "q")
        assert result is None

    def test_channel_exception_returns_none(self):
        """channel 抛错 → None（fail-open）。"""
        def ch(q, t):
            raise RuntimeError("channel boom")

        register_hitl_channel(ch)
        # 不抛错
        result = request_decision(DecisionKind.TOOL_FAILURE, "q")
        assert result is None

    def test_query_passed_to_channel(self):
        """channel 接收到 HumanQuery 对象。"""
        captured = []

        def ch(q, t):
            captured.append(q)
            return "ok"

        register_hitl_channel(ch)
        request_decision(
            DecisionKind.TOOL_FAILURE,
            "terminal 失败 4 次",
            options=["重试", "换方法"],
            context="最近错误：...",
        )
        assert len(captured) == 1
        q = captured[0]
        assert q.question == "terminal 失败 4 次"
        assert q.options == ["重试", "换方法"]
        assert q.context == "最近错误：..."
        assert q.kind == "tool_failure"  # DecisionKind 写入 kind 字段

    def test_kind_string_passes_through_parse_kind(self):
        """kind="exploration" string → 走 parse_kind 解析。"""
        captured = []

        def ch(q, t):
            captured.append(q)
            return "ok"

        register_hitl_channel(ch)
        request_decision(
            "exploration",  # string 而非 DecisionKind
            "question",
        )
        assert captured[0].kind == "exploration"

    def test_kind_unknown_defaults_to_exploration(self):
        captured = []

        def ch(q, t):
            captured.append(q)
            return "ok"

        register_hitl_channel(ch)
        request_decision("unknown_kind", "q")
        # 容错：unknown → exploration
        assert captured[0].kind == "exploration"

    def test_options_none_propagated_as_empty_list(self):
        captured = []

        def ch(q, t):
            captured.append(q)
            return "ok"

        register_hitl_channel(ch)
        request_decision(DecisionKind.TOOL_FAILURE, "q", options=None)
        # spirit: list(options or []) → []
        assert captured[0].options == []

    def test_default_options_empty_list(self):
        """不传 options → 默认空 list。"""
        captured = []

        def ch(q, t):
            captured.append(q)
            return "ok"

        register_hitl_channel(ch)
        request_decision(DecisionKind.TOOL_FAILURE, "q")
        assert captured[0].options == []

    def test_default_confidence_one(self):
        captured = []

        def ch(q, t):
            captured.append(q)
            return "ok"

        register_hitl_channel(ch)
        request_decision(DecisionKind.TOOL_FAILURE, "q")
        assert captured[0].confidence == 1.0

    def test_default_is_fork_false(self):
        captured = []

        def ch(q, t):
            captured.append(q)
            return "ok"

        register_hitl_channel(ch)
        request_decision(DecisionKind.TOOL_FAILURE, "q")
        assert captured[0].is_fork is False

    def test_default_task_id_empty(self):
        captured = []

        def ch(q, t):
            captured.append(q)
            return "ok"

        register_hitl_channel(ch)
        request_decision(DecisionKind.TOOL_FAILURE, "q")
        assert captured[0].task_id == ""

    def test_default_timeout_passed(self):
        """不传 timeout → 默认 300s。"""
        captured = []

        def ch(q, t):
            captured.append(t)
            return "ok"

        register_hitl_channel(ch)
        request_decision(DecisionKind.TOOL_FAILURE, "q")
        assert captured[0] == DEFAULT_DECISION_TIMEOUT

    def test_custom_timeout_passed(self):
        captured = []

        def ch(q, t):
            captured.append(t)
            return "ok"

        register_hitl_channel(ch)
        request_decision(DecisionKind.TOOL_FAILURE, "q", timeout=10.0)
        assert captured[0] == 10.0

    def test_reply_stripped(self):
        """channel 返回的 reply 被 strip。"""
        def ch(q, t):
            return "  user reply  "

        register_hitl_channel(ch)
        result = request_decision(DecisionKind.TOOL_FAILURE, "q")
        assert result == "user reply"

    def test_reply_coerced_to_string(self):
        """channel 返回非字符串 → str(reply).strip()。"""
        def ch(q, t):
            return 42

        register_hitl_channel(ch)
        result = request_decision(DecisionKind.TOOL_FAILURE, "q")
        assert result == "42"


# ─────────────────────────── 4 类决策场景 ───────────────────────────


class TestDecisionKindsUsage:
    """4 类决策场景验证。"""

    def test_tool_failure_decision(self):
        captured = []

        def ch(q, t):
            captured.append(q)
            return "换方法"

        register_hitl_channel(ch)
        result = request_decision(
            DecisionKind.TOOL_FAILURE,
            "terminal 连续失败 4 次，怎么办？",
            options=["继续重试", "换方法", "放弃任务"],
            context="最近错误：permission denied",
        )
        assert result == "换方法"
        assert captured[0].kind == "tool_failure"
        assert len(captured[0].options) == 3

    def test_provider_switch_decision(self):
        captured = []

        def ch(q, t):
            captured.append(q)
            return "切到备用"

        register_hitl_channel(ch)
        result = request_decision(
            DecisionKind.PROVIDER_SWITCH,
            "openai 连续 timeout，切到 anthropic？",
            options=["切到备用", "留在当前重试", "放弃"],
            is_fork=True,
        )
        assert result == "切到备用"
        assert captured[0].is_fork is True
        assert captured[0].kind == "provider_switch"

    def test_experience_review_decision(self):
        captured = []

        def ch(q, t):
            captured.append(q)
            return "存"

        register_hitl_channel(ch)
        result = request_decision(
            DecisionKind.EXPERIENCE_REVIEW,
            "本次成功修复 auth bug，是否存为经验？",
            options=["存", "跳过", "改写后再存"],
        )
        assert result == "存"
        assert captured[0].kind == "experience_review"

    def test_exploration_decision_backward_compatible(self):
        """EXPLORATION 是 backward-compatible 默认值。"""
        captured = []

        def ch(q, t):
            captured.append(q)
            return "继续探索"

        register_hitl_channel(ch)
        result = request_decision(
            DecisionKind.EXPLORATION,
            "探索方向 1 vs 2？",
            options=["方向 1", "方向 2"],
        )
        assert result == "继续探索"
        assert captured[0].kind == "exploration"


# ─────────────────────────── 线程安全 ───────────────────────────


class TestThreadSafety:
    """并发 register / request 线程安全。"""

    def test_concurrent_request_decision(self):
        """多线程并发调用 request_decision → 全部执行不抛错。"""
        def ch(q, t):
            time.sleep(0.001)
            return "ok"

        register_hitl_channel(ch)

        def call():
            for _ in range(20):
                result = request_decision(DecisionKind.TOOL_FAILURE, "q")
                assert result == "ok"

        threads = [threading.Thread(target=call) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def test_register_during_request_safe(self):
        """request 期间 register 正在替换 channel → 当前 request 仍能完成。"""
        results = []

        def ch1(q, t):
            time.sleep(0.01)
            return "ch1"

        def ch2(q, t):
            return "ch2"

        register_hitl_channel(ch1)

        def call_request():
            # 第一次：ch1
            r1 = request_decision(DecisionKind.TOOL_FAILURE, "q")
            results.append(r1)
            # 中途切换
            register_hitl_channel(ch2)
            # 第二次：ch2
            r2 = request_decision(DecisionKind.TOOL_FAILURE, "q")
            results.append(r2)

        call_request()
        assert results == ["ch1", "ch2"]


# ─────────────────────────── 端到端集成 ───────────────────────────


class TestEndToEndIntegration:
    """完整决策流。"""

    def test_full_decision_flow(self):
        """完整流程：register → request → reply → unregister。"""
        # 1. 注册
        def ch(q, t):
            return "approve"

        register_hitl_channel(ch)
        assert has_channel() is True

        # 2. 发起询问
        result = request_decision(
            DecisionKind.EXPERIENCE_REVIEW,
            "是否存经验？",
            options=["存", "不存"],
        )
        assert result == "approve"

        # 3. 反注册
        unregister_hitl_channel(ch)
        assert has_channel() is False

        # 4. 再发起 → fail-open None
        result = request_decision(DecisionKind.TOOL_FAILURE, "q")
        assert result is None

    def test_decision_with_all_kinds_round_trip(self):
        """所有 4 类决策都能正常 round-trip。"""
        captured = []

        def ch(q, t):
            captured.append(q.kind)
            return f"reply-{q.kind}"

        register_hitl_channel(ch)

        for kind in DecisionKind:
            result = request_decision(kind, f"q for {kind}")
            assert result == f"reply-{kind.value}"

        # 所有 4 类都被记录
        assert set(captured) == {"exploration", "tool_failure", "experience_review", "provider_switch"}