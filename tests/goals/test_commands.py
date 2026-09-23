"""tests/goals/test_commands.py — /goal · /subgoal 命令分发层。

移植自 Hermes ``tests/tui_gateway/test_goal_command.py`` 与
``hermes_cli/cli_commands_mixin.py`` 的 ``_handle_goal_command`` /
``_handle_subgoal_command`` 行为。Spirit 把命令解析剥离成传输无关的
:mod:`spirit.goals.commands`，返回结构化 dict，故无需真实终端/事件循环即可单测。

``run_goal_turn_loop`` 部分对齐 Hermes ``tests/cli/test_cli_goal_interrupt.py``：
用假 agent（注入 :class:`FakeCaller` 裁决 + 脚本化 ``chat``）验证 Ralph Loop 的
"首轮触发 → judge 裁决 → 续传" 全链路，无需真实 LLM。
"""

from __future__ import annotations

import pytest

from spirit.goals import GoalContract, GoalManager, InMemoryGoalStore
from spirit.goals.commands import (
    get_goal_manager,
    handle_goal_command,
    handle_subgoal_command,
    run_goal_turn_loop,
)

from tests.goals.conftest import FakeCaller, make_manager


class FakeAgent:
    """带 ``goal_manager`` property 和脚本化 ``chat`` 的假 SpiritAgent。"""

    def __init__(self, manager=None, chat_responses=None):
        self._manager = manager
        self._chat_responses = list(chat_responses) if chat_responses else ["ok"]
        self.chat_calls: list = []

    @property
    def goal_manager(self):
        return self._manager

    def chat(self, message):
        self.chat_calls.append(message)
        resp = (
            self._chat_responses.pop(0)
            if len(self._chat_responses) > 1
            else self._chat_responses[0]
        )
        return {"response": resp}


class BrokenAgent:
    """goal_manager 访问抛错 —— 验证 get_goal_manager 防御性降级。"""

    @property
    def goal_manager(self):
        raise RuntimeError("boom")


def _agent_with_goal(goal="ship it", *, llm_caller=None, **set_kwargs):
    """构造一个已 set 好目标的 FakeAgent。"""
    mgr = make_manager(llm_caller=llm_caller)
    mgr.set(goal, **set_kwargs)
    return FakeAgent(manager=mgr), mgr


# ──────────────────────────────────────────────────────────────────────
# get_goal_manager
# ──────────────────────────────────────────────────────────────────────


class TestGetGoalManager:
    def test_none_agent_returns_none(self):
        assert get_goal_manager(None) is None

    def test_returns_manager(self):
        mgr = make_manager()
        agent = FakeAgent(manager=mgr)
        assert get_goal_manager(agent) is mgr

    def test_broken_agent_returns_none(self):
        assert get_goal_manager(BrokenAgent()) is None


# ──────────────────────────────────────────────────────────────────────
# /goal — 不可用 & 状态
# ──────────────────────────────────────────────────────────────────────


class TestGoalCommandUnavailableAndStatus:
    def test_no_agent_unavailable(self):
        r = handle_goal_command(None, "ship it")
        assert r["ok"] is False
        assert r["action"] == "unavailable"

    def test_broken_agent_unavailable(self):
        r = handle_goal_command(BrokenAgent(), "status")
        assert r["ok"] is False
        assert r["action"] == "unavailable"

    def test_bare_goal_shows_status(self):
        agent = FakeAgent(manager=make_manager())
        r = handle_goal_command(agent, "")
        assert r["ok"] is True
        assert r["action"] == "status"
        assert "No active goal" in r["message"]

    def test_status_subcommand(self):
        agent, _ = _agent_with_goal("ship it")
        r = handle_goal_command(agent, "status")
        assert r["action"] == "status"
        assert "ship it" in r["status_line"]

    def test_status_case_insensitive(self):
        agent, _ = _agent_with_goal("ship it")
        assert handle_goal_command(agent, "STATUS")["action"] == "status"

    def test_show_includes_contract(self):
        agent, _ = _agent_with_goal(
            "ship it", contract=GoalContract(verification="pytest passes")
        )
        r = handle_goal_command(agent, "show")
        assert r["action"] == "show"
        assert any("pytest passes" in ln for ln in r["lines"])

    def test_show_without_contract(self):
        agent, _ = _agent_with_goal("ship it")
        r = handle_goal_command(agent, "show")
        assert r["action"] == "show"
        assert any("no completion contract" in ln.lower() for ln in r["lines"])


# ──────────────────────────────────────────────────────────────────────
# /goal — 设目标（纯文本 / inline 契约）
# ──────────────────────────────────────────────────────────────────────


class TestGoalCommandSet:
    def test_set_plain_text(self):
        agent = FakeAgent(manager=make_manager())
        r = handle_goal_command(agent, "ship the feature")
        assert r["ok"] is True
        assert r["action"] == "set"
        assert r["kick_off"] == "ship the feature"
        assert agent.goal_manager.state.goal == "ship the feature"

    def test_set_inline_contract(self):
        agent = FakeAgent(manager=make_manager())
        r = handle_goal_command(agent, "Migrate auth\nverify: the suite passes")
        assert r["action"] == "set"
        assert agent.goal_manager.state.goal == "Migrate auth"
        assert agent.goal_manager.has_contract() is True
        assert agent.goal_manager.state.contract.verification == "the suite passes"
        assert any("完成契约" in ln for ln in r["lines"])

    def test_set_kicks_off_loop(self):
        # 设完目标立即启动循环（对齐 Hermes）——kick_off 带回目标文本。
        agent = FakeAgent(manager=make_manager())
        r = handle_goal_command(agent, "do the thing")
        assert r["kick_off"] == "do the thing"


# ──────────────────────────────────────────────────────────────────────
# /goal draft
# ──────────────────────────────────────────────────────────────────────


class TestGoalCommandDraft:
    def test_draft_without_objective_fails(self):
        agent = FakeAgent(manager=make_manager())
        r = handle_goal_command(agent, "draft")
        assert r["ok"] is False
        assert r["action"] == "draft"
        assert "用法" in r["message"]

    def test_draft_with_llm_caller_sets_contract(self):
        caller = FakeCaller('{"outcome": "auth on JWT", "verification": "suite green"}')
        mgr = make_manager(llm_caller=caller)
        agent = FakeAgent(manager=mgr)
        r = handle_goal_command(agent, "draft Migrate auth to JWT")
        assert r["ok"] is True
        assert r["action"] == "draft"
        assert mgr.has_contract() is True
        assert mgr.state.contract.verification == "suite green"
        assert any("起草的完成契约" in ln for ln in r["lines"])
        assert r["kick_off"] == "Migrate auth to JWT"

    def test_draft_without_llm_caller_falls_back_to_bare_goal(self):
        mgr = make_manager(llm_caller=None)
        agent = FakeAgent(manager=mgr)
        r = handle_goal_command(agent, "draft build a thing")
        assert r["ok"] is True
        assert mgr.state.goal == "build a thing"
        assert mgr.has_contract() is False
        assert any("无法起草契约" in ln for ln in r["lines"])


# ──────────────────────────────────────────────────────────────────────
# /goal pause / resume / clear
# ──────────────────────────────────────────────────────────────────────


class TestGoalCommandLifecycle:
    def test_pause(self):
        agent, mgr = _agent_with_goal("ship it")
        r = handle_goal_command(agent, "pause")
        assert r["ok"] is True
        assert r["action"] == "pause"
        assert mgr.state.status == "paused"

    def test_pause_no_goal(self):
        agent = FakeAgent(manager=make_manager())
        r = handle_goal_command(agent, "pause")
        assert r["ok"] is False

    def test_resume(self):
        agent, mgr = _agent_with_goal("ship it")
        mgr.pause()
        r = handle_goal_command(agent, "resume")
        assert r["ok"] is True
        assert r["action"] == "resume"
        assert mgr.state.status == "active"
        assert r["kick_off"] == "ship it"  # 恢复后立即续跑

    def test_resume_no_goal(self):
        agent = FakeAgent(manager=make_manager())
        assert handle_goal_command(agent, "resume")["ok"] is False

    @pytest.mark.parametrize("verb", ["clear", "stop", "done"])
    def test_clear_verbs(self, verb):
        agent, mgr = _agent_with_goal("ship it")
        r = handle_goal_command(agent, verb)
        assert r["ok"] is True
        assert r["action"] == "clear"
        assert mgr.has_goal() is False

    def test_clear_no_goal(self):
        agent = FakeAgent(manager=make_manager())
        assert handle_goal_command(agent, "clear")["ok"] is False


# ──────────────────────────────────────────────────────────────────────
# /goal wait / unwait
# ──────────────────────────────────────────────────────────────────────


class TestGoalCommandWait:
    def test_wait_on_pid(self):
        agent, mgr = _agent_with_goal("ship it")
        r = handle_goal_command(agent, "wait 4242 CI running")
        assert r["ok"] is True
        assert r["action"] == "wait"
        assert mgr.state.waiting_on_pid == 4242
        assert mgr.state.waiting_reason == "CI running"

    def test_wait_without_reason(self):
        agent, mgr = _agent_with_goal("ship it")
        r = handle_goal_command(agent, "wait 4242")
        assert r["ok"] is True
        assert mgr.state.waiting_on_pid == 4242

    def test_wait_invalid_pid(self):
        agent, _ = _agent_with_goal("ship it")
        r = handle_goal_command(agent, "wait abc")
        assert r["ok"] is False
        assert "整数" in r["message"]

    def test_wait_no_arg(self):
        agent, _ = _agent_with_goal("ship it")
        r = handle_goal_command(agent, "wait")
        assert r["ok"] is False
        assert "用法" in r["message"]

    def test_wait_no_active_goal(self):
        agent = FakeAgent(manager=make_manager())
        r = handle_goal_command(agent, "wait 4242")
        assert r["ok"] is False

    def test_unwait_clears_barrier(self):
        agent, mgr = _agent_with_goal("ship it")
        handle_goal_command(agent, "wait 4242")
        r = handle_goal_command(agent, "unwait")
        assert r["ok"] is True
        assert r["action"] == "unwait"
        assert mgr.state.waiting_on_pid is None

    def test_unwait_no_barrier(self):
        agent, _ = _agent_with_goal("ship it")
        assert handle_goal_command(agent, "unwait")["ok"] is False


# ──────────────────────────────────────────────────────────────────────
# /subgoal
# ──────────────────────────────────────────────────────────────────────


class TestSubgoalCommand:
    def test_no_agent_unavailable(self):
        assert handle_subgoal_command(None, "x")["action"] == "unavailable"

    def test_no_goal(self):
        agent = FakeAgent(manager=make_manager())
        r = handle_subgoal_command(agent, "criterion")
        assert r["ok"] is False
        assert r["action"] == "no_goal"

    def test_list_bare(self):
        agent, mgr = _agent_with_goal("ship it")
        mgr.add_subgoal("a")
        r = handle_subgoal_command(agent, "")
        assert r["ok"] is True
        assert r["action"] == "list"
        assert any("a" in ln for ln in r["lines"])

    def test_add(self):
        agent, mgr = _agent_with_goal("ship it")
        r = handle_subgoal_command(agent, "write tests")
        assert r["ok"] is True
        assert r["action"] == "add"
        assert mgr.state.subgoals == ["write tests"]

    def test_add_multiple_numbered(self):
        agent, mgr = _agent_with_goal("ship it")
        handle_subgoal_command(agent, "first")
        r = handle_subgoal_command(agent, "second")
        assert "2" in r["message"]
        assert mgr.state.subgoals == ["first", "second"]

    def test_remove(self):
        agent, mgr = _agent_with_goal("ship it")
        mgr.add_subgoal("a")
        mgr.add_subgoal("b")
        r = handle_subgoal_command(agent, "remove 1")
        assert r["ok"] is True
        assert r["action"] == "remove"
        assert mgr.state.subgoals == ["b"]

    def test_remove_no_index(self):
        agent, _ = _agent_with_goal("ship it")
        assert handle_subgoal_command(agent, "remove")["ok"] is False

    def test_remove_invalid_index(self):
        agent, mgr = _agent_with_goal("ship it")
        mgr.add_subgoal("a")
        r = handle_subgoal_command(agent, "remove xyz")
        assert r["ok"] is False
        assert "整数" in r["message"]

    def test_remove_out_of_range(self):
        agent, mgr = _agent_with_goal("ship it")
        mgr.add_subgoal("a")
        assert handle_subgoal_command(agent, "remove 9")["ok"] is False

    def test_clear(self):
        agent, mgr = _agent_with_goal("ship it")
        mgr.add_subgoal("a")
        mgr.add_subgoal("b")
        r = handle_subgoal_command(agent, "clear")
        assert r["ok"] is True
        assert r["action"] == "clear"
        assert mgr.state.subgoals == []

    def test_clear_none_existing(self):
        agent, _ = _agent_with_goal("ship it")
        assert handle_subgoal_command(agent, "clear")["ok"] is False


# ──────────────────────────────────────────────────────────────────────
# run_goal_turn_loop — Ralph Loop 接到 SpiritAgent
# ──────────────────────────────────────────────────────────────────────


class TestRunGoalTurnLoop:
    def test_inactive_runs_single_turn(self):
        # 无活跃目标 → 退化为只跑一轮，等价普通 chat。
        agent = FakeAgent(manager=make_manager(), chat_responses=["hello back"])
        result = run_goal_turn_loop(agent, "hi")
        assert result["outcome"] == "inactive"
        assert result["responses"] == ["hello back"]
        assert agent.chat_calls == ["hi"]

    def test_done_on_first_turn(self):
        caller = FakeCaller('{"done": true, "reason": "finished"}')
        mgr = make_manager(llm_caller=caller)
        mgr.set("ship it")
        agent = FakeAgent(manager=mgr, chat_responses=["I shipped it"])
        result = run_goal_turn_loop(agent, "ship it")
        assert result["outcome"] == "done"
        assert result["turns_used"] == 1
        assert result["responses"] == ["I shipped it"]
        assert agent.chat_calls == ["ship it"]  # 首轮即完成，无续传

    def test_continue_then_done_drives_extra_turn(self):
        caller = FakeCaller(
            ['{"done": false, "reason": "more"}', '{"done": true, "reason": "ok"}']
        )
        mgr = make_manager(llm_caller=caller, max_turns=20)
        mgr.set("ship it")
        agent = FakeAgent(manager=mgr, chat_responses=["partial", "finished"])
        result = run_goal_turn_loop(agent, "ship it")
        assert result["outcome"] == "done"
        assert result["turns_used"] == 2
        assert len(result["responses"]) == 2
        assert len(agent.chat_calls) == 2
        # 第二轮 chat 收到的是续传 prompt（含目标）。
        assert "ship it" in agent.chat_calls[1]

    def test_on_turn_callback(self):
        caller = FakeCaller('{"done": true, "reason": "ok"}')
        mgr = make_manager(llm_caller=caller)
        mgr.set("ship it")
        agent = FakeAgent(manager=mgr, chat_responses=["done resp"])
        seen: list = []
        run_goal_turn_loop(agent, "ship it", on_turn=lambda text, i: seen.append((text, i)))
        assert seen == [("done resp", 1)]

    def test_on_decision_callback(self):
        caller = FakeCaller('{"done": true, "reason": "ok"}')
        mgr = make_manager(llm_caller=caller)
        mgr.set("ship it")
        agent = FakeAgent(manager=mgr, chat_responses=["r"])
        decisions: list = []
        run_goal_turn_loop(agent, "ship it", on_decision=decisions.append)
        assert len(decisions) == 1
        assert decisions[0]["verdict"] == "done"

    def test_chat_returning_non_dict(self):
        # agent.chat 返回裸字符串也能被消化。
        class StrAgent(FakeAgent):
            def chat(self, message):
                self.chat_calls.append(message)
                return "plain string response"

        mgr = make_manager(llm_caller=FakeCaller('{"done": true, "reason": "ok"}'))
        mgr.set("ship it")
        agent = StrAgent(manager=mgr)
        result = run_goal_turn_loop(agent, "ship it")
        assert result["outcome"] == "done"
        assert result["responses"] == ["plain string response"]
