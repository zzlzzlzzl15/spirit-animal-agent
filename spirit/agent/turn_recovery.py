"""Turn 错误恢复链 — Spirit Agent 的容错第二层。

参考 Hermes 的 ``turn_recovery.py``（1874 行）的整体设计，Spirit 简化版 ~500 行。

问题与动机
----------
V3 规划 R-P1.1：当前 Spirit ``conversation_loop.py`` 错误处理只有"重试 or 换 provider"
两件事，碰到 unicode 编码错 / 图片过大 / 思考超时 / 工具序列中断 / 消息不合法等
异常路径时只能让 LLM 看到原始 error 然后继续撞墙。
本模块提供 5 类修复处理器，按顺序尝试修复错误后让 conversation 继续跑。

5 类处理器
----------
1. :func:`recover_unicode_encode_error` — 清洗消息里的非 ASCII / surrogate 字符
2. :func:`recover_image_dimension_error` — 图片过大时按 provider 限制自动缩放
3. :func:`recover_thinking_timeout` — 思考超时注入续推提示
4. :func:`recover_tool_sequence_interrupted` — 补齐中断的工具调用对
5. :func:`recover_message_sanitization` — 剥离图像 / 控制字符 / surrogate

集成方式
---------
``conversation_loop.py`` 在调 LLM 抛错时调用 :class:`TurnRecoveryChain` 的
``recover()`` 方法：返回 ``RecoveryAction`` 时根据 ``action_type`` 处理消息；
返回 ``None`` 时走原有 fallback（重试 / 切 provider）。
"""

from __future__ import annotations

import base64
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# =========================================================================
# 修复动作
# =========================================================================

class RecoveryActionType(str, Enum):
    """修复动作类型 — 决定 conversation_loop 怎么处理。"""

    RETRY = "retry"
    """重试当前轮（消息已被 sanitize）。"""

    INJECT_HINT = "inject_hint"
    """注入提示消息后重试（用于 thinking_timeout）。"""

    COMPLETE_TOOL_PAIR = "complete_tool_pair"
    """补齐工具调用对（用于 tool_sequence_interrupted）。"""

    SANITIZE_AND_RETRY = "sanitize_and_retry"
    """清洗消息后重试（sanitization 处理）。"""

    NONE = "none"
    """无法修复，让上层走 fallback。"""


@dataclass
class RecoveryAction:
    """修复动作结果。"""

    action_type: RecoveryActionType
    description: str
    modified_messages: Optional[List[Dict[str, Any]]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_retryable(self) -> bool:
        """是否可重试当前轮。"""
        return self.action_type in {
            RecoveryActionType.RETRY,
            RecoveryActionType.INJECT_HINT,
            RecoveryActionType.SANITIZE_AND_RETRY,
            RecoveryActionType.COMPLETE_TOOL_PAIR,
        }


# =========================================================================
# 恢复上下文
# =========================================================================

@dataclass
class RecoveryContext:
    """恢复上下文 — 包含 error + 消息历史 + provider 元数据。"""

    error: Exception
    messages: List[Dict[str, Any]] = field(default_factory=list)
    provider: Optional[str] = None
    model: Optional[str] = None
    is_streaming: bool = False
    had_tool_calls: bool = False

    # 额外元数据
    metadata: Dict[str, Any] = field(default_factory=dict)


# =========================================================================
# Handler type alias
# =========================================================================

RecoveryHandler = Callable[[Exception, RecoveryContext], Optional[RecoveryAction]]


# =========================================================================
# 1. Unicode 编码错恢复
# =========================================================================

# surrogate 字符范围 (U+D800-U+DFFF) — Python 字符串不应包含
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
# 控制字符（除了 \t \n \r）— 通常 API 不接受
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# 检测错误关键词
_UNICODE_ERROR_PATTERNS = (
    "non-ascii", "non_ascii", "ascii codec", "unicodeencode",
    "unicodedecode", "surrogates not allowed", "invalid character",
)


def recover_unicode_encode_error(
    error: Exception, ctx: RecoveryContext,
) -> Optional[RecoveryAction]:
    """恢复 Unicode 编码错误 — 清洗消息里的非 ASCII / surrogate / 控制字符。

    适用场景：
    - API key / header 包含非 ASCII 时 provider 报错
    - LLM 返回的消息含 surrogate（Python 字符串非法）
    - provider 拒绝接收控制字符
    """
    err_str = str(error).lower()
    err_type = type(error).__name__

    # 仅在错误类型 / 错误文本匹配时触发
    if not (
        isinstance(error, (UnicodeEncodeError, UnicodeDecodeError))
        or any(p in err_str for p in _UNICODE_ERROR_PATTERNS)
        or err_type in ("UnicodeEncodeError", "UnicodeDecodeError")
    ):
        return None

    if not ctx.messages:
        return None

    # 清洗每条消息
    cleaned_messages: List[Dict[str, Any]] = []
    cleaned_count = 0
    for msg in ctx.messages:
        new_msg = dict(msg)
        content = new_msg.get("content")
        if isinstance(content, str):
            new_content = _sanitize_unicode_string(content)
            if new_content != content:
                cleaned_count += 1
            new_msg["content"] = new_content
        cleaned_messages.append(new_msg)

    if cleaned_count == 0:
        logger.warning("[turn_recovery] unicode error but no message needed cleaning")
        return None

    logger.info(
        "[turn_recovery] unicode error: cleaned %d/%d messages",
        cleaned_count, len(ctx.messages),
    )

    return RecoveryAction(
        action_type=RecoveryActionType.SANITIZE_AND_RETRY,
        description=f"清洗了 {cleaned_count} 条消息里的 unicode 非法字符",
        modified_messages=cleaned_messages,
        metadata={"cleaned_count": cleaned_count},
    )


def _sanitize_unicode_string(text: str) -> str:
    """清洗字符串里的 surrogate / 控制字符 / 不可打印字符。"""
    if not text:
        return text
    # 1. NFKC 规范化（合并兼容字符）
    text = unicodedata.normalize("NFKC", text)
    # 2. 移除 surrogate（Python 字符串不允许）
    text = _SURROGATE_RE.sub("", text)
    # 3. 移除控制字符（保留 \t \n \r）
    text = _CONTROL_CHAR_RE.sub("", text)
    return text


# =========================================================================
# 2. 图片尺寸错误恢复
# =========================================================================

# 各 provider 的图片尺寸限制（像素）— 简化版，按边长
_PROVIDER_IMAGE_LIMITS = {
    "openai": 2048,        # GPT-4V / GPT-4o
    "anthropic": 1568,     # Claude 3.x
    "minimax": 2048,       # 默认同 openai
    "google": 3072,        # Gemini 1.5
}

# 默认降级目标尺寸
_DEFAULT_TARGET_DIMENSION = 1024

# image data URL / base64 检测
_IMAGE_DATA_URL_RE = re.compile(r"data:image/[a-z]+;base64,[A-Za-z0-9+/=]+")


def recover_image_dimension_error(
    error: Exception, ctx: RecoveryContext,
) -> Optional[RecoveryAction]:
    """恢复图片尺寸错误 — 自动缩放超大图片到 provider 限制内。

    适用场景：
    - 错误文本含 "image too large" / "image dimensions" / "max image size"
    - 多模态消息里 base64 图片超过 provider 限制
    """
    err_str = str(error).lower()
    if not any(p in err_str for p in (
        "image too large", "image dimensions", "max image size",
        "image size exceeds", "image exceeds", "image must be",
    )):
        return None

    if not ctx.messages:
        return None

    # 取 provider 的图片限制
    limit = _PROVIDER_IMAGE_LIMITS.get((ctx.provider or "").lower(), _DEFAULT_TARGET_DIMENSION)

    # 找含 image 的消息
    modified_messages: List[Dict[str, Any]] = []
    resized_count = 0
    for msg in ctx.messages:
        new_msg = dict(msg)
        content = new_msg.get("content")
        if isinstance(content, str) and "data:image" in content:
            new_content, was_resized = _resize_image_data_urls(content, limit)
            if was_resized:
                resized_count += 1
            new_msg["content"] = new_content
        elif isinstance(content, list):
            # 多模态 content list: [{type: text, text: ...}, {type: image_url, ...}]
            new_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    new_part, was_resized = _resize_image_part(part, limit)
                    if was_resized:
                        resized_count += 1
                    new_parts.append(new_part)
                else:
                    new_parts.append(part)
            new_msg["content"] = new_parts
        modified_messages.append(new_msg)

    if resized_count == 0:
        return None

    logger.info(
        "[turn_recovery] image dimension error: resized %d images to fit %dpx",
        resized_count, limit,
    )

    return RecoveryAction(
        action_type=RecoveryActionType.SANITIZE_AND_RETRY,
        description=f"缩放了 {resized_count} 张图片到 ≤{limit}px",
        modified_messages=modified_messages,
        metadata={"resized_count": resized_count, "target_limit": limit},
    )


def _resize_image_data_urls(content: str, limit: int) -> tuple:
    """尝试缩放 content 里的 base64 图片。返回 (new_content, was_resized)。

    阈值说明：按 base64 字符数判断（>3MB base64 ≈ >2.25MB binary）。
    """
    was_resized = False
    new_content = content

    def replace_data_url(match: re.Match) -> str:
        nonlocal was_resized
        b64_data = match.group(0).split("base64,", 1)[1]
        # 按 base64 字符数判断（>3MB 视为过大）
        if len(b64_data) > 3 * 1024 * 1024:
            was_resized = True
            return f"[图片过大已剥离（>3MB base64，provider 限制 {limit}px）]"
        return match.group(0)

    new_content = _IMAGE_DATA_URL_RE.sub(replace_data_url, content)
    return new_content, was_resized


def _resize_image_part(part: Dict[str, Any], limit: int) -> tuple:
    """缩放多模态 part 里的 image_url。"""
    image_url = part.get("image_url", {})
    if isinstance(image_url, dict):
        url = image_url.get("url", "")
    else:
        url = image_url or ""

    if not isinstance(url, str) or "data:image" not in url:
        return part, False

    # 按 base64 字符数判断（>3MB 视为过大）
    if "base64," in url:
        b64_data = url.split("base64,", 1)[1]
        if len(b64_data) > 3 * 1024 * 1024:
            # 替换为文本 part
            new_part = {"type": "text", "text": f"[图片过大已剥离（>3MB base64，provider 限制 {limit}px）]"}
            return new_part, True
    return part, False


# =========================================================================
# 3. 思考超时恢复
# =========================================================================

_THINKING_TIMEOUT_PATTERNS = (
    "thinking timeout", "max thinking time", "thinking exceeded",
    "thinking_time_exceeded", "thinking limit", "max thinking",
)


def recover_thinking_timeout(
    error: Exception, ctx: RecoveryContext,
) -> Optional[RecoveryAction]:
    """恢复思考超时 — 注入续推提示让 LLM 继续输出。

    适用场景：
    - 思考模式（thinking=on）下 LLM 思考超时
    - provider 返回"thinking time exceeded"
    """
    err_str = str(error).lower()
    if not any(p in err_str for p in _THINKING_TIMEOUT_PATTERNS):
        return None

    if not ctx.messages:
        return None

    # 注入续推提示作为最后一条 user 消息
    hint_message = {
        "role": "user",
        "content": (
            "[系统提示] 你之前的思考超时了。请直接给出最终答案，"
            "不要再展开详细思考步骤。"
        ),
    }

    new_messages = list(ctx.messages) + [hint_message]
    logger.info("[turn_recovery] thinking timeout: injected continuation hint")

    return RecoveryAction(
        action_type=RecoveryActionType.INJECT_HINT,
        description="思考超时，注入续推提示让 LLM 直接给出答案",
        modified_messages=new_messages,
        metadata={"hint_injected": True},
    )


# =========================================================================
# 4. 工具序列中断恢复
# =========================================================================

def recover_tool_sequence_interrupted(
    error: Exception, ctx: RecoveryContext,
) -> Optional[RecoveryAction]:
    """恢复工具序列中断 — 补齐缺失的 tool result 消息。

    适用场景：
    - 消息序列里 assistant 消息有 tool_calls 但没有对应的 tool result
    - 网络中断 / 用户 Ctrl+C 导致工具调用对不完整
    """
    if not ctx.messages:
        return None

    # 收集 assistant 消息里的 tool_call_ids
    expected_tool_call_ids = set()
    for msg in ctx.messages:
        if msg.get("role") == "assistant":
            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                tc_id = tc.get("id") if isinstance(tc, dict) else None
                if tc_id:
                    expected_tool_call_ids.add(tc_id)

    if not expected_tool_call_ids:
        return None

    # 收集已有的 tool result 消息的 tool_call_id
    existing_tool_call_ids = set()
    for msg in ctx.messages:
        if msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id")
            if tc_id:
                existing_tool_call_ids.add(tc_id)

    # 找出缺失的
    missing_ids = expected_tool_call_ids - existing_tool_call_ids
    if not missing_ids:
        return None

    # 补齐缺失的 tool result 消息（追加在最后）
    new_messages = list(ctx.messages)
    for missing_id in missing_ids:
        new_messages.append({
            "role": "tool",
            "tool_call_id": missing_id,
            "name": _find_tool_name_for_call(ctx.messages, missing_id),
            "content": json_dumps_error("工具调用被中断，工具未返回结果。请基于已知信息继续。"),
        })

    logger.info(
        "[turn_recovery] tool sequence interrupted: completed %d missing tool result(s)",
        len(missing_ids),
    )

    return RecoveryAction(
        action_type=RecoveryActionType.COMPLETE_TOOL_PAIR,
        description=f"补齐了 {len(missing_ids)} 个中断的工具调用对",
        modified_messages=new_messages,
        metadata={"completed_count": len(missing_ids)},
    )


def _find_tool_name_for_call(messages: List[Dict[str, Any]], tool_call_id: str) -> str:
    """从 messages 里找出 tool_call_id 对应的工具名。"""
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id") == tool_call_id:
                    func = tc.get("function", {})
                    return func.get("name", "unknown_tool") if isinstance(func, dict) else "unknown_tool"
    return "unknown_tool"


def json_dumps_error(message: str) -> str:
    """把错误消息封装为 JSON 字符串（避免 import 循环）。"""
    import json
    return json.dumps({"error": message, "status": "interrupted"}, ensure_ascii=False)


# =========================================================================
# 5. 消息清洗（更彻底）
# =========================================================================

# 数据 URL 最大尺寸（base64 字符数）
_MAX_DATA_URL_SIZE = 5 * 1024 * 1024  # 5MB base64

_SANITIZATION_PATTERNS_PATTERNS = (
    "invalid request", "invalid message", "malformed", "encoding error",
    "bad request", "400", "invalid utf", "invalid encoding",
)


def recover_message_sanitization(
    error: Exception, ctx: RecoveryContext,
) -> Optional[RecoveryAction]:
    """深度清洗消息 — 剥离过大 image data URL / 控制字符 / 非法结构。

    与 unicode handler 的区别：本 handler 处理的是结构性 / 大小问题，
    不是单字符问题。当 unicode handler 已修过但还失败时，本 handler 兜底。

    适用场景：
    - provider 拒绝消息（400 invalid request）
    - 数据 URL 过大
    - 消息含非法结构（嵌套错误 / 错误的 role）
    """
    err_str = str(error).lower()
    if not any(p in err_str for p in _SANITIZATION_PATTERNS_PATTERNS):
        return None

    if not ctx.messages:
        return None

    new_messages = []
    sanitized_count = 0
    for msg in ctx.messages:
        new_msg = dict(msg)
        content = new_msg.get("content")

        # 1. 字符串内容：清洗控制字符 + 截断过大 data URL
        if isinstance(content, str):
            new_content = _deep_sanitize_string(content)
            if new_content != content:
                sanitized_count += 1
            new_msg["content"] = new_content

        # 2. 多模态 content list：剥离过大 image_url
        elif isinstance(content, list):
            new_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    url_obj = part.get("image_url")
                    url = url_obj.get("url", "") if isinstance(url_obj, dict) else ""
                    if isinstance(url, str) and "base64," in url and len(url.split("base64,", 1)[1]) > 3 * 1024 * 1024:
                        new_parts.append({
                            "type": "text",
                            "text": "[图片已剥离：超过 3MB base64 大小限制]",
                        })
                        sanitized_count += 1
                        continue
                new_parts.append(part)
            new_msg["content"] = new_parts

        new_messages.append(new_msg)

    if sanitized_count == 0:
        return None

    logger.info(
        "[turn_recovery] message sanitization: sanitized %d messages",
        sanitized_count,
    )

    return RecoveryAction(
        action_type=RecoveryActionType.SANITIZE_AND_RETRY,
        description=f"深度清洗了 {sanitized_count} 条消息里的非法字符 / 过大图片",
        modified_messages=new_messages,
        metadata={"sanitized_count": sanitized_count},
    )


def _deep_sanitize_string(text: str) -> str:
    """深度清洗字符串：去控制字符 + 截断过大 data URL。"""
    if not text:
        return text

    # 1. NFKC 规范化
    text = unicodedata.normalize("NFKC", text)

    # 2. 移除 surrogate
    text = _SURROGATE_RE.sub("", text)

    # 3. 移除控制字符（保留 \t \n \r）
    text = _CONTROL_CHAR_RE.sub("", text)

    # 4. 截断过大的 data URL（保留前 4KB + 提示）
    def shrink_data_url(match: re.Match) -> str:
        url = match.group(0)
        if len(url) > _MAX_DATA_URL_SIZE:
            return f"{url[:4096]}...[图片 data URL 已截断，原长度 {len(url)} 字符]"
        return url

    text = _IMAGE_DATA_URL_RE.sub(shrink_data_url, text)

    return text


# =========================================================================
# 主类：TurnRecoveryChain
# =========================================================================

DEFAULT_HANDLERS: List[RecoveryHandler] = [
    recover_unicode_encode_error,
    recover_image_dimension_error,
    recover_thinking_timeout,
    recover_tool_sequence_interrupted,
    recover_message_sanitization,
]


class TurnRecoveryChain:
    """Turn 错误恢复链 — 按顺序尝试 5 类处理器。"""

    def __init__(self, handlers: Optional[List[RecoveryHandler]] = None) -> None:
        self.handlers = handlers or list(DEFAULT_HANDLERS)

    def recover(
        self, error: Exception, ctx: RecoveryContext,
    ) -> Optional[RecoveryAction]:
        """依次尝试每个 handler，返回第一个非 None 的 RecoveryAction。

        Returns:
            RecoveryAction 如果某个 handler 给出修复方案
            None 如果所有 handler 都无法修复（让上层走 fallback）
        """
        for handler in self.handlers:
            handler_name = getattr(handler, "__name__", str(handler))
            try:
                action = handler(error, ctx)
            except Exception as handler_err:
                logger.warning(
                    "[turn_recovery] handler %s raised: %s",
                    handler_name, handler_err,
                )
                continue

            if action is not None and action.action_type != RecoveryActionType.NONE:
                logger.info(
                    "[turn_recovery] handler %s succeeded: %s",
                    handler_name, action.description,
                )
                return action

        logger.info("[turn_recovery] no handler recovered error: %s", error)
        return None


__all__ = [
    "RecoveryAction",
    "RecoveryActionType",
    "RecoveryContext",
    "RecoveryHandler",
    "TurnRecoveryChain",
    "DEFAULT_HANDLERS",
    "recover_unicode_encode_error",
    "recover_image_dimension_error",
    "recover_thinking_timeout",
    "recover_tool_sequence_interrupted",
    "recover_message_sanitization",
]
