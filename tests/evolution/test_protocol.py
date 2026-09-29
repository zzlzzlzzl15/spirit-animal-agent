"""A2 协议数据类测试：枚举、容错解析、to_dict/from_dict 往返、预算判定。"""

from spirit.evolution.protocol import (
    ActorLearning,
    CurriculumDecision,
    CurriculumDecisionKind,
    EvolutionResult,
    EvolutionStatus,
    RoleName,
    SelfEvolvingStart,
    TargetVerdict,
    Verdict,
    parse_decision_kind,
    parse_status_value,
    parse_verdict_value,
)


# --------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------

def test_verdict_enum_values():
    assert TargetVerdict.PASS.value == "PASS"
    assert TargetVerdict.FAIL.value == "FAIL"
    assert TargetVerdict.UNVERIFIED.value == "UNVERIFIED"


def test_status_enum_distinguishes_stalled_and_converged():
    assert EvolutionStatus.STALLED is not EvolutionStatus.CONVERGED
    assert EvolutionStatus.READY_FOR_RETRY.value == "READY_FOR_RETRY"


def test_role_names():
    assert {r.value for r in RoleName} == {"actor", "verifier", "curriculum"}


# --------------------------------------------------------------------------
# 容错解析
# --------------------------------------------------------------------------

def test_parse_verdict_value_tolerant():
    assert parse_verdict_value("pass") is TargetVerdict.PASS
    assert parse_verdict_value("FAILED") is TargetVerdict.FAIL
    assert parse_verdict_value(TargetVerdict.PASS) is TargetVerdict.PASS


def test_parse_verdict_value_unknown_is_unverified():
    """铁律：无法识别绝不当 PASS/FAIL。"""
    for raw in ("", None, "weird", "maybe", 123):
        assert parse_verdict_value(raw) is TargetVerdict.UNVERIFIED


def test_parse_verdict_value_aliases():
    assert parse_verdict_value("SUCCESS") is TargetVerdict.PASS
    assert parse_verdict_value("ERROR") is TargetVerdict.UNVERIFIED


def test_parse_status_value_unknown_is_stalled():
    assert parse_status_value("nonsense") is EvolutionStatus.STALLED
    assert parse_status_value("converged") is EvolutionStatus.CONVERGED


def test_parse_decision_kind_aliases_and_default():
    assert parse_decision_kind("wave") is CurriculumDecisionKind.PRACTICE
    assert parse_decision_kind("retry") is CurriculumDecisionKind.READY
    assert parse_decision_kind("???") is CurriculumDecisionKind.STALLED


# --------------------------------------------------------------------------
# ActorLearning
# --------------------------------------------------------------------------

def test_actor_learning_requires_diagnosis():
    assert ActorLearning(memory="m", diagnosis="").is_valid() is False
    assert ActorLearning(memory="m", diagnosis="   ").is_valid() is False
    assert ActorLearning(memory="m", diagnosis="因为X").is_valid() is True


def test_actor_learning_roundtrip():
    al = ActorLearning(memory="m", diagnosis="d", task_id="t1")
    assert ActorLearning.from_dict(al.to_dict()) == al


def test_actor_learning_from_dict_none():
    al = ActorLearning.from_dict(None)
    assert al.memory == "" and al.diagnosis == "" and al.is_valid() is False


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------

def test_verdict_normalizes_string():
    v = Verdict(verdict="pass", reason="ok")
    assert v.verdict is TargetVerdict.PASS
    assert v.passed is True and v.failed is False and v.unverified is False


def test_verdict_is_valid_requires_reason():
    assert Verdict(verdict=TargetVerdict.PASS, reason="").is_valid() is False
    assert Verdict(verdict=TargetVerdict.PASS, reason="依据X").is_valid() is True


def test_verdict_roundtrip():
    v = Verdict(verdict=TargetVerdict.FAIL, reason="r", task_id="t", confidence=0.4)
    v2 = Verdict.from_dict(v.to_dict())
    assert v2.verdict is TargetVerdict.FAIL and v2.confidence == 0.4 and v2.task_id == "t"


def test_verdict_from_dict_bad_confidence_defaults():
    v = Verdict.from_dict({"verdict": "PASS", "reason": "r", "confidence": "NaN!"})
    assert v.confidence == 1.0


# --------------------------------------------------------------------------
# CurriculumDecision
# --------------------------------------------------------------------------

def test_curriculum_decision_helpers():
    assert CurriculumDecision(decision="practice").should_practice is True
    assert CurriculumDecision(decision="converged").should_stop is True
    assert CurriculumDecision(decision="stalled").should_stop is True
    assert CurriculumDecision(decision="ready").should_stop is False


def test_curriculum_decision_roundtrip():
    d = CurriculumDecision(decision="ready", next_task="n", rationale="r")
    assert CurriculumDecision.from_dict(d.to_dict()) == d


# --------------------------------------------------------------------------
# EvolutionResult
# --------------------------------------------------------------------------

def test_evolution_result_helpers():
    assert EvolutionResult(status="converged").converged is True
    assert EvolutionResult(status="stalled").stalled is True
    assert EvolutionResult(status="ready_for_retry").converged is False


def test_evolution_result_projects_non_list_coerced():
    r = EvolutionResult.from_dict({"status": "CONVERGED", "projects": "single"})
    assert r.projects == ["single"]


def test_evolution_result_roundtrip():
    r = EvolutionResult(status="stalled", memory="m", projects=["a", "b"], reason="预算耗尽")
    assert EvolutionResult.from_dict(r.to_dict()) == r


# --------------------------------------------------------------------------
# SelfEvolvingStart（预算计数器）
# --------------------------------------------------------------------------

def test_budget_not_exhausted_initially():
    s = SelfEvolvingStart()
    assert s.budget_exhausted() is False


def test_budget_exhausted_on_target_cycles():
    s = SelfEvolvingStart(max_target_cycles=2)
    s.record_target_cycle()
    assert s.budget_exhausted() is False
    s.record_target_cycle()
    assert s.budget_exhausted() is True


def test_budget_exhausted_on_practice():
    s = SelfEvolvingStart(max_practice_projects=1)
    s.record_practice()
    assert s.budget_exhausted() is True


def test_remaining_non_negative():
    s = SelfEvolvingStart(max_evolutions=3)
    for _ in range(5):
        s.record_evolution()
    assert s.remaining()["evolutions"] == 0


def test_self_evolving_start_roundtrip_and_bad_ints():
    s = SelfEvolvingStart(target_cycles=1, evolutions=2, practice_projects=3)
    s2 = SelfEvolvingStart.from_dict(s.to_dict())
    assert (s2.target_cycles, s2.evolutions, s2.practice_projects) == (1, 2, 3)
    bad = SelfEvolvingStart.from_dict({"target_cycles": "x", "max_evolutions": None})
    assert bad.target_cycles == 0 and bad.max_evolutions == 16
