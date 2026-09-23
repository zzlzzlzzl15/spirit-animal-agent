"""MoA 预设配置 — Spirit Agent（Phase 4.1）。

对标 Hermes ``hermes_cli/moa_config.py``：把用户手写/前端下发的 ``moa`` 配置
归一化成带命名预设（presets）的结构，并在读时**容错降级到默认**（永不因坏配置
崩溃），在写时用 :func:`validate_moa_payload` **严格校验**（拒绝半填的 slot，
避免把用户没选过的默认值静默写回）。

一个「slot」就是一个模型选择：``{"provider": ..., "model": ..., 可选
"reasoning_effort": ...}``。:func:`resolve_slot_runtime` 把 slot 解析成
``create_transport`` 需要的真实运行时（base_url/api_key），等价 Hermes 的
``resolve_runtime_provider``——用 Spirit 自己的 ``PROVIDER_BASE_URLS`` /
``PROVIDER_KEY_ENV`` 映射。
"""

from __future__ import annotations

import logging
import os
from copy import deepcopy
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_MOA_PRESET_NAME = "default"

DEFAULT_MOA_REFERENCE_MODELS: List[Dict[str, str]] = [
    {"provider": "openrouter", "model": "deepseek/deepseek-v3"},
    {"provider": "openai", "model": "gpt-4o"},
]

DEFAULT_MOA_AGGREGATOR: Dict[str, str] = {
    "provider": "anthropic", "model": "claude-3-5-sonnet",
}

# slot 可接受的推理努力档位（对齐 Spirit agent.reasoning_effort 取值 + 兼容档）。
_VALID_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "ultra"}


class MoAPresetNotFoundError(Exception):
    """请求的 MoA 预设不存在（不可重试：换一个存在的预设名）。"""


# ---------------------------------------------------------------------------
# 标量容错强转
# ---------------------------------------------------------------------------

def _coerce_float_or_none(value: Any) -> Optional[float]:
    """强转为 float；未设/空白/非法时为 None（= 不下发该参数，用 provider 默认）。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _coerce_int_or_none(value: Any) -> Optional[int]:
    """强转为正整数；未设/空白/非法/非正数时为 None（= 不封顶）。"""
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        try:
            n = int(float(value))
        except (TypeError, ValueError):
            return None
    return n if n > 0 else None


def _coerce_fanout(value: Any) -> str:
    """归一化 fan-out 节奏；未知值回退默认 per_iteration。"""
    mode = str(value or "").strip().lower()
    return mode if mode in {"per_iteration", "user_turn"} else "per_iteration"


def _clean_reasoning_effort(value: Any) -> Optional[str]:
    """返回规范化的 per-slot 推理努力档；未设/非法时 None。

    对齐 Hermes ``parse_reasoning_effort``：``False`` → "none"（显式关闭），
    ``True``/None → None（不覆盖，用 provider/全局默认），大小写归一，
    无法识别的字符串丢弃（回退默认，绝不因坏值崩溃）。
    """
    if value is None or value is True:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    if s in ("false", "0", "off"):
        return "none"
    if s in _VALID_EFFORTS:
        return s
    return None


# ---------------------------------------------------------------------------
# slot 清洗 / 校验
# ---------------------------------------------------------------------------

def _clean_slot(slot: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(slot, dict):
        return None
    provider = str(slot.get("provider") or "").strip()
    model = str(slot.get("model") or "").strip()
    if not provider or not model:
        return None
    # moa 是虚拟 provider，其预设本身就是一次 MoA。允许它当参考/聚合器会形成
    # 递归 MoA 树（运行时守卫只能中途拦截），故在此直接拒绝：非法 slot 被丢弃，
    # 回退到预设默认。
    if provider.lower() == "moa":
        return None
    clean: Dict[str, Any] = {"provider": provider, "model": model}
    effort = _clean_reasoning_effort(slot.get("reasoning_effort"))
    if effort:
        clean["reasoning_effort"] = effort
    return clean


def _slot_problem(slot: Any) -> Optional[str]:
    """返回 ``_clean_slot`` 会丢弃的 slot 的人类可读问题；None 表示完整合法。

    与 ``_clean_slot`` 逐条对齐，确保写边界校验器与容错运行时归一器对「什么可
    接受」永不产生分歧。
    """
    if not isinstance(slot, dict):
        return "must be an object with 'provider' and 'model'"
    provider = str(slot.get("provider") or "").strip()
    model = str(slot.get("model") or "").strip()
    if not provider and not model:
        return "provider and model are required"
    if not provider:
        return "provider is required"
    if not model:
        return f"model is required (provider '{provider}' has no model selected)"
    if provider.lower() == "moa":
        return "the Mixture of Agents provider cannot be used inside a preset (recursive MoA)"
    return None


def validate_moa_payload(raw: Any) -> List[str]:
    """返回 ``normalize_moa_config`` 会静默修复的问题清单（空 = 可安全保存）。

    ``normalize_moa_config`` 在读时故意容错（坏配置降级为默认，不崩溃）；同样的
    容错放到写时就是「配置损坏引擎」——半填的 slot 会让整个预设被静默换成硬编码
    默认。API 写路径先调本函数， loudly 拒绝而非保存用户从未选择的东西。
    """
    if not isinstance(raw, dict):
        return ["MoA config must be an object"]

    presets_raw = raw.get("presets")
    if isinstance(presets_raw, dict) and presets_raw:
        presets: Dict[Any, Any] = presets_raw
    else:
        presets = {DEFAULT_MOA_PRESET_NAME: raw}

    problems: List[str] = []
    for name, preset in presets.items():
        label = str(name or "").strip() or "(unnamed)"
        if not isinstance(preset, dict):
            problems.append(f"preset '{label}': must be an object")
            continue

        refs = preset.get("reference_models")
        if not isinstance(refs, list):
            refs = [refs] if isinstance(refs, dict) else []
        complete_refs = 0
        for index, slot in enumerate(refs):
            issue = _slot_problem(slot)
            if issue:
                problems.append(f"preset '{label}' reference {index + 1}: {issue}")
            else:
                complete_refs += 1
        if not complete_refs:
            problems.append(f"preset '{label}': needs at least one complete reference model")

        agg_issue = _slot_problem(preset.get("aggregator"))
        if agg_issue:
            problems.append(f"preset '{label}' aggregator: {agg_issue}")

    return problems


# ---------------------------------------------------------------------------
# 预设归一化
# ---------------------------------------------------------------------------

def _default_preset() -> Dict[str, Any]:
    return {
        "reference_models": deepcopy(DEFAULT_MOA_REFERENCE_MODELS),
        "aggregator": deepcopy(DEFAULT_MOA_AGGREGATOR),
        # None = 温度不出现在 API 调用里（用 provider 默认），对齐单模型 Agent。
        "reference_temperature": None,
        "aggregator_temperature": None,
        "max_tokens": 4096,
        "reference_max_tokens": None,
        "fanout": "per_iteration",
        "enabled": True,
    }


def _normalize_preset(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}

    raw_refs = raw.get("reference_models")
    if not isinstance(raw_refs, list):
        # 手写的标量 / 单映射（或坏类型）必须降级为默认而非崩溃迭代，
        # 与下面标量字段（temperature / max_tokens）的容错对称。
        raw_refs = [raw_refs] if isinstance(raw_refs, dict) else []
    refs = [_clean_slot(item) for item in raw_refs]
    refs = [item for item in refs if item is not None]
    if not refs:
        refs = deepcopy(DEFAULT_MOA_REFERENCE_MODELS)

    aggregator = _clean_slot(raw.get("aggregator")) or deepcopy(DEFAULT_MOA_AGGREGATOR)

    return {
        "enabled": bool(raw.get("enabled", True)),
        "reference_models": refs,
        "aggregator": aggregator,
        "reference_temperature": _coerce_float_or_none(raw.get("reference_temperature")),
        "aggregator_temperature": _coerce_float_or_none(raw.get("aggregator_temperature")),
        "max_tokens": _coerce_int(raw.get("max_tokens"), 4096),
        # 每个参考 ADVISOR 每轮可生成的可选上限。None（默认）= 不封顶。
        # 设一个值（如 600）让 advisor 给精简建议——MoA 主要延迟来自 advisor
        # 生成，聚合器只需每条建议的要旨，故封顶可显著降低每轮墙钟时间。
        # **不**封顶 acting 聚合器（其输出是用户可见答案）。
        "reference_max_tokens": _coerce_int_or_none(raw.get("reference_max_tokens")),
        # 参考 fan-out 何时运行。"per_iteration"（默认）在 advisory 视图变化时
        # 重跑（即每个工具迭代，让建议跟随实时任务状态）；"user_turn" 每用户轮
        # 只跑一次（原始 MoA 形态）。
        "fanout": _coerce_fanout(raw.get("fanout")),
    }


def normalize_moa_config(raw: Any) -> Dict[str, Any]:
    """返回带命名预设的、已校验的 MoA 配置。

    向后兼容首个版本的扁平形态：``moa`` 直接含 ``reference_models`` /
    ``aggregator``（无 ``presets``）时，整体成为 default 预设。
    """
    if not isinstance(raw, dict):
        raw = {}

    presets_raw = raw.get("presets")
    presets: Dict[str, Dict[str, Any]] = {}
    if isinstance(presets_raw, dict):
        for name, preset in presets_raw.items():
            clean_name = str(name or "").strip()
            if clean_name:
                presets[clean_name] = _normalize_preset(preset)

    # 扁平 legacy 配置成为 default 预设。
    if not presets:
        presets[DEFAULT_MOA_PRESET_NAME] = _normalize_preset(raw)

    default_name = str(raw.get("default_preset") or "").strip()
    if not default_name or default_name not in presets:
        default_name = next(iter(presets), DEFAULT_MOA_PRESET_NAME)
    if default_name not in presets:
        presets[default_name] = _default_preset()

    active_name = str(raw.get("active_preset") or "").strip()
    if active_name not in presets:
        active_name = ""

    active = presets[default_name]
    return {
        "default_preset": default_name,
        "active_preset": active_name,
        "presets": presets,
        # 追踪开关（顶层，非 per-preset）。
        "save_traces": bool(raw.get("save_traces", False)),
        "trace_dir": raw.get("trace_dir") or None,
        # 兼容/扁平视图，供 dashboard/desktop 调用方直接读。
        "reference_models": deepcopy(active["reference_models"]),
        "aggregator": deepcopy(active["aggregator"]),
        "reference_temperature": active["reference_temperature"],
        "aggregator_temperature": active["aggregator_temperature"],
        "max_tokens": active["max_tokens"],
        "reference_max_tokens": active.get("reference_max_tokens"),
        "fanout": active.get("fanout", "per_iteration"),
        "enabled": active["enabled"],
    }


def resolve_moa_preset(config: Any, name: Optional[str] = None) -> Dict[str, Any]:
    """按名字取一个预设（deepcopy）；缺失时抛 :class:`MoAPresetNotFoundError`。

    绝不静默回退到别的预设——改名/删预设后仍命中旧名会让用户以为在跑 A 实际跑 B。
    错误消息列出所有可用预设名 + 提示命令，可操作。
    """
    cfg = normalize_moa_config(config)
    preset_name = str(name or cfg.get("default_preset") or DEFAULT_MOA_PRESET_NAME).strip()
    preset = cfg["presets"].get(preset_name)
    if preset is None:
        available = ", ".join(cfg["presets"]) or "(none)"
        raise MoAPresetNotFoundError(
            f"MoA preset '{preset_name}' was not found. Available presets: "
            f"{available}. Run `/moa list`."
        )
    return deepcopy(preset)


def exact_moa_preset_name(config: Any, text: str) -> Optional[str]:
    """当且仅当 ``text`` 精确匹配一个**已启用**预设时返回其名字。

    供无显式 provider 的 ``/model <preset>`` 切换路径识别裸预设名。这是隐式匹配，
    故必须尊重 per-preset 的 ``enabled`` 退出：用户设 ``enabled: false`` 关掉某预
    设后，一个恰好同名的普通模型切换不得把会话静默拐到 MoA 虚拟 provider 上。
    """
    wanted = str(text or "").strip()
    if not wanted:
        return None
    cfg = normalize_moa_config(config)
    preset = cfg["presets"].get(wanted)
    if preset is None or not preset.get("enabled", True):
        return None
    return wanted


def set_active_moa_preset(config: Any, name: Optional[str]) -> Dict[str, Any]:
    cfg = normalize_moa_config(config)
    clean = str(name or "").strip()
    if clean and clean not in cfg["presets"]:
        raise KeyError(clean)
    cfg["active_preset"] = clean
    return cfg


def list_moa_presets(config: Any) -> List[str]:
    cfg = normalize_moa_config(config)
    return list(cfg["presets"].keys())


# ---------------------------------------------------------------------------
# slot → 运行时解析（Spirit 版 resolve_runtime_provider）
# ---------------------------------------------------------------------------

def resolve_slot_runtime(slot: Dict[str, Any]) -> Dict[str, Any]:
    """把一个参考/聚合器 slot 解析成 ``create_transport`` 需要的真实运行时。

    一个 MoA slot 只是模型选择，必须像其它地方调用模型那样被调用——而不是留空
    base_url/api_key 让 transport 的 auto 检测去猜。这里用 Spirit 的
    ``PROVIDER_BASE_URLS`` / ``PROVIDER_KEY_ENV`` 映射把 provider 解析成其真实
    API 端点与凭据（对齐 config._resolve_provider 的逻辑）。

    返回 ``{"provider", "model", 可选 "base_url", 可选 "api_key"}``。slot 自带
    ``base_url``/``api_key`` 时优先（用户显式覆盖）。任何解析失败都回退到裸
    provider/model，让配错的 slot 仍尝试调用而非中断整个 MoA 轮。
    """
    provider = str(slot.get("provider") or "").strip()
    model = str(slot.get("model") or "").strip()
    out: Dict[str, Any] = {"provider": provider, "model": model}
    try:
        from spirit.config import PROVIDER_BASE_URLS, PROVIDER_KEY_ENV

        base_url = str(slot.get("base_url") or "").strip()
        if not base_url:
            base_url = PROVIDER_BASE_URLS.get(provider.lower(), "")
        if base_url:
            out["base_url"] = base_url

        api_key = str(slot.get("api_key") or "").strip()
        if not api_key:
            for env_name in PROVIDER_KEY_ENV.get(provider.lower(), []):
                key = os.environ.get(env_name, "")
                if key:
                    api_key = key
                    break
        if api_key:
            out["api_key"] = api_key
    except Exception as exc:  # pragma: no cover - 防御性
        logger.debug("MoA slot 运行时解析失败 %s: %s", slot, exc)
    return out


__all__ = [
    "DEFAULT_MOA_PRESET_NAME",
    "DEFAULT_MOA_REFERENCE_MODELS",
    "DEFAULT_MOA_AGGREGATOR",
    "MoAPresetNotFoundError",
    "normalize_moa_config",
    "resolve_moa_preset",
    "exact_moa_preset_name",
    "set_active_moa_preset",
    "list_moa_presets",
    "validate_moa_payload",
    "resolve_slot_runtime",
]
