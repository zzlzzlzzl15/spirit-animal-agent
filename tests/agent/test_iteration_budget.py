"""Spirit Agent iteration_budget.py 迭代预算管理回归测试（M-P1.1 子模块 已交付未测）。

参考 hermes:
    - hermes agent/iteration_budget.py

IterationBudget 数据类 + consume / refund / reset / utilization + create_budget_from_config。
"""

from __future__ import annotations

import pytest

from spirit.agent.iteration_budget import (
    IterationBudget,
    create_budget_from_config,
)


# ─────────────────────────── IterationBudget 构造 ───────────────────────────


class TestIterationBudgetConstruction:
    """IterationBudget 构造 + __post_init__。"""

    def test_default_max_total(self):
        b = IterationBudget()
        assert b.max_total == 50

    def test_default_used(self):
        b = IterationBudget()
        assert b.used == 0

    def test_default_remaining_equals_max(self):
        """__post_init__ → remaining = max_total。"""
        b = IterationBudget()
        assert b.remaining == 50

    def test_custom_max_total(self):
        b = IterationBudget(max_total=100)
        assert b.max_total == 100
        assert b.remaining == 100

    def test_remaining_initialized_correctly(self):
        """remaining 字段是 init=False，由 __post_init__ 计算。"""
        b = IterationBudget(max_total=20)
        assert b.remaining == 20

    def test_max_total_zero(self):
        """max_total=0 → remaining=0 → 立即耗尽。"""
        b = IterationBudget(max_total=0)
        assert b.remaining == 0
        assert b.utilization == 0.0  # max_total <= 0 → 0.0

    def test_negative_max_total(self):
        """负 max_total → remaining 也负（不会校验）。"""
        b = IterationBudget(max_total=-1)
        assert b.remaining == -1
        # utilization 仍 0.0（<= 0 判断）
        assert b.utilization == 0.0


# ─────────────────────────── consume ───────────────────────────


class TestConsume:
    """consume 消耗一次迭代。"""

    def test_consume_returns_true_when_remaining(self):
        b = IterationBudget(max_total=3)
        assert b.consume() is True

    def test_consume_decrements_remaining(self):
        b = IterationBudget(max_total=3)
        b.consume()
        assert b.remaining == 2
        assert b.used == 1

    def test_consume_until_zero(self):
        b = IterationBudget(max_total=3)
        assert b.consume() is True  # 2 remaining
        assert b.consume() is True  # 1 remaining
        assert b.consume() is True  # 0 remaining
        assert b.remaining == 0
        assert b.used == 3

    def test_consume_returns_false_when_exhausted(self):
        b = IterationBudget(max_total=2)
        b.consume()
        b.consume()
        # 已耗尽
        assert b.consume() is False

    def test_consume_returns_false_repeatedly_when_exhausted(self):
        b = IterationBudget(max_total=1)
        b.consume()
        # 多次调用都返回 False
        assert b.consume() is False
        assert b.consume() is False
        assert b.consume() is False

    def test_consume_when_exhausted_does_not_change_used(self):
        """耗尽后 consume 不修改 used（避免 used 超过 max_total）。"""
        b = IterationBudget(max_total=2)
        b.consume()
        b.consume()
        before = b.used
        b.consume()  # False
        # used 不变
        assert b.used == before

    def test_consume_max_zero(self):
        """max_total=0 → consume 立即返回 False。"""
        b = IterationBudget(max_total=0)
        assert b.consume() is False


# ─────────────────────────── refund ───────────────────────────


class TestRefund:
    """refund 退还一次迭代（压缩后重试）。"""

    def test_refund_returns_true_after_consume(self):
        b = IterationBudget(max_total=3)
        b.consume()
        assert b.refund() is True

    def test_refund_decrements_used(self):
        b = IterationBudget(max_total=3)
        b.consume()
        b.consume()
        b.refund()
        assert b.used == 1
        assert b.remaining == 2

    def test_refund_returns_false_when_nothing_to_refund(self):
        """used=0 时无法退还。"""
        b = IterationBudget(max_total=3)
        assert b.refund() is False

    def test_refund_increments_remaining(self):
        b = IterationBudget(max_total=3)
        b.consume()
        b.refund()
        assert b.remaining == 3  # 回到初始

    def test_refund_after_full_exhaustion(self):
        """耗尽后 refund → 恢复一次。"""
        b = IterationBudget(max_total=2)
        b.consume()
        b.consume()
        assert b.consume() is False  # 已耗尽
        b.refund()
        assert b.consume() is True  # 可再次消耗

    def test_multiple_refunds(self):
        b = IterationBudget(max_total=3)
        b.consume()
        b.consume()
        b.refund()
        b.refund()
        assert b.used == 0
        assert b.remaining == 3


# ─────────────────────────── reset ───────────────────────────


class TestReset:
    """reset 重置到初始状态。"""

    def test_reset_clears_used(self):
        b = IterationBudget(max_total=5)
        b.consume()
        b.consume()
        b.reset()
        assert b.used == 0

    def test_reset_restores_remaining(self):
        b = IterationBudget(max_total=5)
        b.consume()
        b.consume()
        b.reset()
        assert b.remaining == 5

    def test_reset_allows_consume_again(self):
        b = IterationBudget(max_total=2)
        b.consume()
        b.consume()
        b.consume()  # 失败
        b.reset()
        # reset 后可再次消耗
        assert b.consume() is True

    def test_reset_keeps_max_total(self):
        b = IterationBudget(max_total=10)
        b.consume()
        b.consume()
        b.reset()
        assert b.max_total == 10


# ─────────────────────────── utilization ───────────────────────────


class TestUtilization:
    """utilization 利用率（property）。"""

    def test_utilization_zero_initially(self):
        b = IterationBudget(max_total=10)
        assert b.utilization == 0.0

    def test_utilization_after_some_consumes(self):
        b = IterationBudget(max_total=10)
        b.consume()
        b.consume()
        assert abs(b.utilization - 0.2) < 1e-9

    def test_utilization_full(self):
        b = IterationBudget(max_total=4)
        b.consume()
        b.consume()
        b.consume()
        b.consume()
        assert b.utilization == 1.0

    def test_utilization_max_total_zero(self):
        """max_total=0 → utilization 0.0（避免除零）。"""
        b = IterationBudget(max_total=0)
        assert b.utilization == 0.0

    def test_utilization_negative_max(self):
        b = IterationBudget(max_total=-1)
        assert b.utilization == 0.0


# ─────────────────────────── __repr__ ───────────────────────────


class TestRepr:
    """__repr__ 字符串表示。"""

    def test_repr_includes_fields(self):
        b = IterationBudget(max_total=10)
        r = repr(b)
        assert "IterationBudget" in r
        assert "used=" in r
        assert "remaining=" in r
        assert "max=" in r

    def test_repr_after_consume(self):
        b = IterationBudget(max_total=5)
        b.consume()
        r = repr(b)
        assert "used=1" in r
        assert "remaining=4" in r
        assert "max=5" in r


# ─────────────────────────── create_budget_from_config ───────────────────────────


class TestCreateBudgetFromConfig:
    """create_budget_from_config 从配置字典构造。"""

    def test_basic_config(self):
        config = {"max_iterations": 100}
        b = create_budget_from_config(config)
        assert b.max_total == 100
        assert b.remaining == 100

    def test_default_max_iterations(self):
        """无 max_iterations key → 默认 50。"""
        b = create_budget_from_config({})
        assert b.max_total == 50

    def test_config_with_other_keys(self):
        """其他 key 被忽略。"""
        config = {"max_iterations": 20, "other_key": "ignored"}
        b = create_budget_from_config(config)
        assert b.max_total == 20


# ─────────────────────────── 端到端集成 ───────────────────────────


class TestEndToEndIntegration:
    """完整 consume / refund / reset 集成。"""

    def test_compress_then_refund_cycle(self):
        """模拟"压缩后重试"语义：consume → 压缩 → refund → 再 consume。"""
        b = IterationBudget(max_total=5)
        b.consume()  # used=1
        b.consume()  # used=2
        # 假设 compress 发生 → refund 一次（节省的 token 值得重试）
        b.refund()   # used=1
        # 再次 consume
        b.consume()  # used=2
        assert b.used == 2
        assert b.remaining == 3

    def test_long_running_consume_until_exhaustion(self):
        b = IterationBudget(max_total=3)
        # 完整生命周期
        results = [b.consume() for _ in range(5)]
        assert results == [True, True, True, False, False]
        assert b.used == 3
        assert b.remaining == 0
        assert b.utilization == 1.0

    def test_repr_after_full_lifecycle(self):
        b = IterationBudget(max_total=3)
        b.consume()
        b.consume()
        b.consume()
        r = repr(b)
        assert "used=3" in r
        assert "remaining=0" in r
        assert "max=3" in r