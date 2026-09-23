"""``spirit.computer_use.permissions`` 就绪度 / 权限探测测试（Phase 4.5）。

对标 Hermes ``tests/tools/test_computer_use.py``（含 ``TestCuaEnvironmentScrubbing``）：验证
``driver_cmd`` 解析优先级、``_child_env`` 凭据剥离、``computer_use_status`` 各平台 / 二进制缺失 /
doctor 折叠 / macOS TCC 就绪 / 探测失败的就绪度折叠，以及 ``request_permissions_grant`` 的退出码
语义（64 非 macOS / 2 缺失 / 驱动退出码 / 130 中断 / 2 异常）。

所有外部副作用（``shutil.which`` 找二进制、``subprocess.run`` 跑探测）经 ``which`` / ``runner``
seam 注入假实现，离线断言各平台 / 各故障分支，无需真实桌面驱动。
"""

from __future__ import annotations

import json

import pytest

from spirit.computer_use import permissions as perms
from spirit.computer_use.permissions import (
    RUNTIME_PLATFORMS,
    computer_use_status,
    driver_cmd,
    request_permissions_grant,
)

from .conftest import FakeCompleted, make_runner, make_which


BINARY = "/usr/local/bin/cua-driver"


def _doctor_json(ok=True, probes=None):
    return json.dumps({"ok": ok, "probes": probes or []})


def _mac_json(accessibility=True, screen_recording=True, capturable=True, source=None):
    payload = {
        "accessibility": accessibility,
        "screen_recording": screen_recording,
        "screen_recording_capturable": capturable,
    }
    if source is not None:
        payload["source"] = source
    return json.dumps(payload)


def _raiser(exc):
    """造一个收到调用即抛 ``exc`` 的假 runner 响应（用于 KeyboardInterrupt 等非 Exception）。"""
    def _raise(cmd, timeout):
        raise exc
    return _raise


# ---------------------------------------------------------------------------
# driver_cmd 解析优先级：override > env > config > 默认
# ---------------------------------------------------------------------------

class TestDriverCmd:
    def test_override_wins(self, monkeypatch):
        monkeypatch.setenv("SPIRIT_CUA_DRIVER_CMD", "env-driver")
        assert driver_cmd("override-driver") == "override-driver"

    def test_env_over_config(self, monkeypatch):
        from spirit.config import DEFAULT_CONFIG

        monkeypatch.setitem(DEFAULT_CONFIG["computer_use"], "driver_cmd", "cfg-driver")
        monkeypatch.setenv("SPIRIT_CUA_DRIVER_CMD", "env-driver")
        assert driver_cmd() == "env-driver"

    def test_config_when_no_env(self, monkeypatch):
        from spirit.config import DEFAULT_CONFIG

        monkeypatch.delenv("SPIRIT_CUA_DRIVER_CMD", raising=False)
        monkeypatch.setitem(DEFAULT_CONFIG["computer_use"], "driver_cmd", "cfg-driver")
        assert driver_cmd() == "cfg-driver"

    def test_default_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("SPIRIT_CUA_DRIVER_CMD", raising=False)
        assert driver_cmd() == "cua-driver"

    def test_blank_env_falls_through_to_config(self, monkeypatch):
        from spirit.config import DEFAULT_CONFIG

        monkeypatch.setitem(DEFAULT_CONFIG["computer_use"], "driver_cmd", "cfg-driver")
        monkeypatch.setenv("SPIRIT_CUA_DRIVER_CMD", "   ")
        assert driver_cmd() == "cfg-driver"


# ---------------------------------------------------------------------------
# _child_env — 凭据剥离（对标 Hermes TestCuaEnvironmentScrubbing）
# ---------------------------------------------------------------------------

class TestChildEnv:
    def test_strips_api_key_token_secret(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        monkeypatch.setenv("MY_SECRET", "shh")
        monkeypatch.setenv("SAFE_VAR", "keep")
        env = perms._child_env()
        assert "OPENAI_API_KEY" not in env
        assert "GITHUB_TOKEN" not in env
        assert "MY_SECRET" not in env
        assert env.get("SAFE_VAR") == "keep"

    def test_suffix_match_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("lower_token", "x")
        monkeypatch.setenv("Mixed_Api_Key", "y")
        env = perms._child_env()
        assert "lower_token" not in env
        assert "Mixed_Api_Key" not in env

    def test_inherits_non_sensitive(self, monkeypatch):
        monkeypatch.setenv("SPIRIT_CUA_TEST_VAR", "hello")
        assert perms._child_env().get("SPIRIT_CUA_TEST_VAR") == "hello"

    def test_only_suffix_not_substring(self, monkeypatch):
        # "TOKENIZER" 含 "TOKEN" 但非以 _TOKEN 结尾 → 不应被剥离
        monkeypatch.setenv("MY_TOKENIZER", "keep")
        assert perms._child_env().get("MY_TOKENIZER") == "keep"


# ---------------------------------------------------------------------------
# computer_use_status — 二进制缺失
# ---------------------------------------------------------------------------

class TestStatusMissingBinary:
    def test_early_return_shape(self):
        out = computer_use_status(
            "cua-driver", platform="darwin", which=make_which(default=None),
        )
        assert out["installed"] is False
        assert out["ready"] is None
        assert out["error"] is None
        assert out["version"] is None
        assert out["checks"] == []
        assert out["platform"] == "darwin"
        assert out["platform_supported"] is True
        assert out["can_grant"] is True

    def test_no_runner_calls_when_missing(self):
        calls: list = []
        computer_use_status(
            "cua-driver", platform="linux",
            which=make_which(default=None), runner=make_runner(recorder=calls),
        )
        assert calls == []  # 二进制缺失 → 不 spawn 任何探测


# ---------------------------------------------------------------------------
# computer_use_status — 平台门
# ---------------------------------------------------------------------------

class TestStatusPlatform:
    def test_unsupported_platform_flag(self):
        out = computer_use_status(
            "cua-driver", platform="freebsd", which=make_which(default=BINARY),
            runner=make_runner({"--version": "1.0", "doctor --json": _doctor_json(True)}),
        )
        assert out["platform_supported"] is False
        assert out["can_grant"] is False

    def test_runtime_platforms_membership(self):
        assert RUNTIME_PLATFORMS == frozenset({"darwin", "win32", "linux"})


# ---------------------------------------------------------------------------
# computer_use_status — macOS（TCC 就绪）
# ---------------------------------------------------------------------------

class TestStatusDarwin:
    def _runner(self, acc=True, sr=True, doctor_ok=True, version="2.1.0", perm_error=False):
        return make_runner({
            "--version": version,
            "doctor --json": _doctor_json(doctor_ok),
            "permissions status --json": "not json" if perm_error else _mac_json(acc, sr),
        })

    def _status(self, runner):
        return computer_use_status(
            "cua-driver", platform="darwin", which=make_which(default=BINARY), runner=runner,
        )

    def test_ready_when_both_tcc_granted(self):
        out = self._status(self._runner(acc=True, sr=True))
        assert out["ready"] is True
        assert out["accessibility"] is True
        assert out["screen_recording"] is True
        assert out["can_grant"] is True
        assert out["version"] == "2.1.0"

    def test_not_ready_when_screen_recording_missing(self):
        out = self._status(self._runner(acc=True, sr=False))
        assert out["ready"] is False
        assert out["screen_recording"] is False

    def test_not_ready_when_accessibility_missing(self):
        out = self._status(self._runner(acc=False, sr=True))
        assert out["ready"] is False

    def test_permissions_probe_error_sets_error_ready_none(self):
        out = self._status(self._runner(perm_error=True))
        assert out["error"]
        assert out["ready"] is None

    def test_checks_folded_from_doctor_probes(self):
        runner = make_runner({
            "--version": "1.0",
            "doctor --json": _doctor_json(True, probes=[
                {"label": "binary", "status": "pass", "message": "ok"},
                {"label": "display", "status": "warn", "message": "no display"},
            ]),
            "permissions status --json": _mac_json(True, True),
        })
        out = self._status(runner)
        assert out["checks"][0] == {"label": "binary", "status": "pass", "message": "ok"}
        assert len(out["checks"]) == 2

    def test_source_folded_when_dict(self):
        runner = make_runner({
            "--version": "1.0",
            "doctor --json": _doctor_json(True),
            "permissions status --json": _mac_json(True, True, source={"kind": "tcc"}),
        })
        assert self._status(runner)["source"] == {"kind": "tcc"}


# ---------------------------------------------------------------------------
# computer_use_status — 非 macOS（就绪度 == doctor.ok）
# ---------------------------------------------------------------------------

class TestStatusNonDarwin:
    def test_ready_equals_doctor_ok(self):
        out = computer_use_status(
            "cua-driver", platform="win32", which=make_which(default=BINARY),
            runner=make_runner({"--version": "1.0", "doctor --json": _doctor_json(True)}),
        )
        assert out["ready"] is True
        assert out["can_grant"] is False
        assert out["accessibility"] is None  # 非 macOS 不跑 permissions status

    def test_not_ready_when_doctor_not_ok(self):
        out = computer_use_status(
            "cua-driver", platform="linux", which=make_which(default=BINARY),
            runner=make_runner({"--version": "1.0", "doctor --json": _doctor_json(False)}),
        )
        assert out["ready"] is False

    def test_ready_none_when_doctor_json_invalid(self):
        out = computer_use_status(
            "cua-driver", platform="linux", which=make_which(default=BINARY),
            runner=make_runner({"--version": "1.0", "doctor --json": "garbage"}),
        )
        assert out["ready"] is None

    def test_version_none_when_probe_raises(self):
        out = computer_use_status(
            "cua-driver", platform="linux", which=make_which(default=BINARY),
            runner=make_runner({
                "--version": _raiser(RuntimeError("boom")),
                "doctor --json": _doctor_json(True),
            }),
        )
        assert out["version"] is None
        assert out["ready"] is True  # doctor 仍可用


# ---------------------------------------------------------------------------
# request_permissions_grant — 退出码语义
# ---------------------------------------------------------------------------

class TestRequestPermissionsGrant:
    def test_non_darwin_returns_64(self):
        assert request_permissions_grant("cua-driver", platform="win32") == 64
        assert request_permissions_grant("cua-driver", platform="linux") == 64

    def test_missing_binary_returns_2(self):
        assert request_permissions_grant(
            "cua-driver", platform="darwin", which=make_which(default=None),
        ) == 2

    def test_success_returns_driver_exit_code(self):
        assert request_permissions_grant(
            "cua-driver", platform="darwin", which=make_which(default=BINARY),
            runner=make_runner({"permissions grant": FakeCompleted(returncode=0)}),
        ) == 0

    def test_nonzero_exit_passthrough(self):
        assert request_permissions_grant(
            "cua-driver", platform="darwin", which=make_which(default=BINARY),
            runner=make_runner({"permissions grant": FakeCompleted(returncode=3)}),
        ) == 3

    def test_keyboard_interrupt_returns_130(self):
        assert request_permissions_grant(
            "cua-driver", platform="darwin", which=make_which(default=BINARY),
            runner=make_runner({"permissions grant": _raiser(KeyboardInterrupt())}),
        ) == 130

    def test_generic_exception_returns_2(self):
        assert request_permissions_grant(
            "cua-driver", platform="darwin", which=make_which(default=BINARY),
            runner=make_runner({"permissions grant": _raiser(RuntimeError("boom"))}),
        ) == 2
