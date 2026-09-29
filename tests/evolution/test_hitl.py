"""HITL 人在环测试（Phase 6.E）：模式 / 触发 / 超时 fail-open / 建议回灌。"""

from spirit.evolution.hitl import (
    HITLMode,
    HumanAnswer,
    HumanInTheLoop,
    HumanQuery,
    make_hitl,
    parse_mode,
)
from spirit.evolution.memory import EvolutionMemory


def _query(confidence=1.0, is_fork=False, options=None):
    return HumanQuery(
        question="走哪条路线？",
        options=options or ["路线A", "路线B"],
        confidence=confidence,
        is_fork=is_fork,
        task_id="t1",
    )


# --------------------------------------------------------------------------
# 模式解析
# --------------------------------------------------------------------------

def test_parse_mode_aliases_and_default():
    assert parse_mode("never") is HITLMode.NEVER
    assert parse_mode("ALWAYS") is HITLMode.ALWAYS
    assert parse_mode("auto") is HITLMode.AUTO
    assert parse_mode("胡言乱语") is HITLMode.AUTO
    assert parse_mode(None) is HITLMode.AUTO


# --------------------------------------------------------------------------
# 触发判定（E1）
# --------------------------------------------------------------------------

def test_never_mode_never_asks():
    hitl = HumanInTheLoop(mode=HITLMode.NEVER)
    assert hitl.should_ask(_query(confidence=0.0, is_fork=True)) is False


def test_always_mode_always_asks():
    hitl = HumanInTheLoop(mode=HITLMode.ALWAYS)
    assert hitl.should_ask(_query(confidence=1.0)) is True


def test_auto_asks_on_low_confidence():
    hitl = HumanInTheLoop(mode=HITLMode.AUTO, confidence_threshold=0.5)
    assert hitl.should_ask(_query(confidence=0.3)) is True
    assert hitl.should_ask(_query(confidence=0.9)) is False


def test_auto_asks_on_fork():
    hitl = HumanInTheLoop(mode=HITLMode.AUTO)
    assert hitl.should_ask(_query(confidence=1.0, is_fork=True)) is True


# --------------------------------------------------------------------------
# consult：不打扰 / 询问 / 超时 fail-open
# --------------------------------------------------------------------------

def test_high_confidence_auto_does_not_disturb():
    calls = []
    hitl = HumanInTheLoop(mode=HITLMode.AUTO, ask=lambda q, t: calls.append(q) or "X")
    answer = hitl.consult(_query(confidence=1.0))
    assert calls == []           # 未打扰用户
    assert answer.asked is False
    assert answer.source == "auto"
    assert answer.text == "路线A"  # fail-open 取首个选项


def test_ask_returns_user_answer():
    hitl = HumanInTheLoop(mode=HITLMode.AUTO, ask=lambda q, t: "路线B")
    answer = hitl.consult(_query(confidence=0.1))
    assert answer.answered is True
    assert answer.from_user is True
    assert answer.text == "路线B"


def test_timeout_returns_none_fail_open():
    hitl = HumanInTheLoop(mode=HITLMode.ALWAYS, ask=lambda q, t: None)
    answer = hitl.consult(_query())
    assert answer.answered is False
    assert answer.timed_out is True
    assert answer.source == "auto"
    assert answer.text == "路线A"  # 自主继续，不卡死


def test_empty_reply_fail_open():
    hitl = HumanInTheLoop(mode=HITLMode.ALWAYS, ask=lambda q, t: "   ")
    answer = hitl.consult(_query())
    assert answer.timed_out is True
    assert answer.source == "auto"


def test_ask_exception_fail_open():
    def boom(q, t):
        raise RuntimeError("gateway 挂了")

    hitl = HumanInTheLoop(mode=HITLMode.ALWAYS, ask=boom)
    answer = hitl.consult(_query())
    assert answer.timed_out is True
    assert answer.source == "auto"
    assert answer.text == "路线A"


def test_need_ask_but_no_gateway_fail_open():
    hitl = HumanInTheLoop(mode=HITLMode.ALWAYS, ask=None)
    answer = hitl.consult(_query())
    assert answer.asked is False
    assert answer.source == "auto"


def test_custom_auto_decide_used_on_timeout():
    hitl = HumanInTheLoop(
        mode=HITLMode.ALWAYS, ask=lambda q, t: None, auto_decide=lambda q: "自定义决策"
    )
    answer = hitl.consult(_query())
    assert answer.text == "自定义决策"


# --------------------------------------------------------------------------
# 建议回灌记忆（E4）
# --------------------------------------------------------------------------

def test_user_suggestion_persisted_to_memory(evo_home):
    mem = EvolutionMemory()
    hitl = HumanInTheLoop(mode=HITLMode.AUTO, ask=lambda q, t: "路线B", memory=mem)
    hitl.consult(_query(confidence=0.1))
    insights = mem.insights(active_only=True)
    assert len(insights) == 1
    assert insights[0].confidence == 0.95
    assert "user" in insights[0].tags
    assert "路线B" in insights[0].text


def test_timeout_not_persisted(evo_home):
    mem = EvolutionMemory()
    hitl = HumanInTheLoop(mode=HITLMode.ALWAYS, ask=lambda q, t: None, memory=mem)
    hitl.consult(_query())
    assert mem.stats()["insights_active"] == 0


# --------------------------------------------------------------------------
# query 渲染 / answer 序列化 / 便捷构造
# --------------------------------------------------------------------------

def test_query_render_includes_options_and_context():
    q = HumanQuery(question="选路线？", options=["A", "B"], context="背景说明")
    text = q.render()
    assert "选路线？" in text and "A" in text and "背景说明" in text


def test_answer_to_dict():
    d = HumanAnswer(text="x", answered=True, asked=True, source="user").to_dict()
    assert d["source"] == "user" and d["answered"] is True


def test_make_hitl_convenience():
    hitl = make_hitl(mode="never")
    assert isinstance(hitl, HumanInTheLoop)
    assert hitl.mode is HITLMode.NEVER
