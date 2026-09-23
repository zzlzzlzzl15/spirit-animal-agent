"""tests/skills_hub/test_usage.py — 技能使用追踪核心。

对标 Hermes ``tests/tools/test_skill_usage.py`` 的**追踪核心**子集（Spirit 只移植计数 +
时间戳 + 活跃度聚合，未移植 curator 的 state/archive/pin 生命周期）：``bump_use`` /
``bump_view`` 计数与时间戳、空名 noop、并发安全（进程内锁）、``activity_count`` /
``latest_activity_at`` 聚合、损坏文件降级、持久化到 ``<hub>/usage.json``。
"""

from __future__ import annotations

import json
import threading

from spirit.skills_hub import paths, usage


class TestRoundTrip:
    def test_empty_get_returns_none(self, skills_home):
        assert usage.get_usage("nonexistent") is None

    def test_empty_all_returns_empty(self, skills_home):
        assert usage.all_usage() == {}

    def test_persists_under_hub(self, skills_home):
        usage.bump_use("my-skill")
        assert paths.usage_file().exists()
        data = json.loads(paths.usage_file().read_text(encoding="utf-8"))
        assert "my-skill" in data

    def test_corrupt_file_degrades(self, skills_home):
        paths.usage_file().parent.mkdir(parents=True, exist_ok=True)
        paths.usage_file().write_text("{ not json }", encoding="utf-8")
        assert usage.all_usage() == {}
        assert usage.get_usage("x") is None


class TestBumps:
    def test_bump_use_increments_and_timestamps(self, skills_home):
        usage.bump_use("my-skill")
        usage.bump_use("my-skill")
        rec = usage.get_usage("my-skill")
        assert rec["use_count"] == 2
        assert rec["last_used_at"]

    def test_bump_view_increments_and_timestamps(self, skills_home):
        usage.bump_view("my-skill")
        rec = usage.get_usage("my-skill")
        assert rec["view_count"] == 1
        assert rec["last_viewed_at"]

    def test_new_record_has_first_seen(self, skills_home):
        usage.bump_use("fresh")
        rec = usage.get_usage("fresh")
        assert "first_seen_at" in rec

    def test_empty_name_is_noop(self, skills_home):
        usage.bump_use("")
        usage.bump_view("   ")
        assert usage.all_usage() == {}

    def test_bumps_do_not_corrupt_others(self, skills_home):
        usage.bump_view("skill-a")
        usage.bump_use("skill-b")
        usage.bump_view("skill-a")
        a = usage.get_usage("skill-a")
        b = usage.get_usage("skill-b")
        assert a["view_count"] == 2
        assert a.get("use_count", 0) == 0
        assert b["use_count"] == 1

    def test_all_usage_returns_every_record(self, skills_home):
        usage.bump_use("a")
        usage.bump_view("b")
        assert set(usage.all_usage()) == {"a", "b"}

    def test_concurrent_bumps_preserve_all(self, skills_home):
        # 进程内锁串行化读-改-写：N 线程 × M 次 → N*M。
        n_threads, iters = 8, 30
        def _worker():
            for _ in range(iters):
                usage.bump_use("shared")
        threads = [threading.Thread(target=_worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert usage.get_usage("shared")["use_count"] == n_threads * iters


class TestAggregation:
    def test_activity_count_sums(self):
        rec = {"use_count": 2, "view_count": 3, "patch_count": 1}
        assert usage.activity_count(rec) == 6

    def test_activity_count_handles_missing_and_bad(self):
        assert usage.activity_count({}) == 0
        assert usage.activity_count({"use_count": "x", "view_count": 2}) == 2
        assert usage.activity_count("not a dict") == 0

    def test_latest_activity_at_picks_max(self):
        rec = {
            "last_used_at": "2026-01-01T00:00:00+00:00",
            "last_viewed_at": "2026-03-01T00:00:00+00:00",
            "last_patched_at": "2026-02-01T00:00:00+00:00",
        }
        assert usage.latest_activity_at(rec) == "2026-03-01T00:00:00+00:00"

    def test_latest_activity_at_none_when_empty(self):
        assert usage.latest_activity_at({}) is None
        assert usage.latest_activity_at("bad") is None

    def test_aggregation_from_real_record(self, skills_home):
        usage.bump_use("s")
        usage.bump_view("s")
        rec = usage.get_usage("s")
        assert usage.activity_count(rec) == 2
        assert usage.latest_activity_at(rec) is not None
