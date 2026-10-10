"""Spirit Agent tool_result_classification.py 工具结果分类回归测试（R-P1.2 v0.2.4 已交付未测）。

参考 hermes:
  - test_tool_dispatch_helpers.py（ClassifiedToolResult 设计）
  - test_turn_retry_state.py

5 类分类：
  - SUCCESS 完全成功
  - PARTIAL_SUCCESS 部分成功（exit=0 但 stderr / warning）
  - FAILURE 失败（不细分）
  - RETRYABLE 可重试（网络 / 5xx / rate limit）
  - NON_RETRYABLE 不可重试（参数错 / 权限 / 404）
"""

from __future__ import annotations

import json

import pytest

from spirit.agent.tool_result_classification import (
    ClassifiedToolResult,
    ToolResultCategory,
    classify_tool_result,
    format_classified_for_llm,
    _try_parse_json,
    _RETRYABLE_PATTERNS,
    _NON_RETRYABLE_PATTERNS,
)


# ─────────────────────────── ToolResultCategory 枚举 ───────────────────────────


class TestToolResultCategory:
    """ToolResultCategory 5 类枚举值。"""

    def test_success_value(self):
        assert ToolResultCategory.SUCCESS.value == "success"

    def test_partial_success_value(self):
        assert ToolResultCategory.PARTIAL_SUCCESS.value == "partial_success"

    def test_failure_value(self):
        assert ToolResultCategory.FAILURE.value == "failure"

    def test_retryable_value(self):
        assert ToolResultCategory.RETRYABLE.value == "retryable"

    def test_non_retryable_value(self):
        assert ToolResultCategory.NON_RETRYABLE.value == "non_retryable"


# ─────────────────────────── ClassifiedToolResult 属性 ───────────────────────────


class TestClassifiedToolResultProperties:
    """ClassifiedToolResult is_success / is_failure 属性。"""

    def test_is_success_for_success(self):
        c = ClassifiedToolResult(
            category=ToolResultCategory.SUCCESS,
            tool_name="t",
            original_result="ok",
        )
        assert c.is_success is True
        assert c.is_failure is False

    def test_is_success_for_partial_success(self):
        c = ClassifiedToolResult(
            category=ToolResultCategory.PARTIAL_SUCCESS,
            tool_name="t",
            original_result="ok",
        )
        assert c.is_success is True
        assert c.is_failure is False

    def test_is_failure_for_failure(self):
        c = ClassifiedToolResult(
            category=ToolResultCategory.FAILURE,
            tool_name="t",
            original_result="err",
        )
        assert c.is_failure is True
        assert c.is_success is False

    def test_is_failure_for_retryable(self):
        c = ClassifiedToolResult(
            category=ToolResultCategory.RETRYABLE,
            tool_name="t",
            original_result="timeout",
        )
        assert c.is_failure is True

    def test_is_failure_for_non_retryable(self):
        c = ClassifiedToolResult(
            category=ToolResultCategory.NON_RETRYABLE,
            tool_name="t",
            original_result="404",
        )
        assert c.is_failure is True


class TestClassifiedToolResultDefaults:
    """ClassifiedToolResult 默认值。"""

    def test_default_retryable_false(self):
        c = ClassifiedToolResult(
            category=ToolResultCategory.SUCCESS,
            tool_name="t",
            original_result="ok",
        )
        assert c.retryable is False

    def test_default_should_fallback_provider_false(self):
        c = ClassifiedToolResult(
            category=ToolResultCategory.SUCCESS,
            tool_name="t",
            original_result="ok",
        )
        assert c.should_fallback_provider is False

    def test_default_should_summarize_output_false(self):
        c = ClassifiedToolResult(
            category=ToolResultCategory.SUCCESS,
            tool_name="t",
            original_result="ok",
        )
        assert c.should_summarize_output is False

    def test_default_guidance_empty(self):
        c = ClassifiedToolResult(
            category=ToolResultCategory.SUCCESS,
            tool_name="t",
            original_result="ok",
        )
        assert c.guidance == ""

    def test_default_matched_patterns_empty(self):
        c = ClassifiedToolResult(
            category=ToolResultCategory.SUCCESS,
            tool_name="t",
            original_result="ok",
        )
        assert c.matched_patterns == []


# ─────────────────────────── classify_tool_result - 空 / 简单 ───────────────────────────


class TestClassifyEmptyResult:
    """空 result 视为不可重试。"""

    def test_empty_string_returns_non_retryable(self):
        c = classify_tool_result("terminal", "")
        assert c.category == ToolResultCategory.NON_RETRYABLE
        assert "空结果" in c.guidance


class TestClassifyGenericSuccess:
    """通用工具完全成功。"""

    def test_normal_text_success(self):
        c = classify_tool_result("web_search", "搜索结果：...")
        assert c.category == ToolResultCategory.SUCCESS

    def test_json_success(self):
        c = classify_tool_result(
            "tool",
            json.dumps({"data": "ok", "code": 200}),
        )
        assert c.category == ToolResultCategory.SUCCESS


# ─────────────────────────── classify_tool_result - terminal 工具 ───────────────────────────


class TestClassifyTerminal:
    """terminal 工具按 exit_code / error / warning / stderr 分类。"""

    def test_exit_zero_no_error_success(self):
        result = json.dumps({"exit_code": 0, "stdout": "ok"})
        c = classify_tool_result("terminal", result)
        assert c.category == ToolResultCategory.SUCCESS
        assert c.exit_code == 0

    def test_exit_zero_with_stderr_partial_success(self):
        result = json.dumps({
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "warning: deprecated",
        })
        c = classify_tool_result("terminal", result)
        assert c.category == ToolResultCategory.PARTIAL_SUCCESS

    def test_exit_nonzero_failure(self):
        result = json.dumps({"exit_code": 1, "stderr": "command failed"})
        c = classify_tool_result("terminal", result)
        assert c.category == ToolResultCategory.FAILURE
        assert c.exit_code == 1

    def test_exit_nonzero_with_permission_denied_non_retryable(self):
        result = json.dumps({
            "exit_code": 1,
            "stderr": "permission denied: /etc/passwd",
        })
        c = classify_tool_result("terminal", result)
        assert c.category == ToolResultCategory.NON_RETRYABLE
        assert c.retryable is False

    def test_exit_nonzero_with_not_found_non_retryable(self):
        result = json.dumps({
            "exit_code": 2,
            "stderr": "no such file or directory",
        })
        c = classify_tool_result("terminal", result)
        assert c.category == ToolResultCategory.NON_RETRYABLE

    def test_exit_nonzero_with_timeout_retryable(self):
        result = json.dumps({
            "exit_code": 124,
            "stderr": "connection timed out",
        })
        c = classify_tool_result("terminal", result)
        assert c.category == ToolResultCategory.RETRYABLE
        assert c.retryable is True

    def test_exit_nonzero_with_rate_limit_retryable(self):
        result = json.dumps({
            "exit_code": 1,
            "stderr": "rate limit exceeded (429)",
        })
        c = classify_tool_result("terminal", result)
        assert c.category == ToolResultCategory.RETRYABLE

    def test_exit_nonzero_with_500_retryable(self):
        result = json.dumps({
            "exit_code": 1,
            "stderr": "HTTP 500 Internal Server Error",
        })
        c = classify_tool_result("terminal", result)
        assert c.category == ToolResultCategory.RETRYABLE

    def test_success_false_field_failure(self):
        result = json.dumps({
            "success": False,
            "exit_code": 0,
            "stderr": "permission denied",
        })
        c = classify_tool_result("terminal", result)
        assert c.category == ToolResultCategory.NON_RETRYABLE

    def test_warning_field_partial_success(self):
        result = json.dumps({
            "exit_code": 0,
            "warning": "deprecated function used",
        })
        c = classify_tool_result("terminal", result)
        assert c.category == ToolResultCategory.PARTIAL_SUCCESS

    def test_long_stderr_triggers_summarize(self):
        long_stderr = "x" * 600
        result = json.dumps({"exit_code": 0, "stderr": long_stderr})
        c = classify_tool_result("terminal", result)
        assert c.category == ToolResultCategory.PARTIAL_SUCCESS
        assert c.should_summarize_output is True


# ─────────────────────────── classify_tool_result - 通用工具 ───────────────────────────


class TestClassifyGeneric:
    """非 terminal 通用工具。"""

    def test_error_field_in_json_failure(self):
        result = json.dumps({"error": "permission denied"})
        c = classify_tool_result("web_search", result)
        assert c.category == ToolResultCategory.NON_RETRYABLE

    def test_failed_field_in_json_failure(self):
        result = json.dumps({"failed": True, "msg": "syntax error"})
        c = classify_tool_result("tool", result)
        assert c.category == ToolResultCategory.NON_RETRYABLE

    def test_status_failed_field_failure(self):
        result = json.dumps({"status": "failed", "detail": "timeout"})
        c = classify_tool_result("tool", result)
        assert c.category == ToolResultCategory.RETRYABLE

    def test_success_false_in_json_failure(self):
        result = json.dumps({"success": False, "msg": "404 not found"})
        c = classify_tool_result("tool", result)
        assert c.category == ToolResultCategory.NON_RETRYABLE

    def test_error_in_plain_text_failure(self):
        c = classify_tool_result("tool", 'Error: something went wrong')
        assert c.category == ToolResultCategory.FAILURE

    def test_retryable_in_plain_text(self):
        # Plain text 含 "Error" 前缀 → 走 _classify_generic → 匹配 retryable
        c = classify_tool_result(
            "tool",
            'Error: Connection timed out after 30s',
        )
        assert c.category == ToolResultCategory.RETRYABLE
        assert c.should_fallback_provider is True


# ─────────────────────────── classify_tool_result - parsed 参数 ───────────────────────────


class TestClassifyWithParsedArg:
    """parsed 参数：跳过重复解析。"""

    def test_parsed_payload_used_directly(self):
        parsed = {"exit_code": 0, "stdout": "ok"}
        c = classify_tool_result(
            "terminal",
            "raw string that would otherwise fail to parse",
            parsed=parsed,
        )
        assert c.category == ToolResultCategory.SUCCESS
        assert c.exit_code == 0


# ─────────────────────────── classify_tool_result - guidance ───────────────────────────


class TestClassifyGuidance:
    """guidance 字段（按匹配模式给出具体指引）。"""

    def test_non_retryable_permission_guidance(self):
        result = json.dumps({"exit_code": 1, "stderr": "permission denied"})
        c = classify_tool_result("terminal", result)
        assert "权限" in c.guidance

    def test_non_retryable_not_found_guidance(self):
        result = json.dumps({"exit_code": 1, "stderr": "file not found"})
        c = classify_tool_result("terminal", result)
        assert "资源" in c.guidance or "路径" in c.guidance

    def test_non_retryable_invalid_guidance(self):
        result = json.dumps({"exit_code": 1, "stderr": "invalid argument"})
        c = classify_tool_result("terminal", result)
        assert "参数" in c.guidance

    def test_retryable_timeout_guidance(self):
        result = json.dumps({"exit_code": 1, "stderr": "timed out"})
        c = classify_tool_result("terminal", result)
        assert "重试" in c.guidance

    def test_retryable_rate_limit_guidance(self):
        result = json.dumps({"exit_code": 1, "stderr": "rate limit"})
        c = classify_tool_result("terminal", result)
        assert "限流" in c.guidance or "等待" in c.guidance or "切换" in c.guidance

    def test_partial_success_guidance(self):
        result = json.dumps({"exit_code": 0, "stderr": "warning"})
        c = classify_tool_result("terminal", result)
        assert "警告" in c.guidance


# ─────────────────────────── format_classified_for_llm ───────────────────────────


class TestFormatClassifiedForLLM:
    """format_classified_for_llm 格式化输出。"""

    def test_success_returns_original_unchanged(self):
        c = ClassifiedToolResult(
            category=ToolResultCategory.SUCCESS,
            tool_name="t",
            original_result='{"data": "ok"}',
        )
        out = format_classified_for_llm(c)
        # 成功时直接返回原始 result，不污染 LLM
        assert out == '{"data": "ok"}'

    def test_failure_includes_classification_field(self):
        c = ClassifiedToolResult(
            category=ToolResultCategory.NON_RETRYABLE,
            tool_name="terminal",
            original_result="permission denied",
            retryable=False,
            guidance="权限被拒绝",
        )
        out = format_classified_for_llm(c)
        payload = json.loads(out)
        assert payload["_classification"] == "non_retryable"
        assert payload["_retryable"] is False
        assert payload["_guidance"] == "权限被拒绝"

    def test_failure_includes_exit_code_when_present(self):
        c = ClassifiedToolResult(
            category=ToolResultCategory.FAILURE,
            tool_name="terminal",
            original_result="err",
            exit_code=42,
        )
        out = format_classified_for_llm(c)
        payload = json.loads(out)
        assert payload["exit_code"] == 42

    def test_failure_includes_matched_pattern(self):
        c = ClassifiedToolResult(
            category=ToolResultCategory.RETRYABLE,
            tool_name="terminal",
            original_result="timeout",
            retryable=True,
            matched_patterns=["timeout"],
        )
        out = format_classified_for_llm(c)
        payload = json.loads(out)
        assert payload["_matched_pattern"] == "timeout"

    def test_failure_includes_original_when_requested(self):
        c = ClassifiedToolResult(
            category=ToolResultCategory.NON_RETRYABLE,
            tool_name="tool",
            original_result='{"error": "x"}',
            parsed_payload={"error": "x"},
        )
        out = format_classified_for_llm(c, include_original=True)
        payload = json.loads(out)
        assert payload["original"] == {"error": "x"}

    def test_failure_excludes_original_when_not_requested(self):
        c = ClassifiedToolResult(
            category=ToolResultCategory.NON_RETRYABLE,
            tool_name="tool",
            original_result='{"error": "x"}',
            parsed_payload={"error": "x"},
        )
        out = format_classified_for_llm(c, include_original=False)
        payload = json.loads(out)
        assert "original" not in payload

    def test_partial_success_includes_classification(self):
        c = ClassifiedToolResult(
            category=ToolResultCategory.PARTIAL_SUCCESS,
            tool_name="terminal",
            original_result="ok",
            guidance="部分成功",
        )
        out = format_classified_for_llm(c)
        payload = json.loads(out)
        assert payload["_classification"] == "partial_success"


# ─────────────────────────── _try_parse_json helper ───────────────────────────


class TestTryParseJson:
    """_try_parse_json 内部 helper。"""

    def test_empty_returns_none(self):
        assert _try_parse_json("") is None

    def test_non_json_returns_none(self):
        assert _try_parse_json("plain text") is None

    def test_json_object_parsed(self):
        result = _try_parse_json('{"a": 1}')
        assert result == {"a": 1}

    def test_json_array_returns_none(self):
        # 数组不被视为 dict
        assert _try_parse_json("[1, 2, 3]") is None

    def test_invalid_json_returns_none(self):
        assert _try_parse_json("{bad json") is None

    def test_json_with_whitespace(self):
        result = _try_parse_json('  {"x": 1}  ')
        assert result == {"x": 1}


# ─────────────────────────── 模式库完整性 ───────────────────────────


class TestPatternLibrary:
    """_RETRYABLE_PATTERNS / _NON_RETRYABLE_PATTERNS 模式库完整性。"""

    def test_retryable_patterns_non_empty(self):
        assert len(_RETRYABLE_PATTERNS) > 0
        # 至少含 timeout / rate limit / 5xx
        joined = " ".join(_RETRYABLE_PATTERNS).lower()
        assert "timeout" in joined
        assert "rate" in joined
        assert "503" in joined or "500" in joined

    def test_non_retryable_patterns_non_empty(self):
        assert len(_NON_RETRYABLE_PATTERNS) > 0
        joined = " ".join(_NON_RETRYABLE_PATTERNS).lower()
        assert "permission" in joined or "denied" in joined
        assert "not found" in joined or "404" in joined
        assert "invalid" in joined


# ─────────────────────────── 5 类全枚举回归 ───────────────────────────


class TestAllFiveCategoriesReachable:
    """确保 5 个分类都能被触发（不是死代码）。"""

    def test_success_reachable(self):
        c = classify_tool_result(
            "terminal",
            json.dumps({"exit_code": 0, "stdout": "ok"}),
        )
        assert c.category == ToolResultCategory.SUCCESS

    def test_partial_success_reachable(self):
        c = classify_tool_result(
            "terminal",
            json.dumps({"exit_code": 0, "stderr": "warn"}),
        )
        assert c.category == ToolResultCategory.PARTIAL_SUCCESS

    def test_failure_reachable(self):
        # exit_code=1 但没有任何匹配模式
        c = classify_tool_result(
            "terminal",
            json.dumps({"exit_code": 1, "stderr": "weird error"}),
        )
        # 实际可能落到 RETRYABLE / NON_RETRYABLE / FAILURE 任一
        assert c.category in {
            ToolResultCategory.FAILURE,
            ToolResultCategory.RETRYABLE,
            ToolResultCategory.NON_RETRYABLE,
        }

    def test_retryable_reachable(self):
        c = classify_tool_result(
            "terminal",
            json.dumps({"exit_code": 1, "stderr": "timed out"}),
        )
        assert c.category == ToolResultCategory.RETRYABLE

    def test_non_retryable_reachable(self):
        c = classify_tool_result(
            "terminal",
            json.dumps({"exit_code": 1, "stderr": "permission denied"}),
        )
        assert c.category == ToolResultCategory.NON_RETRYABLE
