"""传输无关的 /moa 命令分发层 — Spirit Agent（Phase 4.1）。

参考 Hermes ``hermes_cli/moa_cmd.py``（``hermes moa list/config/delete``）与会话内
``/moa <prompt>`` 一次性模式，但像 :mod:`spirit.goals.commands` 那样把「解析 + 派发
+ 结果」从具体 UI（CLI 的 Rich console、桌宠的 WebSocket）里剥离出来：这里只吃一个
``agent`` 和一段 ``arg`` 字符串，吐一个结构化 dict。CLI 与 ws_server 各自把 dict 渲染
成自己的形式。

这样做有两个好处（对齐 goals.commands 的设计动机）：

1. **可测试**——命令解析 / MoA 会话切换无需真实终端或 LLM 即可单测（对齐用户
   「和 Hermes 一样的任务检查测试用例」的要求）；一次性模式用假 agent 的 ``chat``
   即可验证「切换→跑→恢复」的原子性。
2. **单一事实源**——list/use/off/oneshot 的语义只在这里定义一次，CLI 和桌宠不会
   各写一套而漂移。

命令形式::

    /moa                  列出预设（default/active + 每预设 reference models + aggregator）
    /moa list|ls          同上
    /moa use <name>       把当前会话切到 MoA 预设（provider="moa", model=<name>）
    /moa on <name>        use 的别名
    /moa off              退出 MoA，恢复切换前的 provider/model
    /moa <prompt>         一次性：临时切到默认/激活预设跑该 prompt，跑完恢复

返回 dict 的约定键（对齐 :mod:`spirit.goals.commands`）::

    ok          bool        命令是否成功执行（未知预设 / 参数错误 → False）
    action      str         归一化后的动作名（list/use/off/oneshot/unavailable）
    message     str         一行主消息（给用户看）
    lines       List[str]   多行输出（预设清单等）
    status_line str         当前 MoA 状态一行（便于 UI 常驻显示）
    response    str|None    一次性模式的聚合器最终响应（其它动作为 None）

命令层本身只在一次性模式下调 LLM（``agent.chat``）；list/use/off 都是纯状态流转，
不阻塞、不触网。``provider == "moa"`` 的会话切换只改 ``agent`` 的运行时字段并重置
``_client``（触发 client property 重建为 :class:`~spirit.moa.moa_loop.MoAClient`），
**不**写持久配置——那是 ``hermes moa config`` / API 写路径的职责。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from spirit.moa.config import (
    DEFAULT_MOA_PRESET_NAME,
    MoAPresetNotFoundError,
    normalize_moa_config,
    resolve_moa_preset,
)

logger = logging.getLogger(__name__)


def _result(
    ok: bool,
    action: str,
    message: str = "",
    *,
    lines: Optional[List[str]] = None,
    status_line: str = "",
    response: Optional[str] = None,
) -> Dict[str, Any]:
    """构造统一的结果 dict。"""
    return {
        "ok": ok,
        "action": action,
        "message": message,
        "lines": lines or [],
        "status_line": status_line,
        "response": response,
    }


def moa_usage() -> str:
    """``/moa`` 的用法提示（对标 Hermes ``moa_usage()``）。"""
    return (
        "Usage: /moa <prompt>  (runs one prompt through the default MoA preset, "
        "then restores your model)  |  /moa list  |  /moa use <name>  |  /moa off"
    )


def _load_moa_config() -> Dict[str, Any]:
    """读取用户 MoA 配置节（monkeypatch seam，供测试注入预设）。

    与 :func:`spirit.moa.moa_loop._load_moa_config` 同源：走 ``load_config()``（含
    YAML 用户预设 + env + 覆盖），而非只读 DEFAULT_CONFIG 的 ``get_config_value``。
    任何异常都降级为空配置（``normalize_moa_config`` 会补齐默认），绝不崩溃。
    """
    try:
        from spirit.config import load_config

        return load_config().get("moa") or {}
    except Exception as exc:  # pragma: no cover - 防御性
        logger.debug("MoA 配置加载失败: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# slot / 预设渲染
# ---------------------------------------------------------------------------

def _format_slot(slot: Any) -> str:
    """把一个 slot 渲染成 ``provider:model [reasoning=effort]``（对标 Hermes）。"""
    if not isinstance(slot, dict):
        return "(unset)"
    provider = str(slot.get("provider") or "").strip()
    model = str(slot.get("model") or "").strip()
    label = f"{provider}:{model}"
    effort = str(slot.get("reasoning_effort") or "").strip()
    return f"{label} [reasoning={effort}]" if effort else label


def _preset_lines(name: str, preset: Dict[str, Any]) -> List[str]:
    """渲染单个预设的 reference models + aggregator（use 确认 / list 复用）。"""
    lines: List[str] = [f"预设 '{name}':", "  Reference models:"]
    for idx, slot in enumerate(preset.get("reference_models") or [], start=1):
        lines.append(f"    {idx}. {_format_slot(slot)}")
    lines.append(f"  Aggregator: {_format_slot(preset.get('aggregator') or {})}")
    return lines


def _status_line(agent, cfg: Dict[str, Any]) -> str:
    """当前会话 MoA 状态一行（供 UI 常驻显示）。"""
    if agent is not None and _in_moa(agent):
        return f"MoA: on (preset={getattr(agent, 'model', '?')})"
    default = cfg.get("default_preset") or DEFAULT_MOA_PRESET_NAME
    return f"MoA: off (default preset={default})"


# ---------------------------------------------------------------------------
# 会话运行时切换（provider="moa" ⇄ 原始 provider/model）
# ---------------------------------------------------------------------------

def _in_moa(agent) -> bool:
    return str(getattr(agent, "provider", "") or "").strip().lower() == "moa"


def _snapshot_runtime(agent) -> Dict[str, Any]:
    """快照当前 provider/model/base_url/api_key，供退出 MoA 时恢复。"""
    return {
        "provider": getattr(agent, "provider", None),
        "model": getattr(agent, "model", None),
        "base_url": getattr(agent, "base_url", None),
        "api_key": getattr(agent, "api_key", None),
    }


def _restore_runtime(agent, snapshot: Optional[Dict[str, Any]]) -> None:
    """把 agent 运行时恢复为快照值并重置 ``_client``（下次访问重建）。"""
    if not snapshot:
        return
    agent.provider = snapshot.get("provider")
    agent.model = snapshot.get("model")
    agent.base_url = snapshot.get("base_url")
    agent.api_key = snapshot.get("api_key")
    agent._client = None


def _enter_moa(agent, preset_name: str) -> None:
    """把会话切到 MoA 虚拟 provider（``model`` 即预设名）。

    仅在尚未处于 MoA 时快照原始运行时，避免连续 ``/moa use`` 把真正的原始
    provider/model 覆盖掉（否则 ``/moa off`` 会恢复到一个 MoA 状态）。
    """
    if not _in_moa(agent):
        agent._moa_saved_runtime = _snapshot_runtime(agent)
    agent.provider = "moa"
    agent.model = preset_name
    agent._client = None  # 触发 client property 重建为 MoAClient


def _exit_moa(agent) -> Optional[Dict[str, Any]]:
    """退出 MoA，恢复切换前运行时；返回被恢复的快照（无则 None）。"""
    saved = getattr(agent, "_moa_saved_runtime", None)
    _restore_runtime(agent, saved)
    agent._moa_saved_runtime = None
    return saved


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------

def _list_presets(agent) -> Dict[str, Any]:
    cfg = normalize_moa_config(_load_moa_config())
    default_name = cfg.get("default_preset") or DEFAULT_MOA_PRESET_NAME
    lines: List[str] = [
        f"Default: {default_name}",
        f"Active in config: {cfg.get('active_preset') or '(off)'}",
    ]
    for name, preset in cfg.get("presets", {}).items():
        marker = "*" if name == default_name else " "
        suffix = "" if preset.get("enabled", True) else "  (disabled)"
        lines.append(f"{marker} {name}{suffix}")
        lines.append("  Reference models:")
        for idx, slot in enumerate(preset.get("reference_models") or [], start=1):
            lines.append(f"    {idx}. {_format_slot(slot)}")
        lines.append(f"  Aggregator: {_format_slot(preset.get('aggregator') or {})}")
    lines.append("")
    lines.append("  /moa use <name> 切到某预设 · /moa off 恢复 · /moa <prompt> 一次性")
    return _result(True, "list", "Mixture of Agents 预设：", lines=lines, status_line=_status_line(agent, cfg))


def _moa_use(agent, name: str) -> Dict[str, Any]:
    cfg = normalize_moa_config(_load_moa_config())
    preset_name = (name or "").strip() or cfg.get("default_preset") or DEFAULT_MOA_PRESET_NAME
    try:
        preset = resolve_moa_preset(cfg, preset_name)
    except MoAPresetNotFoundError as exc:
        return _result(False, "use", str(exc), status_line=_status_line(agent, cfg))
    if not preset.get("enabled", True):
        return _result(
            False, "use",
            f"MoA 预设 '{preset_name}' 已禁用（enabled: false）——用 /moa list 看可用预设。",
            status_line=_status_line(agent, cfg),
        )
    _enter_moa(agent, preset_name)
    lines = _preset_lines(preset_name, preset)
    lines.append("  后续消息将走参考 fan-out + 聚合器；/moa off 恢复原模型。")
    return _result(
        True, "use",
        f"⊙ 会话已切到 MoA 预设 '{preset_name}'。",
        lines=lines, status_line=_status_line(agent, cfg),
    )


def _moa_off(agent) -> Dict[str, Any]:
    if not _in_moa(agent):
        return _result(False, "off", "当前会话不在 MoA 模式（无需退出）。")
    saved = _exit_moa(agent)
    restored = (saved or {}).get("model") or "?"
    cfg = normalize_moa_config(_load_moa_config())
    return _result(True, "off", f"✓ 已退出 MoA，恢复模型: {restored}", status_line=_status_line(agent, cfg))


def _moa_oneshot(
    agent,
    prompt: str,
    *,
    on_delta: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """一次性：临时切到 MoA 跑该 prompt，跑完（无论成败）恢复原运行时。

    已处于 MoA 模式时沿用当前预设、不快照不恢复（用户显式选了预设，一次性 prompt
    就该用它）。否则快照→切默认/激活预设→跑→在 ``finally`` 里恢复，保证任何异常
    都不会把会话卡在 MoA 上。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return _result(False, "oneshot", moa_usage())

    cfg = normalize_moa_config(_load_moa_config())

    # 已在 MoA：用当前预设直接跑，不动运行时。
    if _in_moa(agent):
        preset_name = getattr(agent, "model", None) or cfg.get("default_preset") or DEFAULT_MOA_PRESET_NAME
        text = _run_chat(agent, prompt, on_delta)
        return _result(
            True, "oneshot",
            f"⊙ 已通过 MoA 预设 '{preset_name}' 跑完一次性 prompt。",
            response=text, status_line=_status_line(agent, cfg),
        )

    # 不在 MoA：快照 → 切默认/激活预设 → 跑 → 恢复。
    preset_name = cfg.get("active_preset") or cfg.get("default_preset") or DEFAULT_MOA_PRESET_NAME
    try:
        resolve_moa_preset(cfg, preset_name)
    except MoAPresetNotFoundError as exc:
        return _result(False, "oneshot", str(exc), status_line=_status_line(agent, cfg))

    snapshot = _snapshot_runtime(agent)
    try:
        _enter_moa(agent, preset_name)
        text = _run_chat(agent, prompt, on_delta)
    finally:
        _restore_runtime(agent, snapshot)
    return _result(
        True, "oneshot",
        f"⊙ 已通过 MoA 预设 '{preset_name}' 跑完一次性 prompt，已恢复原模型。",
        response=text, status_line=_status_line(agent, cfg),
    )


def _run_chat(agent, prompt: str, on_delta: Optional[Callable[[str], None]]) -> str:
    """跑一次 ``agent.chat``，抽出最终文本响应（容忍非 dict 返回）。"""
    result = agent.chat(prompt, stream_callback=on_delta) if on_delta is not None else agent.chat(prompt)
    if isinstance(result, dict):
        return result.get("response", "") or ""
    return str(result or "")


# ---------------------------------------------------------------------------
# 主派发
# ---------------------------------------------------------------------------

def handle_moa_command(
    agent,
    arg: str,
    *,
    on_delta: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """派发 ``/moa`` 子命令（对齐 :func:`spirit.goals.commands.handle_goal_command`）。

    Args:
        agent: 持有 ``provider`` / ``model`` / ``_client`` / ``chat()`` 的 SpiritAgent。
        arg: ``/moa`` 之后的整段参数（可为空）。
        on_delta: 可选流式增量回调，仅一次性模式转发给 ``agent.chat``。

    Returns:
        结构化结果 dict（见模块 docstring）。
    """
    if agent is None:
        return _result(False, "unavailable", "MoA 不可用（无活跃 Agent 会话）。")

    arg = (arg or "").strip()
    lower = arg.lower()

    # 裸 /moa 或 /moa list|ls → 列出预设
    if not arg or lower in {"list", "ls"}:
        return _list_presets(agent)

    tokens = arg.split(None, 1)
    verb = tokens[0].lower()
    rest = tokens[1].strip() if len(tokens) > 1 else ""

    # /moa use|on <name> → 切会话到 MoA 预设
    if verb in {"use", "on", "enable"}:
        return _moa_use(agent, rest)

    # /moa off|disable → 退出 MoA，恢复原运行时
    if verb in {"off", "disable"}:
        return _moa_off(agent)

    # /moa help → 用法
    if verb in {"help", "-h", "--help"}:
        return _result(True, "help", moa_usage(), status_line=_status_line(agent, normalize_moa_config(_load_moa_config())))

    # 其余：把整段 arg 当作一次性 prompt
    return _moa_oneshot(agent, arg, on_delta=on_delta)


__all__ = [
    "handle_moa_command",
    "moa_usage",
]
