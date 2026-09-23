"""tests/goals/test_goal_state.py — GoalState / GoalContract / parse_contract。

移植自 Hermes ``tests/hermes_cli/test_goals.py`` 的：
- TestParseContract
- TestGoalContractSerialization
- TestGoalStateSubgoalsBackcompat
- 以及散落在 TestWaitBarrier / TestSessionTriggerBarrier 里的"旧 state_meta 行
  向后兼容加载"用例。
"""

from __future__ import annotations

import json

from spirit.goals import GoalContract, GoalState, parse_contract


# ──────────────────────────────────────────────────────────────────────
# parse_contract — inline `field: value` 解析
# ──────────────────────────────────────────────────────────────────────


class TestParseContract:
    def test_plain_goal_no_contract(self):
        headline, contract = parse_contract("Migrate auth to JWT")
        assert headline == "Migrate auth to JWT"
        assert contract.is_empty()

    def test_incidental_colon_not_treated_as_field(self):
        # "Fix bug:" —— "fix bug" 不是已知别名，所以整行留在 headline，
        # 没有契约字段被填充（带冒号的普通自由目标不被破坏）。
        headline, contract = parse_contract("Fix bug: the parser drops trailing commas")
        assert headline == "Fix bug: the parser drops trailing commas"
        assert contract.is_empty()

    def test_inline_fields_parsed(self):
        text = (
            "Migrate auth to JWT\n"
            "verify: the auth test suite passes\n"
            "constraints: keep the /login response shape unchanged\n"
            "boundaries: only touch services/auth and its tests\n"
            "stop when: a schema change needs product sign-off"
        )
        headline, contract = parse_contract(text)
        assert headline == "Migrate auth to JWT"
        assert contract.verification == "the auth test suite passes"
        assert contract.constraints == "keep the /login response shape unchanged"
        assert contract.boundaries == "only touch services/auth and its tests"
        assert contract.stop_when == "a schema change needs product sign-off"
        assert not contract.is_empty()

    def test_alias_variants(self):
        _, c = parse_contract("Goal\nverified by: tests green\npreserve: public API")
        assert c.verification == "tests green"
        assert c.constraints == "public API"

    def test_multiple_lines_same_field_joined(self):
        _, c = parse_contract("G\nconstraints: a\nconstraints: b")
        assert c.constraints == "a b"

    def test_empty_text(self):
        headline, contract = parse_contract("")
        assert headline == ""
        assert contract.is_empty()


# ──────────────────────────────────────────────────────────────────────
# GoalContract 序列化 + 渲染
# ──────────────────────────────────────────────────────────────────────


class TestGoalContractSerialization:
    def test_roundtrip_with_contract(self):
        state = GoalState(
            goal="ship it",
            contract=GoalContract(
                verification="pytest passes",
                constraints="don't break the API",
            ),
        )
        restored = GoalState.from_json(state.to_json())
        assert restored.goal == "ship it"
        assert restored.contract.verification == "pytest passes"
        assert restored.contract.constraints == "don't break the API"
        assert restored.has_contract()

    def test_old_row_without_contract_loads_clean(self):
        # 本特性之前写入的 state_meta 行没有 "contract" 键。
        legacy = '{"goal": "old goal", "status": "active", "turns_used": 2}'
        state = GoalState.from_json(legacy)
        assert state.goal == "old goal"
        assert state.turns_used == 2
        assert state.contract.is_empty()
        assert not state.has_contract()

    def test_render_block_omits_empty_fields(self):
        block = GoalContract(outcome="X", verification="Y").render_block()
        assert "Outcome: X" in block
        assert "Verification: Y" in block
        assert "Constraints" not in block

    def test_is_empty_true_for_blank_contract(self):
        assert GoalContract().is_empty()
        assert GoalContract(outcome="   ").is_empty()

    def test_from_dict_handles_none_and_garbage(self):
        assert GoalContract.from_dict(None).is_empty()
        assert GoalContract.from_dict("not a dict").is_empty()

    def test_to_dict_roundtrip(self):
        c = GoalContract(outcome="O", verification="V", boundaries="B")
        assert GoalContract.from_dict(c.to_dict()) == c


# ──────────────────────────────────────────────────────────────────────
# GoalState 向后兼容加载（旧行缺字段）
# ──────────────────────────────────────────────────────────────────────


class TestGoalStateBackcompat:
    def test_old_state_meta_row_loads_without_subgoals(self):
        """subgoals 字段出现之前序列化的目标必须回填空列表，而非崩溃。"""
        legacy = json.dumps({
            "goal": "do a thing",
            "status": "active",
            "turns_used": 2,
            "max_turns": 20,
            "created_at": 1.0,
            "last_turn_at": 2.0,
            "consecutive_parse_failures": 0,
        })
        state = GoalState.from_json(legacy)
        assert state.goal == "do a thing"
        assert state.subgoals == []

    def test_subgoals_round_trip(self):
        state = GoalState(goal="g", subgoals=["a", "b", "c"])
        rt = GoalState.from_json(state.to_json())
        assert rt.subgoals == ["a", "b", "c"]

    def test_old_state_row_loads_without_barrier_fields(self):
        """向后兼容：barrier 存在之前写入的 state_meta 行加载时无屏障。"""
        legacy = json.dumps({
            "goal": "old goal",
            "status": "active",
            "turns_used": 2,
            "max_turns": 20,
        })
        st = GoalState.from_json(legacy)
        assert st.goal == "old goal"
        assert st.waiting_on_pid is None
        assert st.waiting_reason is None
        assert st.waiting_since == 0.0
        assert st.waiting_until == 0.0

    def test_old_state_loads_without_session_field(self):
        st = GoalState.from_json(json.dumps({
            "goal": "g", "status": "active", "turns_used": 0, "max_turns": 20,
        }))
        assert st.waiting_on_session is None

    def test_full_roundtrip_preserves_all_fields(self):
        st = GoalState(
            goal="full",
            status="paused",
            turns_used=3,
            max_turns=7,
            last_verdict="continue",
            last_reason="more",
            paused_reason="budget",
            consecutive_parse_failures=1,
            subgoals=["s1", "s2"],
            waiting_on_pid=4242,
            waiting_reason="ci",
            contract=GoalContract(verification="v"),
        )
        rt = GoalState.from_json(st.to_json())
        assert rt.goal == "full"
        assert rt.status == "paused"
        assert rt.turns_used == 3
        assert rt.max_turns == 7
        assert rt.paused_reason == "budget"
        assert rt.consecutive_parse_failures == 1
        assert rt.subgoals == ["s1", "s2"]
        assert rt.waiting_on_pid == 4242
        assert rt.waiting_reason == "ci"
        assert rt.contract.verification == "v"

    def test_render_subgoals_block_numbered(self):
        st = GoalState(goal="g", subgoals=["first", "second"])
        block = st.render_subgoals_block()
        assert "- 1. first" in block
        assert "- 2. second" in block

    def test_render_subgoals_block_empty(self):
        assert GoalState(goal="g").render_subgoals_block() == ""

    def test_subgoals_json_filters_blank_entries(self):
        # from_json 应剔除空白子目标项。
        raw = json.dumps({"goal": "g", "subgoals": ["a", "  ", "", "b"]})
        st = GoalState.from_json(raw)
        assert st.subgoals == ["a", "b"]
