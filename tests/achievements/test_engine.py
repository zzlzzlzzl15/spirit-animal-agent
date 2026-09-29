"""spirit.achievements.engine 单元测试 —— 评估语义。"""

import pytest

from spirit.achievements import definitions
from spirit.achievements.engine import (
    AchievementEngine,
    evaluate_boolean,
    evaluate_definition,
    evaluate_requirements,
    evaluate_tiered,
    _tier_rank,
)


def _tiered(metric="total_tool_calls", thresholds=(10, 50, 100, 500, 1000), **extra):
    d = {
        "id": "test_tiered", "name": "T", "category": "C", "kind": "lifetime",
        "threshold_metric": metric, "tiers": definitions.tiers(list(thresholds)),
    }
    d.update(extra)
    return d


def _multi(reqs, **extra):
    d = {
        "id": "test_multi", "name": "M", "category": "C", "kind": "multi_condition",
        "requirements": reqs,
    }
    d.update(extra)
    return d


class TestEvaluateTiered:
    def test_no_progress_not_unlocked(self):
        res = evaluate_tiered(_tiered(), {"total_tool_calls": 0})
        assert res["unlocked"] is False
        assert res["tier"] is None
        assert res["state"] == "discovered"

    def test_first_tier_unlocked(self):
        res = evaluate_tiered(_tiered(), {"total_tool_calls": 10})
        assert res["unlocked"] is True
        assert res["tier"] == "Copper"

    def test_highest_achieved_tier_selected(self):
        res = evaluate_tiered(_tiered(), {"total_tool_calls": 60})
        assert res["tier"] == "Silver"  # 60 ≥ 50(Silver) 但 < 100(Gold)

    def test_max_tier(self):
        res = evaluate_tiered(_tiered(), {"total_tool_calls": 5000})
        assert res["tier"] == "Olympian"
        assert res["progress_pct"] == 100

    def test_progress_pct_between_tiers(self):
        # 30 在 Copper(10) → Silver(50) 之间：(30-10)/(50-10)=50%
        res = evaluate_tiered(_tiered(), {"total_tool_calls": 30})
        assert res["progress_pct"] == 50
        assert res["next_tier"] == "Silver"
        assert res["next_threshold"] == 50

    def test_missing_metric_defaults_zero(self):
        res = evaluate_tiered(_tiered(), {})
        assert res["progress"] == 0
        assert res["unlocked"] is False

    def test_secret_hidden_until_progress(self):
        d = _tiered(secret=True)
        # 无进度 → secret，discovered False
        res0 = evaluate_tiered(d, {"total_tool_calls": 0})
        assert res0["state"] == "secret"
        assert res0["discovered"] is False
        # 有进度但未达首 tier → discovered
        res1 = evaluate_tiered(d, {"total_tool_calls": 5})
        assert res1["state"] == "discovered"
        assert res1["discovered"] is True


class TestEvaluateRequirements:
    def test_all_met_unlocks(self):
        d = _multi([definitions.req("a", 5), definitions.req("b", 10)])
        res = evaluate_requirements(d, {"a": 5, "b": 10})
        assert res["unlocked"] is True
        assert res["progress_pct"] == 100

    def test_partial_not_unlocked(self):
        d = _multi([definitions.req("a", 5), definitions.req("b", 10)])
        res = evaluate_requirements(d, {"a": 5, "b": 5})
        assert res["unlocked"] is False
        # a 完成(1.0) + b 半成(0.5) → 平均 75%
        assert res["progress_pct"] == 75

    def test_over_threshold_capped(self):
        d = _multi([definitions.req("a", 5)])
        res = evaluate_requirements(d, {"a": 100})
        assert res["progress_pct"] == 100

    def test_empty_requirements(self):
        d = _multi([])
        res = evaluate_requirements(d, {"a": 100})
        assert res["unlocked"] is False

    def test_secret_multi_condition(self):
        d = _multi([definitions.req("a", 5)], secret=True)
        res = evaluate_requirements(d, {"a": 0})
        assert res["state"] == "secret"


class TestEvaluateBooleanAndDispatch:
    def test_boolean(self):
        d = {"id": "b", "metric": "flag"}
        assert evaluate_boolean(d, {"flag": True})["unlocked"] is True
        assert evaluate_boolean(d, {"flag": 0})["unlocked"] is False

    def test_dispatch_multi_condition(self):
        d = _multi([definitions.req("a", 1)])
        res = evaluate_definition(d, {"a": 1})
        assert res["unlocked"] is True

    def test_dispatch_tiered(self):
        d = _tiered()
        res = evaluate_definition(d, {"total_tool_calls": 10})
        assert res["tier"] == "Copper"


class TestTierRank:
    def test_ranks(self):
        assert _tier_rank(None) == 0
        assert _tier_rank("Copper") == 1
        assert _tier_rank("Olympian") == 5

    def test_unknown_tier(self):
        assert _tier_rank("Platinum") == 0


class TestEvaluateAll:
    def test_catalog_evaluated(self):
        eng = AchievementEngine()
        out = eng.evaluate_all({"total_tool_calls": 0})
        assert out["total"] == len(definitions.ACHIEVEMENTS)
        assert set(out["results"].keys()) == definitions.BY_ID.keys()

    def test_newly_unlocked_detected(self):
        cat = [_tiered()]
        eng = AchievementEngine(catalog=cat)
        out = eng.evaluate_all({"total_tool_calls": 60}, prior_unlocks={})
        assert len(out["newly_unlocked"]) == 1
        assert out["newly_unlocked"][0]["tier"] == "Silver"
        assert out["unlocked_count"] == 1

    def test_no_duplicate_unlock_when_already_recorded(self):
        cat = [_tiered()]
        eng = AchievementEngine(catalog=cat)
        prior = {"test_tiered": {"tier": "Silver"}}
        out = eng.evaluate_all({"total_tool_calls": 60}, prior_unlocks=prior)
        assert out["newly_unlocked"] == []
        assert out["upgraded"] == []

    def test_upgrade_detected(self):
        cat = [_tiered()]
        eng = AchievementEngine(catalog=cat)
        prior = {"test_tiered": {"tier": "Copper"}}
        out = eng.evaluate_all({"total_tool_calls": 120}, prior_unlocks=prior)
        assert out["newly_unlocked"] == []
        assert len(out["upgraded"]) == 1
        assert out["upgraded"][0]["tier"] == "Gold"  # 120 ≥ 100(Gold)

    def test_progress_view_hides_secret(self):
        cat = [_tiered(secret=True)]
        eng = AchievementEngine(catalog=cat)
        view = eng.progress({"total_tool_calls": 0})
        assert view[0]["name"] == "???"
        assert view[0]["state"] == "secret"

    def test_progress_view_shows_unlocked(self):
        cat = [_tiered()]
        eng = AchievementEngine(catalog=cat)
        view = eng.progress({"total_tool_calls": 60})
        assert view[0]["name"] == "T"
        assert view[0]["tier"] == "Silver"
