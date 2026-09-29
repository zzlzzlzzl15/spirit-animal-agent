"""tests/profile/test_describer.py — 注入式 LLM 自动描述。

覆盖 ``spirit.profile.describer``：正常 JSON 回复、围栏包裹、裸文本兜底、空回复、
缺字段、LLM 抛错、无 caller 降级、覆盖保护、技能采样/封顶、_extract_json_blob、
list_describable_profiles。全部离线（FakeCaller）。
"""

from __future__ import annotations

from spirit.profile.describer import (
    MAX_SKILLS_FOR_PROMPT,
    _collect_skills,
    _extract_json_blob,
    describe_profile,
    list_describable_profiles,
)
from spirit.profile.manager import create_profile, read_profile_meta
from tests.profile.conftest import FakeCaller, add_skill, write_config_yaml


# ---------------------------------------------------------------------------
# _extract_json_blob
# ---------------------------------------------------------------------------

def test_extract_json_plain():
    assert _extract_json_blob('{"description": "hi"}') == {"description": "hi"}


def test_extract_json_fenced():
    raw = '```json\n{"description": "hi"}\n```'
    assert _extract_json_blob(raw) == {"description": "hi"}


def test_extract_json_embedded_in_prose():
    raw = 'Sure! Here you go:\n{"description": "hi"}\nHope that helps.'
    assert _extract_json_blob(raw) == {"description": "hi"}


def test_extract_json_invalid():
    assert _extract_json_blob("no json here") is None
    assert _extract_json_blob("") is None
    assert _extract_json_blob("[1, 2, 3]") is None  # 非对象


# ---------------------------------------------------------------------------
# _collect_skills
# ---------------------------------------------------------------------------

def test_collect_skills_category_and_flat(profile_home):
    p = create_profile("work")
    add_skill(p, "devops", "deploy")
    add_skill(p, "research", "search")
    names = _collect_skills(p)
    assert "devops/deploy" in names
    assert "research/search" in names


def test_collect_skills_empty(profile_home):
    p = create_profile("work")
    # 引导时建了空 skills/ 目录
    assert _collect_skills(p) == []


def test_collect_skills_capped_and_sampled(profile_home):
    """超过上限时等距采样，返回恰好 MAX_SKILLS_FOR_PROMPT 个。"""
    p = create_profile("work")
    for i in range(MAX_SKILLS_FOR_PROMPT + 40):
        add_skill(p, "cat", f"skill{i:03d}")
    names = _collect_skills(p)
    assert len(names) == MAX_SKILLS_FOR_PROMPT
    # 等距采样应保留首尾附近的样本（不是纯取头部）
    assert names[0] == "cat/skill000"


# ---------------------------------------------------------------------------
# describe_profile — 成功路径
# ---------------------------------------------------------------------------

def test_describe_writes_auto_meta(profile_home):
    p = create_profile("work")
    write_config_yaml(p, "qwen3-max", "alibaba")
    add_skill(p, "quant", "backtest")
    caller = FakeCaller('{"description": "Runs quantitative backtests on A-share data."}')

    outcome = describe_profile("work", llm_caller=caller)
    assert outcome.ok is True
    assert outcome.description == "Runs quantitative backtests on A-share data."

    meta = read_profile_meta(p)
    assert meta["description"] == "Runs quantitative backtests on A-share data."
    assert meta["description_auto"] is True


def test_describe_prompt_contains_signals(profile_home):
    p = create_profile("work")
    write_config_yaml(p, "gpt-x", "openai")
    add_skill(p, "devops", "deploy")
    caller = FakeCaller()

    describe_profile("work", llm_caller=caller)
    user_msg = caller.last_user_msg
    assert "work" in user_msg
    assert "gpt-x" in user_msg
    assert "openai" in user_msg
    assert "devops/deploy" in user_msg


def test_describe_truncates_long_description(profile_home):
    create_profile("work")
    long = "x" * 500
    caller = FakeCaller('{"description": "%s"}' % long)
    outcome = describe_profile("work", llm_caller=caller)
    assert outcome.ok is True
    assert len(outcome.description) == 280


def test_describe_fenced_json(profile_home):
    create_profile("work")
    caller = FakeCaller('```json\n{"description": "fenced ok"}\n```')
    outcome = describe_profile("work", llm_caller=caller)
    assert outcome.ok is True
    assert outcome.description == "fenced ok"


def test_describe_falls_back_to_raw_text(profile_home):
    """非 JSON 回复 → 取首段作为描述。"""
    create_profile("work")
    caller = FakeCaller("Just a plain prose description.\n\nSecond paragraph ignored.")
    outcome = describe_profile("work", llm_caller=caller)
    assert outcome.ok is True
    assert outcome.description == "Just a plain prose description."


# ---------------------------------------------------------------------------
# describe_profile — 失败/降级路径（绝不抛）
# ---------------------------------------------------------------------------

def test_describe_no_caller_degrades(profile_home):
    create_profile("work")
    outcome = describe_profile("work", llm_caller=None)
    assert outcome.ok is False
    assert "no llm_caller" in outcome.reason


def test_describe_missing_profile(profile_home):
    caller = FakeCaller()
    outcome = describe_profile("ghost", llm_caller=caller)
    assert outcome.ok is False
    assert outcome.reason == "profile not found"
    assert caller.call_count == 0


def test_describe_llm_error(profile_home):
    create_profile("work")
    caller = FakeCaller(RuntimeError("boom"))
    outcome = describe_profile("work", llm_caller=caller)
    assert outcome.ok is False
    assert "LLM error" in outcome.reason


def test_describe_empty_response(profile_home):
    create_profile("work")
    caller = FakeCaller("   ")
    outcome = describe_profile("work", llm_caller=caller)
    assert outcome.ok is False
    assert "empty" in outcome.reason


def test_describe_missing_field(profile_home):
    create_profile("work")
    caller = FakeCaller('{"not_description": "oops"}')
    outcome = describe_profile("work", llm_caller=caller)
    assert outcome.ok is False
    assert "missing 'description'" in outcome.reason


# ---------------------------------------------------------------------------
# 覆盖保护
# ---------------------------------------------------------------------------

def test_describe_respects_user_description(profile_home):
    p = create_profile("work", description="用户手写的描述")
    caller = FakeCaller('{"description": "auto attempt"}')
    outcome = describe_profile("work", llm_caller=caller)
    assert outcome.ok is False
    assert "user-authored" in outcome.reason
    assert caller.call_count == 0  # 未浪费一次 LLM 调用
    # 原描述保持不变
    assert read_profile_meta(p)["description"] == "用户手写的描述"


def test_describe_overwrite_replaces_user_description(profile_home):
    p = create_profile("work", description="用户手写的描述")
    caller = FakeCaller('{"description": "auto wins"}')
    outcome = describe_profile("work", llm_caller=caller, overwrite=True)
    assert outcome.ok is True
    meta = read_profile_meta(p)
    assert meta["description"] == "auto wins"
    assert meta["description_auto"] is True


def test_describe_replaces_previous_auto(profile_home):
    """自动生成的描述始终可被替换（无需 overwrite）。"""
    create_profile("work")
    describe_profile("work", llm_caller=FakeCaller('{"description": "first"}'))
    outcome = describe_profile("work", llm_caller=FakeCaller('{"description": "second"}'))
    assert outcome.ok is True
    assert outcome.description == "second"


# ---------------------------------------------------------------------------
# default profile 也可描述
# ---------------------------------------------------------------------------

def test_describe_default_profile(profile_home):
    caller = FakeCaller('{"description": "the default profile"}')
    outcome = describe_profile("default", llm_caller=caller)
    assert outcome.ok is True
    assert outcome.profile_name == "default"


# ---------------------------------------------------------------------------
# list_describable_profiles
# ---------------------------------------------------------------------------

def test_list_describable_missing_only(profile_home):
    create_profile("work", description="已有手写描述")
    create_profile("finance")
    # missing_only=True：跳过有用户手写描述的 work，保留 default + finance
    names = set(list_describable_profiles(missing_only=True))
    assert "finance" in names
    assert "work" not in names


def test_list_describable_all(profile_home):
    create_profile("work", description="已有手写描述")
    create_profile("finance")
    names = set(list_describable_profiles(missing_only=False))
    assert {"default", "work", "finance"} <= names
