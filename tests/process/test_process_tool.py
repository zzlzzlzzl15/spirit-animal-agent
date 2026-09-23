"""``process`` 工具 handler + 输出脱敏单测。

对标 Hermes ``tests/tools/test_process_registry.py`` 的 TestProcessToolHandler
与 TestHandleProcessRedaction。handler 走 args-dict 约定（``_handle_process(args)``），
用 patch 把 ``process_tool.process_registry`` 换成受控实例来隔离全局单例。
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from spirit.process.registry import ProcessRegistry
from spirit.tools import process_tool
from spirit.tools.process_tool import (
    _handle_process,
    _redact_process_result,
    _redact_text,
)

from .conftest import _make_session


@pytest.fixture()
def tool_registry():
    """把 process_tool 用的全局注册表换成受控实例。"""
    reg = ProcessRegistry(rehydrate_delegations=False)
    with patch.object(process_tool, "process_registry", reg):
        yield reg


def _call(args, **kwargs) -> dict:
    return json.loads(_handle_process(args, **kwargs))


# =========================================================================
# 脱敏
# =========================================================================

class TestRedaction:
    def test_aws_key(self):
        assert "[REDACTED]" in _redact_text("key=AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in _redact_text("AKIAIOSFODNN7EXAMPLE")

    def test_github_token(self):
        out = _redact_text("token ghp_abcdefghijklmnopqrstuvwxyz0123")
        assert "ghp_abcdefgh" not in out
        assert "[REDACTED]" in out

    def test_openai_style_key(self):
        out = _redact_text("sk-abcdefghijklmnopqrstuvwxyz")
        assert "sk-abcdefgh" not in out

    def test_bearer_header(self):
        out = _redact_text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz")
        assert "Bearer abcdefg" not in out
        assert "[REDACTED]" in out

    def test_kv_password_preserves_key(self):
        out = _redact_text("password: hunter2secret")
        assert "password" in out          # 键名保留
        assert "hunter2secret" not in out  # 值被抹
        assert "[REDACTED]" in out

    def test_kv_api_key_equals(self):
        out = _redact_text("api_key=supersecretvalue")
        assert "api_key" in out
        assert "supersecretvalue" not in out

    def test_empty_and_clean_text_unchanged(self):
        assert _redact_text("") == ""
        assert _redact_text("just normal output") == "just normal output"

    def test_redact_result_fields(self):
        result = {
            "command": "echo password=hunter2secret",
            "output": "AKIAIOSFODNN7EXAMPLE",
            "output_preview": "token ghp_abcdefghijklmnopqrstuvwxyz0",
            "status": "exited",
        }
        red = _redact_process_result(result)
        assert "hunter2secret" not in red["command"]
        assert "AKIAIOSFODNN7EXAMPLE" not in red["output"]
        assert "ghp_abcdefgh" not in red["output_preview"]
        assert red["status"] == "exited"  # 非敏感字段原样

    def test_redact_non_dict_passthrough(self):
        assert _redact_process_result("plain") == "plain"
        assert _redact_process_result(None) is None


# =========================================================================
# handler 动作
# =========================================================================

class TestProcessToolHandler:
    def test_unknown_action(self, tool_registry):
        out = _call({"action": "bogus"})
        assert "error" in out
        assert "bogus" in out["error"]

    def test_missing_session_id(self, tool_registry):
        out = _call({"action": "poll"})
        assert "error" in out
        assert "session_id" in out["error"]

    def test_list(self, tool_registry):
        s = _make_session(sid="L1", task_id="t1")
        tool_registry._running[s.id] = s
        out = _call({"action": "list"}, task_id="t1")
        assert out["running"] == 1
        assert any(p["session_id"] == "L1" for p in out["processes"])

    def test_list_integer_session_tolerated(self, tool_registry):
        """list 不需要 session_id；即便给了整数也不报错。"""
        out = _call({"action": "list", "session_id": 123})
        assert "processes" in out

    def test_poll(self, tool_registry):
        s = _make_session(sid="P1", output="hello world")
        tool_registry._running[s.id] = s
        out = _call({"action": "poll", "session_id": "P1"})
        assert out["status"] == "running"
        assert "hello world" in out["output_preview"]

    def test_poll_redacts_output(self, tool_registry):
        s = _make_session(sid="P2", output="AKIAIOSFODNN7EXAMPLE")
        tool_registry._running[s.id] = s
        out = _call({"action": "poll", "session_id": "P2"})
        assert "AKIAIOSFODNN7EXAMPLE" not in out["output_preview"]

    def test_log(self, tool_registry):
        s = _make_session(sid="G1", output="\n".join(f"l{i}" for i in range(10)))
        tool_registry._running[s.id] = s
        out = _call({"action": "log", "session_id": "G1", "limit": 3})
        assert out["total_lines"] == 10
        assert out["output"].splitlines()[-1] == "l9"

    def test_wait(self, tool_registry):
        s = _make_session(sid="W1", exited=True, exit_code=0, output="done")
        tool_registry._finished[s.id] = s
        out = _call({"action": "wait", "session_id": "W1", "timeout": 1})
        assert out["status"] == "exited"
        assert out["exit_code"] == 0

    def test_kill(self, tool_registry):
        s = _make_session(sid="K1", exited=True, exit_code=0)
        tool_registry._finished[s.id] = s
        out = _call({"action": "kill", "session_id": "K1"})
        assert out["status"] == "already_exited"

    def test_write_submit_close_not_found(self, tool_registry):
        for action in ("write", "submit", "close"):
            out = _call({"action": action, "session_id": "nope", "data": "x"})
            assert out["status"] == "not_found"

    def test_submit_uses_stdin(self, tool_registry):
        from unittest.mock import MagicMock

        s = _make_session(sid="S1")
        s.process = MagicMock()
        s.process.stdin = MagicMock()
        tool_registry._running[s.id] = s
        out = _call({"action": "submit", "session_id": "S1", "data": "yes"})
        assert out["status"] == "ok"
        s.process.stdin.write.assert_called_once_with("yes\n")

    def test_integer_session_id_coerced(self, tool_registry):
        """模型有时把 session_id 发成整数 → handler 强制转字符串。"""
        s = _make_session(sid="42", output="x")
        tool_registry._running["42"] = s
        out = _call({"action": "poll", "session_id": 42})
        assert out["status"] == "running"


# =========================================================================
# schema / 注册
# =========================================================================

class TestSchemaAndRegistration:
    def test_schema_shape(self):
        schema = process_tool.PROCESS_SCHEMA
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] == "process"
        actions = fn["parameters"]["properties"]["action"]["enum"]
        assert set(actions) == {
            "list", "poll", "log", "wait", "kill", "write", "submit", "close",
        }
        assert fn["parameters"]["required"] == ["action"]

    def test_registered_in_tool_registry(self):
        from spirit.tools.registry import registry as tool_reg

        assert "process" in tool_reg.get_tool_names()
