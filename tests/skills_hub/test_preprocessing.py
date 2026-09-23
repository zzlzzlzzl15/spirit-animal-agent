"""tests/skills_hub/test_preprocessing.py — 模板变量替换 + 内联 shell 展开。

对标 Hermes ``tests/agent/test_skill_commands.py`` 的 ``TestTemplateVarSubstitution`` +
``TestInlineShellExpansion``：``${SPIRIT_SKILL_DIR}`` / ``${SPIRIT_SESSION_ID}`` 只替换有值
token（未解析的原样保留）；``!`cmd``` 内联 shell **默认关闭**，开启后以技能目录为 CWD、
输出封顶、失败返回标记而非抛异常（一个坏片段不毁整条消息）。

真实子进程只跑一个跨平台 ``echo`` 冒烟；边界（超时 / shell 缺失 / 截断 / CWD）经
monkeypatch ``subprocess.run`` 确定性驱动，避免平台差异与真实耗时。
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from spirit.skills_hub import preprocessing


# ---------------------------------------------------------------------------
# 模板变量替换
# ---------------------------------------------------------------------------

class TestTemplateVars:
    def test_substitutes_skill_dir(self, tmp_path):
        out = preprocessing.substitute_template_vars(
            "run ${SPIRIT_SKILL_DIR}/x.py", tmp_path, None
        )
        assert str(tmp_path) in out
        assert "${SPIRIT_SKILL_DIR}" not in out

    def test_substitutes_session_id(self):
        out = preprocessing.substitute_template_vars(
            "session ${SPIRIT_SESSION_ID}", None, "sess-42"
        )
        assert out == "session sess-42"

    def test_both_tokens(self, tmp_path):
        out = preprocessing.substitute_template_vars(
            "${SPIRIT_SKILL_DIR} ${SPIRIT_SESSION_ID}", tmp_path, "s1"
        )
        assert str(tmp_path) in out and "s1" in out

    def test_unresolved_token_kept(self):
        # 无值 token 原样保留（方便作者调试）。
        out = preprocessing.substitute_template_vars(
            "dir=${SPIRIT_SKILL_DIR} sid=${SPIRIT_SESSION_ID}", None, None
        )
        assert "${SPIRIT_SKILL_DIR}" in out
        assert "${SPIRIT_SESSION_ID}" in out

    def test_partial_resolution(self, tmp_path):
        out = preprocessing.substitute_template_vars(
            "${SPIRIT_SKILL_DIR} ${SPIRIT_SESSION_ID}", tmp_path, None
        )
        assert str(tmp_path) in out
        assert "${SPIRIT_SESSION_ID}" in out

    def test_empty_content(self):
        assert preprocessing.substitute_template_vars("", None, "s") == ""

    def test_no_tokens_unchanged(self, tmp_path):
        text = "plain body, no tokens"
        assert preprocessing.substitute_template_vars(text, tmp_path, "s") == text


# ---------------------------------------------------------------------------
# 内联 shell — 真实冒烟（跨平台 echo）
# ---------------------------------------------------------------------------

class TestInlineShellReal:
    def test_expand_echo(self, tmp_path):
        out = preprocessing.expand_inline_shell("today: !`echo hello`", tmp_path, timeout=10)
        assert "today: hello" in out

    def test_no_marker_unchanged(self, tmp_path):
        text = "no inline shell here"
        assert preprocessing.expand_inline_shell(text, tmp_path, timeout=5) == text


# ---------------------------------------------------------------------------
# 内联 shell — 边界（monkeypatch subprocess.run 确定性驱动）
# ---------------------------------------------------------------------------

def _fake_run(stdout="", stderr="", monkeypatch=None, capture=None):
    def _run(argv, **kwargs):
        if capture is not None:
            capture.update({"argv": argv, "kwargs": kwargs})
        return SimpleNamespace(stdout=stdout, stderr=stderr)
    monkeypatch.setattr(preprocessing.subprocess, "run", _run)


class TestInlineShellEdges:
    def test_timeout_returns_marker(self, monkeypatch):
        def _raise(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1)
        monkeypatch.setattr(preprocessing.subprocess, "run", _raise)
        out = preprocessing.run_inline_shell("sleep 99", None, timeout=1)
        assert out.startswith("[inline-shell timeout")

    def test_shell_not_found_marker(self, monkeypatch):
        def _raise(argv, **kwargs):
            raise FileNotFoundError("no shell")
        monkeypatch.setattr(preprocessing.subprocess, "run", _raise)
        out = preprocessing.run_inline_shell("whatever", None, timeout=1)
        assert out == "[inline-shell error: shell not found]"

    def test_generic_error_marker(self, monkeypatch):
        def _raise(argv, **kwargs):
            raise RuntimeError("boom")
        monkeypatch.setattr(preprocessing.subprocess, "run", _raise)
        out = preprocessing.run_inline_shell("x", None, timeout=1)
        assert out.startswith("[inline-shell error:")
        assert "boom" in out

    def test_stderr_used_when_stdout_empty(self, monkeypatch):
        _fake_run(stdout="", stderr="warn text", monkeypatch=monkeypatch)
        out = preprocessing.run_inline_shell("x", None, timeout=5)
        assert out == "warn text"

    def test_output_truncated(self, monkeypatch):
        big = "x" * (preprocessing._INLINE_SHELL_MAX_OUTPUT + 500)
        _fake_run(stdout=big, monkeypatch=monkeypatch)
        out = preprocessing.run_inline_shell("x", None, timeout=5)
        assert out.endswith("...[truncated]")
        assert len(out) <= preprocessing._INLINE_SHELL_MAX_OUTPUT + len("...[truncated]")

    def test_cwd_is_skill_dir(self, tmp_path, monkeypatch):
        capture = {}
        _fake_run(stdout="ok", monkeypatch=monkeypatch, capture=capture)
        preprocessing.expand_inline_shell("!`pwd`", tmp_path, timeout=5)
        assert capture["kwargs"]["cwd"] == str(tmp_path)

    def test_empty_command_left_unchanged(self, monkeypatch):
        # !`` 空片段：正则 [^`\n]+ 要求至少一个字符，故不匹配、不触发子进程、原样保留。
        called = {"n": 0}
        def _run(argv, **kwargs):
            called["n"] += 1
            return SimpleNamespace(stdout="x", stderr="")
        monkeypatch.setattr(preprocessing.subprocess, "run", _run)
        out = preprocessing.expand_inline_shell("a!``b", None, timeout=5)
        assert called["n"] == 0
        assert out == "a!``b"


# ---------------------------------------------------------------------------
# preprocess_skill_content — 配置驱动
# ---------------------------------------------------------------------------

class TestPreprocessSkillContent:
    def test_template_vars_on_by_default(self, skills_home):
        # skills_cfg=None → 读 load_skills_config()；隔离的 SPIRIT_HOME 无 config.yaml，
        # 故回落到 DEFAULT_CONFIG.skills（template_vars=True）。
        out = preprocessing.preprocess_skill_content(
            "dir=${SPIRIT_SKILL_DIR}", skills_home, session_id="s1"
        )
        assert str(skills_home) in out

    def test_inline_shell_off_by_default(self, tmp_path, monkeypatch):
        # 默认关闭：!`...` 原样保留，绝不触发子进程。
        called = {"n": 0}
        def _run(argv, **kwargs):
            called["n"] += 1
            return SimpleNamespace(stdout="x", stderr="")
        monkeypatch.setattr(preprocessing.subprocess, "run", _run)
        out = preprocessing.preprocess_skill_content(
            "run !`echo hi`", tmp_path, skills_cfg={"template_vars": True, "inline_shell": False}
        )
        assert "!`echo hi`" in out
        assert called["n"] == 0

    def test_inline_shell_enabled_expands(self, tmp_path, monkeypatch):
        _fake_run(stdout="EXPANDED", monkeypatch=monkeypatch)
        out = preprocessing.preprocess_skill_content(
            "run !`echo hi`", tmp_path,
            skills_cfg={"template_vars": True, "inline_shell": True, "inline_shell_timeout": 7},
        )
        assert "EXPANDED" in out
        assert "!`" not in out

    def test_template_vars_disabled(self, tmp_path):
        out = preprocessing.preprocess_skill_content(
            "dir=${SPIRIT_SKILL_DIR}", tmp_path, session_id="s",
            skills_cfg={"template_vars": False},
        )
        assert "${SPIRIT_SKILL_DIR}" in out

    def test_empty_content_short_circuits(self):
        assert preprocessing.preprocess_skill_content("", None) == ""

    def test_session_id_passed_to_template(self, tmp_path):
        out = preprocessing.preprocess_skill_content(
            "sid=${SPIRIT_SESSION_ID}", tmp_path, session_id="abc",
            skills_cfg={"template_vars": True},
        )
        assert "abc" in out
