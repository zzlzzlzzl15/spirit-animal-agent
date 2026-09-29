"""DRS 自进化状态机测试（Phase 6.C）：收敛/卡死可区分 + 预算 + 记忆提交 + 续跑。"""

from spirit.evolution.loop import (
    STOP_VERIFIER_PASS,
    AttemptResult,
    EvolutionLoop,
    run_evolution,
)
from spirit.evolution.memory import EvolutionMemory
from spirit.evolution.protocol import (
    CurriculumDecision,
    CurriculumDecisionKind,
    EvolutionStatus,
    SelfEvolvingStart,
    TargetVerdict,
)
from spirit.evolution.roles import ActorRole
from spirit.evolution.verifier import Evidence, ExitCodeVerifier, Verifier

from .conftest import FakeCaller, json_response


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------

class ScriptedCurriculum:
    """按脚本返回决策序列的课程规划器（耗尽后一律 STALLED）。"""

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.reviews = 0

    def review(self, target_task, *, history="", memory=None):
        self.reviews += 1
        if self.decisions:
            return self.decisions.pop(0)
        return CurriculumDecision(
            decision=CurriculumDecisionKind.STALLED, rationale="脚本耗尽"
        )

    def next_task(self, target_task, decision):
        if decision.should_practice and decision.next_task:
            return decision.next_task
        return target_task


def make_execute(exit_codes):
    """按序回放退出码的 execute seam（默认失败码 1）。"""
    codes = list(exit_codes)

    def execute(task):
        code = codes.pop(0) if codes else 1
        return Evidence(task=task, exit_code=code, logs=f"exit {code}")

    return execute


def _verifier():
    # 客观退出码验证器：0→PASS，非0→FAIL，无退出码→UNVERIFIED（无 caller）
    return Verifier(domain_verifiers=[ExitCodeVerifier(0)])


def _practice(next_task="练一下"):
    return CurriculumDecision(
        decision=CurriculumDecisionKind.PRACTICE, next_task=next_task, rationale="补弱点"
    )


def _ready():
    return CurriculumDecision(decision=CurriculumDecisionKind.READY, rationale="就绪重试")


def _converged():
    return CurriculumDecision(decision=CurriculumDecisionKind.CONVERGED, rationale="达标")


def _stalled():
    return CurriculumDecision(decision=CurriculumDecisionKind.STALLED, rationale="无进展")


# --------------------------------------------------------------------------
# AttemptResult.coerce
# --------------------------------------------------------------------------

def test_coerce_from_attempt_result_passthrough():
    ar = AttemptResult(observation="o", evidence=Evidence(task="T", exit_code=0))
    out = AttemptResult.coerce("T", ar)
    assert out is ar


def test_coerce_from_evidence_fills_observation():
    ev = Evidence(task="T", exit_code=0, logs="hello")
    out = AttemptResult.coerce("T", ev)
    assert out.evidence is ev
    assert "hello" in out.observation


def test_coerce_from_dict():
    out = AttemptResult.coerce("T", {"exit_code": 0, "logs": "x"})
    assert out.evidence.exit_code == 0


def test_coerce_from_str_becomes_logs():
    out = AttemptResult.coerce("T", "some text")
    assert out.observation == "some text"
    assert out.evidence.logs == "some text"
    assert out.evidence.task == "T"


def test_coerce_none_is_empty():
    out = AttemptResult.coerce("T", None)
    assert out.observation == ""
    assert out.evidence.task == "T"


# --------------------------------------------------------------------------
# 收敛路径
# --------------------------------------------------------------------------

def test_verifier_pass_policy_converges_immediately(evo_home):
    loop = EvolutionLoop(
        execute=make_execute([0]),
        verifier=_verifier(),
        curriculum=ScriptedCurriculum([]),  # verifier_pass 不该调用课程
        stop_policy=STOP_VERIFIER_PASS,
    )
    result = loop.run("目标任务")
    assert result.status is EvolutionStatus.CONVERGED
    assert loop.history[0]["verdict"] == TargetVerdict.PASS.value


def test_curriculum_review_converges_on_pass(evo_home):
    loop = EvolutionLoop(
        execute=make_execute([0]),
        verifier=_verifier(),
        curriculum=ScriptedCurriculum([_converged()]),
    )
    result = loop.run("目标任务")
    assert result.converged is True


def test_fail_then_practice_then_pass_converges(evo_home):
    # r1 target 失败 → 练习(通过) → r2 target 通过 → 课程收敛
    loop = EvolutionLoop(
        execute=make_execute([1, 0, 0]),
        verifier=_verifier(),
        curriculum=ScriptedCurriculum([_practice(), _converged()]),
        max_rounds=5,
    )
    result = loop.run("目标任务")
    assert result.status is EvolutionStatus.CONVERGED
    kinds = [h["kind"] for h in loop.history]
    assert "practice" in kinds
    assert kinds[0] == "target"


def test_fail_then_ready_then_pass_converges(evo_home):
    loop = EvolutionLoop(
        execute=make_execute([1, 0]),
        verifier=_verifier(),
        curriculum=ScriptedCurriculum([_ready(), _converged()]),
        max_rounds=5,
    )
    result = loop.run("目标任务")
    assert result.status is EvolutionStatus.CONVERGED


def test_retry_injects_previous_verifier_feedback(evo_home):
    # 第1轮失败→第2轮带上一轮校验反馈重试（“迭代改进”而非盲目重做）
    seen = []

    def cap_execute(task):
        seen.append(task)
        code = 0 if len(seen) >= 2 else 1   # 首轮失败，次轮成功
        return Evidence(task=task, exit_code=code, logs=f"exit {code}")

    loop = EvolutionLoop(
        execute=cap_execute,
        verifier=_verifier(),
        curriculum=ScriptedCurriculum([_ready(), _converged()]),
        max_rounds=5,
    )
    result = loop.run("目标任务")
    assert result.status is EvolutionStatus.CONVERGED
    assert len(seen) >= 2
    assert seen[0] == "目标任务"                 # 首轮无反馈，原样任务
    assert "上一轮校验反馈" in seen[1]          # 次轮注入了反馈段
    assert "目标任务" in seen[1]                 # 原任务仍在
    # 校验仍针对原始任务（evidence.task 不被反馈污染）
    assert all(h["task"] == "目标任务" for h in loop.history if h["kind"] == "target")


# --------------------------------------------------------------------------
# 卡死路径（STALLED 与 CONVERGED 必须可区分）
# --------------------------------------------------------------------------

def test_curriculum_stalled_marks_stalled(evo_home):
    loop = EvolutionLoop(
        execute=make_execute([1]),
        verifier=_verifier(),
        curriculum=ScriptedCurriculum([_stalled()]),
    )
    result = loop.run("目标任务")
    assert result.status is EvolutionStatus.STALLED
    assert result.converged is False


def test_budget_exhausted_is_stalled_not_converged(evo_home):
    loop = EvolutionLoop(
        execute=make_execute([]),  # 永远失败
        verifier=_verifier(),
        curriculum=ScriptedCurriculum([_ready()] * 20),
        budget=SelfEvolvingStart(max_target_cycles=2),
        max_rounds=10,
    )
    result = loop.run("目标任务")
    assert result.status is EvolutionStatus.STALLED
    assert "预算耗尽" in result.reason


def test_max_rounds_is_stalled(evo_home):
    loop = EvolutionLoop(
        execute=make_execute([]),  # 永远失败
        verifier=_verifier(),
        curriculum=ScriptedCurriculum([_ready()] * 50),
        max_rounds=3,
    )
    result = loop.run("目标任务")
    assert result.status is EvolutionStatus.STALLED


def test_no_execute_seam_is_stalled_not_success(evo_home):
    # 无 execute → 空证据 → UNVERIFIED；无 caller 课程 → STALLED
    loop = EvolutionLoop(verifier=_verifier())
    result = loop.run("目标任务")
    assert result.status is EvolutionStatus.STALLED
    assert loop.history[0]["verdict"] == TargetVerdict.UNVERIFIED.value


# --------------------------------------------------------------------------
# 记忆提交（C3：仅已验证经验边界）
# --------------------------------------------------------------------------

def test_memory_records_trajectory_and_insight(evo_home):
    mem = EvolutionMemory()
    actor = ActorRole(FakeCaller(response=json_response(memory="经验要点", diagnosis="改进方向")))
    loop = EvolutionLoop(
        execute=make_execute([0]),
        verifier=_verifier(),
        curriculum=ScriptedCurriculum([_converged()]),
        actor=actor,
        memory=mem,
    )
    loop.run("目标任务")
    stats = mem.stats()
    assert stats["trajectories"] >= 1
    assert stats["insights_active"] >= 1


def test_no_insight_when_learning_invalid(evo_home):
    # actor 无 caller → diagnosis 空 → 不沉淀洞察，但仍记轨迹
    mem = EvolutionMemory()
    loop = EvolutionLoop(
        execute=make_execute([1]),
        verifier=_verifier(),
        curriculum=ScriptedCurriculum([_stalled()]),
        memory=mem,
    )
    loop.run("目标任务")
    assert mem.stats()["trajectories"] >= 1
    assert mem.stats()["insights_active"] == 0


def test_commit_memory_disabled(evo_home):
    mem = EvolutionMemory()
    loop = EvolutionLoop(
        execute=make_execute([0]),
        verifier=_verifier(),
        curriculum=ScriptedCurriculum([_converged()]),
        memory=mem,
        commit_memory=False,
    )
    loop.run("目标任务")
    assert mem.stats()["trajectories"] == 0


# --------------------------------------------------------------------------
# 执行异常 fail-soft
# --------------------------------------------------------------------------

def test_execute_exception_yields_unverified_not_crash(evo_home):
    def boom(task):
        raise RuntimeError("环境炸了")

    loop = EvolutionLoop(
        execute=boom,
        verifier=_verifier(),
        curriculum=ScriptedCurriculum([_stalled()]),
    )
    result = loop.run("目标任务")
    assert result.status is EvolutionStatus.STALLED
    assert loop.history[0]["verdict"] == TargetVerdict.UNVERIFIED.value


# --------------------------------------------------------------------------
# C5：可序列化状态（续跑）
# --------------------------------------------------------------------------

def test_state_restore_roundtrip(evo_home):
    loop = EvolutionLoop(
        execute=make_execute([1]),
        verifier=_verifier(),
        curriculum=ScriptedCurriculum([_stalled()]),
    )
    loop.run("目标任务")
    state = loop.state()
    assert state["budget"]["target_cycles"] >= 1

    fresh = EvolutionLoop(execute=make_execute([0]), verifier=_verifier())
    fresh.restore(state)
    assert fresh.budget.target_cycles == state["budget"]["target_cycles"]
    assert fresh.history == state["history"]


def test_run_evolution_entry_with_state(evo_home):
    result = run_evolution(
        "目标任务",
        execute=make_execute([0]),
        verifier=_verifier(),
        curriculum=ScriptedCurriculum([_converged()]),
        stop_policy=STOP_VERIFIER_PASS,
    )
    assert result.status is EvolutionStatus.CONVERGED
