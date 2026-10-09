"""工具执行结果分类 — 让 LLM 区分"成功 / 部分成功 / 失败 / 可重试 / 不可重试"。

参考 Hermes ``tool_result_classification.py`` + ``tool_dispatch_helpers.py`` 的
ClassifiedToolResult 设计（V3 规划 R-P1.2，简化版 ~220 行）。

问题与动机
----------
Spirit 改造前：``tool_executor.py`` 把"工具成功"和"工具返回 ``{"error": ...}``"一视同仁，
LLM 拿到同样的结果分不清"成功 / 部分成功 / 失败 / 可重试 / 不可重试"，导致：
- 重复失败的工具调用（不会换方案）
- 网络超时不自动重试
- 参数错误后不调整参数继续撞墙

本模块提供：
- :class:`ToolResultCategory`：5 类枚举（成功 / 部分成功 / 失败 / 可重试 / 不可重试）
- :class:`ClassifiedToolResult`：分类结果（带 retryable / guidance / original_result）
- :func:`classify_tool_result`：核心分类函数（按 tool_name 差异化）
- :func:`format_classified_for_llm`：把分类结果格式化为 LLM 可见的 JSON 前缀

集成方式
---------
``tool_executor.py`` 调用 :func:`classify_tool_result` 替代旧版
``_detect_tool_failure``；调用 :func:`format_classified_for_llm` 把分类前缀注入
tool 消息 content；这样 LLM 能看到："这次失败是参数错误，请改参数"。
"""

from __future__ import annotations

import enum
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# =========================================================================
# 分类枚举
# =========================================================================

class ToolResultCategory(str, enum.Enum):
    """工具执行结果分类 — 决定 LLM 看到的提示与下一步行为。"""

    SUCCESS = "success"
    """完全成功 — exit_code=0 + 无 error / warning。"""

    PARTIAL_SUCCESS = "partial_success"
    """部分成功 — exit_code=0 但有 warning / stderr 非空 / 数据不完整。"""

    FAILURE = "failure"
    """失败 — exit_code≠0 或 error 字段存在（不细分是否可重试）。"""

    RETRYABLE = "retryable"
    """可重试失败 — 网络超时 / 5xx / 临时错误 / rate limit。"""

    NON_RETRYABLE = "non_retryable"
    """不可重试失败 — 参数错误 / 权限错误 / 文件不存在 / 编译错误。"""


# =========================================================================
# 分类结果
# =========================================================================

@dataclass
class ClassifiedToolResult:
    """结构化工具结果分类。"""

    category: ToolResultCategory
    tool_name: str
    original_result: str

    # 恢复动作提示
    retryable: bool = False
    should_fallback_provider: bool = False
    should_summarize_output: bool = False

    # 给 LLM 看的指引（人类可读）
    guidance: str = ""

    # 解析得到的原始字段（如果 result 是 JSON）
    parsed_payload: Optional[Dict[str, Any]] = None
    exit_code: Optional[int] = None

    # 元数据
    matched_patterns: list = field(default_factory=list)

    @property
    def is_success(self) -> bool:
        return self.category in {ToolResultCategory.SUCCESS, ToolResultCategory.PARTIAL_SUCCESS}

    @property
    def is_failure(self) -> bool:
        return self.category in {
            ToolResultCategory.FAILURE,
            ToolResultCategory.RETRYABLE,
            ToolResultCategory.NON_RETRYABLE,
        }


# =========================================================================
# 模式库（用于判断 retryable vs non-retryable）
# =========================================================================

# 可重试：网络/服务端临时错误
_RETRYABLE_PATTERNS = [
    r"\b(timeout|timed out|connection (?:reset|refused|closed|aborted))\b",
    r"\b(temporarily unavailable|try again (?:later|in))\b",
    r"\b(rate ?limit(ed)?|too many requests|429\b|throttl(ed|ing))\b",
    r"\b(503|502|504|500|529)\b",
    r"\b(server error|internal error|upstream error|overloaded)\b",
    r"\b(network (?:is )?unreachable|host unreachable)\b",
    r"\b(ssl|certificate|tls) (?:error|verify|handshake)\b",
]

# 不可重试：参数 / 权限 / 资源不存在
_NON_RETRYABLE_PATTERNS = [
    r"\b(permission denied|access denied|unauthorized|forbidden|401\b|403\b)\b",
    r"\b(no such file|not found|does not exist|404\b)\b",
    r"\b(invalid (?:argument|parameter|input|syntax)|parse error|syntax error)\b",
    r"\b(command not found|unknown command)\b",
    r"\b(unauthorized|authentication failed|invalid api ?key)\b",
    r"\b(bad request|400\b|malformed)\b",
]

_RETRYABLE_RE = re.compile("|".join(_RETRYABLE_PATTERNS), re.IGNORECASE)
_NON_RETRYABLE_RE = re.compile("|".join(_NON_RETRYABLE_PATTERNS), re.IGNORECASE)

# _classify_generic fallback 关键词（regex 未匹配时补充判定）
_NON_RETRYABLE_FALLBACK_KEYWORDS = (
    "invalid", "syntax", "parse error", "parse failure", "malformed",
    "bad request", "unsupported", "illegal",
)


# =========================================================================
# 核心分类函数
# =========================================================================

def classify_tool_result(
    tool_name: str,
    result: str,
    *,
    parsed: Optional[Dict[str, Any]] = None,
) -> ClassifiedToolResult:
    """对工具执行结果分类。

    Args:
        tool_name: 工具名（不同工具的 result schema 不同）
        result: 工具返回的字符串（可能被 JSON 化）
        parsed: 已解析的 result payload（避免重复解析）

    Returns:
        ClassifiedToolResult（带 category / guidance / retryable 等）
    """
    if parsed is None:
        parsed = _try_parse_json(result)

    classified = ClassifiedToolResult(
        category=ToolResultCategory.SUCCESS,
        tool_name=tool_name,
        original_result=result,
        parsed_payload=parsed,
    )

    if not result:
        classified.category = ToolResultCategory.NON_RETRYABLE
        classified.guidance = "工具返回了空结果。请检查工具是否正确执行，或者尝试不同参数。"
        return classified

    # --- 步骤 1: terminal 类工具按 JSON 解析 ---
    if tool_name == "terminal" and isinstance(parsed, dict):
        exit_code = parsed.get("exit_code")
        classified.exit_code = exit_code

        stderr = parsed.get("stderr", "") or ""
        error_field = parsed.get("error")
        warning_field = parsed.get("warning") or parsed.get("warnings")
        success_field = parsed.get("success")

        # 1.1 success 字段显式为 false → 失败
        if success_field is False:
            classified = _classify_failure(parsed, exit_code, stderr, error_field)
            classified.guidance = _build_guidance(classified)
            return classified

        # 1.2 exit_code 非 0 → 失败
        if exit_code is not None and exit_code != 0:
            classified = _classify_failure(parsed, exit_code, stderr, error_field)
            classified.guidance = _build_guidance(classified)
            return classified

        # 1.3 error 字段存在 → 失败
        if error_field:
            classified = _classify_failure(parsed, exit_code, stderr, error_field)
            classified.guidance = _build_guidance(classified)
            return classified

        # 1.4 stderr 非空但 exit_code=0 → 部分成功
        if stderr.strip():
            classified.category = ToolResultCategory.PARTIAL_SUCCESS
            classified.guidance = (
                "工具返回了 exit_code=0，但 stderr 有输出。请检查 stderr 内容判断是否有警告。"
            )
            classified.should_summarize_output = len(stderr) > 500
            return classified

        # 1.5 warning 字段 → 部分成功
        if warning_field:
            classified.category = ToolResultCategory.PARTIAL_SUCCESS
            classified.guidance = (
                "工具成功但有警告。请根据警告内容调整后续调用或告知用户。"
            )
            return classified

        # 1.6 完全成功
        classified.category = ToolResultCategory.SUCCESS
        classified.guidance = ""
        return classified

    # --- 步骤 2: 通用工具（看 result 字符串）---
    if isinstance(parsed, dict):
        # 有 structured error 字段
        if parsed.get("error") or parsed.get("failed") or parsed.get("status") == "failed":
            return _classify_generic(parsed, result)

        # success=false 显式失败
        if parsed.get("success") is False:
            return _classify_generic(parsed, result)

    # 看 result 字符串前 500 字符
    snippet = (result or "")[:500].lower()

    if '"error"' in snippet or '"failed"' in snippet or result.startswith("Error"):
        return _classify_generic(parsed, result)

    # 完全成功
    classified.category = ToolResultCategory.SUCCESS
    classified.guidance = ""
    return classified


def _classify_failure(
    parsed: Dict[str, Any],
    exit_code: Optional[int],
    stderr: str,
    error_field: Any,
) -> ClassifiedToolResult:
    """对 terminal 类工具的失败进一步分类为可重试 / 不可重试。"""
    classified = ClassifiedToolResult(
        category=ToolResultCategory.FAILURE,
        tool_name="terminal",
        original_result=parsed.get("stdout", "") or "",
        parsed_payload=parsed,
        exit_code=exit_code,
    )

    # 拼接所有可用错误文本用于模式匹配
    error_text = " ".join(filter(None, [
        str(error_field) if error_field else "",
        str(stderr) if stderr else "",
        parsed.get("stderr", "") or "",
    ]))

    # 优先匹配 NON_RETRYABLE（参数 / 权限）
    non_retry_match = _NON_RETRYABLE_RE.search(error_text)
    if non_retry_match:
        classified.category = ToolResultCategory.NON_RETRYABLE
        classified.retryable = False
        classified.matched_patterns.append(non_retry_match.group(0))
        return classified

    # 然后匹配 RETRYABLE（网络 / 服务端）
    retry_match = _RETRYABLE_RE.search(error_text)
    if retry_match:
        classified.category = ToolResultCategory.RETRYABLE
        classified.retryable = True
        classified.matched_patterns.append(retry_match.group(0))
        return classified

    # 兜底为 FAILURE（不细分）
    classified.category = ToolResultCategory.FAILURE
    classified.retryable = False
    return classified


def _classify_generic(
    parsed: Optional[Dict[str, Any]],
    result: str,
) -> ClassifiedToolResult:
    """对非 terminal 工具的失败分类。"""
    classified = ClassifiedToolResult(
        category=ToolResultCategory.FAILURE,
        tool_name="",
        original_result=result,
        parsed_payload=parsed,
    )

    snippet = result[:500]

    # 先匹配 NON_RETRYABLE
    non_retry_match = _NON_RETRYABLE_RE.search(snippet)
    if non_retry_match:
        classified.category = ToolResultCategory.NON_RETRYABLE
        classified.retryable = False
        classified.matched_patterns.append(non_retry_match.group(0))
        classified.guidance = _build_guidance(classified)
        return classified

    # 再匹配 RETRYABLE
    retry_match = _RETRYABLE_RE.search(snippet)
    if retry_match:
        classified.category = ToolResultCategory.RETRYABLE
        classified.retryable = True
        classified.matched_patterns.append(retry_match.group(0))
        classified.should_fallback_provider = True
        classified.guidance = _build_guidance(classified)
        return classified

    # Fallback：含 error 字段 + 错误关键词（invalid/syntax/parse 等）→ non_retryable
    if '"error"' in snippet and any(kw in snippet for kw in _NON_RETRYABLE_FALLBACK_KEYWORDS):
        classified.category = ToolResultCategory.NON_RETRYABLE
        classified.retryable = False
        classified.guidance = (
            "参数错误。请检查工具参数格式与字段名是否正确。"
        )
        return classified

    # 兜底
    classified.category = ToolResultCategory.FAILURE
    classified.retryable = False
    classified.guidance = _build_guidance(classified)
    return classified


# =========================================================================
# LLM 可见的格式化
# =========================================================================

def format_classified_for_llm(
    classified: ClassifiedToolResult,
    *,
    include_original: bool = True,
) -> str:
    """把分类结果格式化为 LLM 可见的 JSON 字符串。

    失败时会自动加 ``_classification`` / ``_retryable`` / ``_guidance`` 字段；
    成功时只返回原始 payload（不污染 LLM 上下文）。
    """
    if classified.category == ToolResultCategory.SUCCESS:
        # 成功时返回原结果，不加分类前缀
        return classified.original_result

    # 失败 / 部分成功时返回分类前缀
    payload: Dict[str, Any] = {
        "_classification": classified.category.value,
        "_retryable": classified.retryable,
    }

    if classified.guidance:
        payload["_guidance"] = classified.guidance

    if classified.exit_code is not None:
        payload["exit_code"] = classified.exit_code

    if classified.matched_patterns:
        payload["_matched_pattern"] = classified.matched_patterns[0]

    if include_original and classified.parsed_payload is not None:
        payload["original"] = classified.parsed_payload

    return json.dumps(payload, ensure_ascii=False)


# =========================================================================
# 内部 helpers
# =========================================================================

def _try_parse_json(text: str) -> Optional[Dict[str, Any]]:
    """尝试把 result 字符串解析为 JSON dict。"""
    if not text:
        return None
    text = text.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _build_guidance(classified: ClassifiedToolResult) -> str:
    """根据分类结果生成给 LLM 的指引。"""
    if classified.category == ToolResultCategory.NON_RETRYABLE:
        if classified.matched_patterns:
            pat = classified.matched_patterns[0].lower()
            if "permission" in pat or "denied" in pat or "unauthorized" in pat:
                return "权限被拒绝。请检查工具参数中是否包含了正确的凭证或路径权限。"
            if "not found" in pat or "no such" in pat or "404" in pat:
                return "资源不存在。请检查参数中的路径/ID 是否正确。"
            if "invalid" in pat or "syntax" in pat or "parse" in pat:
                return "参数错误。请检查工具参数格式与字段名是否正确。"
            if "bad request" in pat or "400" in pat:
                return "请求格式错误。请检查参数类型与必填字段。"
        return "不可重试错误（参数 / 权限 / 资源不存在）。请调整参数而非重试相同调用。"

    if classified.category == ToolResultCategory.RETRYABLE:
        if classified.matched_patterns:
            pat = classified.matched_patterns[0].lower()
            if "timeout" in pat or "timed out" in pat:
                return "网络超时。建议重试一次，或降低请求复杂度。"
            if "rate limit" in pat or "429" in pat or "throttle" in pat:
                return "触发限流。建议等待几秒后重试，或切换到备用 provider。"
            if "503" in pat or "502" in pat or "500" in pat or "529" in pat:
                return "服务端临时错误。建议重试一次或切换到备用 provider。"
            if "connection" in pat or "network" in pat:
                return "网络连接问题。建议重试一次。"
        return "可重试错误（网络 / 服务端临时）。建议重试一次或切换到备用 provider。"

    if classified.category == ToolResultCategory.PARTIAL_SUCCESS:
        return "工具部分成功（有警告 / stderr 输出）。请根据警告内容判断是否需要调整。"

    # FAILURE
    return "工具调用失败。请根据错误信息调整参数或换方案。"
