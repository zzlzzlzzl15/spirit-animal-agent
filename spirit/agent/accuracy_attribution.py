"""准确率归因 + 提升 — Spirit Agent 的错误归因层。

参考 Hermes ``accuracy_attribution.py``（Hermes 在 swe-bench runner 里
做的归因分析），Spirit 简化版 ~250 行。

问题与动机
----------
V3 规划 M-P1.0：Spirit 单任务准确率 62% < Hermes 80%。根因拆解需要：
- **归因**：5 类错误分类，每类占比 → 决定后续功能项优先级
- **MoA 兜底**：主 LLM 信心低时调一次参考模型
- **自我纠错轻量版**：上一轮结果"看起来不对" → 注入审视提示

5 类错误分类（V3 规划）
-----------------------
1. :attr:`ErrorCategory.TOOL_FAILURE_NO_REPLAN` — 工具失败没换方案（最常见）
2. :attr:`ErrorCategory.OUTPUT_TRUNCATION` — 输出截断没续写
3. :attr:`ErrorCategory.PROVIDER_SWITCH_LOST` — provider 切换上下文丢失
4. :attr:`ErrorCategory.THINKING_TIMEOUT` — 思考超时
5. :attr:`ErrorCategory.OTHER` — 其它

自我纠错触发条件
----------------
- 上一轮 assistant 回复包含 ``<error>`` / ``failed`` / ``Exception``
- 用户后续追问"哪里不对" / "为什么" / "再看看"
- 工具结果包含 ``"status": "error"`` 且 LLM 没换方案

MoA 兜底触发条件
----------------
- 主 LLM 信心分 < 阈值（默认 0.7）—— Spirit 暂无模型内置 logprobs；
  启发式判断：回复长度极短 / 包含"我不确定" / 包含 fallback 标记

集成方式
--------
``conversation_loop.py`` 在主 LLM 调用后：
1. :func:`should_self_correct` 检测是否触发自我纠错 → 是则注入提示
2. :func:`should_activate_moa` 检测是否启用 MoA 参考模型
"""

from __future__ import annotations

import enum
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# =========================================================================
# 错误分类枚举
# =========================================================================

class ErrorCategory(str, enum.Enum):
    """5 类错误归因。"""

    TOOL_FAILURE_NO_REPLAN = "tool_failure_no_replan"
    """工具失败没换方案（最常见）：拿到失败工具结果后没换参数/工具。"""

    OUTPUT_TRUNCATION = "output_truncation"
    """输出截断没续写：finish_reason=length 后没注入续写提示。"""

    PROVIDER_SWITCH_LOST = "provider_switch_lost"
    """provider 切换上下文丢失：切换后 LLM 看到不一致的上下文。"""

    THINKING_TIMEOUT = "thinking_timeout"
    """思考超时：thinking 模型推理超时被 abort。"""

    OTHER = "other"
    """其它错误（语法、格式、内容政策等）。"""


# =========================================================================
# 数据类
# =========================================================================

@dataclass
class TaskErrorSample:
    """单个任务的错误样本。"""

    task_name: str
    category: ErrorCategory
    raw_error: str = ""
    """原始错误消息 / 工具结果 / LLM 输出片段。"""

    context: Dict[str, Any] = field(default_factory=dict)
    """上下文（tool_name / finish_reason / provider / 等）。"""


@dataclass
class AccuracyAttributionReport:
    """批量归因报告（每类计数 + 占比 + 总数）。"""

    samples: List[TaskErrorSample] = field(default_factory=list)
    total: int = 0

    def add(self, sample: TaskErrorSample) -> None:
        self.samples.append(sample)
        self.total += 1

    def count_by_category(self) -> Dict[str, int]:
        """按类别计数。"""
        counter: Counter = Counter()
        for s in self.samples:
            counter[s.category.value] += 1
        return dict(counter)

    def ratio_by_category(self) -> Dict[str, float]:
        """按类别占比（0.0 - 1.0）。"""
        if self.total == 0:
            return {}
        counts = self.count_by_category()
        return {k: v / self.total for k, v in counts.items()}

    def top_category(self) -> Optional[ErrorCategory]:
        """占比最高的类别（没有样本返回 None）。"""
        if not self.samples:
            return None
        counts = self.count_by_category()
        top_value = max(counts.values())
        top_keys = [k for k, v in counts.items() if v == top_value]
        if len(top_keys) == 1:
            return ErrorCategory(top_keys[0])
        # 平局：按枚举顺序
        for cat in ErrorCategory:
            if cat.value in top_keys:
                return cat
        return None

    def to_markdown(self) -> str:
        """导出为 markdown 报告（人读）。"""
        lines = [
            "# Accuracy Attribution Report",
            "",
            f"**Total samples**: {self.total}",
            "",
            "## Distribution",
            "",
            "| Category | Count | Ratio |",
            "|----------|-------|-------|",
        ]
        counts = self.count_by_category()
        ratios = self.ratio_by_category()
        for cat in ErrorCategory:
            c = counts.get(cat.value, 0)
            r = ratios.get(cat.value, 0.0)
            lines.append(f"| {cat.value} | {c} | {r:.1%} |")
        top = self.top_category()
        if top:
            lines.append("")
            lines.append(f"**Top category**: `{top.value}` ({ratios.get(top.value, 0.0):.1%})")
        return "\n".join(lines)


# =========================================================================
# 错误分类器
# =========================================================================

# 各 category 的指纹正则（按优先级匹配，第一个命中即返回）
_TRUNCATION_RE = re.compile(
    r"finish_reason[\"']?\s*[:=]\s*[\"']?length|"
    r"output_truncated|"
    r"截断|max_tokens|"
    r"truncation_continuations",
    re.IGNORECASE,
)

_PROVIDER_SWITCH_RE = re.compile(
    r"provider.*(switch|switched|fallback)|"
    r"已切换 fallback|"
    r"provider_mismatch|"
    r"primary_runtime.*lost|"
    r"切换.*上下文",
    re.IGNORECASE,
)

_THINKING_TIMEOUT_RE = re.compile(
    r"thinking.*timeout|"
    r"思考超时|"
    r"reasoning.*timeout|"
    r"thought.*exceed|"
    r"max_thinking_tokens",
    re.IGNORECASE,
)

_TOOL_FAILURE_RE = re.compile(
    r"工具.*失败|"
    r"tool.*fail|"
    r"\"error\"\s*:\s*\"|"
    r"\"status\"\s*:\s*\"(error|timeout|cancelled)\"|"
    r"参数.*无效|"
    r"invalid argument|"
    r"parse error",
    re.IGNORECASE,
)

# 用户追问"哪里不对"的指纹
_USER_FOLLOWUP_RE = re.compile(
    r"哪里不对|为什么|再看看|错了|不对|不对的|"
    r"what('?s| is) wrong|why did you|"
    r"please reconsider|"
    r"are you sure",
    re.IGNORECASE,
)


def classify_error_cause(
    raw_error: str = "",
    *,
    context: Optional[Dict[str, Any]] = None,
) -> ErrorCategory:
    """把一条错误 / 输出 / 工具结果归到 5 类之一。

    Args:
        raw_error: 原始错误消息或工具结果或 LLM 输出片段
        context: 上下文 dict（可选字段）：
            - tool_name: 失败的工具名（"terminal" / "search_files" / ...）
            - finish_reason: LLM 响应的 finish_reason
            - provider: 当前 provider
            - provider_switched: 是否本轮发生过 provider 切换
            - thinking_timeout: 是否思考超时

    Returns:
        :class:`ErrorCategory` 枚举值
    """
    ctx = context or {}

    # 优先级：context 显式字段 > 文本指纹

    # 1. thinking_timeout context 直接判断
    if ctx.get("thinking_timeout"):
        return ErrorCategory.THINKING_TIMEOUT

    # 2. provider 切换后丢失 → context 优先
    if ctx.get("provider_switched") and ctx.get("context_lost"):
        return ErrorCategory.PROVIDER_SWITCH_LOST

    # 3. finish_reason=length → truncation
    if str(ctx.get("finish_reason", "")).lower() == "length":
        return ErrorCategory.OUTPUT_TRUNCATION

    # 4. 文本指纹匹配（按优先级）
    text = raw_error or ""
    if _TRUNCATION_RE.search(text):
        return ErrorCategory.OUTPUT_TRUNCATION
    if _PROVIDER_SWITCH_RE.search(text):
        return ErrorCategory.PROVIDER_SWITCH_LOST
    if _THINKING_TIMEOUT_RE.search(text):
        return ErrorCategory.THINKING_TIMEOUT
    if _TOOL_FAILURE_RE.search(text):
        return ErrorCategory.TOOL_FAILURE_NO_REPLAN

    # 5. 兜底
    return ErrorCategory.OTHER


# =========================================================================
# 批量归因
# =========================================================================

def attribute_task_errors(
    samples: List[Dict[str, Any]],
) -> AccuracyAttributionReport:
    """批量归因——接受 ``[{"task_name": ..., "raw_error": ..., "context": ...}, ...]``。

    Args:
        samples: 原始样本列表（每个 dict 含 task_name + raw_error + context）

    Returns:
        :class:`AccuracyAttributionReport` 实例
    """
    report = AccuracyAttributionReport()
    for s in samples:
        category = classify_error_cause(
            raw_error=s.get("raw_error", ""),
            context=s.get("context", {}),
        )
        report.add(TaskErrorSample(
            task_name=s.get("task_name", "<unnamed>"),
            category=category,
            raw_error=s.get("raw_error", ""),
            context=s.get("context", {}),
        ))
    return report


# =========================================================================
# MoA 兜底判断
# =========================================================================

# 主 LLM 信心低的指纹（启发式）
_LOW_CONFIDENCE_RE = re.compile(
    r"我不确定|无法确定|可能|也许|或许|"
    r"i('?m| am) not (sure|certain)|"
    r"it('?s| is) unclear|"
    r"possibly|maybe|perhaps",
    re.IGNORECASE,
)

# 主 LLM 信心高的指纹（让 MoA 不触发）
_HIGH_CONFIDENCE_RE = re.compile(
    r"我(已|已经)(完成|做完|修改)|"
    r"修改后的.*(代码|文件)|"
    r"the (updated|modified) (code|file)|"
    r"successfully",
    re.IGNORECASE,
)


def should_activate_moa(
    response_text: str = "",
    *,
    response_length: Optional[int] = None,
    threshold_length: int = 50,
    confidence_threshold: float = 0.7,
) -> bool:
    """判断是否启用 MoA（参考模型）兜底。

    启发式判断（Spirit 暂无模型内置 logprobs）：
    - 回复极短（< threshold_length 字符）
    - 包含"我不确定" / "可能" / "也许" 等低信心短语
    - 不包含"成功" / "完成" 等高信心短语

    Args:
        response_text: 主 LLM 的响应文本
        response_length: 响应长度（可选；不传则按 ``len(response_text)`` 计算）
        threshold_length: 低于此长度视为"太短"
        confidence_threshold: 信心阈值（保留参数；目前用启发式判断）

    Returns:
        True → 启用 MoA 兜底；False → 不启用
    """
    if not response_text:
        return True  # 空响应一定触发 MoA

    text_len = response_length if response_length is not None else len(response_text)

    # 长度极短
    if text_len < threshold_length:
        return True

    # 低信心指纹
    if _LOW_CONFIDENCE_RE.search(response_text):
        return True

    # 高信心指纹 → 不触发
    if _HIGH_CONFIDENCE_RE.search(response_text):
        return False

    # 默认：中等长度 + 无明确信心标记 → 不触发（避免噪音）
    return False


# =========================================================================
# 自我纠错判断
# =========================================================================

# 上一轮"看起来不对"的指纹
_SELF_CORRECT_TRIGGERS = [
    re.compile(r"<error>", re.IGNORECASE),
    re.compile(r"\"error\"\s*:\s*\"", re.IGNORECASE),
    re.compile(r"\"status\"\s*:\s*\"(error|timeout|cancelled|failed)\"", re.IGNORECASE),
    re.compile(r"\b(Exception|Traceback)\b", re.IGNORECASE),
    re.compile(r"\b(failed|FAIL)\b", re.IGNORECASE),
    re.compile(r"无法完成|失败|出错|错误", re.IGNORECASE),
]


def should_self_correct(
    previous_response: str = "",
    user_message: str = "",
    *,
    last_tool_results: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """判断是否应该触发自我纠错（注入"请审视上一轮答案"提示）。

    Args:
        previous_response: 上一轮 assistant 的回复文本
        user_message: 当前用户消息
        last_tool_results: 上一轮的工具结果列表（可选；含 status=error 的工具
                           + LLM 没换方案就触发）

    Returns:
        True → 触发自我纠错
    """
    # 1. 用户后续追问"哪里不对"——强信号
    if user_message and _USER_FOLLOWUP_RE.search(user_message):
        return True

    # 2. 上一轮包含错误指纹
    if previous_response:
        for pattern in _SELF_CORRECT_TRIGGERS:
            if pattern.search(previous_response):
                return True

    # 3. 工具结果含 status=error 且 LLM 没换方案
    if last_tool_results:
        for result in last_tool_results:
            content = result.get("content", "")
            if isinstance(content, str):
                if '"status": "error"' in content or '"status": "timeout"' in content:
                    return True

    return False


def build_self_correction_prompt(
    reason: str = "上一轮结果包含错误标识",
) -> str:
    """构造自我纠错提示（注入到 messages 里让 LLM 重新审视）。"""
    return (
        "[自我纠错提示]\n"
        f"触发原因: {reason}\n\n"
        "请审视你上一轮的回复是否真的解决了用户的问题。\n"
        "如果上一轮有错误 / 不完整 / 与用户问题不符，请直接重做并说明纠正了什么。\n"
        "不要无脑重复上一轮的答案，也不要假装问题已解决。"
    )


# =========================================================================
# agent 状态 / 调试辅助
# =========================================================================

def get_attribution_summary(report: AccuracyAttributionReport) -> Dict[str, Any]:
    """归因报告 → dict（用于 log / debug / agent state）。"""
    return {
        "total": report.total,
        "counts": report.count_by_category(),
        "ratios": report.ratio_by_category(),
        "top_category": report.top_category().value if report.top_category() else None,
    }


__all__ = [
    "ErrorCategory",
    "TaskErrorSample",
    "AccuracyAttributionReport",
    "classify_error_cause",
    "attribute_task_errors",
    "should_activate_moa",
    "should_self_correct",
    "build_self_correction_prompt",
    "get_attribution_summary",
]
