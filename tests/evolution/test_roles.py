"""A3 三角色测试：规格 / prompt 渲染 / model slot 解析 / 注入式 caller 解析 / fail-soft。"""

from types import SimpleNamespace

import pytest

from spirit.evolution.protocol import (
    CurriculumDecisionKind,
    RoleName,
    TargetVerdict,
)
from spirit.evolution.roles import (
    ROLE_SPECS,
    ActorRole,
    CurriculumRole,
    VerifierRole,
    build_role_caller,
    get_role_spec,
    parse_actor_learning,
    parse_curriculum_decision,
    parse_verdict,
    render_messages,
    resolve_model,
)

from .conftest import FakeCaller, RaisingCaller, json_response


# --------------------------------------------------------------------------
# 角色规格
# --------------------------------------------------------------------------

def test_three_role_specs():
    assert set(ROLE_SPECS) == {RoleName.ACTOR, RoleName.VERIFIER, RoleName.CURRICULUM}


def test_get_role_spec_by_enum_and_string():
    assert get_role_spec(RoleName.VERIFIER).name is RoleName.VERIFIER
    assert get_role_spec("curriculum").name is RoleName.CURRICULUM


def test_get_role_spec_unknown_falls_back_to_actor():
    assert get_role_spec("bogus").name is RoleName.ACTOR


def test_verifier_and_curriculum_are_deterministic():
    assert ROLE_SPECS[RoleName.VERIFIER].temperature == 0.0
    assert ROLE_SPECS[RoleName.CURRICULUM].temperature == 0.0


def test_spec_describe():
    d = get_role_spec("actor").describe()
    assert d["name"] == "actor" and d["model_config_key"] == "evolution.roles.actor.model"


# --------------------------------------------------------------------------
# prompt 渲染
# --------------------------------------------------------------------------

def test_render_messages_structure():
    msgs = render_messages(RoleName.VERIFIER, "任务X 证据Y")
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert "任务X 证据Y" in msgs[1]["content"]


def test_system_prompts_encode_constraints():
    actor = ROLE_SPECS[RoleName.ACTOR].system_prompt
    verifier = ROLE_SPECS[RoleName.VERIFIER].system_prompt
    curriculum = ROLE_SPECS[RoleName.CURRICULUM].system_prompt
    assert "diagnosis" in actor            # Actor 必须诊断
    assert "UNVERIFIED" in verifier         # Verifier 三态
    assert "看不到" in verifier             # 屏蔽 Actor 私有推理/记忆
    assert "不打分" in curriculum           # Curriculum 不打分不写记忆


# --------------------------------------------------------------------------
# model slot 解析
# --------------------------------------------------------------------------

def test_resolve_model_prefers_config(monkeypatch):
    import spirit.config as config
    monkeypatch.setattr(
        config, "get_config_value",
        lambda key, default=None: "cfg-model" if key.endswith("actor.model") else default,
        raising=False,
    )
    agent = SimpleNamespace(model="agent-model")
    assert resolve_model(RoleName.ACTOR, agent) == "cfg-model"


def test_resolve_model_falls_back_to_agent(monkeypatch):
    import spirit.config as config
    monkeypatch.setattr(config, "get_config_value", lambda key, default=None: default, raising=False)
    agent = SimpleNamespace(model="agent-model")
    assert resolve_model(RoleName.VERIFIER, agent) == "agent-model"


def test_resolve_model_none_without_anything(monkeypatch):
    import spirit.config as config
    monkeypatch.setattr(config, "get_config_value", lambda key, default=None: default, raising=False)
    assert resolve_model(RoleName.CURRICULUM, None) is None


# --------------------------------------------------------------------------
# build_role_caller（复用 agent.client 的 side call）
# --------------------------------------------------------------------------

class _FakeCompletions:
    def __init__(self, content):
        self.content = content
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


class FakeAgent:
    def __init__(self, content="{}", model="test-model"):
        self.completions = _FakeCompletions(content)
        self.client = SimpleNamespace(
            chat=SimpleNamespace(completions=self.completions)
        )
        self.model = model


def test_build_role_caller_returns_content(monkeypatch):
    import spirit.config as config
    monkeypatch.setattr(config, "get_config_value", lambda key, default=None: default, raising=False)
    agent = FakeAgent(content='{"verdict":"PASS","reason":"ok"}')
    caller = build_role_caller(agent, RoleName.VERIFIER)
    out = caller([{"role": "user", "content": "hi"}], 0.0, 100, 30.0)
    assert "PASS" in out
    assert agent.completions.last_kwargs["model"] == "test-model"


def test_build_role_caller_uses_role_model_slot(monkeypatch):
    import spirit.config as config
    monkeypatch.setattr(
        config, "get_config_value",
        lambda key, default=None: "cheap-model" if key.endswith("verifier.model") else default,
        raising=False,
    )
    agent = FakeAgent(content="x", model="strong-model")
    caller = build_role_caller(agent, RoleName.VERIFIER)
    caller([{"role": "user", "content": "hi"}], 0.0, 100, 30.0)
    assert agent.completions.last_kwargs["model"] == "cheap-model"


def test_build_role_caller_swallows_exception(monkeypatch):
    import spirit.config as config
    monkeypatch.setattr(config, "get_config_value", lambda key, default=None: default, raising=False)

    class BoomAgent(FakeAgent):
        def __init__(self):
            super().__init__(content="x")
            self.client = SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(
                        create=lambda **k: (_ for _ in ()).throw(RuntimeError("api down"))
                    )
                )
            )

    caller = build_role_caller(BoomAgent(), RoleName.ACTOR)
    assert caller([{"role": "user", "content": "hi"}], 0.3, 100, 30.0) == ""


# --------------------------------------------------------------------------
# Actor 解析
# --------------------------------------------------------------------------

def test_actor_learns_from_json():
    caller = FakeCaller(json_response(memory="要点", diagnosis="因为没止损"))
    learning = ActorRole(caller).learn("任务", "观察", task_id="t1")
    assert learning.is_valid() is True
    assert learning.memory == "要点" and learning.diagnosis == "因为没止损"
    assert learning.task_id == "t1"


def test_actor_malformed_json_falls_back_to_memory():
    caller = FakeCaller("这不是 JSON")
    learning = ActorRole(caller).learn("任务", "观察")
    assert learning.memory == "这不是 JSON"
    assert learning.diagnosis == "" and learning.is_valid() is False


def test_actor_no_caller_returns_empty():
    learning = ActorRole().learn("任务", "观察")
    assert learning.memory == "" and learning.is_valid() is False


def test_actor_raising_caller_fail_soft():
    learning = ActorRole(RaisingCaller()).learn("任务", "观察")
    assert learning.is_valid() is False


# --------------------------------------------------------------------------
# Verifier 解析
# --------------------------------------------------------------------------

def test_verifier_pass():
    caller = FakeCaller(json_response(verdict="PASS", reason="日志显示退出码0", confidence=0.95))
    v = VerifierRole(caller).verify("任务", "证据")
    assert v.verdict is TargetVerdict.PASS and v.passed is True
    assert v.confidence == 0.95 and v.is_valid() is True


def test_verifier_fail():
    caller = FakeCaller(json_response(verdict="FAIL", reason="回测亏损"))
    assert VerifierRole(caller).verify("任务", "证据").failed is True


def test_verifier_no_caller_is_unverified():
    """铁律：无 caller = 基础设施不可用 → UNVERIFIED，绝不当 PASS/FAIL。"""
    v = VerifierRole().verify("任务", "证据")
    assert v.verdict is TargetVerdict.UNVERIFIED
    assert v.reason and v.is_valid() is True


def test_verifier_malformed_is_unverified():
    caller = FakeCaller("垃圾输出")
    v = VerifierRole(caller).verify("任务", "证据")
    assert v.verdict is TargetVerdict.UNVERIFIED and v.reason


def test_verifier_empty_reply_is_unverified():
    caller = FakeCaller("")
    v = VerifierRole(caller).verify("任务", "证据")
    assert v.verdict is TargetVerdict.UNVERIFIED
    assert v.reason and v.is_valid() is True


def test_verifier_missing_reason_filled():
    caller = FakeCaller(json_response(verdict="PASS"))
    v = VerifierRole(caller).verify("任务", "证据")
    assert v.reason == "Verifier 未提供理由"


# --------------------------------------------------------------------------
# Curriculum 解析
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["practice", "ready", "converged", "stalled"])
def test_curriculum_decisions(kind):
    caller = FakeCaller(json_response(decision=kind, next_task="下一步", rationale="理由"))
    d = CurriculumRole(caller).decide("目标", "进度")
    assert d.decision.value == kind
    assert d.next_task == "下一步"


def test_curriculum_no_caller_is_stalled():
    d = CurriculumRole().decide("目标")
    assert d.decision is CurriculumDecisionKind.STALLED and d.should_stop is True


def test_curriculum_malformed_is_stalled():
    caller = FakeCaller("非 JSON")
    d = CurriculumRole(caller).decide("目标")
    assert d.decision is CurriculumDecisionKind.STALLED and d.rationale


# --------------------------------------------------------------------------
# 解析函数直测 + 通用
# --------------------------------------------------------------------------

def test_parse_verdict_extracts_from_noisy_text():
    raw = "思考...\n最终结论：\n{\"verdict\":\"FAIL\",\"reason\":\"证据不足\"}\n完毕"
    v = parse_verdict(raw)
    assert v.verdict is TargetVerdict.FAIL and v.reason == "证据不足"


def test_parse_functions_on_empty():
    assert parse_actor_learning("").is_valid() is False
    assert parse_verdict("").verdict is TargetVerdict.UNVERIFIED
    assert parse_curriculum_decision("").decision is CurriculumDecisionKind.STALLED


def test_role_invoke_uses_injected_caller_override():
    base = FakeCaller("base")
    override = FakeCaller(json_response(verdict="PASS", reason="r"))
    role = VerifierRole(base)
    v = role.verify("t", "e", caller=override)
    assert v.verdict is TargetVerdict.PASS
    assert base.calls == []  # 注入的 override 生效，base 未被调用


def test_role_has_caller():
    assert VerifierRole().has_caller() is False
    assert VerifierRole(FakeCaller("x")).has_caller() is True
