"""Provider 切换保护 — Spirit Agent 的跨 provider 切换语义增强。

参考 Hermes ``conversation_loop.py`` ``try_activate_fallback`` 的设计，
Spirit 简化版 ~150 行。

问题与动机
----------
V3 规划 R-P1.4：当前 Spirit ``_try_activate_fallback`` 切换 provider 时只换了
连接参数 + 重置 client，但 LLM 端看到的是同一份消息历史，丢失"刚才那段是
哪家 provider 回答的"上下文。后果：
1. LLM 不知道已切换 → 期望原 provider 的工具调用语义，新 provider 不兼容
2. 上一轮工具结果 / 思考在切换点上"半丢失"——LLM 不知道这段还可用
3. 切换提示只走 ``stream_callback`` 给前端看，LLM 端没有"我刚切过来"的提示

3 类保护
--------
1. **provider 标签**：每条消息打 ``_provider / _model`` 元数据，LLM 端不可见
   （prepare_api_messages 不输出元数据），但 agent 内部可追溯"哪段是哪家答的"
2. **保留有效工具结果**：切换**不**丢弃任何 tool message —— 上一轮成功的工具
   结果仍有效，新 provider 可以继续基于它们推理
3. **切换提示注入**：切换后给 messages 注入一条 system 消息，明确告诉 LLM：
   "刚切换了 provider，请基于现有工具结果继续，不要重复已成功的调用"

集成方式
--------
``_try_activate_fallback`` 切换成功后，调用 :func:`protect_provider_switch`
给 ``agent._messages`` 打标签 + 注入切换提示，下一轮 ``prepare_api_messages``
重建时元数据被剥离（不影响 LLM 看到的格式），但切换提示作为 system message
被保留。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


# =========================================================================
# 元数据常量（避免散落字符串）
# =========================================================================

# 当前消息归属的 provider / model
META_PROVIDER = "_provider"
META_MODEL = "_model"
META_ROUTE_STARTED_AT = "_route_started_at"

# 切换提示消息专用：从哪切到哪、原因
META_FROM_PROVIDER = "_from_provider"
META_FROM_MODEL = "_from_model"
META_SWITCH_REASON = "_switch_reason"


# =========================================================================
# 数据类
# =========================================================================

@dataclass(frozen=True)
class ProviderRoute:
    """Provider 路由快照（不可变）。"""

    provider: str
    model: str
    base_url: str = ""
    api_key: str = ""

    def label(self) -> str:
        return f"{self.provider}/{self.model}"


# =========================================================================
# 切换提示模板
# =========================================================================

# 给 LLM 的 system 提示：刚切换了 provider，请基于现有工具结果继续
_SWITCH_NOTICE_TEMPLATE = (
    "[系统提示：API provider 已切换]\n"
    "刚才的 API 调用在原 provider {from_provider}（模型 {from_model}）失败，\n"
    "原因：{reason}。\n"
    "现在切换到新 provider {to_provider}（模型 {to_model}）。\n\n"
    "请基于现有的工具结果（tool messages）和对话上下文继续推理。\n"
    "工具调用和工具结果的 id 仍然有效，可以继续正常调用工具。\n"
    "不要重复刚才已成功的工具调用，除非你认为结果已失效。"
)


# =========================================================================
# 标签操作
# =========================================================================

def mark_provider_route(
    message: Dict[str, Any],
    route: ProviderRoute,
    *,
    started_at: Optional[float] = None,
) -> Dict[str, Any]:
    """给单条消息打 provider 标签。返回新 dict（不修改入参）。"""
    new_msg = dict(message)
    new_msg[META_PROVIDER] = route.provider
    new_msg[META_MODEL] = route.model
    if started_at is not None:
        new_msg[META_ROUTE_STARTED_AT] = started_at
    return new_msg


def extract_provider_route(message: Dict[str, Any]) -> Optional[ProviderRoute]:
    """从消息读 provider 路由（无标签返回 None）。"""
    p = message.get(META_PROVIDER)
    m = message.get(META_MODEL)
    if not p or not m:
        return None
    return ProviderRoute(provider=p, model=m)


def annotate_messages_with_provider(
    messages: List[Dict[str, Any]],
    current_route: ProviderRoute,
    *,
    skip_system: bool = True,
) -> List[Dict[str, Any]]:
    """给 messages 里**没有** provider 标签的消息补标签（已有标签不动）。

    Args:
        skip_system: True 时跳过 system 消息（system prompt 是 provider 无关的，
            不应该被打 provider 标签）。
    """
    out: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role", "")
        if skip_system and role == "system":
            out.append(msg)
            continue
        # 已有标签不动
        if msg.get(META_PROVIDER):
            out.append(msg)
            continue
        out.append(mark_provider_route(msg, current_route))
    return out


# =========================================================================
# 切换提示
# =========================================================================

def build_provider_switch_notice(
    route_from: ProviderRoute,
    route_to: ProviderRoute,
    reason: str = "",
) -> Dict[str, Any]:
    """构造给 LLM 的切换提示 system 消息。

    标记 ``META_FROM_PROVIDER / META_FROM_MODEL / META_SWITCH_REASON`` 元数据，
    方便后续追踪/调试。``_provider / _model`` 设为新 provider（语义上"切换
    提示是新的 provider 发出的"）。
    """
    text = _SWITCH_NOTICE_TEMPLATE.format(
        from_provider=route_from.provider or "(unknown)",
        from_model=route_from.model or "(unknown)",
        to_provider=route_to.provider or "(unknown)",
        to_model=route_to.model or "(unknown)",
        reason=reason or "未知失败",
    )
    return {
        "role": "system",
        "content": text,
        META_PROVIDER: route_to.provider,
        META_MODEL: route_to.model,
        META_FROM_PROVIDER: route_from.provider,
        META_FROM_MODEL: route_from.model,
        META_SWITCH_REASON: reason,
    }


def protect_provider_switch(
    messages: List[Dict[str, Any]],
    route_from: ProviderRoute,
    route_to: ProviderRoute,
    reason: str = "",
    *,
    inject_notice: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """跨 provider 切换时的一站式保护。

    步骤：
    1. 给所有无标签的消息打 ``route_to`` 标签（跳过 system）
    2. 构造切换提示 system 消息（标记 from/to/reason）
    3. 切换提示插入到"最后一条非 system 消息之后"

    Args:
        messages: 切换前的消息列表
        route_from: 切换前的 provider 路由
        route_to: 切换后的 provider 路由
        reason: 切换原因（如 "timeout" / "rate_limit"）
        inject_notice: 是否注入切换提示（默认 True）

    Returns:
        ``(新消息列表, 切换提示消息)``

    注意：**不**过滤任何 tool message —— 这是"保留有效工具结果"语义。
    上一轮成功的工具结果仍然有效，新 provider 可以继续基于它们推理。
    唯一可能"失效"的 token 是当前这一轮 LLM 没看到的请求/响应，这些不在
    messages 里，所以也不会丢失。
    """
    annotated = annotate_messages_with_provider(messages, route_to)
    notice = build_provider_switch_notice(route_from, route_to, reason)

    if inject_notice:
        # 找最后一条非 system 消息的位置，切换提示插在它之后
        # 这样 system prompt（首位）不被插入打断，新 provider 收到 prompt 时
        # 仍能看到完整的 system 上下文，切换提示只是"额外提示"
        insert_idx = len(annotated)
        for i in range(len(annotated) - 1, -1, -1):
            if annotated[i].get("role") != "system":
                insert_idx = i + 1
                break
        annotated = annotated[:insert_idx] + [notice] + annotated[insert_idx:]

    return annotated, notice


# =========================================================================
# Agent 状态快照（用于 debug / 测试）
# =========================================================================

def snapshot_agent_route(agent) -> ProviderRoute:
    """从 agent 当前属性读出 ProviderRoute（用 _primary_runtime 优先）。"""
    primary = getattr(agent, "_primary_runtime", None)
    if primary:
        return ProviderRoute(
            provider=primary.get("provider", ""),
            model=primary.get("model", ""),
            base_url=primary.get("base_url", ""),
            api_key=primary.get("api_key", ""),
        )
    return ProviderRoute(
        provider=getattr(agent, "provider", ""),
        model=getattr(agent, "model", ""),
        base_url=getattr(agent, "base_url", ""),
        api_key=getattr(agent, "api_key", ""),
    )


def get_provider_switch_state(agent) -> Dict[str, Any]:
    """从 agent 读出切换状态快照（用于 debug / 测试）。"""
    return {
        "primary_runtime": getattr(agent, "_primary_runtime", None),
        "fallback_activated": getattr(agent, "_fallback_activated", False),
        "fallback_index": getattr(agent, "_fallback_index", 0),
        "primary_cooldown_until": getattr(agent, "_primary_cooldown_until", 0),
        "current_provider": getattr(agent, "provider", ""),
        "current_model": getattr(agent, "model", ""),
    }


# =========================================================================
# apply_provider_labels（准备 API 消息前的标签剥离 + 切换提示保留）
# =========================================================================

def apply_provider_labels(
    messages: List[Dict[str, Any]],
    current_route: ProviderRoute,
    *,
    strip_labels: bool = True,
) -> List[Dict[str, Any]]:
    """在 prepare_api_messages 之前调用：补齐标签（缺失的话）+ 剥离标签。

    ``prepare_api_messages`` 不识别 ``_provider / _model`` 元数据，会原样
    输出到 LLM——这里主动剥掉避免污染 API payload。

    Args:
        messages: 内部消息列表
        current_route: 当前 provider 路由（用于补齐缺失标签）
        strip_labels: 是否剥离标签（默认 True；debug 时可 False）

    Returns:
        新的消息列表（消息内容不变，仅调整元数据）
    """
    if strip_labels:
        # 先补齐标签（确保每条都有归属），再剥离
        annotated = annotate_messages_with_provider(messages, current_route)
        out = []
        for msg in annotated:
            new_msg = {k: v for k, v in msg.items() if k not in (
                META_PROVIDER,
                META_MODEL,
                META_ROUTE_STARTED_AT,
                META_FROM_PROVIDER,
                META_FROM_MODEL,
                META_SWITCH_REASON,
            )}
            out.append(new_msg)
        return out
    else:
        return annotate_messages_with_provider(messages, current_route)


__all__ = [
    "ProviderRoute",
    "META_PROVIDER",
    "META_MODEL",
    "META_ROUTE_STARTED_AT",
    "META_FROM_PROVIDER",
    "META_FROM_MODEL",
    "META_SWITCH_REASON",
    "mark_provider_route",
    "extract_provider_route",
    "annotate_messages_with_provider",
    "build_provider_switch_notice",
    "protect_provider_switch",
    "snapshot_agent_route",
    "get_provider_switch_state",
    "apply_provider_labels",
]
