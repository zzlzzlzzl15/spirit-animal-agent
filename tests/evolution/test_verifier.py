"""B Verifier 独立校验闭环测试：客观证据 / 领域验证器 / LLM 回退 / 回滚 / UNVERIFIED。"""

from spirit.checkpoint import CheckpointManager
from spirit.evolution.protocol import TargetVerdict, Verdict
from spirit.evolution.verifier import (
    Evidence,
    ExitCodeVerifier,
    FileContentVerifier,
    MetricThresholdVerifier,
    PatternVerifier,
    Verifier,
)

from .conftest import FakeCaller, json_response


# --------------------------------------------------------------------------
# Evidence：客观证据容器（B1/B2）
# --------------------------------------------------------------------------

def test_evidence_render_includes_all_sections():
    ev = Evidence(
        task="T", exit_code=0, logs="L", files={"/p": "c"},
        artifacts={"a": "b"}, env_state={"k": "v"}, metrics={"sharpe": 1.5},
    )
    text = ev.render()
    for frag in ("T", "L", "/p", "sharpe", "1.5", "k: v", "a"):
        assert frag in text


def test_evidence_render_skips_empty_sections():
    text = Evidence(task="T", logs="L").render()
    assert "退出码" not in text and "客观指标" not in text and "文件快照" not in text


def test_evidence_is_empty():
    assert Evidence(task="T").is_empty() is True
    assert Evidence(task="T", exit_code=1).is_empty() is False
    assert Evidence(task="T", logs="x").is_empty() is False


def test_evidence_has_no_actor_private_field():
    """隔离保证：Evidence 结构性地不承载 Actor 私有推理/记忆。"""
    fields = set(Evidence.__dataclass_fields__)
    assert not (fields & {"reasoning", "thoughts", "memory", "actor_memory", "private"})


def test_evidence_roundtrip():
    ev = Evidence(
        task="T", exit_code=0, logs="L", files={"/p": "c"},
        artifacts={"a": "b"}, env_state={"k": "v"}, metrics={"m": 1.0},
    )
    assert Evidence.from_dict(ev.to_dict()) == ev


def test_evidence_from_dict_bad_types_coerced():
    ev = Evidence.from_dict({"exit_code": "x", "metrics": {"m": "bad", "n": 2}, "files": "nope"})
    assert ev.exit_code is None
    assert ev.files == {}
    assert "m" not in ev.metrics and ev.metrics["n"] == 2.0


# --------------------------------------------------------------------------
# 领域验证器（B5，客观信号）
# --------------------------------------------------------------------------

def test_exit_code_verifier():
    v = ExitCodeVerifier(0)
    assert v.verify("t", Evidence(exit_code=0)).passed is True
    assert v.verify("t", Evidence(exit_code=2)).failed is True
    assert v.verify("t", Evidence()) is None  # 无退出码信号 → 不裁决


def test_file_content_verifier_contains():
    v = FileContentVerifier(expected={"/a": "hello"}, mode="contains")
    assert v.verify("t", Evidence(files={"/a": "say hello world"})).passed is True
    assert v.verify("t", Evidence(files={"/a": "nope"})).failed is True


def test_file_content_verifier_missing_file_is_none():
    v = FileContentVerifier(expected={"/a": "hello"})
    assert v.verify("t", Evidence(files={})) is None
    assert v.verify("t", Evidence()) is None


def test_file_content_verifier_equals():
    v = FileContentVerifier(expected={"/a": "exact"}, mode="equals")
    assert v.verify("t", Evidence(files={"/a": "exact"})).passed is True
    assert v.verify("t", Evidence(files={"/a": "exact!"})).failed is True


def test_file_content_verifier_no_expected_is_none():
    assert FileContentVerifier().verify("t", Evidence(files={"/a": "x"})) is None


def test_pattern_verifier_match():
    v = PatternVerifier(pattern=r"OK", field_name="logs")
    assert v.verify("t", Evidence(logs="all OK")).passed is True
    assert v.verify("t", Evidence(logs="failed")).failed is True
    assert v.verify("t", Evidence(logs="")) is None


def test_pattern_verifier_negated():
    v = PatternVerifier(pattern=r"ERROR", should_match=False)
    assert v.verify("t", Evidence(logs="clean")).passed is True
    assert v.verify("t", Evidence(logs="has ERROR")).failed is True


def test_pattern_verifier_bad_regex_is_none():
    assert PatternVerifier(pattern="[").verify("t", Evidence(logs="x")) is None


def test_metric_threshold_verifier():
    v = MetricThresholdVerifier(metric="sharpe", min_value=1.0)
    assert v.verify("t", Evidence(metrics={"sharpe": 1.5})).passed is True
    assert v.verify("t", Evidence(metrics={"sharpe": 0.5})).failed is True
    assert v.verify("t", Evidence(metrics={})) is None


# --------------------------------------------------------------------------
# Verifier 编排：客观优先 / LLM 回退（B3/B5）
# --------------------------------------------------------------------------

def test_objective_verifier_wins_over_llm():
    """客观信号 > 模型自评：客观 PASS 时不调用 LLM。"""
    caller = FakeCaller(json_response(verdict="FAIL", reason="llm 说失败"))
    v = Verifier(caller=caller, domain_verifiers=[ExitCodeVerifier(0)])
    res = v.verify("t", Evidence(exit_code=0))
    assert res.passed is True
    assert caller.calls == []  # LLM 未被调用


def test_falls_back_to_llm_when_no_objective():
    caller = FakeCaller(json_response(verdict="PASS", reason="证据充分"))
    v = Verifier(caller=caller, domain_verifiers=[ExitCodeVerifier(0)])
    res = v.verify("t", Evidence(logs="ok"))  # 无 exit_code → 客观验证器返回 None
    assert res.passed is True and len(caller.calls) == 1


def test_no_objective_no_caller_is_unverified():
    v = Verifier(domain_verifiers=[ExitCodeVerifier(0)])
    res = v.verify("t", Evidence(logs="x"))
    assert res.verdict is TargetVerdict.UNVERIFIED and res.reason


def test_use_llm_false_no_objective_is_unverified():
    caller = FakeCaller(json_response(verdict="PASS", reason="r"))
    v = Verifier(caller=caller, domain_verifiers=[])
    res = v.verify("t", Evidence(logs="x"), use_llm=False)
    assert res.verdict is TargetVerdict.UNVERIFIED and caller.calls == []


def test_domain_verifier_exception_is_skipped():
    class Boom:
        name = "boom"

        def verify(self, task, ev):
            raise RuntimeError("verifier boom")

    caller = FakeCaller(json_response(verdict="PASS", reason="llm 兜底"))
    v = Verifier(caller=caller, domain_verifiers=[Boom()])
    res = v.verify("t", Evidence(logs="x"))
    assert res.passed is True  # 跳过 Boom，回退 LLM


def test_add_verifier_chains():
    v = Verifier().add_verifier(ExitCodeVerifier(0))
    assert len(v.domain_verifiers) == 1


def test_stable_pass_fail_on_success_and_failure():
    """验收：成功任务稳定判 PASS，失败任务稳定判 FAIL。"""
    v = Verifier(domain_verifiers=[ExitCodeVerifier(0), PatternVerifier(pattern=r"SUCCESS")])
    ok = v.verify("run", Evidence(exit_code=0, logs="SUCCESS"))
    bad = v.verify("run", Evidence(exit_code=1, logs="CRASH"))
    assert ok.passed is True and bad.failed is True


# --------------------------------------------------------------------------
# 证据强制转换
# --------------------------------------------------------------------------

def test_string_evidence_coerced_and_rendered():
    caller = FakeCaller(json_response(verdict="PASS", reason="r"))
    v = Verifier(caller=caller)
    v.verify("任务T", "纯文本日志")
    sent = caller.last_call["messages"][1]["content"]
    assert "纯文本日志" in sent and "任务T" in sent


def test_dict_evidence_coerced():
    v = Verifier(domain_verifiers=[ExitCodeVerifier(0)])
    assert v.verify("t", {"exit_code": 0}).passed is True


# --------------------------------------------------------------------------
# 探针 + 回滚保护（B4）
# --------------------------------------------------------------------------

def test_probe_exception_is_unverified():
    v = Verifier()

    def probe(ev):
        raise RuntimeError("disk on fire")

    res = v.verify("t", Evidence(), probe=probe, use_llm=False)
    assert res.verdict is TargetVerdict.UNVERIFIED
    assert "disk on fire" in res.reason


def test_probe_returning_verdict_is_used():
    v = Verifier()

    def probe(ev):
        return Verdict(verdict=TargetVerdict.PASS, reason="探针确认")

    res = v.verify("t", Evidence(), probe=probe)
    assert res.passed is True and res.reason == "探针确认"


def test_probe_verdict_empty_reason_filled():
    v = Verifier()

    def probe(ev):
        return Verdict(verdict=TargetVerdict.FAIL, reason="")

    res = v.verify("t", Evidence(), probe=probe)
    assert res.failed is True and res.reason  # 理由被补全为非空


def test_probe_returning_none_continues_to_objective():
    v = Verifier(domain_verifiers=[ExitCodeVerifier(0)])

    def probe(ev):
        return None

    res = v.verify("t", Evidence(exit_code=0), probe=probe)
    assert res.passed is True


def test_probe_rollback_restores_mutated_file(evo_home, tmp_path):
    """探针试探性改动环境后，校验结束文件被回滚到原始内容（复用 checkpoint）。"""
    target = tmp_path / "state.txt"
    target.write_text("original", encoding="utf-8")
    ev = Evidence(task="t", files={str(target): "original"})
    mgr = CheckpointManager()
    v = Verifier(domain_verifiers=[], checkpoint_manager=mgr)

    def probe(e):
        target.write_text("MUTATED", encoding="utf-8")
        return None

    res = v.verify("t", ev, probe=probe, use_llm=False)
    assert res.verdict is TargetVerdict.UNVERIFIED  # 无客观/LLM 裁决
    assert target.read_text(encoding="utf-8") == "original"  # 已回滚


def test_probe_rollback_disabled_leaves_mutation(evo_home, tmp_path):
    """enable_rollback=False 时不回滚（对照）。"""
    target = tmp_path / "state.txt"
    target.write_text("original", encoding="utf-8")
    ev = Evidence(task="t", files={str(target): "original"})
    v = Verifier(checkpoint_manager=CheckpointManager(), enable_rollback=False)

    def probe(e):
        target.write_text("MUTATED", encoding="utf-8")
        return Verdict(verdict=TargetVerdict.PASS, reason="ok")

    v.verify("t", ev, probe=probe)
    assert target.read_text(encoding="utf-8") == "MUTATED"
