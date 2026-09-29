"""spirit.achievements.definitions 单元测试 —— 目录完整性。

关键守护：每个成就引用的 metric 都必须在 METRIC_LABELS 中登记（即能被
metrics.collect_metrics 派生），否则成就永远无法解锁 —— 这是目录与采集器之间的契约。
"""

import pytest

from spirit.achievements import definitions
from spirit.achievements.metrics import collect_metrics


class TestCatalogIntegrity:
    def test_ids_unique(self):
        ids = [a["id"] for a in definitions.ACHIEVEMENTS]
        assert len(ids) == len(set(ids))

    def test_by_id_index_complete(self):
        assert set(definitions.BY_ID.keys()) == {
            a["id"] for a in definitions.ACHIEVEMENTS
        }

    def test_every_achievement_has_required_fields(self):
        for a in definitions.ACHIEVEMENTS:
            assert a.get("id")
            assert a.get("name")
            assert a.get("category")
            assert a.get("kind") in ("lifetime", "best_session", "multi_condition")

    def test_tiered_have_five_ordered_tiers(self):
        for a in definitions.ACHIEVEMENTS:
            if a.get("tiers"):
                assert len(a["tiers"]) == len(definitions.TIER_NAMES)
                thresholds = [t["threshold"] for t in a["tiers"]]
                assert thresholds == sorted(thresholds)
                assert all(t > 0 for t in thresholds)

    def test_multi_condition_have_requirements(self):
        for a in definitions.ACHIEVEMENTS:
            if a["kind"] == "multi_condition":
                assert a.get("requirements"), f"{a['id']} 缺 requirements"
                for r in a["requirements"]:
                    assert "metric" in r and "gte" in r


class TestMetricContract:
    def _referenced_metrics(self):
        metrics = set()
        for a in definitions.ACHIEVEMENTS:
            if a.get("threshold_metric"):
                metrics.add(a["threshold_metric"])
            for r in a.get("requirements", []):
                metrics.add(r["metric"])
        return metrics

    def test_all_referenced_metrics_are_labeled(self):
        referenced = self._referenced_metrics()
        missing = referenced - set(definitions.METRIC_LABELS.keys())
        assert not missing, f"未登记标签的 metric: {missing}"

    def test_all_referenced_metrics_are_collectable(self, db):
        """目录引用的每个 metric 都必须由 collect_metrics 实际产出。"""
        produced = set(collect_metrics(db).keys())
        referenced = self._referenced_metrics()
        missing = referenced - produced
        assert not missing, f"collect_metrics 未产出的 metric: {missing}"


class TestHelpers:
    def test_tiers_zip_with_names(self):
        out = definitions.tiers([1, 2, 3, 4, 5])
        assert [t["name"] for t in out] == definitions.TIER_NAMES
        assert [t["threshold"] for t in out] == [1, 2, 3, 4, 5]

    def test_req_shape(self):
        assert definitions.req("foo", 10) == {"metric": "foo", "gte": 10}

    def test_categories_sorted_unique(self):
        assert definitions.CATEGORIES == sorted(set(definitions.CATEGORIES))
