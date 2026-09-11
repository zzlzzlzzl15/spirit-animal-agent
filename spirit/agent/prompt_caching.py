"""Anthropic 风格 prompt caching — 减少多轮对话的输入 token 成本。

参考 Hermes 的 prompt_caching.py (120行)。
核心策略: system_and_3 — 最多 4 个 cache_control 断点:
  - 系统提示词 (1 个)
  - 最后 3 条非系统消息 (3 个)

纯函数模块，无类状态，无 Agent 依赖。
支持:
  - 原生 Anthropic 布局 (cache_control 在 content block 内)
  - OpenRouter/OpenAI-wire 代理布局 (cache_control 在消息信封上)
  - 自动检测 provider 是否支持 cache_control

减少约 75% 的输入 token 成本（多轮对话场景）。
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# =========================================================================
# 内部辅助
# =========================================================================

def _build_marker(ttl: str) -> Dict[str, str]:
    """构建 cache_control 标记字典。

    Args:
        ttl: 缓存有效期，'5m' 或 '1h'
    """
    marker: Dict[str, str] = {"type": "ephemeral"}
    if ttl == "1h":
        marker["ttl"] = "1h"
    return marker


def _apply_cache_marker(
    msg: dict,
    cache_marker: dict,
    native_anthropic: bool = False,
) -> None:
    """为单条消息添加 cache_control 标记。

    处理所有格式变体:
    - 字符串 content → 转为 content block 数组
    - 列表 content → 在最后一个 block 上添加标记
    - 空 content → 直接在消息顶层添加标记
    - role=tool + native → 顶层标记（adapter 会移动）

    Args:
        msg: 消息字典（就地修改）
        cache_marker: cache_control 标记
        native_anthropic: 是否为原生 Anthropic API 布局
    """
    role = msg.get("role", "")
    content = msg.get("content")

    # 原生 Anthropic 布局: role=tool 的顶层标记
    if role == "tool" and native_anthropic:
        msg["cache_control"] = cache_marker
        return

    # 空内容处理
    if content is None or content == "":
        if role == "tool" and not native_anthropic:
            # OpenRouter 拒绝 role:tool 上的顶层 cache_control
            # 空消息没有 content part 可以承载标记 → 跳过
            return
        if role == "assistant" and not native_anthropic:
            # 空 assistant 消息是纯 tool_calls，顶层标记会被忽略
            return
        msg["cache_control"] = cache_marker
        return

    # 字符串 content → 转为 content block 数组
    if isinstance(content, str):
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": cache_marker}
        ]
        return

    # 列表 content → 在最后一个 block 上添加标记
    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = cache_marker


def _can_carry_marker(msg: dict, native_anthropic: bool) -> bool:
    """判断消息是否能有效承载 cache_control 标记。

    原生 Anthropic: 所有消息都可以。
    OpenRouter 等代理: 只有非空 content 的消息可以
    （空 assistant/tool 消息上的标记会被忽略，浪费断点）。
    """
    if native_anthropic:
        return True
    content = msg.get("content")
    if content is None or content == "":
        return False
    if isinstance(content, list):
        return bool(content) and isinstance(content[-1], dict)
    return isinstance(content, str)


# =========================================================================
# 公共 API
# =========================================================================

def apply_cache_control(
    api_messages: List[Dict[str, Any]],
    cache_ttl: str = "5m",
    native_anthropic: bool = False,
) -> List[Dict[str, Any]]:
    """应用 system_and_3 缓存策略到消息列表。

    放置最多 4 个 cache_control 断点:
    - 系统提示词 (第 1 条消息，如果 role=system)
    - 最后 3 条非系统消息

    Args:
        api_messages: API 消息列表
        cache_ttl: 缓存有效期 ('5m' 或 '1h')
        native_anthropic: 是否使用原生 Anthropic 布局

    Returns:
        深拷贝后的消息列表，带有 cache_control 断点注入
    """
    messages = copy.deepcopy(api_messages)
    if not messages:
        return messages

    marker = _build_marker(cache_ttl)
    breakpoints_used = 0

    # 系统提示词断点
    if messages[0].get("role") == "system":
        _apply_cache_marker(messages[0], marker, native_anthropic=native_anthropic)
        breakpoints_used += 1

    # 剩余断点分配给最后的非系统消息
    remaining = 4 - breakpoints_used
    non_sys = [
        i
        for i in range(len(messages))
        if messages[i].get("role") != "system"
        and _can_carry_marker(messages[i], native_anthropic=native_anthropic)
    ]
    for idx in non_sys[-remaining:]:
        _apply_cache_marker(messages[idx], marker, native_anthropic=native_anthropic)

    return messages


def should_use_prompt_caching(
    provider: str = "",
    model: str = "",
    base_url: str = "",
) -> tuple[bool, bool]:
    """判断是否应启用 prompt caching 以及使用哪种布局。

    Returns:
        (should_cache, use_native_layout):
        - should_cache: 是否注入 cache_control 断点
        - use_native_layout: True=原生 Anthropic 布局, False=OpenAI-wire 代理布局

    支持的 provider:
    - anthropic (原生) → 缓存 + 原生布局
    - openrouter + Claude → 缓存 + 代理布局
    - 其他 Claude 模型 → 缓存 + 代理布局
    """
    provider_lower = (provider or "").lower().strip()
    model_lower = (model or "").lower()
    base_url_lower = (base_url or "").lower()

    is_claude = "claude" in model_lower

    # 原生 Anthropic
    if provider_lower == "anthropic":
        return True, True

    # OpenRouter + Claude
    if "openrouter" in provider_lower or "openrouter" in base_url_lower:
        if is_claude:
            return True, False

    # 其他 provider 使用 Claude 模型（通过代理）
    if is_claude:
        return True, False

    return False, False


__all__ = [
    "apply_cache_control",
    "should_use_prompt_caching",
]
