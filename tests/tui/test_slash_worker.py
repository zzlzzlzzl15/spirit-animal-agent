"""``spirit.tui.slash_worker`` 斜杠 worker 协议 + 看门狗测试（Phase 4.4）。

对标 Hermes ``tests/test_slash_worker_watchdog.py``（``_is_orphaned`` 语义 + 看门狗签名契约，
逐字移植仅改 import）；Spirit 额外覆盖 :func:`_env_float`（畸形环境旋钮回退）、
:func:`normalize_command`（补 ``/`` / strip / 空）、:func:`serve_stream`（把协议循环从
stdin/stdout 解耦后经 ``StringIO`` + 假 runner 离线断言：ok 帧、命令规范化、空命令不调 runner、
跳空行、runner 异常折叠、非法 JSON 折叠、多请求计数、id 类型保留）。全程无需 spawn 子进程。
"""

from __future__ import annotations

import inspect
import io
import json
from typing import Callable, List, Tuple

from spirit.tui.slash_worker import (
    _env_float,
    _is_orphaned,
    _start_parent_death_watchdog,
    normalize_command,
    serve_stream,
)


def _serve(lines: List[str], runner: Callable[[str], str]) -> Tuple[int, List[dict]]:
    """把 ``lines`` 喂进 :func:`serve_stream`，返回 ``(handled, 解析出的响应帧列表)``。"""
    text = "".join(line + "\n" for line in lines)
    inp = io.StringIO(text)
    out = io.StringIO()
    handled = serve_stream(inp, out, runner)
    frames = [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]
    return handled, frames


# ── 父进程死亡看门狗（对标 Hermes test_slash_worker_watchdog.py，逐字）──

def test_is_orphaned_true_when_ppid_changes():
    # 父进程消失，本 worker 被 reparent 到 subreaper/init。
    assert _is_orphaned(1234, getppid=lambda: 999999) is True


def test_is_orphaned_false_when_direct_parent_is_unchanged():
    original_ppid = 1234
    assert _is_orphaned(original_ppid, getppid=lambda: original_ppid) is False


def test_parent_death_watchdog_contract_has_no_create_time_plumbing():
    assert list(inspect.signature(_is_orphaned).parameters) == [
        "original_ppid",
        "getppid",
    ]
    assert list(inspect.signature(_start_parent_death_watchdog).parameters) == [
        "original_ppid",
    ]


# ── _env_float：畸形/缺失环境旋钮回退（绝不 import 期抛错）──

def test_env_float_parses_valid(monkeypatch):
    monkeypatch.setenv("SPIRIT_TEST_KNOB", "3.5")
    assert _env_float("SPIRIT_TEST_KNOB", 1.0) == 3.5


def test_env_float_missing_returns_default(monkeypatch):
    monkeypatch.delenv("SPIRIT_TEST_KNOB", raising=False)
    assert _env_float("SPIRIT_TEST_KNOB", 1.0) == 1.0


def test_env_float_malformed_returns_default(monkeypatch):
    monkeypatch.setenv("SPIRIT_TEST_KNOB", "2s")  # 笔误 → ValueError 被吞
    assert _env_float("SPIRIT_TEST_KNOB", 1.0) == 1.0


def test_env_float_empty_returns_default(monkeypatch):
    monkeypatch.setenv("SPIRIT_TEST_KNOB", "")
    assert _env_float("SPIRIT_TEST_KNOB", 1.0) == 1.0


# ── normalize_command：补 / / strip / 空 ──

def test_normalize_prepends_slash():
    assert normalize_command("goal") == "/goal"


def test_normalize_keeps_existing_slash():
    assert normalize_command("/goal") == "/goal"


def test_normalize_strips_whitespace():
    assert normalize_command("  /goal  ") == "/goal"


def test_normalize_empty_string():
    assert normalize_command("") == ""


def test_normalize_none():
    assert normalize_command(None) == ""


def test_normalize_whitespace_only():
    assert normalize_command("   ") == ""


# ── serve_stream：解耦协议循环（StringIO + 假 runner）──

def test_serve_stream_ok_frame(echo_slash_runner):
    handled, frames = _serve(['{"id":1,"command":"goal"}'], echo_slash_runner)
    assert handled == 1
    assert frames[0] == {"id": 1, "ok": True, "output": "ran:/goal"}


def test_serve_stream_normalizes_command(echo_slash_runner):
    _serve(['{"id":1,"command":"goal"}'], echo_slash_runner)
    assert echo_slash_runner.calls == ["/goal"]


def test_serve_stream_empty_command_skips_runner(echo_slash_runner):
    handled, frames = _serve(['{"id":1,"command":""}'], echo_slash_runner)
    assert handled == 1
    assert frames[0] == {"id": 1, "ok": True, "output": ""}
    assert echo_slash_runner.calls == []  # 空命令不得调 runner


def test_serve_stream_skips_blank_lines(echo_slash_runner):
    handled, frames = _serve(
        ['{"id":1,"command":"goal"}', "", "   ", '{"id":2,"command":"status"}'],
        echo_slash_runner,
    )
    assert handled == 2  # 两个空行被跳过
    assert [f["id"] for f in frames] == [1, 2]


def test_serve_stream_runner_exception_folds_to_error():
    def boom(command: str) -> str:
        raise ValueError("kaboom")

    handled, frames = _serve(['{"id":7,"command":"goal"}'], boom)
    assert handled == 1
    assert frames[0]["ok"] is False
    assert frames[0]["id"] == 7
    assert "kaboom" in frames[0]["error"]


def test_serve_stream_invalid_json_folds_to_error_with_null_id():
    handled, frames = _serve(["not json at all"], lambda c: "x")
    assert handled == 1
    assert frames[0]["ok"] is False
    assert frames[0]["id"] is None  # 无法解析出 id
    assert frames[0]["error"]


def test_serve_stream_multiple_requests(echo_slash_runner):
    handled, frames = _serve(
        ['{"id":1,"command":"goal"}', '{"id":2,"command":"/status"}'],
        echo_slash_runner,
    )
    assert handled == 2
    assert [f["id"] for f in frames] == [1, 2]
    assert echo_slash_runner.calls == ["/goal", "/status"]


def test_serve_stream_preserves_string_id(echo_slash_runner):
    _serve(['{"id":"abc","command":"goal"}'], echo_slash_runner)
    handled, frames = _serve(['{"id":"xyz","command":"goal"}'], echo_slash_runner)
    assert frames[0]["id"] == "xyz"  # id 类型/值原样回传
    assert handled == 1
