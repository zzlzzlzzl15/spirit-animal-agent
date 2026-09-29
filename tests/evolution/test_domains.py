"""领域适配器测试（Phase 6.F）：Domain 基类 fail-soft + 金融示例回测裁决。"""

from spirit.evolution.domains.base import Domain, DomainInfo
from spirit.evolution.domains.finance import FinanceDomain
from spirit.evolution.protocol import TargetVerdict
from spirit.evolution.verifier import Evidence


# --------------------------------------------------------------------------
# Domain 基类默认（fail-soft）
# --------------------------------------------------------------------------

def test_base_domain_defaults():
    d = Domain()
    info = d.collect_info()
    assert isinstance(info, DomainInfo)
    assert info.data == {}
    assert d.propose_tasks(info) == []
    assert d.verify("t", Evidence()) is None
    assert d.reward_signal("t", {}) == 0.0
    assert d.verifier_for("t") is None


def test_base_domain_unverified_helper():
    v = Domain().unverified("数据缺失", task_id="t1")
    assert v.verdict is TargetVerdict.UNVERIFIED
    assert v.task_id == "t1"
    assert v.reason


def test_domain_info_to_dict():
    d = DomainInfo(domain="x", data={"a": 1}, summary="s")
    assert d.to_dict()["domain"] == "x"


# --------------------------------------------------------------------------
# FinanceDomain · 信息收集
# --------------------------------------------------------------------------

def test_collect_info_uses_data_source():
    src = lambda: {"symbols": ["AAPL", "MSFT"], "news": ["n1", "n2"]}
    dom = FinanceDomain(data_source=src)
    info = dom.collect_info()
    assert info.data["symbols"] == ["AAPL", "MSFT"]
    assert "标的 2 个" in info.summary
    assert "新闻 2 条" in info.summary


def test_collect_info_without_source_is_empty():
    dom = FinanceDomain(symbols=["X"])
    info = dom.collect_info()
    assert info.data == {}
    assert info.domain == "finance"


def test_collect_info_source_raises_fail_soft():
    def boom():
        raise RuntimeError("行情 API 挂了")

    dom = FinanceDomain(data_source=boom)
    info = dom.collect_info()
    assert info.data == {}


# --------------------------------------------------------------------------
# FinanceDomain · 任务生成
# --------------------------------------------------------------------------

def test_propose_tasks_from_info():
    dom = FinanceDomain(sharpe_threshold=1.5)
    info = DomainInfo(domain="finance", data={"symbols": ["AAPL", "MSFT", "GOOG"]})
    tasks = dom.propose_tasks(info, limit=2)
    assert len(tasks) == 2
    assert "AAPL" in tasks[0]
    assert "sharpe ≥ 1.5" in tasks[0]


def test_propose_tasks_falls_back_to_symbols():
    dom = FinanceDomain(symbols=["TSLA"])
    tasks = dom.propose_tasks(None, limit=5)
    assert len(tasks) == 1
    assert "TSLA" in tasks[0]


def test_propose_tasks_empty_when_no_symbols():
    assert FinanceDomain().propose_tasks(None) == []


# --------------------------------------------------------------------------
# FinanceDomain · 客观验证（回测夏普阈值）
# --------------------------------------------------------------------------

def test_verify_pass_above_threshold():
    dom = FinanceDomain(sharpe_threshold=1.0)
    v = dom.verify("策略", Evidence(metrics={"sharpe": 1.5}))
    assert v.verdict is TargetVerdict.PASS


def test_verify_fail_below_threshold():
    dom = FinanceDomain(sharpe_threshold=1.0)
    v = dom.verify("策略", Evidence(metrics={"sharpe": 0.3}))
    assert v.verdict is TargetVerdict.FAIL


def test_verify_unverified_without_metrics():
    dom = FinanceDomain()
    v = dom.verify("策略", Evidence())
    assert v.verdict is TargetVerdict.UNVERIFIED


def test_verify_accepts_dict_evidence():
    dom = FinanceDomain(sharpe_threshold=1.0)
    v = dom.verify("策略", {"metrics": {"sharpe": 2.0}})
    assert v.verdict is TargetVerdict.PASS


# --------------------------------------------------------------------------
# FinanceDomain · 奖励信号 + 回测
# --------------------------------------------------------------------------

def test_reward_signal_from_evidence():
    dom = FinanceDomain()
    assert dom.reward_signal("t", Evidence(metrics={"sharpe": 1.7})) == 1.7


def test_reward_signal_from_dict():
    dom = FinanceDomain()
    assert dom.reward_signal("t", {"sharpe": 0.9}) == 0.9


def test_reward_signal_from_nested_metrics():
    dom = FinanceDomain()
    assert dom.reward_signal("t", {"metrics": {"sharpe": 2.2}}) == 2.2


def test_reward_signal_missing_is_zero():
    dom = FinanceDomain()
    assert dom.reward_signal("t", {"other": 5}) == 0.0


def test_run_backtest_packs_metrics():
    bt = lambda task, data: {"sharpe": 1.3, "return": 0.2}
    dom = FinanceDomain(backtest=bt)
    ev = dom.run_backtest("策略", {"prices": []})
    assert ev.metrics["sharpe"] == 1.3


def test_run_backtest_without_backtest_is_empty():
    dom = FinanceDomain()
    ev = dom.run_backtest("策略")
    assert ev.metrics == {}


def test_run_backtest_exception_fail_soft():
    def boom(task, data):
        raise RuntimeError("回测崩了")

    dom = FinanceDomain(backtest=boom)
    ev = dom.run_backtest("策略")
    assert ev.metrics == {}
    assert "回测异常" in ev.logs
