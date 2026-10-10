"""Spirit Agent accuracy_attribution.py 错误归因回归测试（M-P1.0 v0.2.4 已交付未测）。

参考 hermes:
        - accuracy_attribution 在 hermes/swe-bench runner 里做归因分析

5 类错误归因 + AccuracyAttributionReport 统计 + MoA 兜底判定 + 自我纠错判定。
"""

from __future__ import annotations

import pytest

from spirit.agent.accuracy_attribution import (
    AccuracyAttributionReport,
    ErrorCategory,
    TaskErrorSample,
    attribute_task_errors,
    build_self_correction_prompt,
    classify_error_cause,
    get_attribution_summary,
    should_activate_moa,
    should_self_correct,
)


# ─────────────────────────── ErrorCategory 枚举 ───────────────────────────


class TestErrorCategory:
    """ErrorCategory 5 类枚举（同时是 str 枚举，兼容 str 拼接）。"""

    def test_has_five_categories(self):
        assert len(ErrorCategory) == 5

    def test_tool_failure_no_replan_value(self):
        assert ErrorCategory.TOOL_FAILURE_NO_REPLAN.value == "tool_failure_no_replan"

    def test_output_truncation_value(self):
        assert ErrorCategory.OUTPUT_TRUNCATION.value == "output_truncation"

    def test_provider_switch_lost_value(self):
        assert ErrorCategory.PROVIDER_SWITCH_LOST.value == "provider_switch_lost"

    def test_thinking_timeout_value(self):
        assert ErrorCategory.THINKING_TIMEOUT.value == "thinking_timeout"

    def test_other_value(self):
        assert ErrorCategory.OTHER.value == "other"

    def test_is_str_enum(self):
        # str enum 兼容性：可直接当字符串用
        assert ErrorCategory.OTHER == "other"
        assert str(ErrorCategory.OTHER) == "ErrorCategory.OTHER"


# ─────────────────────────── TaskErrorSample 数据类 ───────────────────────────


class TestTaskErrorSample:
    """TaskErrorSample 不可变错误样本数据类。"""

    def test_basic_construction(self):
        sample = TaskErrorSample(task_name="task-1", category=ErrorCategory.OTHER)
        assert sample.task_name == "task-1"
        assert sample.category == ErrorCategory.OTHER
        assert sample.raw_error == ""
        assert sample.context == {}

    def test_default_raw_error_empty(self):
        sample = TaskErrorSample(task_name="t", category=ErrorCategory.OTHER)
        assert sample.raw_error == ""

    def test_default_context_is_factory(self):
        """默认 context 是新 dict（避免可变默认陷阱）。"""
        s1 = TaskErrorSample(task_name="a", category=ErrorCategory.OTHER)
        s2 = TaskErrorSample(task_name="b", category=ErrorCategory.OTHER)
        s1.context["x"] = 1
        # s2 不应受影响
        assert s2.context == {}

    def test_with_raw_error_and_context(self):
        sample = TaskErrorSample(
            task_name="t",
            category=ErrorCategory.TOOL_FAILURE_NO_REPLAN,
            raw_error="tool failed",
            context={"tool_name": "search"},
        )
        assert sample.raw_error == "tool failed"
        assert sample.context["tool_name"] == "search"


# ─────────────────────────── AccuracyAttributionReport 报告 ───────────────────────────


class TestAccuracyAttributionReport:
    """AccuracyAttributionReport 批量归因报告。"""

    def test_empty_report(self):
        report = AccuracyAttributionReport()
        assert report.samples == []
        assert report.total == 0

    def test_add_increments_total(self):
        report = AccuracyAttributionReport()
        sample = TaskErrorSample(task_name="t", category=ErrorCategory.OTHER)
        report.add(sample)
        assert report.total == 1
        assert len(report.samples) == 1

    def test_add_multiple_samples(self):
        report = AccuracyAttributionReport()
        for i in range(3):
            report.add(TaskErrorSample(task_name=f"t{i}", category=ErrorCategory.OTHER))
        assert report.total == 3
        assert len(report.samples) == 3

    def test_count_by_category_empty(self):
        report = AccuracyAttributionReport()
        assert report.count_by_category() == {}

    def test_count_by_category(self):
        report = AccuracyAttributionReport()
        report.add(TaskErrorSample(task_name="a", category=ErrorCategory.OTHER))
        report.add(TaskErrorSample(task_name="b", category=ErrorCategory.OTHER))
        report.add(TaskErrorSample(task_name="c", category=ErrorCategory.TOOL_FAILURE_NO_REPLAN))
        counts = report.count_by_category()
        assert counts["other"] == 2
        assert counts["tool_failure_no_replan"] == 1

    def test_ratio_by_category_empty(self):
        report = AccuracyAttributionReport()
        assert report.ratio_by_category() == {}

    def test_ratio_by_category(self):
        report = AccuracyAttributionReport()
        report.add(TaskErrorSample(task_name="a", category=ErrorCategory.OTHER))
        report.add(TaskErrorSample(task_name="b", category=ErrorCategory.OTHER))
        report.add(TaskErrorSample(task_name="c", category=ErrorCategory.TOOL_FAILURE_NO_REPLAN))
        ratios = report.ratio_by_category()
        assert abs(ratios["other"] - 2 / 3) < 1e-9
        assert abs(ratios["tool_failure_no_replan"] - 1 / 3) < 1e-9

    def test_top_category_empty_returns_none(self):
        report = AccuracyAttributionReport()
        assert report.top_category() is None

    def test_top_category_single_max(self):
        report = AccuracyAttributionReport()
        report.add(TaskErrorSample(task_name="a", category=ErrorCategory.OTHER))
        report.add(TaskErrorSample(task_name="b", category=ErrorCategory.TOOL_FAILURE_NO_REPLAN))
        report.add(TaskErrorSample(task_name="c", category=ErrorCategory.TOOL_FAILURE_NO_REPLAN))
        # tool_failure_no_replan 占多数
        assert report.top_category() == ErrorCategory.TOOL_FAILURE_NO_REPLAN

    def test_top_category_tie_uses_enum_order(self):
        """平局时按枚举顺序优先。"""
        report = AccuracyAttributionReport()
        report.add(TaskErrorSample(task_name="a", category=ErrorCategory.TOOL_FAILURE_NO_REPLAN))
        report.add(TaskErrorSample(task_name="b", category=ErrorCategory.OTHER))
        # 两者各 1 次，平手 → TOOL_FAILURE_NO_REPLAN 排在枚举前面
        assert report.top_category() == ErrorCategory.TOOL_FAILURE_NO_REPLAN

    def test_to_markdown_empty(self):
        report = AccuracyAttributionReport()
        md = report.to_markdown()
        assert "Total samples" in md
        assert "**Total samples**: 0" in md
        assert "tool_failure_no_replan" in md  # 表格列出所有类别

    def test_to_markdown_with_data(self):
        report = AccuracyAttributionReport()
        report.add(TaskErrorSample(task_name="a", category=ErrorCategory.OTHER))
        report.add(TaskErrorSample(task_name="b", category=ErrorCategory.OTHER))
        md = report.to_markdown()
        assert "**Total samples**: 2" in md
        assert "Top category" in md
        assert "other" in md


# ─────────────────────────── classify_error_cause 核心分类 ───────────────────────────


class TestClassifyErrorCause:
    """classify_error_cause 5 类错误归因（context 优先 + 文本指纹兜底）。"""

    def test_thinking_timeout_context_takes_priority(self):
        # 即使文本是 tool failure，context 优先
        cat = classify_error_cause(
            raw_error="tool failed",
            context={"thinking_timeout": True},
        )
        assert cat == ErrorCategory.THINKING_TIMEOUT

    def test_provider_switched_with_context_lost(self):
        cat = classify_error_cause(
            raw_error="x",
            context={"provider_switched": True, "context_lost": True},
        )
        assert cat == ErrorCategory.PROVIDER_SWITCH_LOST

    def test_provider_switched_without_context_lost(self):
        """provider_switched=True 但 context_lost=False → 不归 PROVIDER_SWITCH_LOST。"""
        cat = classify_error_cause(
            raw_error="x",
            context={"provider_switched": True, "context_lost": False},
        )
        # 无文本指纹 → OTHER
        assert cat == ErrorCategory.OTHER

    def test_finish_reason_length(self):
        cat = classify_error_cause(raw_error="x", context={"finish_reason": "length"})
        assert cat == ErrorCategory.OUTPUT_TRUNCATION

    def test_finish_reason_length_case_insensitive(self):
        cat = classify_error_cause(raw_error="x", context={"finish_reason": "Length"})
        assert cat == ErrorCategory.OUTPUT_TRUNCATION

    def test_truncation_text_fingerprint(self):
        cat = classify_error_cause(raw_error="finish_reason=length")
        assert cat == ErrorCategory.OUTPUT_TRUNCATION

    def test_truncation_chinese(self):
        cat = classify_error_cause(raw_error="输出被截断")
        assert cat == ErrorCategory.OUTPUT_TRUNCATION

    def test_max_tokens_truncation(self):
        cat = classify_error_cause(raw_error="max_tokens exceeded")
        assert cat == ErrorCategory.OUTPUT_TRUNCATION

    def test_provider_switch_text(self):
        cat = classify_error_cause(raw_error="provider switched to fallback")
        assert cat == ErrorCategory.PROVIDER_SWITCH_LOST

    def test_provider_switch_chinese(self):
        cat = classify_error_cause(raw_error="已切换 fallback")
        assert cat == ErrorCategory.PROVIDER_SWITCH_LOST

    def test_thinking_timeout_text(self):
        cat = classify_error_cause(raw_error="thinking timeout")
        assert cat == ErrorCategory.THINKING_TIMEOUT

    def test_thinking_timeout_chinese(self):
        cat = classify_error_cause(raw_error="思考超时")
        assert cat == ErrorCategory.THINKING_TIMEOUT

    def test_tool_failure_text(self):
        cat = classify_error_cause(raw_error="tool execution failed")
        assert cat == ErrorCategory.TOOL_FAILURE_NO_REPLAN

    def test_tool_failure_chinese(self):
        cat = classify_error_cause(raw_error="工具调用失败")
        assert cat == ErrorCategory.TOOL_FAILURE_NO_REPLAN

    def test_tool_failure_status_error(self):
        cat = classify_error_cause(raw_error='{"status": "error", "msg": "x"}')
        assert cat == ErrorCategory.TOOL_FAILURE_NO_REPLAN

    def test_other_fallback(self):
        cat = classify_error_cause(raw_error="some random unknown error")
        assert cat == ErrorCategory.OTHER

    def test_empty_raw_error(self):
        cat = classify_error_cause()
        assert cat == ErrorCategory.OTHER

    def test_context_none_is_safe(self):
        cat = classify_error_cause("tool failed", context=None)
        assert cat == ErrorCategory.TOOL_FAILURE_NO_REPLAN

    def test_priority_context_beats_text(self):
        """context 显式字段 > 文本指纹。"""
        # 文本像是 truncation，但 context 是 thinking_timeout
        cat = classify_error_cause(
            raw_error="finish_reason=length",
            context={"thinking_timeout": True},
        )
        assert cat == ErrorCategory.THINKING_TIMEOUT

    def test_priority_truncation_beats_provider_switch(self):
        """文本指纹优先级：truncation > provider_switch。"""
        cat = classify_error_cause(
            raw_error="finish_reason=length, provider switched",
        )
        # truncation 优先级更高
        assert cat == ErrorCategory.OUTPUT_TRUNCATION


# ─────────────────────────── attribute_task_errors 批量归因 ───────────────────────────


class TestAttributeTaskErrors:
    """attribute_task_errors 批量归因函数。"""

    def test_empty_samples(self):
        report = attribute_task_errors([])
        assert report.total == 0
        assert report.samples == []

    def test_single_sample(self):
        samples = [{"task_name": "t1", "raw_error": "tool failed", "context": {}}]
        report = attribute_task_errors(samples)
        assert report.total == 1
        assert report.samples[0].task_name == "t1"
        assert report.samples[0].category == ErrorCategory.TOOL_FAILURE_NO_REPLAN

    def test_multiple_samples(self):
        samples = [
            {"task_name": "t1", "raw_error": "tool failed", "context": {}},
            {"task_name": "t2", "raw_error": "思考超时", "context": {}},
            {"task_name": "t3", "raw_error": "unknown", "context": {}},
        ]
        report = attribute_task_errors(samples)
        assert report.total == 3
        assert report.samples[0].category == ErrorCategory.TOOL_FAILURE_NO_REPLAN
        assert report.samples[1].category == ErrorCategory.THINKING_TIMEOUT
        assert report.samples[2].category == ErrorCategory.OTHER

    def test_missing_task_name_defaults_to_unnamed(self):
        samples = [{"raw_error": "x", "context": {}}]
        report = attribute_task_errors(samples)
        assert report.samples[0].task_name == "<unnamed>"

    def test_missing_context_defaults_to_empty(self):
        samples = [{"task_name": "t", "raw_error": "tool failed"}]
        report = attribute_task_errors(samples)
        # 不报错，类别正确
        assert report.samples[0].category == ErrorCategory.TOOL_FAILURE_NO_REPLAN

    def test_context_propagation_to_report_sample(self):
        samples = [
            {
                "task_name": "t",
                "raw_error": "x",
                "context": {"thinking_timeout": True},
            },
        ]
        report = attribute_task_errors(samples)
        assert report.samples[0].context == {"thinking_timeout": True}
        assert report.samples[0].category == ErrorCategory.THINKING_TIMEOUT


# ─────────────────────────── should_activate_moa MoA 兜底 ───────────────────────────


class TestShouldActivateMoa:
    """should_activate_moa MoA 兜底判定（启发式）。"""

    def test_empty_response_triggers_moa(self):
        assert should_activate_moa("") is True

    def test_none_response_treated_as_empty(self):
        # 不传 response_text
        assert should_activate_moa() is True

    def test_short_response_triggers_moa(self):
        # < 50 字符（默认 threshold_length=50）
        assert should_activate_moa("hi") is True

    def test_long_response_without_confidence_marker_no_moa(self):
        # 中等长度 + 无信心标记 → 默认不触发（避免噪音）
        text = "这是一段很长的、但没有任何信心标记的回答" * 3
        assert should_activate_moa(text) is False

    def test_low_confidence_chinese_triggers(self):
        text = "我已完成此任务"
        # 但其实 "我已完成" 包含 high confidence 标记
        # 测试 "我不确定"
        text_low = "我不确定这个答案对不对"
        assert should_activate_moa(text_low) is True

    def test_low_confidence_english_triggers(self):
        text = "I'm not sure what the answer should be" + "x" * 60
        assert should_activate_moa(text) is True

    def test_low_confidence_maybe_triggers(self):
        text = "This might be the case, perhaps" + "x" * 60
        assert should_activate_moa(text) is True

    def test_high_confidence_chinese_no_moa(self):
        text = "我已完成此任务，代码已修改" + "x" * 60
        assert should_activate_moa(text) is False

    def test_high_confidence_successfully_no_moa(self):
        text = "The task has been successfully completed" + "x" * 60
        assert should_activate_moa(text) is False

    def test_response_length_override(self):
        """response_length 显式传入 → 跳过 len(response_text) 计算。"""
        # 文本很长（> 50），但传 response_length=10 → 视为短
        text = "x" * 100
        assert should_activate_moa(text, response_length=10) is True

    def test_threshold_length_override(self):
        text = "abcdefghij"  # 10 字符
        # 默认 threshold=50 → 触发
        assert should_activate_moa(text) is True
        # threshold=5 → 不视为短 → 看其他条件
        assert should_activate_moa(text, threshold_length=5) is False

    def test_confidence_threshold_accepted(self):
        """confidence_threshold 参数被接受但不抛错（启发式判断优先）。"""
        text = "no markers here" + "x" * 60
        # 不抛错
        result = should_activate_moa(text, confidence_threshold=0.5)
        assert isinstance(result, bool)


# ─────────────────────────── should_self_correct 自我纠错 ───────────────────────────


class TestShouldSelfCorrect:
    """should_self_correct 自我纠错触发判定。"""

    def test_empty_all_returns_false(self):
        assert should_self_correct() is False

    def test_user_followup_where_wrong_triggers(self):
        assert should_self_correct(user_message="哪里不对？") is True

    def test_user_followup_why_triggers(self):
        assert should_self_correct(user_message="为什么这样？") is True

    def test_user_followup_please_reconsider_triggers(self):
        assert should_self_correct(user_message="please reconsider this") is True

    def test_user_followup_are_you_sure_triggers(self):
        assert should_self_correct(user_message="are you sure?") is True

    def test_normal_user_message_no_trigger(self):
        assert should_self_correct(user_message="hello, please help me") is False

    def test_previous_response_with_error_tag_triggers(self):
        assert should_self_correct(previous_response="<error>failed</error>") is True

    def test_previous_response_with_exception_triggers(self):
        assert should_self_correct(
            previous_response="Exception: invalid argument",
        ) is True

    def test_previous_response_with_traceback_triggers(self):
        assert should_self_correct(
            previous_response="Traceback (most recent call last):",
        ) is True

    def test_previous_response_with_failed_word_triggers(self):
        assert should_self_correct(previous_response="Task failed") is True

    def test_previous_response_with_status_error_triggers(self):
        assert should_self_correct(
            previous_response='{"status": "error", "msg": "x"}',
        ) is True

    def test_previous_response_normal_no_trigger(self):
        assert should_self_correct(
            previous_response="The task is complete",
        ) is False

    def test_tool_results_with_status_error_triggers(self):
        results = [{"content": '{"status": "error"}'}]
        assert should_self_correct(last_tool_results=results) is True

    def test_tool_results_with_status_timeout_triggers(self):
        results = [{"content": '{"status": "timeout"}'}]
        assert should_self_correct(last_tool_results=results) is True

    def test_tool_results_with_success_no_trigger(self):
        results = [{"content": '{"status": "ok"}'}]
        assert should_self_correct(last_tool_results=results) is False

    def test_tool_results_empty_list_no_trigger(self):
        assert should_self_correct(last_tool_results=[]) is False

    def test_tool_results_non_string_content_skipped(self):
        """content 不是字符串时跳过（避免 .contains 异常）。"""
        results = [{"content": {"status": "error"}}]  # dict 不是 string
        # 不抛错
        assert should_self_correct(last_tool_results=results) is False

    def test_user_followup_priority_over_other_signals(self):
        """用户追问优先级最高。"""
        assert should_self_correct(
            previous_response="normal",
            user_message="哪里不对",
            last_tool_results=[{"content": "ok"}],
        ) is True


# ─────────────────────────── build_self_correction_prompt ───────────────────────────


class TestBuildSelfCorrectionPrompt:
    """build_self_correction_prompt 构造自我纠错提示文本。"""

    def test_default_reason(self):
        prompt = build_self_correction_prompt()
        assert "自我纠错提示" in prompt
        assert "上一轮结果包含错误标识" in prompt
        assert "审视" in prompt

    def test_custom_reason(self):
        prompt = build_self_correction_prompt(reason="工具失败未换方案")
        assert "工具失败未换方案" in prompt

    def test_contains_actionable_advice(self):
        prompt = build_self_correction_prompt()
        assert "重做" in prompt
        assert "不要无脑重复" in prompt


# ─────────────────────────── get_attribution_summary ───────────────────────────


class TestGetAttributionSummary:
    """get_attribution_summary 报告 → dict 序列化。"""

    def test_empty_report(self):
        report = AccuracyAttributionReport()
        summary = get_attribution_summary(report)
        assert summary["total"] == 0
        assert summary["counts"] == {}
        assert summary["ratios"] == {}
        assert summary["top_category"] is None

    def test_populated_report(self):
        report = AccuracyAttributionReport()
        report.add(TaskErrorSample(task_name="a", category=ErrorCategory.OTHER))
        report.add(TaskErrorSample(task_name="b", category=ErrorCategory.TOOL_FAILURE_NO_REPLAN))
        summary = get_attribution_summary(report)
        assert summary["total"] == 2
        assert summary["counts"]["other"] == 1
        assert summary["counts"]["tool_failure_no_replan"] == 1
        # top_category 多数 → tool_failure_no_replan
        assert summary["top_category"] == "tool_failure_no_replan"

    def test_ratios_sum_to_one(self):
        report = AccuracyAttributionReport()
        report.add(TaskErrorSample(task_name="a", category=ErrorCategory.OTHER))
        report.add(TaskErrorSample(task_name="b", category=ErrorCategory.OTHER))
        report.add(TaskErrorSample(task_name="c", category=ErrorCategory.TOOL_FAILURE_NO_REPLAN))
        summary = get_attribution_summary(report)
        assert abs(sum(summary["ratios"].values()) - 1.0) < 1e-9


# ─────────────────────────── 端到端集成 ───────────────────────────


class TestEndToEndPipeline:
    """完整流水线：samples → attribute_task_errors → 统计 → 序列化。"""

    def test_pipeline_with_real_world_samples(self):
        samples = [
            {
                "task_name": "edit_file_1",
                "raw_error": "tool failed: file not found",
                "context": {},
            },
            {
                "task_name": "edit_file_2",
                "raw_error": "思考超时，请重试",
                "context": {},
            },
            {
                "task_name": "edit_file_3",
                "raw_error": "finish_reason=length",
                "context": {},
            },
            {
                "task_name": "edit_file_4",
                "raw_error": "provider switched to fallback",
                "context": {},
            },
            {
                "task_name": "edit_file_5",
                "raw_error": "unknown issue",
                "context": {},
            },
        ]
        report = attribute_task_errors(samples)
        # 5 类各出现 1 次
        assert report.total == 5
        counts = report.count_by_category()
        assert counts["tool_failure_no_replan"] == 1
        assert counts["thinking_timeout"] == 1
        assert counts["output_truncation"] == 1
        assert counts["provider_switch_lost"] == 1
        assert counts["other"] == 1

    def test_pipeline_self_correct_after_tool_failure(self):
        """模拟：工具失败 + LLM 没换方案 → 触发自我纠错。"""
        # 上一轮：工具失败（ErrorCategory 归 TOOL_FAILURE_NO_REPLAN）
        prev = "I tried to call the file editor but it failed with exception"
        # 当前轮：用户没追问
        result = should_self_correct(previous_response=prev, user_message="")
        assert result is True
        # 触发后注入提示
        prompt = build_self_correction_prompt(
            reason="工具失败未换方案",
        )
        assert "工具失败未换方案" in prompt

    def test_pipeline_moa_triggered_for_uncertain_reply(self):
        """模拟：主 LLM 信心低 → 触发 MoA 兜底。"""
        reply = "我不确定这个答案对不对，可能需要再看看"
        assert should_activate_moa(reply) is True