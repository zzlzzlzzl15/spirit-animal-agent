"""tests/goals/test_judge.py — judge 裁决：解析、fail-open、模板切换、契约起草。

移植自 Hermes ``tests/hermes_cli/test_goals.py`` 的：
- TestParseJudgeResponse
- TestJudgeGoal（fail-open 语义）
- TestJudgeParseFailureAutoPause 里针对 _parse_judge_response / judge_goal 的部分
- TestJudgeGoalWithSubgoals（子目标模板切换）
- TestJudgeWithContract（契约模板）
- TestDraftContract
- TestContractAndBackgroundCompose
- TestSessionTriggerBarrier 里的 ``_parse_judge_response`` session 指令解析

架构适配：Hermes 用 ``patch("agent.auxiliary_client.call_llm")``；Spirit 用注入的
``llm_caller``（:class:`FakeCaller`），它记录 messages 并返回脚本化内容字符串。
"""

from __future__ import annotations

import pytest

from spirit.goals import GoalContract, draft_contract, judge_goal
from spirit.goals.judge import _parse_judge_response

from tests.goals.conftest import FakeCaller


# ──────────────────────────────────────────────────────────────────────
# _parse_judge_response
# ──────────────────────────────────────────────────────────────────────


class TestParseJudgeResponse:
    def test_clean_json_done(self):
        verdict, reason, _pf, wait = _parse_judge_response('{"done": true, "reason": "all good"}')
        assert verdict == "done"
        assert reason == "all good"
        assert wait is None

    def test_clean_json_continue(self):
        verdict, reason, _pf, wait = _parse_judge_response('{"done": false, "reason": "more work needed"}')
        assert verdict == "continue"
        assert reason == "more work needed"
        assert wait is None

    def test_json_in_markdown_fence(self):
        raw = '```json\n{"done": true, "reason": "done"}\n```'
        verdict, reason, _pf, _w = _parse_judge_response(raw)
        assert verdict == "done"
        assert "done" in reason

    def test_json_embedded_in_prose(self):
        """有些模型在吐 JSON 前先写推理——我们把它抽出来。"""
        raw = 'Looking at this... the agent says X. Verdict: {"done": false, "reason": "partial"}'
        verdict, reason, _pf, _w = _parse_judge_response(raw)
        assert verdict == "continue"
        assert reason == "partial"

    def test_string_done_values(self):
        for s in ("true", "yes", "done", "1"):
            verdict, _, _, _ = _parse_judge_response(f'{{"done": "{s}", "reason": "r"}}')
            assert verdict == "done"
        for s in ("false", "no", "not yet"):
            verdict, _, _, _ = _parse_judge_response(f'{{"done": "{s}", "reason": "r"}}')
            assert verdict == "continue"

    def test_new_verdict_shape(self):
        """显式 {"verdict": ...} 形状被尊重。"""
        v, _, _, _ = _parse_judge_response('{"verdict": "done", "reason": "r"}')
        assert v == "done"
        v, _, _, _ = _parse_judge_response('{"verdict": "continue", "reason": "r"}')
        assert v == "continue"

    def test_wait_verdict_with_pid(self):
        v, reason, pf, wait = _parse_judge_response(
            '{"verdict": "wait", "wait_on_pid": 4242, "reason": "CI running"}'
        )
        assert v == "wait"
        assert pf is False
        assert wait == {"pid": 4242}
        assert reason == "CI running"

    def test_wait_verdict_with_seconds(self):
        v, _, _, wait = _parse_judge_response(
            '{"verdict": "wait", "wait_for_seconds": 90, "reason": "rate limited"}'
        )
        assert v == "wait"
        assert wait == {"seconds": 90}

    def test_wait_verdict_without_target_downgrades_to_continue(self):
        """wait 裁决但无 pid/seconds → 没法泊在任何东西上 → continue。"""
        v, _, pf, wait = _parse_judge_response('{"verdict": "wait", "reason": "vague"}')
        assert v == "continue"
        assert wait is None
        assert pf is False

    def test_unknown_verdict_falls_back_to_continue(self):
        v, _, _, _ = _parse_judge_response('{"verdict": "maybe", "reason": "r"}')
        assert v == "continue"

    def test_malformed_json_fails_open(self):
        """非 JSON → continue + parse_failed，reason 带错误信息。"""
        verdict, reason, parse_failed, _w = _parse_judge_response("this is not json at all")
        assert verdict == "continue"
        assert parse_failed is True
        assert reason  # 非空

    def test_empty_response(self):
        verdict, reason, parse_failed, _w = _parse_judge_response("")
        assert verdict == "continue"
        assert parse_failed is True
        assert reason

    # --- 自动暂停护栏相关的解析断言 ---

    def test_parse_response_flags_empty_as_parse_failure(self):
        verdict, reason, parse_failed, _w = _parse_judge_response("")
        assert verdict == "continue"
        assert parse_failed is True
        assert "empty" in reason.lower()

    def test_parse_response_flags_non_json_as_parse_failure(self):
        verdict, reason, parse_failed, _w = _parse_judge_response(
            "Let me analyze whether the goal is fully satisfied based on the agent's response..."
        )
        assert verdict == "continue"
        assert parse_failed is True
        assert "not json" in reason.lower()

    def test_parse_response_clean_json_is_not_parse_failure(self):
        verdict, _, parse_failed, _w = _parse_judge_response('{"done": false, "reason": "more work"}')
        assert verdict == "continue"
        assert parse_failed is False

    def test_session_directive_parsed_from_judge(self):
        v, _, pf, wd = _parse_judge_response(
            '{"verdict": "wait", "wait_on_session": "proc_abc", "reason": "r"}'
        )
        assert v == "wait"
        assert pf is False
        assert wd == {"session_id": "proc_abc"}


# ──────────────────────────────────────────────────────────────────────
# judge_goal — fail-open 语义
# ──────────────────────────────────────────────────────────────────────


class TestJudgeGoal:
    def test_empty_goal_skipped(self):
        verdict, _, _, _wd = judge_goal("", "some response")
        assert verdict == "skipped"

    def test_empty_response_continues(self):
        verdict, _, _, _wd = judge_goal("ship the thing", "")
        assert verdict == "continue"

    def test_no_llm_caller_continues(self):
        """Fail-open：无 llm_caller 时必须返回 continue，而非 skipped/done。"""
        verdict, _, _, _wd = judge_goal("my goal", "my response", llm_caller=None)
        assert verdict == "continue"

    def test_api_error_continues(self):
        """judge 异常 → fail-open continue（不因 judge bug 卡死进度）。"""
        caller = FakeCaller(RuntimeError("boom"))
        verdict, reason, parse_failed, _wd = judge_goal("goal", "response", llm_caller=caller)
        assert verdict == "continue"
        assert "judge error" in reason.lower()
        # API/传输错误不计入 parse_failed（那是瞬时的）。
        assert parse_failed is False

    def test_judge_says_done(self):
        caller = FakeCaller('{"done": true, "reason": "achieved"}')
        verdict, reason, _, _wd = judge_goal("goal", "agent response", llm_caller=caller)
        assert verdict == "done"
        assert reason == "achieved"

    def test_judge_says_continue(self):
        caller = FakeCaller('{"done": false, "reason": "not yet"}')
        verdict, reason, _, _wd = judge_goal("goal", "agent response", llm_caller=caller)
        assert verdict == "continue"
        assert reason == "not yet"

    def test_empty_judge_reply_flagged_as_parse_failure(self):
        """端到端：judge 返回空内容 → parse_failed=True。"""
        caller = FakeCaller("")
        verdict, _, parse_failed, _wd = judge_goal("goal", "response", llm_caller=caller)
        assert verdict == "continue"
        assert parse_failed is True

    def test_judge_called_with_temperature_zero(self):
        """裁决要确定性 —— temperature 必须为 0。"""
        caller = FakeCaller('{"verdict": "done", "reason": "ok"}')
        judge_goal("goal", "response", llm_caller=caller)
        assert caller.calls[0]["temperature"] == 0


# ──────────────────────────────────────────────────────────────────────
# judge_goal — 子目标 / 契约 / 后台进程模板切换
# ──────────────────────────────────────────────────────────────────────


class TestJudgeGoalWithSubgoals:
    def test_judge_uses_subgoals_template_when_provided(self):
        """subgoals 非空时 judge_goal 切换模板——用 FakeCaller 捕获 prompt。"""
        caller = FakeCaller('{"done": true, "reason": "all done"}')
        verdict, reason, parse_failed, _wd = judge_goal(
            "ship the feature",
            "ok shipped",
            llm_caller=caller,
            subgoals=["write tests", "update docs"],
        )
        user_msg = caller.last_user_msg
        assert "Additional criteria" in user_msg
        assert "1. write tests" in user_msg
        assert "2. update docs" in user_msg
        assert "every additional criterion" in user_msg
        assert verdict == "done"

    def test_judge_uses_original_template_when_no_subgoals(self):
        caller = FakeCaller('{"done": true, "reason": "ok"}')
        judge_goal("ship it", "done", llm_caller=caller, subgoals=None)
        user_msg = caller.last_user_msg
        assert "Additional criteria" not in user_msg
        assert "ship it" in user_msg


class TestJudgeWithContract:
    def test_judge_uses_contract_template(self):
        caller = FakeCaller('{"done": false, "reason": "more"}')
        judge_goal(
            "ship it", "I think it's done",
            llm_caller=caller,
            contract=GoalContract(verification="pytest -q passes"),
        )
        user_msg = caller.last_user_msg
        assert "completion contract" in user_msg.lower()
        assert "pytest -q passes" in user_msg
        assert "concrete evidence" in user_msg

    def test_contract_plus_subgoals_combine(self):
        caller = FakeCaller('{"done": false, "reason": "more"}')
        judge_goal(
            "ship it", "done",
            llm_caller=caller,
            subgoals=["write changelog"],
            contract=GoalContract(verification="pytest passes"),
        )
        user_msg = caller.last_user_msg
        assert "pytest passes" in user_msg
        assert "write changelog" in user_msg


class TestContractAndBackgroundCompose:
    """带契约的目标阻塞在后台进程上时，必须同时把契约块和后台进程列表呈现给
    judge，让它能返回 done（证据满足）或 wait（泊在轮询器上）。"""

    def test_judge_prompt_carries_contract_and_background(self):
        caller = FakeCaller(
            '{"verdict": "wait", "wait_on_pid": 4242, "reason": "CI still running"}'
        )
        bg = [{
            "session_id": "ci-watch", "pid": 4242, "status": "running",
            "command": "wait_for_pr_green.sh 50501", "trigger": "exit",
        }]
        verdict, reason, parse_failed, wait_directive = judge_goal(
            "ship the PR",
            "I pushed and started the CI watcher; waiting on it now.",
            llm_caller=caller,
            contract=GoalContract(verification="PR CI goes green"),
            background_processes=bg,
        )
        user_msg = caller.last_user_msg
        # 两个面都在同一个 prompt 里。
        assert "completion contract" in user_msg.lower()
        assert "PR CI goes green" in user_msg
        assert "Background processes" in user_msg
        assert "4242" in user_msg
        # judge 能在契约目标上返回 wait 裁决。
        assert verdict == "wait"
        assert wait_directive and wait_directive.get("pid") == 4242

    def test_contract_goal_can_still_complete_on_evidence(self):
        caller = FakeCaller('{"verdict": "done", "reason": "CI is green, evidence shown"}')
        bg = [{"session_id": "ci", "pid": 4242, "status": "running", "command": "ci", "trigger": "exit"}]
        verdict, reason, parse_failed, wait_directive = judge_goal(
            "ship the PR",
            "CI finished: 30 passed, 0 failed. Done.",
            llm_caller=caller,
            contract=GoalContract(verification="PR CI goes green"),
            background_processes=bg,
        )
        assert verdict == "done"
        assert wait_directive is None

    def test_exited_background_processes_not_rendered(self):
        """已退出的进程不值得展示 —— 只有 RUNNING 的进 prompt。"""
        caller = FakeCaller('{"verdict": "continue", "reason": "more"}')
        bg = [{"pid": 999, "status": "exited", "command": "done.sh"}]
        judge_goal("g", "r", llm_caller=caller, background_processes=bg)
        assert "Background processes" not in caller.last_user_msg


# ──────────────────────────────────────────────────────────────────────
# draft_contract — 大白话目标 → 结构化契约
# ──────────────────────────────────────────────────────────────────────


class TestDraftContract:
    def test_draft_parses_json(self):
        caller = FakeCaller(
            '{"outcome": "auth on JWT", "verification": "auth suite green", '
            '"constraints": "no API change", "boundaries": "services/auth", '
            '"stop_when": "schema change needed"}'
        )
        contract = draft_contract("Migrate auth to JWT", llm_caller=caller)
        assert contract is not None
        assert contract.outcome == "auth on JWT"
        assert contract.verification == "auth suite green"
        assert not contract.is_empty()

    def test_draft_returns_none_on_bad_json(self):
        caller = FakeCaller("I cannot produce JSON, sorry")
        assert draft_contract("anything", llm_caller=caller) is None

    def test_draft_returns_none_when_no_client(self):
        # 无 llm_caller（辅助模型不可用）→ None，回退裸自由目标。
        assert draft_contract("anything", llm_caller=None) is None

    def test_draft_returns_none_on_api_error(self):
        caller = FakeCaller(RuntimeError("No LLM provider configured"))
        assert draft_contract("anything", llm_caller=caller) is None

    def test_draft_empty_objective_returns_none(self):
        caller = FakeCaller('{"outcome": "x"}')
        assert draft_contract("   ", llm_caller=caller) is None
        assert caller.call_count == 0  # 空目标不该调模型

    def test_draft_uses_draft_system_prompt(self):
        caller = FakeCaller('{"outcome": "o", "verification": "v"}')
        draft_contract("build a thing", llm_caller=caller)
        assert "completion contract" in caller.last_system_msg.lower()
