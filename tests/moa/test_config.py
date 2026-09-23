"""MoA 预设配置归一化 / 校验测试 — 对标 Hermes ``tests/hermes_cli/test_moa_config.py``。

覆盖 ``spirit.moa.config`` 的读时容错归一化（``normalize_moa_config``）、精确预设解析
（``resolve_moa_preset`` / ``exact_moa_preset_name``）、写时严格校验
（``validate_moa_payload``）、递归 MoA 守卫，以及 Spirit 特有的 slot→运行时解析
（``resolve_slot_runtime``）与 fan-out 节奏归一化。

与 Hermes 的差异：Spirit 无 ``build_moa_turn_prompt`` / ``decode_moa_turn``（Spirit 用
``MoAClient`` facade + 运行时 provider 切换，而非编码 prompt）；缺失预设的错误消息提示
``/moa list``（而非 ``hermes moa list``）。
"""

from __future__ import annotations

import pytest

from spirit.moa.config import (
    DEFAULT_MOA_AGGREGATOR,
    DEFAULT_MOA_PRESET_NAME,
    DEFAULT_MOA_REFERENCE_MODELS,
    MoAPresetNotFoundError,
    exact_moa_preset_name,
    normalize_moa_config,
    resolve_moa_preset,
    resolve_slot_runtime,
    set_active_moa_preset,
    validate_moa_payload,
)


# ---------------------------------------------------------------------------
# normalize_moa_config — 读时容错归一化
# ---------------------------------------------------------------------------

def test_normalize_moa_config_uses_default_named_preset():
    cfg = normalize_moa_config({})

    assert cfg["default_preset"] == DEFAULT_MOA_PRESET_NAME
    assert list(cfg["presets"]) == [DEFAULT_MOA_PRESET_NAME]
    assert cfg["reference_models"] == DEFAULT_MOA_REFERENCE_MODELS
    assert cfg["aggregator"] == DEFAULT_MOA_AGGREGATOR


def test_normalize_moa_config_preserves_named_presets():
    cfg = normalize_moa_config(
        {
            "default_preset": "coding",
            "presets": {
                "coding": {
                    "reference_models": [{"provider": "openai-codex", "model": "gpt-5.5"}],
                    "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
                },
                "review": {
                    "reference_models": [{"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"}],
                    "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
                },
            },
        }
    )

    assert cfg["default_preset"] == "coding"
    assert set(cfg["presets"]) == {"coding", "review"}
    # 扁平兼容视图暴露 default 预设（coding）的 reference models。
    assert cfg["reference_models"] == [{"provider": "openai-codex", "model": "gpt-5.5"}]


def test_legacy_flat_config_becomes_default_preset():
    cfg = normalize_moa_config(
        {
            "reference_models": [{"provider": "openai-codex", "model": "gpt-5.5"}],
            "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
        }
    )

    assert cfg["presets"][DEFAULT_MOA_PRESET_NAME]["reference_models"] == [
        {"provider": "openai-codex", "model": "gpt-5.5"}
    ]


def test_normalize_moa_config_tolerates_non_numeric_values():
    """手编 config.yaml 里的非数值字符串必须降级为默认，而非用 ValueError 崩溃。"""
    cfg = normalize_moa_config(
        {
            "presets": {
                "broken": {
                    "max_tokens": "notanumber",
                    "reference_temperature": "hot",
                    "aggregator_temperature": "",
                }
            }
        }
    )

    preset = cfg["presets"]["broken"]
    assert preset["max_tokens"] == 4096
    # 无法解析 / 空白的温度降级为 None = "不下发该参数；用 provider 默认"
    # （匹配单模型行为），而非某个硬编码采样值。
    assert preset["reference_temperature"] is None
    assert preset["aggregator_temperature"] is None


def test_normalize_moa_config_tolerates_non_list_reference_models():
    """手编的标量 reference_models 必须降级为默认，而非用 TypeError 崩溃。"""
    cfg = normalize_moa_config({"presets": {"broken": {"reference_models": 2}}})
    assert cfg["presets"]["broken"]["reference_models"] == DEFAULT_MOA_REFERENCE_MODELS


def test_normalize_moa_config_wraps_bare_dict_reference_models():
    """没写 list 包裹的单个参考 slot 被救回。"""
    cfg = normalize_moa_config(
        {"presets": {"p": {"reference_models": {"provider": "openai", "model": "gpt-4o"}}}}
    )
    assert cfg["presets"]["p"]["reference_models"] == [{"provider": "openai", "model": "gpt-4o"}]


def test_normalize_moa_config_preserves_slot_reasoning_effort():
    cfg = normalize_moa_config(
        {
            "presets": {
                "p": {
                    "reference_models": [
                        {"provider": "openai-codex", "model": "gpt-5.6-sol", "reasoning_effort": "LOW"},
                        {"provider": "openai-codex", "model": "gpt-5.6-sol", "reasoning_effort": False},
                        {"provider": "openai-codex", "model": "gpt-5.6-sol", "reasoning_effort": "nonsense"},
                        {"provider": "openai-codex", "model": "gpt-5.6-sol", "reasoning_effort": "ultra"},
                    ],
                    "aggregator": {"provider": "openai-codex", "model": "gpt-5.6-sol", "reasoning_effort": "xhigh"},
                }
            }
        }
    )

    preset = cfg["presets"]["p"]
    assert preset["reference_models"][0]["reasoning_effort"] == "low"
    assert preset["reference_models"][1]["reasoning_effort"] == "none"
    assert "reasoning_effort" not in preset["reference_models"][2]
    assert preset["reference_models"][3]["reasoning_effort"] == "ultra"
    assert preset["aggregator"]["reasoning_effort"] == "xhigh"


def test_normalize_moa_config_coerces_numeric_strings():
    """合法的数值字符串（如 YAML round-trip 产生的）必须正确强转。"""
    cfg = normalize_moa_config({"max_tokens": "8192", "reference_temperature": "0.9"})

    preset = cfg["presets"][DEFAULT_MOA_PRESET_NAME]
    assert preset["max_tokens"] == 8192
    assert preset["reference_temperature"] == 0.9


def test_normalize_moa_config_coerces_float_max_tokens():
    """max_tokens: 4096.0（YAML 来的 float）必须强转为 int。"""
    cfg = normalize_moa_config({"max_tokens": 4096.0})
    assert cfg["presets"][DEFAULT_MOA_PRESET_NAME]["max_tokens"] == 4096

    cfg2 = normalize_moa_config({"max_tokens": "4096.5"})
    assert cfg2["presets"][DEFAULT_MOA_PRESET_NAME]["max_tokens"] == 4096


# ---------------------------------------------------------------------------
# fan-out 节奏归一化（Spirit 特有：per_iteration / user_turn）
# ---------------------------------------------------------------------------

def test_fanout_defaults_to_per_iteration():
    cfg = normalize_moa_config({"presets": {"p": {}}})
    assert cfg["presets"]["p"]["fanout"] == "per_iteration"


def test_fanout_preserves_user_turn():
    cfg = normalize_moa_config({"presets": {"p": {"fanout": "user_turn"}}})
    assert cfg["presets"]["p"]["fanout"] == "user_turn"


def test_fanout_unknown_value_falls_back():
    cfg = normalize_moa_config({"presets": {"p": {"fanout": "sometimes"}}})
    assert cfg["presets"]["p"]["fanout"] == "per_iteration"


# ---------------------------------------------------------------------------
# exact_moa_preset_name — 隐式裸名匹配（供 /model <preset> 切换路径）
# ---------------------------------------------------------------------------

def test_exact_preset_matching_is_not_fuzzy():
    config = {"presets": {"coding": {}, "review": {}}}

    assert exact_moa_preset_name(config, "coding") == "coding"
    assert exact_moa_preset_name(config, "cod") is None
    assert exact_moa_preset_name(config, "coding please fix this") is None


def test_exact_preset_matching_skips_disabled_presets():
    """禁用的预设不得匹配隐式裸名切换路径。

    回归守卫： With ``enabled: false`` 预设，一个名字恰好与预设键冲突（如
    ``default``）的普通模型切换不得把会话静默拐到 MoA 虚拟 provider 上。per-preset
    的 ``enabled`` 退出必须门控这个隐式匹配。
    """
    config = {
        "presets": {
            "default": {"enabled": False},
            "klo": {"enabled": False},
        },
    }
    assert exact_moa_preset_name(config, "default") is None
    assert exact_moa_preset_name(config, "klo") is None


def test_exact_preset_matching_allows_enabled_presets():
    """显式启用的预设仍匹配裸名切换路径。"""
    config = {
        "presets": {
            "fast": {"enabled": True},
            "slow": {"enabled": False},
        },
    }
    assert exact_moa_preset_name(config, "fast") == "fast"
    assert exact_moa_preset_name(config, "slow") is None
    # 默认（无显式 enabled 键）即启用，仍匹配。
    assert exact_moa_preset_name({"presets": {"x": {}}}, "x") == "x"


# ---------------------------------------------------------------------------
# set_active_moa_preset / resolve_moa_preset
# ---------------------------------------------------------------------------

def test_active_preset_toggle_validation():
    config = {"default_preset": "coding", "presets": {"coding": {}, "review": {}}}

    active = set_active_moa_preset(config, "review")
    assert active["active_preset"] == "review"

    inactive = set_active_moa_preset(active, "")
    assert inactive["active_preset"] == ""


def test_set_active_unknown_preset_raises():
    config = {"default_preset": "coding", "presets": {"coding": {}}}
    with pytest.raises(KeyError):
        set_active_moa_preset(config, "nope")


def test_resolve_moa_preset_returns_requested_model_set():
    cfg = normalize_moa_config(
        {
            "presets": {
                "coding": {"reference_models": [{"provider": "openai-codex", "model": "gpt-5.5"}]},
                "review": {"reference_models": [{"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"}]},
            }
        }
    )

    assert resolve_moa_preset(cfg, "review")["reference_models"] == [
        {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"}
    ]


def test_resolve_missing_moa_preset_has_actionable_error():
    cfg = {
        "default_preset": "日常对话-高峰",
        "presets": {"日常对话-高峰": {}, "日常对话-非高峰": {}},
    }

    with pytest.raises(MoAPresetNotFoundError) as exc_info:
        resolve_moa_preset(cfg, "日常对话-高峰期")

    message = str(exc_info.value)
    assert "日常对话-高峰期" in message
    assert "日常对话-高峰" in message
    assert "日常对话-非高峰" in message
    # Spirit 提示 /moa list（对标 Hermes 的 hermes moa list）。
    assert "/moa list" in message


def test_resolve_missing_moa_preset_does_not_silently_fallback():
    cfg = {
        "default_preset": "日常对话-高峰",
        "presets": {"日常对话-高峰": {}},
    }

    with pytest.raises(MoAPresetNotFoundError):
        resolve_moa_preset(cfg, "renamed-preset")


# ---------------------------------------------------------------------------
# 递归 MoA 守卫：moa provider 不得作为参考 / 聚合器 slot
# ---------------------------------------------------------------------------

def test_moa_provider_rejected_as_reference_slot():
    """指向 moa 虚拟 provider 的参考 slot 被丢弃，故预设不能递归引用另一次 MoA。"""
    cfg = normalize_moa_config(
        {
            "presets": {
                "p": {
                    "reference_models": [
                        {"provider": "moa", "model": "default"},
                        {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
                    ],
                    "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
                }
            }
        }
    )

    refs = cfg["presets"]["p"]["reference_models"]
    assert {"provider": "moa", "model": "default"} not in refs
    assert refs == [{"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"}]


def test_moa_provider_rejected_as_aggregator_slot():
    """指向 moa 虚拟 provider 的聚合器 slot 被丢弃并回退默认聚合器，绝不递归 MoA。"""
    cfg = normalize_moa_config(
        {
            "presets": {
                "p": {
                    "reference_models": [{"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"}],
                    "aggregator": {"provider": "moa", "model": "default"},
                }
            }
        }
    )

    agg = cfg["presets"]["p"]["aggregator"]
    assert agg["provider"] != "moa"
    assert agg == DEFAULT_MOA_AGGREGATOR


def test_moa_provider_rejected_case_insensitive():
    """``MoA`` 等大小写变体也被拦截。"""
    cfg = normalize_moa_config(
        {"presets": {"p": {"aggregator": {"provider": "MoA", "model": "default"}}}}
    )

    assert cfg["presets"]["p"]["aggregator"]["provider"] != "moa"
    assert cfg["presets"]["p"]["aggregator"] == DEFAULT_MOA_AGGREGATOR


# ---------------------------------------------------------------------------
# reference_max_tokens — advisor 输出可选封顶
# ---------------------------------------------------------------------------

def _preset(**extra):
    base = {
        "reference_models": [{"provider": "openrouter", "model": "anthropic/claude-opus-4.8"}],
        "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
    }
    base.update(extra)
    return {"default_preset": "p", "presets": {"p": base}}


def test_reference_max_tokens_defaults_to_none_uncapped():
    """未设的 reference_max_tokens 解析为 None（不封顶），现有预设保持uncapped。"""
    p = resolve_moa_preset(_preset(), "p")
    assert p["reference_max_tokens"] is None


def test_reference_max_tokens_positive_value_preserved():
    p = resolve_moa_preset(_preset(reference_max_tokens=600), "p")
    assert p["reference_max_tokens"] == 600


def test_reference_max_tokens_invalid_falls_back_to_none():
    """非正 / 非数值的封顶降级为 None（uncapped），而非钳到无意义值或崩溃。"""
    for bad in (0, -5, "abc", "", None):
        p = resolve_moa_preset(_preset(reference_max_tokens=bad), "p")
        assert p["reference_max_tokens"] is None, bad


def test_reference_max_tokens_string_number_coerced():
    p = resolve_moa_preset(_preset(reference_max_tokens="600"), "p")
    assert p["reference_max_tokens"] == 600


def test_reference_max_tokens_in_flattened_view():
    """扁平兼容视图（dashboard/desktop 调用方）暴露激活预设的 reference_max_tokens。"""
    cfg = normalize_moa_config(_preset(reference_max_tokens=750))
    assert cfg["reference_max_tokens"] == 750


# ---------------------------------------------------------------------------
# validate_moa_payload — 写边界严格校验
#
# normalize_moa_config 在读时故意容错（手编配置降级为默认）。validate_moa_payload 是
# 写时的严格对应物：它必须精确标出 normalize 会静默修复的 payload，使 API 保存路径
# 拒绝它们而非损坏用户配置。
# ---------------------------------------------------------------------------

def _valid_preset_payload():
    return {
        "reference_models": [{"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"}],
        "aggregator": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
    }


def test_validate_moa_payload_accepts_complete_presets():
    assert validate_moa_payload({"presets": {"default": _valid_preset_payload()}}) == []


def test_validate_moa_payload_accepts_legacy_flat_payload():
    assert validate_moa_payload(_valid_preset_payload()) == []


def test_validate_moa_payload_flags_half_filled_reference_slot():
    """provider 选了、model 还空着（编辑中途自动保存）的形状。"""
    preset = _valid_preset_payload()
    preset["reference_models"].append({"provider": "kilo", "model": ""})
    problems = validate_moa_payload({"presets": {"default": preset}})

    assert problems
    assert any("reference 2" in p and "model is required" in p for p in problems)


def test_validate_moa_payload_flags_half_filled_aggregator():
    preset = _valid_preset_payload()
    preset["aggregator"] = {"provider": "openrouter", "model": ""}
    problems = validate_moa_payload({"presets": {"default": preset}})

    assert any("aggregator" in p and "model is required" in p for p in problems)


def test_validate_moa_payload_flags_empty_references():
    preset = _valid_preset_payload()
    preset["reference_models"] = []
    problems = validate_moa_payload({"presets": {"default": preset}})

    assert any("at least one complete reference model" in p for p in problems)


def test_validate_moa_payload_flags_recursive_moa_slot():
    preset = _valid_preset_payload()
    preset["aggregator"] = {"provider": "MoA", "model": "default"}
    problems = validate_moa_payload({"presets": {"default": preset}})

    assert any("recursive MoA" in p for p in problems)


def test_validate_moa_payload_names_the_broken_preset():
    """多预设 payload 必须说清哪个预设坏了。"""
    problems = validate_moa_payload(
        {
            "presets": {
                "good": _valid_preset_payload(),
                "broken": {
                    "reference_models": [{"provider": "", "model": ""}],
                    "aggregator": {"provider": "a", "model": "b"},
                },
            }
        }
    )

    assert problems
    assert all("'broken'" in p for p in problems)
    assert not any("'good'" in p for p in problems)


def test_validate_moa_payload_agrees_with_clean_slot():
    """契约：validate 接受的 payload 必须在 normalize 后 slot 原样存活——validate 与
    _clean_slot 永不能分歧（否则一个 payload 可能通过校验却仍被换成默认）。"""
    payload = {"presets": {"p": _valid_preset_payload()}}
    assert validate_moa_payload(payload) == []

    cfg = normalize_moa_config(payload)
    assert cfg["presets"]["p"]["reference_models"] == payload["presets"]["p"]["reference_models"]
    assert cfg["presets"]["p"]["aggregator"] == payload["presets"]["p"]["aggregator"]


def test_validate_moa_payload_rejects_non_dict():
    assert validate_moa_payload(None)
    assert validate_moa_payload([1, 2])
    assert validate_moa_payload({"presets": {"p": "not-a-dict"}})


# ---------------------------------------------------------------------------
# resolve_slot_runtime — Spirit 特有：slot → transport 运行时
# ---------------------------------------------------------------------------

def test_resolve_slot_runtime_uses_provider_base_url():
    """已知 provider 解析出其真实 base_url（用 Spirit 的 PROVIDER_BASE_URLS 映射）。"""
    from spirit.config import PROVIDER_BASE_URLS

    rt = resolve_slot_runtime({"provider": "openai", "model": "gpt-4o"})
    assert rt["provider"] == "openai"
    assert rt["model"] == "gpt-4o"
    assert rt.get("base_url") == PROVIDER_BASE_URLS.get("openai")


def test_resolve_slot_runtime_explicit_override_wins():
    """slot 自带 base_url/api_key 时优先（用户显式覆盖）。"""
    rt = resolve_slot_runtime(
        {"provider": "openai", "model": "m", "base_url": "http://custom/v1", "api_key": "sk-custom"}
    )
    assert rt["base_url"] == "http://custom/v1"
    assert rt["api_key"] == "sk-custom"


def test_resolve_slot_runtime_falls_back_on_unknown_provider():
    """未知 provider 回退到裸 provider/model，而非中断整个 MoA 轮。"""
    rt = resolve_slot_runtime({"provider": "mystery", "model": "x"})
    assert rt["provider"] == "mystery"
    assert rt["model"] == "x"
    # 未知 provider 无 base_url 映射，故不带 base_url 键（让 transport 自行处理）。
    assert "base_url" not in rt or rt["base_url"] == ""
