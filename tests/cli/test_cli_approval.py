"""tests/cli/test_cli_approval.py — CLI 交互式危险命令审批接线测试（3.6）。

验证两件事：
1. CLI 审批回调的三态映射（y=本次 / a=总是 / n=拒绝，异常默认拒绝）；
2. 两个 CLI 入口都真正注册了审批回调（防止接线回退成死代码）。
"""

import inspect

import pytest

from spirit.cli import main_enhanced
from spirit.tools import approval as approval_mod
from spirit.tools.approval import (
    clear_approval_callback,
    request_command_approval,
    set_approval_callback,
)

DANGEROUS_COMMAND = "rm -rf ./build_tmp_dir"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.delenv("SPIRIT_YOLO", raising=False)
    monkeypatch.setenv("SPIRIT_SESSION_KEY", f"cli-test-{id(monkeypatch)}")
    monkeypatch.setattr(approval_mod, "SPIRIT_HOME", tmp_path / "spirit-home")
    clear_approval_callback()
    yield
    clear_approval_callback()


def stub_input(monkeypatch, answer):
    """替换 rich console.input，避免测试真的等待键盘输入。"""
    printed = []
    monkeypatch.setattr(main_enhanced.console, "input", lambda *a, **kw: answer)
    monkeypatch.setattr(main_enhanced.console, "print", lambda *a, **kw: printed.append(a))
    return printed


class TestCliApprovalCallback:
    @pytest.mark.parametrize("answer,expected", [
        ("y", "session"),
        ("Y", "session"),
        ("yes", "session"),
        ("session", "session"),
        ("a", "always"),
        ("A", "always"),
        ("always", "always"),
        ("n", "deny"),
        ("no", "deny"),
        ("", "deny"),
        ("随便", "deny"),
    ])
    def test_answer_mapping(self, monkeypatch, answer, expected):
        stub_input(monkeypatch, answer)
        cb = main_enhanced._make_cli_approval_callback()
        assert cb(DANGEROUS_COMMAND, "recursive delete", "recursive delete") == expected

    def test_shows_command_and_reason_in_panel(self, monkeypatch):
        printed = stub_input(monkeypatch, "n")
        cb = main_enhanced._make_cli_approval_callback()
        cb("rm -rf ./tmp_dir", "recursive delete", "recursive delete")
        panel = printed[0][0]
        body = str(getattr(panel, "renderable", panel))
        title = str(getattr(panel, "title", ""))
        assert "rm -rf ./tmp_dir" in body
        assert "recursive delete" in body
        assert "危险命令需审批" in title

    @pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
    def test_interrupt_defaults_to_deny(self, monkeypatch, exc):
        def boom(*a, **kw):
            raise exc()

        monkeypatch.setattr(main_enhanced.console, "input", boom)
        monkeypatch.setattr(main_enhanced.console, "print", lambda *a, **kw: None)
        cb = main_enhanced._make_cli_approval_callback()
        assert cb(DANGEROUS_COMMAND, "d", "d") == "deny"

    def test_callback_end_to_end_with_gate(self, monkeypatch):
        """注册 CLI 回调后，审批门禁必须真正征求用户意见。"""
        stub_input(monkeypatch, "y")
        set_approval_callback(main_enhanced._make_cli_approval_callback())

        result = request_command_approval(DANGEROUS_COMMAND)
        assert result["approved"] is True
        assert result["user_approved"] is True

    def test_deny_end_to_end_blocks(self, monkeypatch):
        stub_input(monkeypatch, "n")
        set_approval_callback(main_enhanced._make_cli_approval_callback())
        result = request_command_approval(DANGEROUS_COMMAND)
        assert result["approved"] is False
        assert result["status"] == "denied"


class TestCliWiring:
    """接线回归防护：两个 CLI 入口都必须注册审批回调。"""

    def test_main_enhanced_registers_callback(self):
        src = inspect.getsource(main_enhanced)
        assert "set_approval_callback(" in src
        assert "_make_cli_approval_callback()" in src
        assert "set_interactive_context(True)" in src

    def test_main_registers_callback(self):
        from spirit.cli import main as basic_main
        # click 装饰后 chat 是 Command 对象，取回调函数源码
        src = inspect.getsource(getattr(basic_main.chat, "callback", basic_main.chat))
        assert "set_approval_callback(" in src
        assert "set_interactive_context(True)" in src
