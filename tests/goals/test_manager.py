"""tests/goals/test_manager.py — GoalManager 生命周期 + evaluate_after_turn 决策。

移植自 Hermes ``tests/hermes_cli/test_goals.py`` 的：
- TestGoalManager（set/pause/resume/clear/mark_done 生命周期 + 持久化）
- TestGoalManagerEvaluate（evaluate_after_turn 各 verdict 分支）
- TestGoalManagerBudget（turn 预算耗尽自动暂停）
- TestJudgeParseFailureAutoPause（连续解析失败自动暂停护栏）
- TestSubgoals（/subgoal 用户控制）
- TestWaitBarrier（pid/session/time 等待屏障 + 惰性自动清除）
- TestContinuationPrompt（续传 prompt 契约优先）
- TestStatusLine（一行状态渲染）

架构适配：Hermes 用 ``patch("...judge_goal")`` mock 裁决；Spirit 用注入的
:class:`FakeCaller` 走完整 ``judge_goal``（端到端），或在需要精确控制裁决元组时
``patch("spirit.goals.manager.judge_goal")``。wait 屏障的 ``_pid_alive`` /
``_session_waiting`` 用 ``patch("spirit.goals.manager.<name>")`` 精确控制存活性。
"""

from __future__ import annotations

import os
import time
from unittest.mock import patch

import pytest

from spirit.goals import GoalContract, GoalManager, InMemoryGoalStore
from spirit.goals.goal_state import DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES

from tests.goals.conftest import FakeCaller, make_manager


# ──────────────────────────────────────────────────────────────────────
# 生命周期
# ──────────────────────────────────────────────────────────────────────


class TestGoalManagerLifecycle:
    def test_no_goal_initially(self):
        mgr = make_manager()
        assert mgr.state is None
        assert mgr.is_active() is False
        assert mgr.has_goal() is False

    def test_set_creates_active_goal(self):
        mgr = make_manager()
        state = mgr.set("my goal")
        assert state.goal == "my goal"
        assert state.status == "active"
        assert state.turns_used == 0
        assert mgr.is_active() is True
        assert mgr.has_goal() is True

    def test_set_strips_whitespace(self):
        mgr = make_manager()
        mgr.set("  padded goal  ")
        assert mgr.state.goal == "padded goal"

    def test_set_empty_raises(self):
        mgr = make_manager()
        with pytest.raises(ValueError):
            mgr.set("   ")

    def test_set_custom_max_turns(self):
        mgr = make_manager()
        mgr.set("goal", max_turns=5)
        assert mgr.state.max_turns == 5

    def test_set_uses_manager_default_max_turns(self):
        mgr = make_manager(max_turns=9)
        mgr.set("goal")
        assert mgr.state.max_turns == 9

    def test_set_with_contract(self):
        mgr = make_manager()
        c = GoalContract(outcome="auth on JWT", verification="suite green")
        mgr.set("migrate auth", contract=c)
        assert mgr.has_contract() is True
        assert mgr.state.contract.outcome == "auth on JWT"

    def test_set_contract_after_set(self):
        mgr = make_manager()
        mgr.set("goal")
        assert mgr.has_contract() is False
        mgr.set_contract(GoalContract(verification="tests pass"))
        assert mgr.has_contract() is True
        assert mgr.state.contract.verification == "tests pass"

    def test_set_contract_no_goal_returns_none(self):
        mgr = make_manager()
        assert mgr.set_contract(GoalContract(verification="x")) is None

    def test_pause(self):
        mgr = make_manager()
        mgr.set("goal")
        mgr.pause(reason="brb")
        assert mgr.state.status == "paused"
        assert mgr.state.paused_reason == "brb"
        assert mgr.is_active() is False
        assert mgr.has_goal() is True  # paused 仍算 has_goal

    def test_pause_no_goal_returns_none(self):
        mgr = make_manager()
        assert mgr.pause() is None

    def test_pause_clears_barriers(self):
        mgr = make_manager()
        mgr.set("goal")
        mgr.wait_for_seconds(60)
        mgr.pause()
        assert mgr.state.waiting_until == 0.0

    def test_resume(self):
        mgr = make_manager()
        mgr.set("goal")
        mgr.pause()
        mgr.resume()
        assert mgr.state.status == "active"
        assert mgr.state.paused_reason is None

    def test_resume_resets_budget_by_default(self):
        mgr = make_manager()
        mgr.set("goal")
        mgr.state.turns_used = 5
        mgr.pause()
        mgr.resume()
        assert mgr.state.turns_used == 0

    def test_resume_keeps_budget_when_asked(self):
        mgr = make_manager()
        mgr.set("goal")
        mgr.state.turns_used = 5
        mgr.pause()
        mgr.resume(reset_budget=False)
        assert mgr.state.turns_used == 5

    def test_resume_clears_stale_barrier(self):
        mgr = make_manager()
        mgr.set("goal")
        mgr.wait_for_seconds(60)
        mgr.pause()
        mgr.resume()
        assert mgr.state.waiting_until == 0.0

    def test_resume_no_goal_returns_none(self):
        mgr = make_manager()
        assert mgr.resume() is None

    def test_clear(self):
        mgr = make_manager()
        mgr.set("goal")
        mgr.clear()
        assert mgr.state is None
        assert mgr.has_goal() is False
        assert mgr.is_active() is False

    def test_clear_no_goal_is_noop(self):
        mgr = make_manager()
        mgr.clear()  # 不抛
        assert mgr.state is None

    def test_mark_done(self):
        mgr = make_manager()
        mgr.set("goal")
        mgr.mark_done("achieved")
        assert mgr.state.status == "done"
        assert mgr.state.last_verdict == "done"
        assert mgr.state.last_reason == "achieved"

    def test_set_persists_and_reloads(self):
        store = InMemoryGoalStore()
        mgr = GoalManager("sid", store=store)
        mgr.set("persist me", max_turns=7)
        # 新 manager 从同一 store 重新加载（模拟 /resume）
        mgr2 = GoalManager("sid", store=store)
        assert mgr2.has_goal() is True
        assert mgr2.state.goal == "persist me"
        assert mgr2.state.max_turns == 7

    def test_clear_persists_as_cleared(self):
        store = InMemoryGoalStore()
        mgr = GoalManager("sid", store=store)
        mgr.set("goal")
        mgr.clear()
        mgr2 = GoalManager("sid", store=store)
        assert mgr2.has_goal() is False


# ──────────────────────────────────────────────────────────────────────
# status_line
# ──────────────────────────────────────────────────────────────────────


class TestStatusLine:
    def test_no_goal(self):
        mgr = make_manager()
        assert "No active goal" in mgr.status_line()

    def test_active(self):
        mgr = make_manager()
        mgr.set("ship it")
        line = mgr.status_line()
        assert "ship it" in line
        assert "active" in line

    def test_paused(self):
        mgr = make_manager()
        mgr.set("ship it")
        mgr.pause(reason="user-paused")
        assert "paused" in mgr.status_line()

    def test_done(self):
        mgr = make_manager()
        mgr.set("ship it")
        mgr.mark_done("done reason")
        assert "done" in mgr.status_line().lower()

    def test_parked_on_time(self):
        mgr = make_manager()
        mgr.set("ship it")
        mgr.wait_for_seconds(60, reason="cooldown")
        line = mgr.status_line()
        assert "parked" in line.lower()

    def test_subgoal_count_shown(self):
        mgr = make_manager()
        mgr.set("ship it")
        mgr.add_subgoal("a")
        mgr.add_subgoal("b")
        assert "2 subgoals" in mgr.status_line()

    def test_contract_flag_shown(self):
        mgr = make_manager()
        mgr.set("ship it", contract=GoalContract(verification="tests pass"))
        assert "contract" in mgr.status_line()


# ──────────────────────────────────────────────────────────────────────
# evaluate_after_turn — 各裁决分支
# ──────────────────────────────────────────────────────────────────────


class TestEvaluateAfterTurn:
    def test_inactive_returns_inactive(self):
        mgr = make_manager()
        d = mgr.evaluate_after_turn("hello")
        assert d["verdict"] == "inactive"
        assert d["should_continue"] is False
        assert d["continuation_prompt"] is None

    def test_paused_goal_is_inactive_for_evaluation(self):
        mgr = make_manager()
        mgr.set("goal")
        mgr.pause()
        d = mgr.evaluate_after_turn("hello")
        assert d["verdict"] == "inactive"
        assert d["should_continue"] is False

    def test_done_verdict_marks_done(self):
        caller = FakeCaller('{"done": true, "reason": "shipped"}')
        mgr = make_manager(llm_caller=caller)
        mgr.set("ship it")
        d = mgr.evaluate_after_turn("I shipped it")
        assert d["verdict"] == "done"
        assert d["status"] == "done"
        assert d["should_continue"] is False
        assert mgr.state.status == "done"

    def test_continue_verdict_gives_prompt_and_increments(self):
        caller = FakeCaller('{"done": false, "reason": "more work"}')
        mgr = make_manager(llm_caller=caller)
        mgr.set("ship it")
        d = mgr.evaluate_after_turn("partial progress")
        assert d["verdict"] == "continue"
        assert d["should_continue"] is True
        assert d["continuation_prompt"]
        assert "ship it" in d["continuation_prompt"]
        assert mgr.state.turns_used == 1
        assert mgr.state.last_verdict == "continue"

    def test_empty_response_skips_judge_call_but_burns_turn(self):
        # judge_goal 对空响应直接 continue，不调 llm_caller；但轮次仍计入预算。
        caller = FakeCaller('{"done": true, "reason": "x"}')
        mgr = make_manager(llm_caller=caller)
        mgr.set("goal")
        d = mgr.evaluate_after_turn("")
        assert d["verdict"] == "continue"
        assert caller.call_count == 0
        assert mgr.state.turns_used == 1

    def test_wait_verdict_sets_pid_barrier(self):
        caller = FakeCaller('{"verdict": "wait", "wait_on_pid": 4242, "reason": "CI running"}')
        mgr = make_manager(llm_caller=caller)
        mgr.set("ship PR")
        d = mgr.evaluate_after_turn("started CI")
        assert d["verdict"] == "wait"
        assert d["should_continue"] is False
        assert mgr.state.waiting_on_pid == 4242
        assert mgr.state.turns_used == 1  # wait 轮计入（judge 调了）

    def test_wait_verdict_sets_session_barrier(self):
        caller = FakeCaller('{"verdict": "wait", "wait_on_session": "proc_x", "reason": "watcher"}')
        mgr = make_manager(llm_caller=caller)
        mgr.set("goal")
        d = mgr.evaluate_after_turn("started watcher")
        assert d["verdict"] == "wait"
        assert mgr.state.waiting_on_session == "proc_x"

    def test_wait_verdict_sets_time_barrier(self):
        caller = FakeCaller('{"verdict": "wait", "wait_for_seconds": 90, "reason": "rate limited"}')
        mgr = make_manager(llm_caller=caller)
        mgr.set("goal")
        d = mgr.evaluate_after_turn("hit rate limit")
        assert d["verdict"] == "wait"
        assert mgr.state.waiting_until > 0

    def test_waiting_barrier_short_circuits_judge(self):
        # 已泊车的目标再评估：不调 judge、不烧 turn，直接 verdict="waiting"。
        caller = FakeCaller('{"done": true, "reason": "x"}')
        mgr = make_manager(llm_caller=caller)
        mgr.set("goal")
        mgr.wait_for_seconds(60)
        d = mgr.evaluate_after_turn("anything")
        assert d["verdict"] == "waiting"
        assert d["should_continue"] is False
        assert caller.call_count == 0
        assert mgr.state.turns_used == 0

    def test_user_initiated_false_still_burns_turn(self):
        caller = FakeCaller('{"done": false, "reason": "more"}')
        mgr = make_manager(llm_caller=caller)
        mgr.set("goal")
        mgr.evaluate_after_turn("r", user_initiated=False)
        assert mgr.state.turns_used == 1

    def test_patched_judge_goal_controls_verdict(self):
        # 精确控制裁决元组（对齐 Hermes patch judge_goal 的测试思路）。
        mgr = make_manager()
        mgr.set("goal")
        with patch(
            "spirit.goals.manager.judge_goal",
            return_value=("done", "evidence satisfied", False, None),
        ):
            d = mgr.evaluate_after_turn("final answer")
        assert d["verdict"] == "done"
        assert d["status"] == "done"


# ──────────────────────────────────────────────────────────────────────
# turn 预算
# ──────────────────────────────────────────────────────────────────────


class TestBudget:
    def test_budget_exhausted_pauses(self):
        caller = FakeCaller('{"done": false, "reason": "more"}')
        mgr = make_manager(llm_caller=caller, max_turns=2)
        mgr.set("goal")
        mgr.evaluate_after_turn("r1")  # turns_used=1 < 2 → 仍 active
        assert mgr.state.status == "active"
        d = mgr.evaluate_after_turn("r2")  # turns_used=2 >= 2 → paused
        assert d["status"] == "paused"
        assert d["should_continue"] is False
        assert mgr.state.status == "paused"
        assert "budget" in (mgr.state.paused_reason or "").lower()

    def test_budget_message_mentions_resume(self):
        caller = FakeCaller('{"done": false, "reason": "more"}')
        mgr = make_manager(llm_caller=caller, max_turns=1)
        mgr.set("goal")
        d = mgr.evaluate_after_turn("r")
        assert d["status"] == "paused"
        assert "resume" in d["message"].lower()

    def test_done_before_budget_wins(self):
        # 最后一轮 judge 说 done → done 优先于预算暂停。
        caller = FakeCaller('{"done": true, "reason": "finished"}')
        mgr = make_manager(llm_caller=caller, max_turns=1)
        mgr.set("goal")
        d = mgr.evaluate_after_turn("r")
        assert d["status"] == "done"


# ──────────────────────────────────────────────────────────────────────
# 连续解析失败自动暂停护栏
# ──────────────────────────────────────────────────────────────────────


class TestParseFailureAutoPause:
    def test_consecutive_parse_failures_pause(self):
        caller = FakeCaller("this is prose, not json at all")
        mgr = make_manager(llm_caller=caller, max_turns=50)
        mgr.set("goal")
        for _ in range(DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES - 1):
            d = mgr.evaluate_after_turn("r")
            assert d["status"] == "active"
        d = mgr.evaluate_after_turn("r")  # 第 N 次 → 自动暂停
        assert d["status"] == "paused"
        assert mgr.state.consecutive_parse_failures == DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES
        assert "judge model" in d["message"].lower()

    def test_good_reply_resets_parse_failures(self):
        caller = FakeCaller(["prose not json", '{"verdict": "continue", "reason": "ok"}'])
        mgr = make_manager(llm_caller=caller, max_turns=50)
        mgr.set("goal")
        mgr.evaluate_after_turn("r")  # 解析失败 → consecutive=1
        assert mgr.state.consecutive_parse_failures == 1
        mgr.evaluate_after_turn("r")  # 好回复 → 归零
        assert mgr.state.consecutive_parse_failures == 0
        assert mgr.state.status == "active"

    def test_api_error_not_counted_as_parse_failure(self):
        # API/传输错误 fail-open，parse_failed=False → 不触发自动暂停。
        caller = FakeCaller(RuntimeError("network down"))
        mgr = make_manager(llm_caller=caller, max_turns=50)
        mgr.set("goal")
        for _ in range(5):
            mgr.evaluate_after_turn("r")
        assert mgr.state.consecutive_parse_failures == 0
        assert mgr.state.status == "active"


# ──────────────────────────────────────────────────────────────────────
# 子目标（/subgoal）
# ──────────────────────────────────────────────────────────────────────


class TestSubgoals:
    def test_add_subgoal(self):
        mgr = make_manager()
        mgr.set("goal")
        text = mgr.add_subgoal("write tests")
        assert text == "write tests"
        assert mgr.state.subgoals == ["write tests"]

    def test_add_subgoal_strips(self):
        mgr = make_manager()
        mgr.set("goal")
        mgr.add_subgoal("  padded  ")
        assert mgr.state.subgoals == ["padded"]

    def test_add_subgoal_no_goal_raises(self):
        mgr = make_manager()
        with pytest.raises(RuntimeError):
            mgr.add_subgoal("x")

    def test_add_empty_subgoal_raises(self):
        mgr = make_manager()
        mgr.set("goal")
        with pytest.raises(ValueError):
            mgr.add_subgoal("   ")

    def test_remove_subgoal_1based(self):
        mgr = make_manager()
        mgr.set("goal")
        mgr.add_subgoal("a")
        mgr.add_subgoal("b")
        removed = mgr.remove_subgoal(1)
        assert removed == "a"
        assert mgr.state.subgoals == ["b"]

    def test_remove_out_of_range_raises(self):
        mgr = make_manager()
        mgr.set("goal")
        mgr.add_subgoal("a")
        with pytest.raises(IndexError):
            mgr.remove_subgoal(5)

    def test_clear_subgoals_returns_prev_count(self):
        mgr = make_manager()
        mgr.set("goal")
        mgr.add_subgoal("a")
        mgr.add_subgoal("b")
        prev = mgr.clear_subgoals()
        assert prev == 2
        assert mgr.state.subgoals == []

    def test_render_subgoals_numbered(self):
        mgr = make_manager()
        mgr.set("goal")
        mgr.add_subgoal("a")
        mgr.add_subgoal("b")
        block = mgr.render_subgoals()
        assert "1. a" in block
        assert "2. b" in block

    def test_render_subgoals_empty_hint(self):
        mgr = make_manager()
        mgr.set("goal")
        assert "no subgoals" in mgr.render_subgoals().lower()

    def test_subgoals_persist(self):
        store = InMemoryGoalStore()
        mgr = GoalManager("sid", store=store)
        mgr.set("goal")
        mgr.add_subgoal("criterion")
        mgr2 = GoalManager("sid", store=store)
        assert mgr2.state.subgoals == ["criterion"]


# ──────────────────────────────────────────────────────────────────────
# wait 屏障（pid / session / time）
# ──────────────────────────────────────────────────────────────────────


class TestWaitBarrier:
    def test_wait_on_pid_alive(self):
        mgr = make_manager()
        mgr.set("goal")
        with patch("spirit.goals.manager._pid_alive", return_value=True):
            mgr.wait_on(4242, reason="CI")
            assert mgr.is_waiting() is True
            assert mgr.state.waiting_on_pid == 4242

    def test_wait_on_pid_dead_autoclears(self):
        mgr = make_manager()
        mgr.set("goal")
        mgr.wait_on(4242)
        with patch("spirit.goals.manager._pid_alive", return_value=False):
            assert mgr.is_waiting() is False  # 惰性自动清除
            assert mgr.state.waiting_on_pid is None

    def test_wait_on_real_alive_pid(self):
        # 当前进程必然存活 —— 验证真实 _pid_alive 集成。
        mgr = make_manager()
        mgr.set("goal")
        mgr.wait_on(os.getpid())
        assert mgr.is_waiting() is True

    def test_wait_on_session_barrier(self):
        mgr = make_manager()
        mgr.set("goal")
        with patch("spirit.goals.manager._session_waiting", return_value=True):
            mgr.wait_on_session("proc_abc", reason="watcher")
            assert mgr.is_waiting() is True
            assert mgr.state.waiting_on_session == "proc_abc"

    def test_wait_on_session_autoclears_when_not_waiting(self):
        # Spirit _session_waiting 默认返回 False → 会话屏障立即清除。
        mgr = make_manager()
        mgr.set("goal")
        mgr.wait_on_session("proc_abc")
        assert mgr.is_waiting() is False
        assert mgr.state.waiting_on_session is None

    def test_wait_for_seconds(self):
        mgr = make_manager()
        mgr.set("goal")
        mgr.wait_for_seconds(60, reason="rate limited")
        assert mgr.is_waiting() is True
        assert mgr.state.waiting_until > time.time()

    def test_wait_time_expired_autoclears(self):
        mgr = make_manager()
        mgr.set("goal")
        mgr.wait_for_seconds(60)
        mgr.state.waiting_until = time.time() - 1  # 手动过期
        assert mgr.is_waiting() is False
        assert mgr.state.waiting_until == 0.0

    def test_stop_waiting_clears(self):
        mgr = make_manager()
        mgr.set("goal")
        mgr.wait_for_seconds(60)
        assert mgr.stop_waiting() is True
        assert mgr.is_waiting() is False

    def test_stop_waiting_no_barrier_returns_false(self):
        mgr = make_manager()
        mgr.set("goal")
        assert mgr.stop_waiting() is False

    def test_wait_requires_active_goal(self):
        mgr = make_manager()
        with pytest.raises(RuntimeError):
            mgr.wait_on(123)

    def test_wait_on_invalid_pid_raises(self):
        mgr = make_manager()
        mgr.set("goal")
        with pytest.raises(ValueError):
            mgr.wait_on(0)

    def test_wait_on_session_empty_raises(self):
        mgr = make_manager()
        mgr.set("goal")
        with pytest.raises(ValueError):
            mgr.wait_on_session("   ")

    def test_wait_for_seconds_invalid_raises(self):
        mgr = make_manager()
        mgr.set("goal")
        with pytest.raises(ValueError):
            mgr.wait_for_seconds(0)

    def test_setting_one_barrier_clears_others(self):
        mgr = make_manager()
        mgr.set("goal")
        mgr.wait_for_seconds(60)
        with patch("spirit.goals.manager._pid_alive", return_value=True):
            mgr.wait_on(4242)
        assert mgr.state.waiting_until == 0.0
        assert mgr.state.waiting_on_pid == 4242


# ──────────────────────────────────────────────────────────────────────
# 续传 prompt（契约优先）
# ──────────────────────────────────────────────────────────────────────


class TestContinuationPrompt:
    def test_plain_prompt_contains_goal(self):
        mgr = make_manager()
        mgr.set("ship the thing")
        p = mgr.next_continuation_prompt()
        assert "ship the thing" in p

    def test_subgoals_prompt(self):
        mgr = make_manager()
        mgr.set("goal")
        mgr.add_subgoal("criterion A")
        p = mgr.next_continuation_prompt()
        assert "criterion A" in p

    def test_contract_priority_over_subgoals(self):
        mgr = make_manager()
        mgr.set("goal", contract=GoalContract(verification="pytest passes"))
        mgr.add_subgoal("extra B")
        p = mgr.next_continuation_prompt()
        # 契约优先：契约块 + 子目标作为 extra criterion 折进去
        assert "pytest passes" in p
        assert "extra B" in p

    def test_contract_only_prompt(self):
        mgr = make_manager()
        mgr.set("goal", contract=GoalContract(outcome="done state", verification="tests green"))
        p = mgr.next_continuation_prompt()
        assert "tests green" in p

    def test_none_when_inactive(self):
        mgr = make_manager()
        assert mgr.next_continuation_prompt() is None

    def test_none_when_paused(self):
        mgr = make_manager()
        mgr.set("goal")
        mgr.pause()
        assert mgr.next_continuation_prompt() is None

    def test_render_contract_no_goal(self):
        mgr = make_manager()
        assert "no active goal" in mgr.render_contract().lower()

    def test_render_contract_no_contract(self):
        mgr = make_manager()
        mgr.set("goal")
        assert "no completion contract" in mgr.render_contract().lower()

    def test_render_contract_with_contract(self):
        mgr = make_manager()
        mgr.set("goal", contract=GoalContract(verification="pytest -q passes"))
        assert "pytest -q passes" in mgr.render_contract()
