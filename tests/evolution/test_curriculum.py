"""Curriculum 课程规划器测试（Phase 6.C）：只读复盘 + fail-open 决策 + 派任务。"""

from spirit.evolution.curriculum import CurriculumPlanner
from spirit.evolution.memory import EvolutionMemory
from spirit.evolution.protocol import CurriculumDecision, CurriculumDecisionKind
from spirit.evolution.roles import CurriculumRole

from .conftest import FakeCaller, json_response


# --------------------------------------------------------------------------
# progress_summary：只读记忆汇总
# --------------------------------------------------------------------------

def test_progress_summary_no_memory_is_placeholder():
    planner = CurriculumPlanner()
    assert planner.progress_summary() == "（无历史记忆）"


def test_progress_summary_empty_memory_is_placeholder(evo_home):
    planner = CurriculumPlanner(memory=EvolutionMemory())
    assert planner.progress_summary() == "（无历史记忆）"


def test_progress_summary_lists_insights_ranked_by_confidence(evo_home):
    mem = EvolutionMemory()
    mem.add_insight("低置信经验", confidence=0.3, source_task_id="t1")
    mem.add_insight("高置信经验", confidence=0.9, source_task_id="t2")
    planner = CurriculumPlanner(memory=mem)
    summary = planner.progress_summary()
    assert "高置信经验" in summary
    assert "低置信经验" in summary
    # 高置信度排在前
    assert summary.index("高置信经验") < summary.index("低置信经验")


def test_progress_summary_includes_trajectories(evo_home):
    mem = EvolutionMemory()
    mem.add_trajectory("t9", "做了一些探索", outcome="FAIL")
    planner = CurriculumPlanner(memory=mem)
    summary = planner.progress_summary()
    assert "t9" in summary
    assert "FAIL" in summary


def test_progress_summary_respects_insight_limit(evo_home):
    mem = EvolutionMemory()
    for i in range(20):
        mem.add_insight(f"洞察{i}", confidence=0.5, source_task_id=f"t{i}")
    planner = CurriculumPlanner(memory=mem, insight_limit=3)
    summary = planner.progress_summary()
    # 只保留置信度前 3 条（条目行以 "- [" 开头）
    assert summary.count("- [") == 3


# --------------------------------------------------------------------------
# review：fail-open 与结构化决策
# --------------------------------------------------------------------------

def test_review_without_caller_is_stalled(evo_home):
    planner = CurriculumPlanner(memory=EvolutionMemory())
    decision = planner.review("目标任务")
    assert decision.decision is CurriculumDecisionKind.STALLED
    assert decision.rationale  # 有理由


def test_review_parses_practice_decision(evo_home):
    caller = FakeCaller(
        response=json_response(
            decision="practice", next_task="练习回测", rationale="弱点未覆盖"
        )
    )
    planner = CurriculumPlanner(
        role=CurriculumRole(caller), memory=EvolutionMemory()
    )
    decision = planner.review("目标任务", history="第1轮 FAIL")
    assert decision.decision is CurriculumDecisionKind.PRACTICE
    assert decision.next_task == "练习回测"
    assert decision.should_practice is True
    # history 与记忆汇总都应进入 user 上下文
    user_msg = caller.last_call["messages"][-1]["content"]
    assert "第1轮 FAIL" in user_msg


def test_review_parses_converged_decision(evo_home):
    caller = FakeCaller(response=json_response(decision="converged", rationale="已达标"))
    planner = CurriculumPlanner(role=CurriculumRole(caller), memory=EvolutionMemory())
    decision = planner.review("目标任务")
    assert decision.decision is CurriculumDecisionKind.CONVERGED
    assert decision.should_stop is True


def test_review_injects_memory_summary_into_context(evo_home):
    mem = EvolutionMemory()
    mem.add_insight("夏普>1才纳入", confidence=0.8, source_task_id="t1")
    caller = FakeCaller(response=json_response(decision="ready", rationale="ok"))
    planner = CurriculumPlanner(role=CurriculumRole(caller), memory=mem)
    planner.review("目标任务")
    user_msg = caller.last_call["messages"][-1]["content"]
    assert "夏普>1才纳入" in user_msg


# --------------------------------------------------------------------------
# next_task：练习优先，否则回退目标
# --------------------------------------------------------------------------

def test_next_task_prefers_practice_task():
    planner = CurriculumPlanner()
    decision = CurriculumDecision(
        decision=CurriculumDecisionKind.PRACTICE, next_task="练习A"
    )
    assert planner.next_task("目标任务", decision) == "练习A"


def test_next_task_falls_back_to_target_when_no_practice():
    planner = CurriculumPlanner()
    decision = CurriculumDecision(decision=CurriculumDecisionKind.READY, next_task="")
    assert planner.next_task("目标任务", decision) == "目标任务"


def test_next_task_falls_back_when_practice_but_empty_task():
    planner = CurriculumPlanner()
    decision = CurriculumDecision(decision=CurriculumDecisionKind.PRACTICE, next_task="")
    assert planner.next_task("目标任务", decision) == "目标任务"
