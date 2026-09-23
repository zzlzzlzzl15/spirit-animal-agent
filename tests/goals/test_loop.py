"""tests/goals/test_loop.py — run_goal_loop 外层驱动。

移植自 Hermes ``tests/hermes_cli/test_kanban_goal_mode.py`` 的循环驱动逻辑
（``run_kanban_goal_loop``）。Spirit 没有 kanban，故通用化为 ``run_goal_loop``，
通过依赖注入 ``run_turn`` (str -> str) 与具体会话/任务执行解耦——测试里 ``run_turn``
是一个记录 prompt、返回预设字符串的假函数，无需真实 LLM 或 Agent。

覆盖：inactive / done-on-first / continue→done 多轮驱动 / wait 泊车 / 预算暂停 /
硬上限 budget / run_turn 异常 error / on_decision·log·background_processes_fn 回调。
"""

from __future__ import annotations

import pytest

from spirit.goals import run_goal_loop

from tests.goals.conftest import FakeCaller, make_manager


class _RecordingRunTurn:
    """记录每次收到的续传 prompt，并按脚本返回响应。"""

    def __init__(self, responses):
        self._responses = responses
        self.prompts: list = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        r = self._responses
        if isinstance(r, list):
            if not r:
                return ""
            return r.pop(0) if len(r) > 1 else r[0]
        return r

    @property
    def call_count(self) -> int:
        return len(self.prompts)


# ──────────────────────────────────────────────────────────────────────
# 基本出口
# ──────────────────────────────────────────────────────────────────────


class TestRunLoopOutcomes:
    def test_inactive_goal_returns_inactive(self):
        mgr = make_manager()  # 未 set 目标
        rt = _RecordingRunTurn("x")
        result = run_goal_loop(manager=mgr, run_turn=rt, first_response="hello")
        assert result["outcome"] == "inactive"
        assert rt.call_count == 0

    def test_done_on_first_evaluation(self):
        caller = FakeCaller('{"done": true, "reason": "already finished"}')
        mgr = make_manager(llm_caller=caller)
        mgr.set("goal")
        rt = _RecordingRunTurn("never used")
        result = run_goal_loop(manager=mgr, run_turn=rt, first_response="final answer")
        assert result["outcome"] == "done"
        assert result["turns_used"] == 1
        assert rt.call_count == 0  # 首轮即完成，无需续传

    def test_continue_then_done_drives_one_turn(self):
        caller = FakeCaller(['{"done": false, "reason": "more"}', '{"done": true, "reason": "finished"}'])
        mgr = make_manager(llm_caller=caller, max_turns=20)
        mgr.set("ship the thing")
        rt = _RecordingRunTurn("second response")
        result = run_goal_loop(manager=mgr, run_turn=rt, first_response="partial")
        assert result["outcome"] == "done"
        assert result["turns_used"] == 2
        assert rt.call_count == 1
        # run_turn 收到的是带目标的续传 prompt。
        assert "ship the thing" in rt.prompts[0]

    def test_multi_turn_drive_until_done(self):
        caller = FakeCaller([
            '{"done": false, "reason": "step 1"}',
            '{"done": false, "reason": "step 2"}',
            '{"done": true, "reason": "finished"}',
        ])
        mgr = make_manager(llm_caller=caller, max_turns=20)
        mgr.set("multi step goal")
        rt = _RecordingRunTurn(["r1", "r2"])
        result = run_goal_loop(manager=mgr, run_turn=rt, first_response="initial")
        assert result["outcome"] == "done"
        assert result["turns_used"] == 3
        assert rt.call_count == 2
        assert all("multi step goal" in p for p in rt.prompts)

    def test_wait_verdict_parks_loop(self):
        caller = FakeCaller('{"verdict": "wait", "wait_on_pid": 4242, "reason": "CI running"}')
        mgr = make_manager(llm_caller=caller)
        mgr.set("ship PR")
        rt = _RecordingRunTurn("x")
        result = run_goal_loop(manager=mgr, run_turn=rt, first_response="started CI")
        assert result["outcome"] == "waiting"
        assert rt.call_count == 0  # 泊车，不续传

    def test_budget_exhausted_pauses_loop(self):
        caller = FakeCaller('{"done": false, "reason": "more"}')
        mgr = make_manager(llm_caller=caller, max_turns=2)
        mgr.set("goal")
        rt = _RecordingRunTurn("r")
        result = run_goal_loop(manager=mgr, run_turn=rt, first_response="r0")
        assert result["outcome"] == "paused"
        assert mgr.state.status == "paused"

    def test_hard_cap_returns_budget(self):
        # manager 预算很大，但外层 hard_max_turns=1 → 首轮 continue 后即触顶。
        caller = FakeCaller('{"done": false, "reason": "more"}')
        mgr = make_manager(llm_caller=caller, max_turns=1000)
        mgr.set("goal")
        rt = _RecordingRunTurn("r")
        result = run_goal_loop(manager=mgr, run_turn=rt, first_response="r0", hard_max_turns=1)
        assert result["outcome"] == "budget"
        assert rt.call_count == 0  # 触顶发生在 run_turn 之前

    def test_run_turn_exception_returns_error(self):
        caller = FakeCaller('{"done": false, "reason": "more"}')
        mgr = make_manager(llm_caller=caller, max_turns=20)
        mgr.set("goal")

        def boom(prompt: str) -> str:
            raise RuntimeError("turn exploded")

        result = run_goal_loop(manager=mgr, run_turn=boom, first_response="r0")
        assert result["outcome"] == "error"
        assert "run_turn error" in result["reason"]


# ──────────────────────────────────────────────────────────────────────
# 回调与后台进程
# ──────────────────────────────────────────────────────────────────────


class TestRunLoopCallbacks:
    def test_on_decision_called_each_turn(self):
        decisions: list = []
        caller = FakeCaller(['{"done": false, "reason": "more"}', '{"done": true, "reason": "ok"}'])
        mgr = make_manager(llm_caller=caller, max_turns=20)
        mgr.set("goal")
        rt = _RecordingRunTurn("r")
        run_goal_loop(manager=mgr, run_turn=rt, first_response="r0", on_decision=decisions.append)
        assert len(decisions) == 2
        assert decisions[0]["verdict"] == "continue"
        assert decisions[-1]["verdict"] == "done"

    def test_on_decision_exception_does_not_break_loop(self):
        caller = FakeCaller('{"done": true, "reason": "ok"}')
        mgr = make_manager(llm_caller=caller)
        mgr.set("goal")

        def bad_cb(decision):
            raise ValueError("callback bug")

        result = run_goal_loop(manager=mgr, run_turn=lambda p: "", first_response="r", on_decision=bad_cb)
        assert result["outcome"] == "done"

    def test_log_callback_receives_verdict_lines(self):
        logs: list = []
        caller = FakeCaller('{"done": true, "reason": "ok"}')
        mgr = make_manager(llm_caller=caller)
        mgr.set("goal")
        run_goal_loop(manager=mgr, run_turn=lambda p: "", first_response="r", log=logs.append)
        assert any("verdict=done" in m for m in logs)

    def test_background_processes_reach_judge_prompt(self):
        def bg_fn():
            return [{"pid": 4242, "status": "running", "command": "ci.sh"}]

        caller = FakeCaller('{"done": true, "reason": "ok"}')
        mgr = make_manager(llm_caller=caller)
        mgr.set("goal")
        result = run_goal_loop(
            manager=mgr, run_turn=lambda p: "", first_response="started",
            background_processes_fn=bg_fn,
        )
        assert result["outcome"] == "done"
        assert "Background processes" in caller.last_user_msg
        assert "4242" in caller.last_user_msg

    def test_background_processes_fn_exception_is_tolerated(self):
        def bad_bg():
            raise RuntimeError("registry down")

        caller = FakeCaller('{"done": true, "reason": "ok"}')
        mgr = make_manager(llm_caller=caller)
        mgr.set("goal")
        result = run_goal_loop(
            manager=mgr, run_turn=lambda p: "", first_response="r",
            background_processes_fn=bad_bg,
        )
        # 后台快照失败不影响裁决。
        assert result["outcome"] == "done"

    def test_first_response_flows_to_first_judgement(self):
        caller = FakeCaller('{"done": true, "reason": "ok"}')
        mgr = make_manager(llm_caller=caller)
        mgr.set("goal")
        run_goal_loop(manager=mgr, run_turn=lambda p: "", first_response="UNIQUE_MARKER_TEXT")
        # judge 看到的首轮响应即 first_response。
        assert "UNIQUE_MARKER_TEXT" in caller.last_user_msg
