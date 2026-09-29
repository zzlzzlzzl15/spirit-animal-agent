"""spirit.achievements.store + check_achievements 端到端测试。"""

import json

import pytest

from spirit import config
from spirit.achievements import (
    AchievementStore,
    achievements_dir,
    check_achievements,
    state_path,
)
from spirit.achievements.definitions import ACHIEVEMENTS, tiers

from tests.achievements.conftest import add_message, add_session, toolcall


# --------------------------------------------------------------------------
# 路径解析（按调用读 SPIRIT_HOME）
# --------------------------------------------------------------------------

class TestPathResolution:
    def test_state_path_under_spirit_home(self, spirit_home):
        assert state_path() == spirit_home / "achievements" / "state.json"
        assert achievements_dir() == spirit_home / "achievements"

    def test_monkeypatch_reflects_immediately(self, monkeypatch, tmp_path):
        other = tmp_path / "other_home"
        monkeypatch.setattr(config, "SPIRIT_HOME", other, raising=False)
        assert achievements_dir() == other / "achievements"


# --------------------------------------------------------------------------
# AchievementStore
# --------------------------------------------------------------------------

class TestStore:
    def test_load_missing_returns_empty(self, spirit_home):
        store = AchievementStore()
        state = store.load()
        assert state["unlocks"] == {}
        assert state["schema_version"] == 1

    def test_save_and_load_roundtrip(self, spirit_home):
        store = AchievementStore()
        state = {"schema_version": 1, "unlocks": {"x": {"tier": "Gold"}},
                 "last_evaluated": 123.0}
        assert store.save(state) is True
        loaded = store.load()
        assert loaded["unlocks"]["x"]["tier"] == "Gold"
        assert loaded["last_evaluated"] == 123.0

    def test_corrupt_state_degrades_to_empty(self, spirit_home):
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")
        store = AchievementStore()
        assert store.load()["unlocks"] == {}

    def test_non_dict_state_degrades(self, spirit_home):
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert AchievementStore().load()["unlocks"] == {}

    def test_root_override(self, tmp_path):
        root = tmp_path / "custom_ach"
        store = AchievementStore(root=root)
        store.save({"schema_version": 1, "unlocks": {"a": {}}, "last_evaluated": None})
        assert (root / "state.json").exists()

    def test_get_unlocks(self, spirit_home):
        store = AchievementStore()
        store.save({"schema_version": 1, "unlocks": {"a": {"tier": "Copper"}},
                    "last_evaluated": None})
        assert store.get_unlocks()["a"]["tier"] == "Copper"

    def test_reset_clears(self, spirit_home):
        store = AchievementStore()
        store.save({"schema_version": 1, "unlocks": {"a": {}}, "last_evaluated": 1.0})
        store.reset()
        assert store.get_unlocks() == {}

    def test_record_evaluation_writes_unlocks(self, spirit_home):
        store = AchievementStore()
        evaluation = {
            "newly_unlocked": [
                {"id": "let_him_cook", "tier": "Silver", "name": "放手一搏",
                 "category": "Agent 自主", "icon": "flame"}
            ],
            "upgraded": [],
        }
        state = store.record_evaluation(evaluation)
        assert "let_him_cook" in state["unlocks"]
        assert state["unlocks"]["let_him_cook"]["tier"] == "Silver"
        assert state["last_evaluated"] is not None
        # 已落盘
        assert store.get_unlocks()["let_him_cook"]["tier"] == "Silver"

    def test_record_evaluation_upgrade_preserves_unlocked_at(self, spirit_home):
        store = AchievementStore()
        store.record_evaluation({
            "newly_unlocked": [
                {"id": "x", "tier": "Copper", "name": "X", "category": "C", "icon": "i"}
            ],
            "upgraded": [],
        })
        first = store.get_unlocks()["x"]
        store.record_evaluation({
            "newly_unlocked": [],
            "upgraded": [
                {"id": "x", "tier": "Gold", "name": "X", "category": "C", "icon": "i"}
            ],
        })
        second = store.get_unlocks()["x"]
        assert second["tier"] == "Gold"
        assert second["unlocked_at"] == first["unlocked_at"]  # 保留首次解锁时间
        assert "upgraded_at" in second


# --------------------------------------------------------------------------
# check_achievements 端到端
# --------------------------------------------------------------------------

class TestCheckAchievements:
    def test_empty_db_no_unlocks(self, db, spirit_home):
        result = check_achievements(db)
        assert result["newly_unlocked"] == []
        assert result["metrics"]["session_count"] == 0

    def test_unlocks_persisted_to_state(self, db, spirit_home):
        # 造 1 个会话，1 条消息含 1 个工具调用 → marathon_operator Copper 需 10 会话，
        # 不足以解锁分级成就；用自定义低阈值 catalog 验证端到端落盘。
        cat = [{
            "id": "first_session", "name": "初次会话", "category": "测试",
            "kind": "lifetime", "threshold_metric": "session_count",
            "tiers": tiers([1, 2, 3, 4, 5]),
        }]
        add_session(db, "s1")
        result = check_achievements(db, catalog=cat)
        assert len(result["newly_unlocked"]) == 1
        assert result["newly_unlocked"][0]["tier"] == "Copper"
        # 落盘验证
        assert AchievementStore().get_unlocks()["first_session"]["tier"] == "Copper"

    def test_second_run_no_duplicate_unlock(self, db, spirit_home):
        cat = [{
            "id": "first_session", "name": "初次会话", "category": "测试",
            "kind": "lifetime", "threshold_metric": "session_count",
            "tiers": tiers([1, 2, 3, 4, 5]),
        }]
        add_session(db, "s1")
        check_achievements(db, catalog=cat)
        # 第二次：同一 tier，不应重复报新解锁
        result2 = check_achievements(db, catalog=cat)
        assert result2["newly_unlocked"] == []

    def test_upgrade_across_runs(self, db, spirit_home):
        cat = [{
            "id": "first_session", "name": "初次会话", "category": "测试",
            "kind": "lifetime", "threshold_metric": "session_count",
            "tiers": tiers([1, 2, 3, 4, 5]),
        }]
        add_session(db, "s1")
        check_achievements(db, catalog=cat)      # Copper
        add_session(db, "s2")
        result = check_achievements(db, catalog=cat)  # Silver
        assert result["newly_unlocked"] == []
        assert len(result["upgraded"]) == 1
        assert result["upgraded"][0]["tier"] == "Silver"

    def test_persist_false_does_not_write(self, db, spirit_home):
        cat = [{
            "id": "first_session", "name": "初次会话", "category": "测试",
            "kind": "lifetime", "threshold_metric": "session_count",
            "tiers": tiers([1, 2, 3, 4, 5]),
        }]
        add_session(db, "s1")
        result = check_achievements(db, catalog=cat, persist=False)
        assert len(result["newly_unlocked"]) == 1
        # 未落盘
        assert AchievementStore().get_unlocks() == {}

    def test_custom_store_injection(self, db, tmp_path):
        store = AchievementStore(root=tmp_path / "ach")
        cat = [{
            "id": "first_session", "name": "初次会话", "category": "测试",
            "kind": "lifetime", "threshold_metric": "session_count",
            "tiers": tiers([1, 2, 3, 4, 5]),
        }]
        add_session(db, "s1")
        check_achievements(db, store=store, catalog=cat)
        assert (tmp_path / "ach" / "state.json").exists()

    def test_real_catalog_with_rich_history(self, db, spirit_home):
        # 造一段真实历史，验证内置 catalog 能解锁若干成就且不报错
        for i in range(12):
            add_session(db, f"s{i}", model="gpt-4" if i % 2 else "claude-3")
            add_message(db, f"s{i}", tool_calls=toolcall(
                ["terminal", "read_file", "grep", "web_search"]
            ))
        result = check_achievements(db)
        assert result["metrics"]["session_count"] == 12
        assert result["metrics"]["distinct_model_count"] == 2
        # marathon_operator Copper 阈值 10 → 应解锁
        unlocked_ids = {u["id"] for u in result["newly_unlocked"]}
        assert "marathon_operator" in unlocked_ids

    def test_broken_db_fails_soft(self, spirit_home):
        class BrokenConn:
            def execute(self, *a, **k):
                raise RuntimeError("db down")
        result = check_achievements(BrokenConn())
        # collect_metrics 内部 fail-soft → 全零指标，不报错
        assert result["metrics"]["session_count"] == 0
        assert result["newly_unlocked"] == []
