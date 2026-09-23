"""tests/tools/test_approval.py — 危险命令审批系统测试。

覆盖：
- 静态裁决 check_command_approval（安全 / 危险 / HARDLINE / YOLO / off / 白名单）
- 审批门禁 request_command_approval（force / 无回调 fail-safe / 回调三态 / 回调异常）
- 回调注册机制（全局 vs 线程局部、清除）
- 决策规范化 normalize_approval_decision
"""

import json
import threading

import pytest

from spirit.tools import approval as approval_mod
from spirit.tools.approval import (
    check_command_approval,
    clear_approval_callback,
    get_approval_callback,
    normalize_approval_decision,
    request_command_approval,
    set_approval_callback,
)

SAFE_COMMAND = "ls -la"
DANGEROUS_COMMAND = "rm -rf ./build_tmp_dir"
HARDLINE_COMMAND = "mkfs.ext4 /dev/sda1"


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    """隔离审批状态：关闭 YOLO、独立会话键、独立配置目录、清理回调。"""
    monkeypatch.delenv("SPIRIT_YOLO", raising=False)
    monkeypatch.setenv("SPIRIT_SESSION_KEY", f"test-{id(monkeypatch)}")
    # 指向不存在的配置目录 → 审批模式回落默认 interactive，白名单为空
    monkeypatch.setattr(approval_mod, "SPIRIT_HOME", tmp_path / "spirit-home")
    clear_approval_callback()
    yield
    clear_approval_callback()


# ---------------------------------------------------------------------------
# 静态裁决
# ---------------------------------------------------------------------------

class TestCheckCommandApproval:
    def test_safe_command_approved(self):
        result = check_command_approval(SAFE_COMMAND)
        assert result["approved"] is True
        assert result["requires_user_approval"] is False

    def test_dangerous_command_requires_approval(self):
        result = check_command_approval(DANGEROUS_COMMAND)
        assert result["approved"] is False
        assert result["requires_user_approval"] is True
        assert result["pattern_key"]

    def test_hardline_command_blocked_unconditionally(self):
        result = check_command_approval(HARDLINE_COMMAND)
        assert result["approved"] is False
        assert result["requires_user_approval"] is False
        assert result.get("hardline") is True

    def test_hardline_not_bypassed_by_yolo(self, monkeypatch):
        monkeypatch.setenv("SPIRIT_YOLO", "1")
        assert check_command_approval(HARDLINE_COMMAND)["approved"] is False

    def test_yolo_approves_dangerous(self, monkeypatch):
        monkeypatch.setenv("SPIRIT_YOLO", "1")
        result = check_command_approval(DANGEROUS_COMMAND)
        assert result["approved"] is True

    def test_mode_off_approves_dangerous(self, monkeypatch):
        monkeypatch.setattr(approval_mod, "get_approval_mode", lambda: "off")
        assert check_command_approval(DANGEROUS_COMMAND)["approved"] is True

    def test_session_allowlist_approves(self):
        approval_mod.add_session_approval(check_command_approval(DANGEROUS_COMMAND)["pattern_key"])
        result = check_command_approval(DANGEROUS_COMMAND)
        assert result["approved"] is True

    def test_permanent_allowlist_approves(self, monkeypatch):
        key = check_command_approval(DANGEROUS_COMMAND)["pattern_key"]
        monkeypatch.setattr(approval_mod, "get_permanent_allowlist", lambda: [key])
        assert check_command_approval(DANGEROUS_COMMAND)["approved"] is True


# ---------------------------------------------------------------------------
# 审批门禁
# ---------------------------------------------------------------------------

class TestRequestCommandApproval:
    def test_safe_command_passthrough(self):
        result = request_command_approval(SAFE_COMMAND)
        assert result["approved"] is True

    def test_force_bypasses_gate(self):
        result = request_command_approval(HARDLINE_COMMAND, force=True)
        assert result["approved"] is True
        assert result["user_approved"] is True

    def test_dangerous_without_callback_is_blocked(self):
        """fail-safe：无人确认时危险命令绝不静默执行。"""
        result = request_command_approval(DANGEROUS_COMMAND)
        assert result["approved"] is False
        assert result["status"] == "blocked_no_callback"
        assert "set_approval_callback" in result["message"]

    def test_hardline_not_prompted_even_with_callback(self):
        called = []
        set_approval_callback(lambda *a: called.append(a) or "session")
        result = request_command_approval(HARDLINE_COMMAND)
        assert result["approved"] is False
        assert called == []  # HARDLINE 不征求用户意见

    def test_callback_deny_blocks(self):
        set_approval_callback(lambda cmd, desc, key: "deny")
        result = request_command_approval(DANGEROUS_COMMAND)
        assert result["approved"] is False
        assert result["status"] == "denied"

    def test_callback_session_approves_and_caches(self):
        set_approval_callback(lambda cmd, desc, key: "session")
        result = request_command_approval(DANGEROUS_COMMAND)
        assert result["approved"] is True
        assert result["user_approved"] is True
        # 会话白名单已缓存 → 二次调用无需回调即放行
        clear_approval_callback()
        assert request_command_approval(DANGEROUS_COMMAND)["approved"] is True

    def test_callback_always_persists_allowlist(self, monkeypatch):
        persisted = []
        monkeypatch.setattr(approval_mod, "add_permanent_approval", persisted.append)
        set_approval_callback(lambda cmd, desc, key: "always")
        result = request_command_approval(DANGEROUS_COMMAND)
        assert result["approved"] is True
        assert len(persisted) == 1

    def test_callback_bool_true_maps_to_session(self):
        set_approval_callback(lambda cmd, desc, key: True)
        assert request_command_approval(DANGEROUS_COMMAND)["approved"] is True

    def test_callback_exception_blocks(self):
        def boom(cmd, desc, key):
            raise RuntimeError("ui unavailable")

        set_approval_callback(boom)
        result = request_command_approval(DANGEROUS_COMMAND)
        assert result["approved"] is False
        assert result["status"] == "blocked_callback_error"

    def test_callback_receives_command_and_pattern(self):
        seen = {}

        def cb(cmd, desc, key):
            seen.update({"cmd": cmd, "desc": desc, "key": key})
            return "deny"

        set_approval_callback(cb)
        request_command_approval(DANGEROUS_COMMAND)
        assert seen["cmd"] == DANGEROUS_COMMAND
        assert seen["key"]
        assert seen["desc"]


# ---------------------------------------------------------------------------
# 回调注册机制
# ---------------------------------------------------------------------------

class TestApprovalCallbackRegistry:
    def test_default_is_none(self):
        assert get_approval_callback() is None

    def test_global_scope_visible_from_worker_thread(self):
        """全局回调必须跨线程可见（工具在 ThreadPoolExecutor 中执行）。"""
        set_approval_callback(lambda *a: "session")
        found = []

        def worker():
            found.append(get_approval_callback())

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert found[0] is not None

    def test_thread_scope_overrides_global(self):
        set_approval_callback(lambda *a: "global")
        set_approval_callback(lambda *a: "thread", scope="thread")
        assert get_approval_callback()(None, None, None) == "thread"

    def test_thread_scope_not_visible_in_other_thread(self):
        set_approval_callback(lambda *a: "thread", scope="thread")
        found = []

        def worker():
            found.append(get_approval_callback())

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert found[0] is None

    def test_clear_removes_both_scopes(self):
        set_approval_callback(lambda *a: "global")
        set_approval_callback(lambda *a: "thread", scope="thread")
        clear_approval_callback()
        assert get_approval_callback() is None


# ---------------------------------------------------------------------------
# 决策规范化
# ---------------------------------------------------------------------------

class TestNormalizeApprovalDecision:
    @pytest.mark.parametrize("raw,expected", [
        (True, "session"),
        (False, "deny"),
        ("always", "always"),
        ("ALWAYS", "always"),
        ("a", "always"),
        ("yes", "session"),
        ("y", "session"),
        ("allow", "session"),
        ("ok", "session"),
        ("no", "deny"),
        ("n", "deny"),
        ("", "deny"),
        (None, "deny"),
        ("总是", "always"),
        ("允许", "session"),
        ("拒绝", "deny"),
        ("随便什么", "deny"),
        (123, "deny"),
    ])
    def test_normalize(self, raw, expected):
        assert normalize_approval_decision(raw) == expected


# ---------------------------------------------------------------------------
# terminal 工具集成（门禁真正生效）
# ---------------------------------------------------------------------------

class TestTerminalGateIntegration:
    def test_blocked_command_returns_blocked_json(self):
        from spirit.tools.registry import registry
        result = json.loads(registry.dispatch("terminal", {"command": DANGEROUS_COMMAND}))
        assert result.get("blocked") is True
        assert result["exit_code"] == -1
        assert result["output"] == ""

    def test_safe_command_executes(self):
        from spirit.tools.registry import registry
        result = registry.dispatch("terminal", {"command": "echo spirit_ok"})
        assert "spirit_ok" in result
        assert '"blocked": true' not in result

    def test_denied_command_does_not_execute(self):
        """被拒绝的命令绝不能产生副作用。"""
        from spirit.tools.registry import registry
        marker = "spirit_deny_marker"
        set_approval_callback(lambda *a: "deny")
        cmd = f"rm -rf ./{marker}"
        result = json.loads(registry.dispatch("terminal", {"command": cmd}))
        assert result.get("blocked") is True
        assert result.get("status") == "denied"

    def test_force_flag_skips_gate(self):
        from spirit.tools.registry import registry
        result = registry.dispatch(
            "terminal", {"command": "echo spirit_forced", "force": True}
        )
        assert "spirit_forced" in result

    def test_approved_command_prepends_note(self, monkeypatch):
        """用户批准的危险命令：正常执行并在输出前置审批说明。"""
        import spirit.tools.terminal_tool as tt
        monkeypatch.setattr(tt, "_try_vscode_terminal", lambda *a, **kw: None)
        monkeypatch.setattr(tt, "_execute_subprocess", lambda *a, **kw: "executed")

        set_approval_callback(lambda *a: "session")
        result = tt._handle_terminal("chmod 777 ./spirit_tmp_target")
        assert result.startswith("[审批]")
        assert "executed" in result

    def test_blocked_result_is_valid_json(self):
        import spirit.tools.terminal_tool as tt
        result = tt._handle_terminal(DANGEROUS_COMMAND)
        data = json.loads(result)
        assert data["blocked"] is True
        assert data["requires_user_approval"] is True
        assert data["pattern_key"]
