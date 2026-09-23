"""Mixture-of-Agents（MoA）子系统 — Spirit Agent（Phase 4.1）。

对标 Hermes 的 ``agent/moa_loop.py`` + ``agent/moa_trace.py`` +
``hermes_cli/moa_config.py``。三个模块：

- :mod:`spirit.moa.config` — 预设归一化/校验 + slot→运行时解析。
- :mod:`spirit.moa.moa_loop` — 参考 fan-out + 聚合器 facade（``MoAClient``）。
- :mod:`spirit.moa.moa_trace` — opt-in 的完整轮次追踪持久化。

``provider == "moa"`` 时，``SpiritAgent.client`` 返回 :class:`MoAClient`，其
``.chat.completions.create`` 透明拦截：并行跑参考模型 → 注入 guidance → 调聚合器。
"""

from __future__ import annotations

from spirit.moa.config import (
    DEFAULT_MOA_AGGREGATOR,
    DEFAULT_MOA_PRESET_NAME,
    DEFAULT_MOA_REFERENCE_MODELS,
    MoAPresetNotFoundError,
    exact_moa_preset_name,
    list_moa_presets,
    normalize_moa_config,
    resolve_moa_preset,
    resolve_slot_runtime,
    set_active_moa_preset,
    validate_moa_payload,
)
from spirit.moa.moa_loop import (
    MoAChatCompletions,
    MoAClient,
    aggregate_moa_context,
)
from spirit.moa.moa_trace import save_moa_turn
from spirit.moa.commands import handle_moa_command, moa_usage

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
    "MoAClient",
    "MoAChatCompletions",
    "aggregate_moa_context",
    "save_moa_turn",
    "handle_moa_command",
    "moa_usage",
]
