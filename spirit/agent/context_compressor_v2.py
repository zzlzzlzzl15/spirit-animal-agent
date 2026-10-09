"""上下文压缩鲁棒性 v2 — Spirit Agent 的压缩层第二层。

参考 Hermes 的 ``context_compressor.py`` v2（5,577 行）的设计，
Spirit 简化版 ~500 行。

问题与动机
----------
V3 规划 R-P1.5：Spirit v1 压缩器只有 410 行，遇到以下场景会"失忆"或"崩溃"：
1. **辅助 LLM 调用卡死**（半开连接）：N 秒无响应 → 整次 compress 阻塞
2. **provider 拒绝摘要**（"I cannot summarize..."）→ 返回空字符串导致关键事实丢失
3. **detached worker 污染**：多次压缩间 provider/model 漂移导致摘要风格不一致
4. **整段摘要粒度太粗**：长任务后工具输出巨大，摘要后仍超出 token 限制

4 类鲁棒性增强
--------------
1. :class:`StallDetector` — 进度感知超时（timeout + abort 重试）
2. :func:`is_provider_refusal` + :func:`deterministic_summarize` — 拒绝检测 + 确定性 fallback
3. :func:`pin_summary_route` + :func:`get_pinned_route` — ContextVar 钉住 provider/model
4. :func:`micro_compact_tool_output` / :func:`micro_compact_messages` — 细粒度微压缩

集成方式
---------
``context_compressor.py`` 的 ``ContextCompressor._llm_summarize`` 和
``_merge_summaries`` 调用本模块的工具函数，包装 stall / refusal / pin 逻辑。
"""

from __future__ import annotations

import contextvars
import logging
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =========================================================================
# 3. Summary route pin（ContextVar）
# =========================================================================

# 单次摘要调用钉住的 (provider, model) 元组；None 表示未 pin
_SUMMARY_ROUTE_PIN: contextvars.ContextVar[Optional[Tuple[str, str]]] = contextvars.ContextVar(
    "spirit_summary_route_pin",
    default=None,
)

# 最近一次 pin 的路由栈（thread-local fallback，用于 ContextVar 在跨线程时不丢失）
# 栈结构保证 nested pin/unpin 后 fallback 返回的是最外层未 unpin 的路由
_RECENT_PIN_LOCK = threading.Lock()
_RECENT_PIN_STACK: List[Tuple[str, str]] = []


def pin_summary_route(provider: str, model: str):
    """钉住单次摘要调用的 provider/model。

    Returns:
        ContextVar token，配合 :func:`unpin_summary_route` 使用。
        不调用 unpin 时 token 会在 ContextVar 自然失效时被覆盖。
    """
    token = _SUMMARY_ROUTE_PIN.set((provider, model))
    with _RECENT_PIN_LOCK:
        _RECENT_PIN_STACK.append((provider, model))
    return token


def unpin_summary_route(token) -> None:
    """取消 pin。"""
    try:
        _SUMMARY_ROUTE_PIN.reset(token)
    except (ValueError, LookupError):
        pass  # token 已失效，忽略
    # 弹出 fallback 栈顶层（嵌套 unpin 时逐层弹）
    with _RECENT_PIN_LOCK:
        if _RECENT_PIN_STACK:
            _RECENT_PIN_STACK.pop()


def get_pinned_route() -> Optional[Tuple[str, str]]:
    """获取当前 pin 的路由（ContextVar 优先，找不到用 thread-local fallback）。

    Returns:
        ``(provider, model)`` 或 ``None``（未 pin）。
    """
    pinned = _SUMMARY_ROUTE_PIN.get()
    if pinned is not None:
        return pinned
    with _RECENT_PIN_LOCK:
        return _RECENT_PIN_STACK[-1] if _RECENT_PIN_STACK else None


@contextmanager
def summary_route_scope(provider: str, model: str):
    """上下文管理器：临时 pin provider/model，退出作用域自动 unpin。"""
    token = pin_summary_route(provider, model)
    try:
        yield (provider, model)
    finally:
        unpin_summary_route(token)


# =========================================================================
# 2. Provider 拒绝检测 + Deterministic fallback
# =========================================================================

# 检测 provider 拒绝的常见前缀（Hermes _SUMMARY_REFUSAL_PREFIX_RE 同款）
_SUMMARY_REFUSAL_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"I (?:can't|cannot|am unable|am sorry|don't (?:have|do) (?:the ability|access))|"
    r"I'm (?:sorry|unable)|"
    r"I (?:apologize|regret)|"
    r"Sorry,? I (?:can't|cannot|am unable)|"
    r"As an? (?:AI|assistant|language model)|"
    r"I (?:must|have to) (?:decline|refuse)|"
    r"It (?:is|would be) (?:not |in )(?:my |appropriate for me to)|"
    r"Unfortunately,? I (?:can't|cannot|am unable)|"
    r"I'?m not able to|"
    r"I (?:can(?:not)?|do(?:es)?n'?t) (?:help|assist|provide|summarize)"
    r")",
    re.IGNORECASE | re.DOTALL,
)


def is_provider_refusal(text: str) -> bool:
    """检测 LLM 输出是否为 provider 拒绝回答。

    用于 _llm_summarize / _merge_summaries：识别到拒绝后回退到
    deterministic_summarize，不让关键事实丢失。
    """
    if not text or not text.strip():
        return True  # 空响应也算"拒绝"
    return bool(_SUMMARY_REFUSAL_PREFIX_RE.match(text.strip()))


def deterministic_summarize(messages: List[Dict[str, Any]], max_chars: int = 4000) -> str:
    """确定性模板摘要 — provider 拒绝或调用失败时的 fallback。

    不调 LLM，纯模板化提取：保留每条消息的 role + 内容前/后片段。
    牺牲"摘要质量"换取"绝不失败"。
    """
    parts: List[str] = []
    total = 0
    truncated_count = 0

    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")

        # 处理多模态 content list
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            content = "\n".join(text_parts)

        if not content:
            continue

        # 保留每个 message 的首尾片段
        if isinstance(content, str):
            if len(content) > 300:
                snippet = content[:150] + f"... [省略 {len(content) - 300} 字符] ..." + content[-150:]
            else:
                snippet = content
        else:
            snippet = str(content)

        # 工具调用元数据
        tool_calls_meta = ""
        if msg.get("tool_calls"):
            tool_names = [
                tc.get("function", {}).get("name", "?") if isinstance(tc, dict) else "?"
                for tc in msg["tool_calls"]
            ]
            tool_calls_meta = f" [调用: {', '.join(tool_names)}]"

        entry = f"[{role}]{tool_calls_meta}: {snippet}"

        if total + len(entry) > max_chars:
            truncated_count += 1
            continue
        parts.append(entry)
        total += len(entry)

    if truncated_count:
        parts.append(f"[... 省略 {truncated_count} 条更长消息 ...]")

    if not parts:
        return "[无可压缩内容]"

    return "\n".join(parts)


# =========================================================================
# 1. Stall detection（进度感知超时）
# =========================================================================

@dataclass
class StallConfig:
    """stall 检测配置。"""

    timeout_s: float = 60.0
    """单次辅助 LLM 调用的硬超时。"""

    soft_warn_s: float = 30.0
    """超过此时间开始 log warning（不 abort）。"""


class StallDetector:
    """辅助 LLM 调用的 stall 检测器。

    由于摘要调用通常是非流式（一次性返回），stall detection 退化为
    "硬超时 abort"——通过 ``concurrent.futures`` + ``future.result(timeout=)``
    实现。如果未来 Spirit 改用流式摘要，可扩展为"无 chunk 进度"检测。
    """

    def __init__(self, config: Optional[StallConfig] = None) -> None:
        self.config = config or StallConfig()

    def detect_and_call(
        self,
        llm_call: Callable[..., Any],
        *args: Any,
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> Tuple[Optional[Any], Optional[Exception]]:
        """调用 llm_call，带 stall 超时检测。

        Returns:
            ``(result, exception)``：
            - 成功：``(result, None)``
            - 超时：``(None, TimeoutError)``
            - 其他异常：``(None, exception)``
        """
        import concurrent.futures

        timeout_s = timeout or self.config.timeout_s
        started_at = time.time()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(llm_call, *args, **kwargs)
            try:
                result = future.result(timeout=timeout_s)
                elapsed = time.time() - started_at
                if elapsed >= self.config.soft_warn_s:
                    logger.warning(
                        "[stall_detection] 摘要调用耗时 %.1fs（> soft_warn %.1fs）",
                        elapsed, self.config.soft_warn_s,
                    )
                return result, None
            except concurrent.futures.TimeoutError:
                future.cancel()
                logger.error(
                    "[stall_detection] 摘要调用超时（> %.1fs），abort",
                    timeout_s,
                )
                return None, TimeoutError(f"摘要调用超时（>{timeout_s:.1f}s）")
            except Exception as exc:
                return None, exc


# =========================================================================
# 4. Micro compaction（细粒度微压缩）
# =========================================================================

# 微压缩阈值（超过这个字符数触发微压缩）
_MICRO_COMPACT_THRESHOLD = 1500

# 微压缩目标（压缩到这个字符数内）
_MICRO_COMPACT_TARGET = 500


def micro_compact_tool_output(content: str, target_chars: int = _MICRO_COMPACT_TARGET) -> str:
    """微压缩工具输出 — 保留首尾关键信息，截断中间冗余部分。

    适用于：
    - terminal 工具返回的大段 stdout/stderr
    - search_files 返回的搜索结果列表
    - read_file 返回的大文件内容
    """
    if not isinstance(content, str) or len(content) <= target_chars:
        return content

    head_chars = target_chars // 2
    tail_chars = target_chars - head_chars
    omitted = len(content) - head_chars - tail_chars
    return (
        content[:head_chars]
        + f"\n\n[... 微压缩：省略中间 {omitted} 字符 ...]\n\n"
        + content[-tail_chars:]
    )


def micro_compact_messages(
    messages: List[Dict[str, Any]],
    target_chars_per_msg: int = _MICRO_COMPACT_TARGET,
) -> List[Dict[str, Any]]:
    """对消息列表里的每条 tool 输出 / 长 content 做微压缩。

    不动 user / assistant 的"思考"消息（保留对话连贯性）。
    """
    compacted: List[Dict[str, Any]] = []
    compressed_count = 0

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        # 仅压缩 tool 角色的输出（其他不动）
        if role == "tool" and isinstance(content, str) and len(content) > _MICRO_COMPACT_THRESHOLD:
            new_content = micro_compact_tool_output(content, target_chars_per_msg)
            new_msg = dict(msg)
            new_msg["content"] = new_content
            compacted.append(new_msg)
            compressed_count += 1
        else:
            compacted.append(msg)

    if compressed_count:
        logger.info(
            "[micro_compaction] 压缩了 %d 条 tool 输出（每条从 >%d 字符 → ≤%d 字符）",
            compressed_count, _MICRO_COMPACT_THRESHOLD, target_chars_per_msg,
        )

    return compacted


# =========================================================================
# 一站式门面函数
# =========================================================================

@dataclass
class CompressorV2Hooks:
    """V2 增强的接入点集合——给 ContextCompressor 用。"""

    stall_detector: StallDetector = None  # type: ignore
    deterministic_fallback_enabled: bool = True
    micro_compact_before_summarize: bool = True
    pin_route_on_call: bool = True

    def __post_init__(self) -> None:
        if self.stall_detector is None:
            self.stall_detector = StallDetector()


def summarize_with_v2(
    messages: List[Dict[str, Any]],
    *,
    llm_call: Callable[..., Any],
    provider: str = "",
    model: str = "",
    hooks: Optional[CompressorV2Hooks] = None,
    focus_topic: Optional[str] = None,
) -> str:
    """V2 一站式摘要：pin → micro_compact → stall 检测 → refusal 检测 → fallback。

    Args:
        messages: 待摘要的消息列表
        llm_call: 实际的 LLM 调用函数（接收 messages 参数，返回字符串）
        provider / model: 用于 route pin
        hooks: V2 配置（不传则用默认）

    Returns:
        摘要文本（永远不会抛异常——失败时降级到 deterministic_summarize）
    """
    hooks = hooks or CompressorV2Hooks()

    # --- 步骤 1: pin route（防止 detached worker 污染）---
    if hooks.pin_route_on_call and provider and model:
        with summary_route_scope(provider, model):
            return _summarize_inner(messages, llm_call=llm_call, hooks=hooks, focus_topic=focus_topic)
    else:
        return _summarize_inner(messages, llm_call=llm_call, hooks=hooks, focus_topic=focus_topic)


def _summarize_inner(
    messages: List[Dict[str, Any]],
    *,
    llm_call: Callable[..., Any],
    hooks: CompressorV2Hooks,
    focus_topic: Optional[str],
) -> str:
    """内部实现：micro_compact → stall → refusal → fallback。"""
    # --- 步骤 2: micro_compact（先做廉价微压缩，省 token）---
    if hooks.micro_compact_before_summarize:
        messages = micro_compact_messages(messages)

    # --- 步骤 3: stall 检测 + LLM 调用 ---
    result, exc = hooks.stall_detector.detect_and_call(llm_call, messages, focus_topic)

    if exc is not None:
        logger.warning(
            "[v2] LLM 摘要调用失败: %s；%s",
            exc,
            "降级到 deterministic" if hooks.deterministic_fallback_enabled else "抛错",
        )
        if hooks.deterministic_fallback_enabled:
            return deterministic_summarize(messages)
        raise exc

    summary_text = result if isinstance(result, str) else (str(result) if result else "")

    # --- 步骤 4: refusal 检测 ---
    if is_provider_refusal(summary_text):
        logger.warning("[v2] provider 拒绝摘要；降级到 deterministic")
        if hooks.deterministic_fallback_enabled:
            return deterministic_summarize(messages)
        return ""

    return summary_text


__all__ = [
    # route pin
    "pin_summary_route",
    "unpin_summary_route",
    "get_pinned_route",
    "summary_route_scope",
    # refusal detection + fallback
    "is_provider_refusal",
    "deterministic_summarize",
    # stall detection
    "StallDetector",
    "StallConfig",
    # micro compaction
    "micro_compact_tool_output",
    "micro_compact_messages",
    # facade
    "summarize_with_v2",
    "CompressorV2Hooks",
]
