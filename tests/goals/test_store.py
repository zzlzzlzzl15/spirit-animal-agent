"""tests/goals/test_store.py — GoalStore 持久化后端 + 会话迁移。

移植自 Hermes ``tests/hermes_cli/test_goals.py`` 的 load_goal/save_goal/clear_goal/
migrate_goal_to_session 相关用例。Spirit 把存储抽象成可注入的 :class:`GoalStore`：

- :class:`InMemoryGoalStore` — 测试 / 无 DB 环境（存 JSON、get 返回反序列化副本）。
- :class:`SessionDBGoalStore` — 生产默认（用 SessionDB 的 ``state_meta`` 表，
  key=``goal:<session_id>``），持久化测试用 conftest 的 ``tmp_db`` fixture 隔离到
  tmp_path，无需 monkeypatch HERMES_HOME。

两个后端语义必须逐点一致（避免测试与生产漂移），故对同一组断言参数化跑两遍。
"""

from __future__ import annotations

import pytest

from spirit.goals import (
    GoalContract,
    GoalState,
    GoalStore,
    InMemoryGoalStore,
    SessionDBGoalStore,
    migrate_goal_to_session,
)


class _BrokenDB:
    """get_meta/set_meta 一律抛错的假 DB —— 验证 SessionDBGoalStore 防御性降级。"""

    def get_meta(self, key):
        raise RuntimeError("db down")

    def set_meta(self, key, value):
        raise RuntimeError("db down")


# ──────────────────────────────────────────────────────────────────────
# 抽象接口
# ──────────────────────────────────────────────────────────────────────


class TestGoalStoreAbstract:
    def test_abstract_methods_raise(self):
        store = GoalStore()
        with pytest.raises(NotImplementedError):
            store.get("x")
        with pytest.raises(NotImplementedError):
            store.save("x", GoalState(goal="g"))
        with pytest.raises(NotImplementedError):
            store.clear("x")


# ──────────────────────────────────────────────────────────────────────
# InMemoryGoalStore
# ──────────────────────────────────────────────────────────────────────


class TestInMemoryGoalStore:
    def test_get_missing_returns_none(self):
        assert InMemoryGoalStore().get("nope") is None

    def test_save_get_roundtrip(self):
        store = InMemoryGoalStore()
        store.save("sid", GoalState(goal="carry", max_turns=7))
        got = store.get("sid")
        assert got is not None
        assert got.goal == "carry"
        assert got.max_turns == 7

    def test_get_returns_independent_copy(self):
        # 存 JSON、get 反序列化出新对象 —— mutation 不影响下一次 get。
        store = InMemoryGoalStore()
        store.save("sid", GoalState(goal="original"))
        got = store.get("sid")
        got.goal = "mutated"
        assert store.get("sid").goal == "original"

    def test_clear_marks_cleared_keeps_record(self):
        store = InMemoryGoalStore()
        store.save("sid", GoalState(goal="g"))
        store.clear("sid")
        got = store.get("sid")
        assert got is not None
        assert got.status == "cleared"

    def test_clear_missing_is_noop(self):
        store = InMemoryGoalStore()
        store.clear("nope")  # 不抛
        assert store.get("nope") is None

    def test_empty_session_id_get_none(self):
        assert InMemoryGoalStore().get("") is None

    def test_empty_session_id_save_noop(self):
        store = InMemoryGoalStore()
        store.save("", GoalState(goal="g"))  # 不抛
        assert store.get("") is None

    def test_corrupt_json_returns_none(self):
        store = InMemoryGoalStore()
        store._data["goal:sid"] = "{not valid json"
        assert store.get("sid") is None


# ──────────────────────────────────────────────────────────────────────
# SessionDBGoalStore（真实 state_meta 表，tmp_db 隔离）
# ──────────────────────────────────────────────────────────────────────


class TestSessionDBGoalStore:
    def test_save_get_roundtrip(self, tmp_db):
        store = SessionDBGoalStore(db=tmp_db)
        state = GoalState(goal="persist", max_turns=7)
        state.subgoals = ["a"]
        state.contract = GoalContract(verification="tests pass")
        store.save("sid", state)
        got = store.get("sid")
        assert got is not None
        assert got.goal == "persist"
        assert got.max_turns == 7
        assert got.subgoals == ["a"]
        assert got.contract.verification == "tests pass"

    def test_get_missing_returns_none(self, tmp_db):
        assert SessionDBGoalStore(db=tmp_db).get("nonexistent") is None

    def test_writes_to_state_meta_table(self, tmp_db):
        store = SessionDBGoalStore(db=tmp_db)
        store.save("sid", GoalState(goal="g"))
        raw = tmp_db.get_meta("goal:sid")
        assert raw is not None
        assert "g" in raw

    def test_clear_marks_cleared_keeps_record(self, tmp_db):
        store = SessionDBGoalStore(db=tmp_db)
        store.save("sid", GoalState(goal="g"))
        store.clear("sid")
        got = store.get("sid")
        assert got is not None
        assert got.status == "cleared"

    def test_overwrite_existing(self, tmp_db):
        store = SessionDBGoalStore(db=tmp_db)
        store.save("sid", GoalState(goal="v1"))
        store.save("sid", GoalState(goal="v2"))
        assert store.get("sid").goal == "v2"

    def test_empty_session_id_get_none(self, tmp_db):
        assert SessionDBGoalStore(db=tmp_db).get("") is None

    def test_broken_db_save_is_noop(self):
        store = SessionDBGoalStore(db=_BrokenDB())
        store.save("sid", GoalState(goal="g"))  # 不抛

    def test_broken_db_get_returns_none(self):
        store = SessionDBGoalStore(db=_BrokenDB())
        assert store.get("sid") is None

    def test_broken_db_clear_is_noop(self):
        store = SessionDBGoalStore(db=_BrokenDB())
        store.clear("sid")  # 不抛


# ──────────────────────────────────────────────────────────────────────
# 两后端语义一致性（参数化）
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(params=["memory", "sessiondb"])
def store(request, tmp_path):
    # 两个分支都用 yield —— 含 yield 的 fixture 函数被 pytest 整体视为生成器
    # fixture，任何分支提前 return 都会导致"未 yield"错误。
    if request.param == "memory":
        yield InMemoryGoalStore()
        return
    from spirit.storage.session_db import SessionDB

    db = SessionDB(db_path=tmp_path / "parity.db")
    try:
        yield SessionDBGoalStore(db=db)
    finally:
        try:
            db.close()
        except Exception:
            pass


class TestStoreParity:
    def test_roundtrip_parity(self, store):
        store.save("sid", GoalState(goal="parity", max_turns=3))
        got = store.get("sid")
        assert got.goal == "parity"
        assert got.max_turns == 3

    def test_clear_parity(self, store):
        store.save("sid", GoalState(goal="g"))
        store.clear("sid")
        assert store.get("sid").status == "cleared"

    def test_missing_parity(self, store):
        assert store.get("missing") is None


# ──────────────────────────────────────────────────────────────────────
# migrate_goal_to_session（压缩轮换 session_id 时搬运目标）
# ──────────────────────────────────────────────────────────────────────


class TestMigrateGoalToSession:
    def test_migrate_active_goal(self):
        store = InMemoryGoalStore()
        store.save("old", GoalState(goal="carry me", max_turns=5))
        ok = migrate_goal_to_session("old", "new", store=store, reason="compression")
        assert ok is True
        assert store.get("new").goal == "carry me"
        assert store.get("new").max_turns == 5
        # 旧行归档为 cleared（每个逻辑对话恰好一条活跃目标）。
        assert store.get("old").status == "cleared"

    def test_migrate_paused_goal(self):
        store = InMemoryGoalStore()
        store.save("old", GoalState(goal="paused goal", status="paused"))
        ok = migrate_goal_to_session("old", "new", store=store)
        assert ok is True
        assert store.get("new").status == "paused"

    def test_migrate_same_session_returns_false(self):
        store = InMemoryGoalStore()
        store.save("s", GoalState(goal="g"))
        assert migrate_goal_to_session("s", "s", store=store) is False

    def test_migrate_empty_ids_returns_false(self):
        store = InMemoryGoalStore()
        assert migrate_goal_to_session("", "new", store=store) is False
        assert migrate_goal_to_session("old", "", store=store) is False

    def test_migrate_no_source_goal_returns_false(self):
        store = InMemoryGoalStore()
        assert migrate_goal_to_session("old", "new", store=store) is False

    def test_migrate_cleared_source_returns_false(self):
        store = InMemoryGoalStore()
        store.save("old", GoalState(goal="g", status="cleared"))
        assert migrate_goal_to_session("old", "new", store=store) is False
        assert store.get("new") is None

    def test_migrate_does_not_overwrite_existing_new_goal(self):
        store = InMemoryGoalStore()
        store.save("old", GoalState(goal="old goal"))
        store.save("new", GoalState(goal="new goal"))
        ok = migrate_goal_to_session("old", "new", store=store)
        assert ok is False
        assert store.get("new").goal == "new goal"  # 未被覆盖
        assert store.get("old").status == "active"  # 未被归档

    def test_migrate_preserves_contract_and_subgoals(self):
        store = InMemoryGoalStore()
        src = GoalState(goal="rich", max_turns=9)
        src.subgoals = ["x", "y"]
        src.contract = GoalContract(verification="pytest passes")
        store.save("old", src)
        migrate_goal_to_session("old", "new", store=store)
        got = store.get("new")
        assert got.subgoals == ["x", "y"]
        assert got.contract.verification == "pytest passes"

    def test_migrate_on_sessiondb_store(self, tmp_db):
        store = SessionDBGoalStore(db=tmp_db)
        store.save("old", GoalState(goal="db carry"))
        ok = migrate_goal_to_session("old", "new", store=store)
        assert ok is True
        assert store.get("new").goal == "db carry"
        assert store.get("old").status == "cleared"
