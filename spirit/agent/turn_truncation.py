"""截断多策略恢复 (R-P2.1)。

Hermes `agent/turn_truncation.py` 的 Spirit 适配版。覆盖 6 类截断场景：

1. **思考耗尽阻断** (`detect_thinking_exhausted`): 思考模型把输出配额全耗在
   ``<think>...</think>`` 推理上，可见文本为空——不得续传（再发也只会再把配额
   烧在推理里），直接结束。
2. **Repetition 阻断** (`detect_repetition`): 连续多轮助手回复高度同构（如
   重复执行相同工具调用 / 反复重说同一句话），续传只是把循环放大——直接截断
   返回最近一次有效响应 + 警告。
3. **文本截断续传** (`recover_text_truncation`): ``finish_reason='length'``
   且无工具调用——保存当前可见文本 + 注入续写提示再推一轮，让模型从断点
   继续；上限 2 次（Spirit 配置比 Hermes 4 更严，因 Spirit fallback chain
   比 Hermes 简单）。
4. **工具调用截断重试** (`recover_tool_truncation`): 流在工具调用参数中途
   中断（provider 返回了不完整 JSON / 函数签名）——拒绝执行不完整工具，
   注入 "重发完整工具调用" 提示再推一轮。
5. **内容过滤切换 fallback** (`recover_content_filter_refusal`):
   ``finish_reason in {'content_filter', 'refusal', 'stop_reason='refusal'`` —
   provider 安全拒绝，请求 Spirit `provider_switch_protection` 切换 fallback。
6. **上下文窗口填满终止** (`context_window_filled`): 提示已用满上下文（剩余
   headroom < 512 tokens），续传只会再把同一 prompt 推到同一模型——直接
   结束并附窗口填满通知。

主入口 `detect_truncation()` 返回 `TruncationVerdict`，由
`conversation_loop.run_conversation` 据 `verdict.action` 决定:

- ``"continue"`` —— 在 messages 注入续写提示后 `continue` 主循环
- ``"abort"`` —— 终结本 turn，返回 ``verdict.final_response``
- ``"fallback"`` —— 触发 provider 切换，重启 API 调用

设计要点：

- **不依赖 Hermes ``conversation_compression.context_length``**：Spirit
  `ContextCompressorV2` 的接口不同，本模块读取 agent 暴露的 ``max_context``
  / ``context_length`` 字段（由 agent.py 注入），缺省 0 表示禁用该判定。
- **复用 ``_clean_for_display``**：strip <think></think> / <tool_call> 标签
  后的可见文本——延迟导入避免循环依赖。
- **defensive getattr**：所有 agent 字段读取走 ``getattr`` 兼容测试用
  FakeAgent。
- **纯函数风格**（不依赖 SpiritAgent 实例状态）：模块级 function + first
  arg agent，与 Hermes 一致。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("spirit.agent.turn_truncation")


# ──────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────

# 思考块标签正则（与 conversation_loop._STREAM_OPENER_RE 对齐）
_THINK_TAG_RE = re.compile(
    r"<\s*/?\s*(?:think|thinking|reasoning|REASONING_SCRATCHPAD)\s*>",
    re.IGNORECASE,
)

# Repetition 阻断阈值：连续 N 轮助手回复高度相似触发
_REPETITION_WINDOW = 3
_REPETITION_SIMHASH_RATIO = 0.85  # 字符集 jaccard >= 该值视为同构

# 内容过滤类 finish_reason
_CONTENT_FILTER_REASONS = frozenset({
    "content_filter", "guardrail_intervened", "refusal",
})

# 单轮内截断自动续写最大次数（与 conversation_loop._MAX_TRUNCATION_CONTINUATIONS 对齐）
# 保留 Spirit 较严的值（2 而非 Hermes 的 4）——Spirit fallback chain 较薄，
# 多续一轮只会多烧一轮 token；超限即终结返回 partial。
DEFAULT_MAX_TRUNCATION_CONTINUATIONS = 2

# 上下文窗口填满阈值（剩余 headroom 小于该值视为窗口已满）
_WINDOW_HEADROOM_TOKENS = 512

# 续传提示文案（拆 4 类，对应不同截断原因）
_TEXT_CONTINUATION_PROMPT = (
    "[系统提示] 你上一轮输出因长度上限被截断，其中的工具调用未执行。"
    "请直接从中断处继续任务：重新发出完整的工具调用，"
    "不要重复已写过的说明文字。"
)
_TOOL_CONTINUATION_PROMPT = (
    "[系统提示] 你上一轮的工具调用参数不完整（流中断或 JSON 被截断），"
    "该调用未被执行。请重新发出完整的工具调用参数，"
    "确保 arguments JSON 完整闭合。"
)


# ──────────────────────────────────────────────────────────────────────
# 数据类
# ──────────────────────────────────────────────────────────────────────


@dataclass
class TruncationVerdict:
    """截断恢复判决。``action`` 决定主循环下一步：

    - ``"continue"`` —— 注入续写 prompt 后 ``continue`` 主循环
    - ``"abort"`` —— 终结本 turn，``final_response`` 即用户可见的最终回复
    - ``"fallback"`` —— 触发 provider 切换，重启 API 调用
    """

    action: str
    final_response: str = ""
    error: Optional[str] = None
    # 续传计数（仅 ``continue`` 时增加；abort / fallback 时不变）
    continuation_attempts: int = 0
    # 是否需要重启 API 调用（fallback 时 True；continue 时仅重置部分状态）
    restart_api_call: bool = False
    # 内容过滤触发时的语义原因（供 provider_switch_protection 决策 fallback）
    reason: Optional[str] = None
    # 诊断信息（用于日志 / UI 显示）
    diagnostics: Dict[str, Any] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────
# 入口：detect_truncation
# ──────────────────────────────────────────────────────────────────────


def detect_truncation(
    agent: Any,
    choice: Any,
    messages: List[Dict[str, Any]],
    *,
    finish_reason: str,
    continuation_attempts: int = 0,
    max_continuations: int = DEFAULT_MAX_TRUNCATION_CONTINUATIONS,
) -> TruncationVerdict:
    """截断检测主入口。

    按以下顺序判定（先匹配先返回）：

    1. **Repetition 阻断** —— 重复循环（即便 finish_reason=stop 也算事故）
    2. **思考耗尽阻断** —— finish_reason=length + 仅 think 块无可见文本
    3. **内容过滤切换 fallback** —— finish_reason in content_filter 集合
    4. **窗口填满终止** —— 提示已用满 max_context
    5. **文本截断续传** —— finish_reason=length + 有可见文本 + 未超限
    6. **默认** —— ``abort`` (action=fallthrough, final_response="")

    注意：原 conversation_loop 内的 length 续传逻辑保持兼容；本函数作为
    **额外** 的多策略入口被调用，旧逻辑仍作为兜底保留（双层防御）。
    """
    content = getattr(getattr(choice, "message", None), "content", "") or ""

    # 1) Repetition 阻断（最优先——避免把循环放大）
    rep = _check_repetition(messages, content)
    if rep is not None:
        logger.warning(
            "[R-P2.1] repetition 阻断: 最近 %d 轮助手回复 jaccard=%.2f >= %.2f",
            _REPETITION_WINDOW, rep, _REPETITION_SIMHASH_RATIO,
        )
        return TruncationVerdict(
            action="abort",
            final_response=(
                f"[repetition 中断] 最近 {_REPETITION_WINDOW} 轮回复高度相似"
                f" (jaccard={rep:.2f})，自动中断以免循环放大。"
                "请重新组织问题或明确下一步动作。"
            ),
            error="repetition_dominated",
            diagnostics={"repetition_jaccard": rep},
        )

    # 2) 思考耗尽阻断（finish_reason=length + 仅 think 块 → 续传只会再烧配额）
    if finish_reason == "length" and content:
        stripped = _strip_think_blocks(content)
        # strip 后为空 → 思考耗尽（包含 stripped 完全为空的边界）
        if not stripped.strip():
            logger.warning(
                "[R-P2.1] 思考耗尽阻断: finish_reason=length 但仅 think 块无可见文本"
            )
            return TruncationVerdict(
                action="abort",
                final_response=(
                    "[思考耗尽] 上一轮输出配额全部消耗在内部推理上，无可见结论。"
                    "已自动结束本 turn，建议拆解问题或换用非思考模型。"
                ),
                error="thinking_exhausted",
                diagnostics={"content_len": len(content)},
            )

    # 3) 内容过滤切换 fallback
    if finish_reason in _CONTENT_FILTER_REASONS:
        logger.warning(
            "[R-P2.1] 内容过滤触发 fallback: finish_reason=%s", finish_reason,
        )
        return TruncationVerdict(
            action="fallback",
            final_response="",
            reason=f"content_filter:{finish_reason}",
            restart_api_call=True,
            diagnostics={"finish_reason": finish_reason},
        )

    # 4) 窗口填满终止
    if finish_reason == "length":
        ctx_info = _window_filled(agent)
        if ctx_info is not None:
            prompt_tokens, context_length = ctx_info
            logger.warning(
                "[R-P2.1] 窗口填满终止: prompt=%d / ctx=%d (headroom < %d)",
                prompt_tokens, context_length, _WINDOW_HEADROOM_TOKENS,
            )
            return TruncationVerdict(
                action="abort",
                final_response=(
                    f"[窗口填满] 提示已使用 {prompt_tokens} / {context_length} tokens，"
                    "剩余空间不足以续传，本 turn 结束。"
                ),
                error="context_window_filled",
                diagnostics={
                    "prompt_tokens": prompt_tokens,
                    "context_length": context_length,
                },
            )

    # 5) 文本截断续传
    if finish_reason == "length" and continuation_attempts < max_continuations:
        return TruncationVerdict(
            action="continue",
            continuation_attempts=continuation_attempts + 1,
            diagnostics={
                "strategy": "text_continuation",
                "attempt": continuation_attempts + 1,
                "max": max_continuations,
            },
        )

    # 6) 默认 fallthrough（不截断，正常结束）
    return TruncationVerdict(action="abort", final_response="")


# ──────────────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────────────


def _strip_think_blocks(text: str) -> str:
    """剥离 <think>...</think> / <thinking>...</thinking> 标签及其内容。"""
    if not text:
        return ""
    # 先剥标签内容（multiline）
    cleaned = re.sub(
        r"<\s*(?:think|thinking|reasoning|REASONING_SCRATCHPAD)\s*>.*?"
        r"<\s*/\s*(?:think|thinking|reasoning|REASONING_SCRATCHPAD)\s*>",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # 再剥残留的孤立开闭标签
    cleaned = re.sub(
        r"<\s*/?\s*(?:think|thinking|reasoning|REASONING_SCRATCHPAD)\s*>",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned


def _check_repetition(
    messages: List[Dict[str, Any]], current_content: str,
) -> Optional[float]:
    """检测最近 _REPETITION_WINDOW 轮助手回复是否高度相似。

    返回 jaccard 相似度（0-1）；若未达窗口长度或不含历史助手消息则返回 None。
    """
    if not current_content:
        return None
    # 抽最近 _REPETITION_WINDOW 条 role=assistant 消息的 content
    history: List[str] = []
    for m in reversed(messages):
        if not isinstance(m, dict):
            continue
        if m.get("role") != "assistant":
            continue
        c = m.get("content") or ""
        if not c.strip():
            continue
        history.append(c)
        if len(history) >= _REPETITION_WINDOW:
            break

    if len(history) < _REPETITION_WINDOW:
        return None

    cur_set = _char_ngrams(current_content, 5)
    if not cur_set:
        return None
    # 与最近一轮作 jaccard 比较（避免每条都算，且只有最新一轮就能触发）
    last_set = _char_ngrams(history[0], 5)
    if not last_set:
        return None
    intersection = cur_set & last_set
    union = cur_set | last_set
    jaccard = len(intersection) / len(union) if union else 0.0
    return jaccard if jaccard >= _REPETITION_SIMHASH_RATIO else None


def _char_ngrams(text: str, n: int = 5) -> set:
    """返回字符串字符级 n-gram 集合（去空白避免格式噪声）。"""
    compact = re.sub(r"\s+", "", text)
    if len(compact) < n:
        return {compact}
    return {compact[i:i + n] for i in range(len(compact) - n + 1)}


def _window_filled(agent: Any) -> Optional[Tuple[int, int]]:
    """``(prompt_tokens, context_length)`` 当 prompt 已用满上下文时。

    Spirit 不像 Hermes 那样把 ``context_length`` 暴露在
    ``context_compressor``，而是直接挂在 agent 实例（由 agent.py 注入）。

    - 缺字段 → 返回 None（不触发窗口填满判定）
    - ``max_context`` 或 ``context_length`` <= 0 → 返回 None
    - ``last_prompt_tokens`` 未记录 → 返回 None
    """
    ctx = (
        getattr(agent, "max_context", 0)
        or getattr(agent, "context_length", 0)
        or 0
    )
    prompt = getattr(agent, "last_prompt_tokens", 0) or 0
    if not ctx or not prompt:
        return None
    if ctx - prompt < _WINDOW_HEADROOM_TOKENS:
        return (prompt, ctx)
    return None


# ──────────────────────────────────────────────────────────────────────
# 续传 / 重试 prompt 工厂
# ──────────────────────────────────────────────────────────────────────


def build_text_continuation_prompt() -> str:
    """返回文本截断续传提示文案。供 conversation_loop 注入消息。"""
    return _TEXT_CONTINUATION_PROMPT


def build_tool_continuation_prompt() -> str:
    """返回工具调用截断续传提示文案。供 conversation_loop 注入消息。"""
    return _TOOL_CONTINUATION_PROMPT
