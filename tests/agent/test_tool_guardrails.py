"""tests/agent/test_tool_guardrails.py — 工具调用护栏测试（3.6 安全系统）。

覆盖：配置解析、失败分类、警告阈值、硬停止（opt-in）、幂等无进展检测、
每轮重置。并验证与 terminal 审批门禁的联动（被阻止的命令计为失败）。
"""

import json

import pytest

from spirit.agent.tool_guardrails import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    ToolCallSignature,
    ToolGuardrailDecision,
    canonical_tool_args,
    classify_tool_failure,
)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

class TestGuardrailConfig:
    def test_defaults_are_warn_only(self):
        """默认只警告、不阻止 —— 硬停止必须显式 opt-in。"""
        cfg = ToolCallGuardrailConfig()
        assert cfg.warnings_enabled is True
        assert cfg.hard_stop_enabled is False

    def test_from_mapping_none(self):
        cfg = ToolCallGuardrailConfig.from_mapping(None)
        assert cfg.hard_stop_enabled is False

    def test_from_mapping_nested_thresholds(self):
        cfg = ToolCallGuardrailConfig.from_mapping({
            "hard_stop_enabled": True,
            "warn_after": {"exact_failure": 1, "same_tool_failure": 2,
                           "idempotent_no_progress": 3},
            "hard_stop_after": {"exact_failure": 4, "same_tool_failure": 5,
                                "idempotent_no_progress": 6},
        })
        assert cfg.hard_stop_enabled is True
        assert cfg.exact_failure_warn_after == 1
        assert cfg.same_tool_failure_warn_after == 2
        assert cfg.no_progress_warn_after == 3
        assert cfg.exact_failure_block_after == 4
        assert cfg.same_tool_failure_halt_after == 5
        assert cfg.no_progress_block_after == 6

    def test_from_mapping_ignores_invalid_values(self):
        cfg = ToolCallGuardrailConfig.from_mapping({
            "warn_after": {"exact_failure": -5},
            "hard_stop_after": {"exact_failure": "abc"},
        })
        assert cfg.exact_failure_warn_after == ToolCallGuardrailConfig().exact_failure_warn_after
        assert cfg.exact_failure_block_after == ToolCallGuardrailConfig().exact_failure_block_after


# ---------------------------------------------------------------------------
# 签名与规范化
# ---------------------------------------------------------------------------

class TestSignature:
    def test_canonical_args_key_order_insensitive(self):
        assert canonical_tool_args({"a": 1, "b": 2}) == canonical_tool_args({"b": 2, "a": 1})

    def test_canonical_args_rejects_non_mapping(self):
        with pytest.raises(TypeError):
            canonical_tool_args(["not", "a", "mapping"])

    def test_same_args_same_hash(self):
        s1 = ToolCallSignature.from_call("read_file", {"path": "/a"})
        s2 = ToolCallSignature.from_call("read_file", {"path": "/a"})
        assert s1 == s2

    def test_different_args_different_hash(self):
        s1 = ToolCallSignature.from_call("read_file", {"path": "/a"})
        s2 = ToolCallSignature.from_call("read_file", {"path": "/b"})
        assert s1 != s2

    def test_different_tool_different_signature(self):
        s1 = ToolCallSignature.from_call("read_file", {"path": "/a"})
        s2 = ToolCallSignature.from_call("write_file", {"path": "/a"})
        assert s1 != s2


# ---------------------------------------------------------------------------
# 失败分类
# ---------------------------------------------------------------------------

class TestClassifyFailure:
    def test_none_result_is_not_failure(self):
        assert classify_tool_failure("read_file", None) == (False, "")

    def test_terminal_nonzero_exit_is_failure(self):
        result = json.dumps({"output": "", "exit_code": 1})
        failed, detail = classify_tool_failure("terminal", result)
        assert failed is True
        assert "exit 1" in detail

    def test_terminal_zero_exit_is_success(self):
        result = json.dumps({"output": "ok", "exit_code": 0})
        assert classify_tool_failure("terminal", result)[0] is False

    def test_terminal_invalid_json_falls_back(self):
        assert classify_tool_failure("terminal", "plain text")[0] is False

    def test_generic_error_field(self):
        assert classify_tool_failure("read_file", json.dumps({"error": "not found"}))[0] is True

    def test_generic_error_prefix(self):
        assert classify_tool_failure("read_file", "Error: boom")[0] is True

    def test_plain_output_is_success(self):
        assert classify_tool_failure("read_file", "file contents here")[0] is False

    def test_approval_blocked_terminal_result_counts_as_failure(self):
        """与 3.6 审批门禁联动：被安全策略阻止的命令必须被识别为失败。"""
        blocked = json.dumps({
            "output": "", "exit_code": -1, "blocked": True,
            "error": "命令被安全策略阻止", "status": "blocked_no_callback",
        }, ensure_ascii=False)
        failed, _ = classify_tool_failure("terminal", blocked)
        assert failed is True


# ---------------------------------------------------------------------------
# 控制器决策
# ---------------------------------------------------------------------------

class TestDecision:
    def test_allow_allows_execution(self):
        assert ToolGuardrailDecision().allows_execution is True
        assert ToolGuardrailDecision().should_halt is False

    def test_warn_allows_execution(self):
        d = ToolGuardrailDecision(action="warn")
        assert d.allows_execution is True
        assert d.should_halt is False

    def test_block_halts(self):
        d = ToolGuardrailDecision(action="block")
        assert d.allows_execution is False
        assert d.should_halt is True

    def test_halt_halts(self):
        assert ToolGuardrailDecision(action="halt").should_halt is True


class TestControllerWarnings:
    def test_before_call_allows_by_default(self):
        ctrl = ToolCallGuardrailController()
        d = ctrl.before_call("read_file", {"path": "/a"})
        assert d.action == "allow"
        assert d.signature is not None

    def test_repeated_exact_failure_warns(self):
        ctrl = ToolCallGuardrailController(ToolCallGuardrailConfig(
            exact_failure_warn_after=2, same_tool_failure_warn_after=99,
        ))
        args = {"path": "/missing"}
        first = ctrl.after_call("read_file", args, None, failed=True)
        assert first.action == "allow"
        second = ctrl.after_call("read_file", args, None, failed=True)
        assert second.action == "warn"
        assert second.code == "repeated_exact_failure_warning"
        assert second.count == 2
        # 警告不阻止执行
        assert second.allows_execution is True

    def test_same_tool_failure_warns_with_different_args(self):
        ctrl = ToolCallGuardrailController(ToolCallGuardrailConfig(
            exact_failure_warn_after=99, same_tool_failure_warn_after=2,
        ))
        ctrl.after_call("read_file", {"path": "/a"}, None, failed=True)
        d = ctrl.after_call("read_file", {"path": "/b"}, None, failed=True)
        assert d.action == "warn"
        assert d.code == "same_tool_failure_warning"

    def test_success_clears_failure_counters(self):
        ctrl = ToolCallGuardrailController(ToolCallGuardrailConfig(
            exact_failure_warn_after=2, same_tool_failure_warn_after=99,
        ))
        args = {"path": "/a"}
        ctrl.after_call("read_file", args, None, failed=True)
        ctrl.after_call("read_file", args, "content", failed=False)
        d = ctrl.after_call("read_file", args, None, failed=True)
        assert d.action == "allow"  # 计数已清零，重新从 1 开始

    def test_warnings_disabled_never_warns(self):
        ctrl = ToolCallGuardrailController(ToolCallGuardrailConfig(
            warnings_enabled=False, exact_failure_warn_after=1,
            same_tool_failure_warn_after=1,
        ))
        args = {"path": "/a"}
        for _ in range(4):
            assert ctrl.after_call("read_file", args, None, failed=True).action == "allow"

    def test_hard_stop_disabled_never_blocks(self):
        """默认配置下即使反复失败也不会 block/halt。"""
        ctrl = ToolCallGuardrailController(ToolCallGuardrailConfig(
            exact_failure_warn_after=1, exact_failure_block_after=2,
            same_tool_failure_halt_after=2,
        ))
        args = {"path": "/a"}
        for _ in range(6):
            d = ctrl.before_call("read_file", args)
            assert d.action == "allow"
            assert ctrl.after_call("read_file", args, None, failed=True).action == "warn"
        assert ctrl.halt_decision is None


class TestControllerHardStop:
    def _hard_ctrl(self, **kw):
        return ToolCallGuardrailController(ToolCallGuardrailConfig(
            hard_stop_enabled=True, **kw
        ))

    def test_exact_failure_blocks_before_call(self):
        ctrl = self._hard_ctrl(exact_failure_block_after=2, exact_failure_warn_after=99,
                               same_tool_failure_halt_after=99)
        args = {"command": "false"}
        ctrl.after_call("terminal", args, None, failed=True)
        ctrl.after_call("terminal", args, None, failed=True)
        d = ctrl.before_call("terminal", args)
        assert d.action == "block"
        assert d.code == "repeated_exact_failure_block"
        assert d.allows_execution is False
        assert ctrl.halt_decision is d

    def test_same_tool_failure_halts(self):
        ctrl = self._hard_ctrl(same_tool_failure_halt_after=3,
                               exact_failure_warn_after=99,
                               exact_failure_block_after=99)
        for i in range(3):
            d = ctrl.after_call("read_file", {"path": f"/p{i}"}, None, failed=True)
        assert d.action == "halt"
        assert d.code == "same_tool_failure_halt"
        assert d.count == 3
        assert ctrl.halt_decision is not None

    def test_different_tools_do_not_share_halt_counter(self):
        ctrl = self._hard_ctrl(same_tool_failure_halt_after=3,
                               exact_failure_warn_after=99,
                               exact_failure_block_after=99)
        ctrl.after_call("read_file", {"path": "/a"}, None, failed=True)
        ctrl.after_call("read_file", {"path": "/b"}, None, failed=True)
        d = ctrl.after_call("write_file", {"path": "/c"}, None, failed=True)
        assert d.action != "halt"


class TestControllerNoProgress:
    def test_idempotent_repeated_same_result_warns(self):
        ctrl = ToolCallGuardrailController(ToolCallGuardrailConfig(
            no_progress_warn_after=2, exact_failure_warn_after=99,
        ))
        args = {"path": "/a"}
        ctrl.after_call("read_file", args, "same content", failed=False)
        d = ctrl.after_call("read_file", args, "same content", failed=False)
        assert d.action == "warn"
        assert d.code == "idempotent_no_progress_warning"

    def test_changed_result_resets_progress_counter(self):
        ctrl = ToolCallGuardrailController(ToolCallGuardrailConfig(no_progress_warn_after=2))
        args = {"path": "/a"}
        ctrl.after_call("read_file", args, "v1", failed=False)
        ctrl.after_call("read_file", args, "v2", failed=False)
        d = ctrl.after_call("read_file", args, "v3", failed=False)
        assert d.action == "allow"

    def test_mutating_tool_not_tracked_for_progress(self):
        ctrl = ToolCallGuardrailController(ToolCallGuardrailConfig(no_progress_warn_after=1))
        args = {"path": "/a", "content": "x"}
        for _ in range(3):
            d = ctrl.after_call("write_file", args, "written", failed=False)
        assert d.action == "allow"

    def test_no_progress_blocks_when_hard_stop_enabled(self):
        ctrl = ToolCallGuardrailController(ToolCallGuardrailConfig(
            hard_stop_enabled=True, no_progress_block_after=2,
            no_progress_warn_after=99,
        ))
        args = {"path": "/a"}
        for _ in range(3):
            ctrl.after_call("read_file", args, "same", failed=False)
        d = ctrl.before_call("read_file", args)
        assert d.action == "block"
        assert d.code == "idempotent_no_progress_block"


class TestControllerReset:
    def test_reset_for_turn_clears_state(self):
        ctrl = ToolCallGuardrailController(ToolCallGuardrailConfig(
            hard_stop_enabled=True, exact_failure_block_after=1,
        ))
        args = {"path": "/a"}
        ctrl.after_call("read_file", args, None, failed=True)
        assert ctrl.before_call("read_file", args).action == "block"

        ctrl.reset_for_turn()
        assert ctrl.before_call("read_file", args).action == "allow"
        assert ctrl.halt_decision is None

    def test_none_args_tolerated(self):
        ctrl = ToolCallGuardrailController()
        assert ctrl.before_call("no_args_tool", None).action == "allow"
        assert ctrl.after_call("no_args_tool", None, "ok").action == "allow"
