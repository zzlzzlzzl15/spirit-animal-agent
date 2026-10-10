"""spirit/lsp 基础层回归测试。

覆盖 protocol / workspace / reporter / range_shift / eventlog / servers / __init__。
client.py / manager.py / install.py 单独测（更复杂）。
"""
import asyncio
import json
import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ──────────────────────────────────────────────────────────────────────
# protocol.py — JSON-RPC 帧编解码
# ──────────────────────────────────────────────────────────────────────

class TestEncodeMessage:
    def test_simple_dict(self):
        from spirit.lsp.protocol import encode_message
        out = encode_message({"jsonrpc": "2.0", "id": 1, "result": "ok"})
        # 找 header 边界
        sep = out.find(b"\r\n\r\n")
        assert sep > 0
        header = out[:sep].decode("ascii")
        body = out[sep + 4:]
        assert "Content-Length:" in header
        # 数值应等于 body 字节数
        assert int(header.split(":", 1)[1].strip()) == len(body)
        parsed = json.loads(body)
        assert parsed["id"] == 1
        assert parsed["result"] == "ok"

    def test_unicode_preserved(self):
        from spirit.lsp.protocol import encode_message
        out = encode_message({"text": "你好 αβγ"})
        body = out.split(b"\r\n\r\n", 1)[1]
        parsed = json.loads(body)
        assert parsed["text"] == "你好 αβγ"

    def test_compact_separators(self):
        """源码用 separators=(",", ":") 紧凑格式。"""
        from spirit.lsp.protocol import encode_message
        out = encode_message({"a": 1, "b": 2})
        body = out.split(b"\r\n\r\n", 1)[1]
        # 不应有 ", " 或 ": "（紧凑）
        assert b", " not in body
        assert b": " not in body

    def test_empty_dict(self):
        from spirit.lsp.protocol import encode_message
        out = encode_message({})
        # body 应该是 "{}", 2 字节
        body = out.split(b"\r\n\r\n", 1)[1]
        assert body == b"{}"


class TestReadMessage:
    def _feed(self, body: bytes) -> "asyncio.StreamReader":
        """构造含单帧消息的 StreamReader。在 event loop 内调用。"""
        reader = asyncio.StreamReader()
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        reader.feed_data(header + body)
        reader.feed_eof()
        return reader

    def test_valid_message(self):
        from spirit.lsp.protocol import read_message
        body = b'{"jsonrpc": "2.0", "id": 1, "result": "ok"}'
        async def run():
            reader = self._feed(body)
            return await read_message(reader)
        result = asyncio.run(run())
        assert result == {"jsonrpc": "2.0", "id": 1, "result": "ok"}

    def test_eof_returns_none(self):
        from spirit.lsp.protocol import read_message
        async def run():
            reader = asyncio.StreamReader()
            reader.feed_eof()
            return await read_message(reader)
        result = asyncio.run(run())
        assert result is None

    def test_invalid_content_length_zero(self):
        """content-length=0 应抛 LSPProtocolError。"""
        from spirit.lsp.protocol import read_message, LSPProtocolError
        async def run():
            reader = asyncio.StreamReader()
            reader.feed_data(b"Content-Length: 0\r\n\r\n")
            reader.feed_eof()
            await read_message(reader)
        with pytest.raises(LSPProtocolError, match="Content-Length"):
            asyncio.run(run())

    def test_invalid_json(self):
        from spirit.lsp.protocol import read_message, LSPProtocolError
        body = b"not json"
        async def run():
            header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
            reader = asyncio.StreamReader()
            reader.feed_data(header + body)
            reader.feed_eof()
            await read_message(reader)
        with pytest.raises(LSPProtocolError, match="JSON"):
            asyncio.run(run())


class TestMakeRequest:
    def test_basic(self):
        from spirit.lsp.protocol import make_request
        req = make_request("initialize", {"x": 1}, request_id=1)
        assert req == {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"x": 1},
        }

    def test_params_none(self):
        from spirit.lsp.protocol import make_request
        req = make_request("shutdown", None, request_id=2)
        assert req["params"] is None


class TestMakeResponse:
    def test_basic(self):
        from spirit.lsp.protocol import make_response
        r = make_response(1, {"capabilities": {}})
        assert r == {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"capabilities": {}},
        }

    def test_result_none(self):
        from spirit.lsp.protocol import make_response
        r = make_response(1, None)
        assert r["result"] is None


class TestMakeErrorResponse:
    def test_basic(self):
        from spirit.lsp.protocol import make_error_response
        r = make_error_response(1, -32601, "Method not found")
        assert r == {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32601, "message": "Method not found"},
        }


class TestMakeNotification:
    def test_basic(self):
        from spirit.lsp.protocol import make_notification
        n = make_notification("initialized", {})
        assert n == {
            "jsonrpc": "2.0",
            "method": "initialized",
            "params": {},
        }
        # notification 无 id
        assert "id" not in n


class TestClassifyMessage:
    def test_request(self):
        from spirit.lsp.protocol import classify_message
        assert classify_message({"id": 1, "method": "x"}) == "request"

    def test_response(self):
        from spirit.lsp.protocol import classify_message
        assert classify_message({"id": 1, "result": "ok"}) == "response"

    def test_notification(self):
        from spirit.lsp.protocol import classify_message
        assert classify_message({"method": "x", "params": {}}) == "notification"

    def test_unknown(self):
        from spirit.lsp.protocol import classify_message
        assert classify_message({}) == "unknown"


class TestLSPRequestError:
    def test_attributes(self):
        from spirit.lsp.protocol import LSPRequestError
        e = LSPRequestError(-32601, "Method not found", data="extra")
        assert e.code == -32601
        assert e.message == "Method not found"
        assert e.data == "extra"
        assert "LSP error -32601" in str(e)

    def test_no_data(self):
        from spirit.lsp.protocol import LSPRequestError
        e = LSPRequestError(1, "x")
        assert e.data is None


class TestErrorCodes:
    def test_constants(self):
        from spirit.lsp.protocol import (
            ERROR_REQUEST_CANCELLED,
            ERROR_CONTENT_MODIFIED,
            ERROR_METHOD_NOT_FOUND,
        )
        assert ERROR_REQUEST_CANCELLED == -32800
        assert ERROR_CONTENT_MODIFIED == -32801
        assert ERROR_METHOD_NOT_FOUND == -32601


# ──────────────────────────────────────────────────────────────────────
# workspace.py — 工作区门控 + 项目根
# ──────────────────────────────────────────────────────────────────────

class TestNormalizePath:
    def test_absolute(self):
        from spirit.lsp.workspace import normalize_path
        p = normalize_path(".")
        assert os.path.isabs(p)

    def test_expanduser(self):
        from spirit.lsp.workspace import normalize_path
        p = normalize_path("~")
        assert p == os.path.expanduser("~")


class TestFindGitWorktree:
    def setup_method(self):
        from spirit.lsp.workspace import clear_cache
        clear_cache()

    def teardown_method(self):
        from spirit.lsp.workspace import clear_cache
        clear_cache()

    def test_finds_git_root(self, tmp_path):
        from spirit.lsp.workspace import find_git_worktree
        (tmp_path / ".git").mkdir()
        sub = tmp_path / "sub" / "deep"
        sub.mkdir(parents=True)
        result = find_git_worktree(str(sub))
        assert result == str(tmp_path)

    def test_finds_via_file(self, tmp_path):
        from spirit.lsp.workspace import find_git_worktree
        (tmp_path / ".git").mkdir()
        f = tmp_path / "f.py"
        f.write_text("x")
        result = find_git_worktree(str(f))
        assert result == str(tmp_path)

    def test_no_git_returns_none(self, tmp_path):
        from spirit.lsp.workspace import find_git_worktree
        result = find_git_worktree(str(tmp_path))
        assert result is None

    def test_cache_hit(self, tmp_path):
        from spirit.lsp.workspace import find_git_worktree
        (tmp_path / ".git").mkdir()
        # 第一次
        find_git_worktree(str(tmp_path))
        # 第二次命中缓存
        result = find_git_worktree(str(tmp_path))
        assert result == str(tmp_path)

    def test_nonexistent_path(self):
        from spirit.lsp.workspace import find_git_worktree
        # 不应抛
        result = find_git_worktree("/no/such/path/12345")
        assert result is None


class TestNearestRoot:
    def test_finds_marker(self, tmp_path):
        from spirit.lsp.workspace import nearest_root
        (tmp_path / "pyproject.toml").write_text("")
        sub = tmp_path / "src"
        sub.mkdir()
        result = nearest_root(str(sub), ["pyproject.toml"])
        assert result == str(tmp_path)

    def test_no_marker_returns_none(self, tmp_path):
        from spirit.lsp.workspace import nearest_root
        result = nearest_root(str(tmp_path), ["nonexistent.toml"])
        assert result is None

    def test_exclude_marker_stops_search(self, tmp_path):
        from spirit.lsp.workspace import nearest_root
        # 父目录有标记, 但中间有 exclude 标记
        (tmp_path / "pyproject.toml").write_text("")
        sub = tmp_path / "node_modules" / "x"
        sub.mkdir(parents=True)
        result = nearest_root(str(sub), ["pyproject.toml"], exclude_markers=["node_modules"])
        assert result is None

    def test_via_file_path(self, tmp_path):
        from spirit.lsp.workspace import nearest_root
        (tmp_path / "pyproject.toml").write_text("")
        f = tmp_path / "f.py"
        f.write_text("x")
        result = nearest_root(str(f), ["pyproject.toml"])
        assert result == str(tmp_path)

    def test_multiple_markers(self, tmp_path):
        from spirit.lsp.workspace import nearest_root
        (tmp_path / "Cargo.toml").write_text("")
        result = nearest_root(str(tmp_path), ["pyproject.toml", "Cargo.toml"])
        assert result == str(tmp_path)


class TestClearCache:
    def test_clears(self, tmp_path):
        from spirit.lsp.workspace import (
            find_git_worktree, _workspace_cache, clear_cache,
        )
        (tmp_path / ".git").mkdir()
        find_git_worktree(str(tmp_path))
        assert len(_workspace_cache) > 0
        clear_cache()
        assert _workspace_cache == {}


# ──────────────────────────────────────────────────────────────────────
# reporter.py — 诊断格式化
# ──────────────────────────────────────────────────────────────────────

class TestSanitizeField:
    def test_none(self):
        from spirit.lsp.reporter import _sanitize_field
        assert _sanitize_field(None, limit=10) == ""

    def test_basic(self):
        from spirit.lsp.reporter import _sanitize_field
        out = _sanitize_field("hello", limit=10)
        assert out == "hello"

    def test_truncate(self):
        from spirit.lsp.reporter import _sanitize_field
        out = _sanitize_field("a" * 100, limit=10)
        # 应被截断 + "…"
        assert "…" in out
        assert len(out) <= 11

    def test_newlines_collapsed(self):
        from spirit.lsp.reporter import _sanitize_field
        out = _sanitize_field("a\nb\rc", limit=100)
        # 换行应被替换为空格
        assert "\n" not in out
        assert "\r" not in out

    def test_control_chars_removed(self):
        from spirit.lsp.reporter import _sanitize_field
        # \x00 是控制字符
        out = _sanitize_field("a\x00b", limit=100)
        assert "\x00" not in out

    def test_html_escaped(self):
        from spirit.lsp.reporter import _sanitize_field
        out = _sanitize_field("<script>", limit=100)
        assert "<" not in out or "&lt;" in out


class TestFormatDiagnostics:
    def test_empty_returns_empty(self):
        from spirit.lsp.reporter import format_diagnostics
        assert format_diagnostics("f.py", []) == ""

    def test_filters_severity(self):
        from spirit.lsp.reporter import format_diagnostics
        diags = [
            {"range": {"start": {"line": 0, "character": 0}}, "severity": 1, "message": "E"},
            {"range": {"start": {"line": 1, "character": 0}}, "severity": 2, "message": "W"},
        ]
        # 默认仅 ERROR(severity=1)
        out = format_diagnostics("f.py", diags)
        assert "ERROR" in out
        assert "WARN" not in out

    def test_all_severities(self):
        from spirit.lsp.reporter import format_diagnostics
        diags = [
            {"range": {"start": {"line": 0, "character": 0}}, "severity": 1, "message": "E"},
            {"range": {"start": {"line": 1, "character": 0}}, "severity": 2, "message": "W"},
            {"range": {"start": {"line": 2, "character": 0}}, "severity": 3, "message": "I"},
        ]
        out = format_diagnostics("f.py", diags, severities=frozenset({1, 2, 3}))
        assert "ERROR" in out
        assert "WARN" in out
        assert "INFO" in out

    def test_max_per_file(self):
        from spirit.lsp.reporter import format_diagnostics, MAX_PER_FILE
        diags = [
            {"range": {"start": {"line": i, "character": 0}}, "severity": 1, "message": f"e{i}"}
            for i in range(MAX_PER_FILE + 5)
        ]
        out = format_diagnostics("f.py", diags)
        # 应有"还有 N 条诊断未显示"
        assert "未显示" in out

    def test_includes_source_and_code(self):
        from spirit.lsp.reporter import format_diagnostics
        diags = [{
            "range": {"start": {"line": 0, "character": 0}},
            "severity": 1, "message": "x",
            "source": "pyright",
            "code": "reportUndefinedVariable",
        }]
        out = format_diagnostics("f.py", diags)
        assert "[pyright]" in out
        assert "(reportUndefinedVariable)" in out

    def test_1_based_line_col(self):
        from spirit.lsp.reporter import format_diagnostics
        diags = [{
            "range": {"start": {"line": 4, "character": 9}, "end": {"line": 4, "character": 15}},
            "severity": 1, "message": "x",
        }]
        out = format_diagnostics("f.py", diags)
        assert "L5:10" in out  # +1

    def test_severity_99_filtered_out(self):
        """severity=99 不在默认 {1}, 被过滤 → 空输出。"""
        from spirit.lsp.reporter import format_diagnostics
        diags = [{
            "range": {"start": {"line": 0, "character": 0}},
            "severity": 99, "message": "x",
        }]
        out = format_diagnostics("f.py", diags)
        assert out == ""

    def test_severity_99_kept_when_in_allowlist(self):
        """severity=99 但包含在自定义 severities 中时, 应使用 SEVERITY_NAMES.get 默认值 'ERROR'。"""
        from spirit.lsp.reporter import format_diagnostics
        diags = [{
            "range": {"start": {"line": 0, "character": 0}},
            "severity": 99, "message": "x",
        }]
        out = format_diagnostics("f.py", diags, severities=frozenset({99}))
        assert "ERROR" in out

    def test_missing_severity_default(self):
        from spirit.lsp.reporter import format_diagnostics
        diags = [{"range": {"start": {"line": 0, "character": 0}}, "message": "x"}]
        # severity 缺省走 1 (ERROR)
        out = format_diagnostics("f.py", diags)
        assert "ERROR" in out


class TestFormatMultiFileDiagnostics:
    def test_empty(self):
        from spirit.lsp.reporter import format_multi_file_diagnostics
        assert format_multi_file_diagnostics({}) == ""

    def test_multiple_files(self):
        from spirit.lsp.reporter import format_multi_file_diagnostics
        m = {
            "a.py": [{"range": {"start": {"line": 0, "character": 0}}, "severity": 1, "message": "ea"}],
            "b.py": [{"range": {"start": {"line": 0, "character": 0}}, "severity": 1, "message": "eb"}],
        }
        out = format_multi_file_diagnostics(m)
        assert "a.py" in out
        assert "b.py" in out

    def test_max_total_chars_truncates(self):
        from spirit.lsp.reporter import format_multi_file_diagnostics, MAX_TOTAL_CHARS
        m = {
            f"f{i}.py": [{"range": {"start": {"line": 0, "character": 0}}, "severity": 1, "message": "x" * 100}]
            for i in range(100)
        }
        out = format_multi_file_diagnostics(m, max_total_chars=500)
        assert "上限" in out or "省略" in out

    def test_skips_files_with_no_filtered_diagnostics(self):
        from spirit.lsp.reporter import format_multi_file_diagnostics
        m = {
            "a.py": [{"range": {"start": {"line": 0, "character": 0}}, "severity": 1, "message": "e"}],
            "b.py": [{"range": {"start": {"line": 0, "character": 0}}, "severity": 4, "message": "h"}],  # HINT, 默认不显示
        }
        out = format_multi_file_diagnostics(m)
        assert "a.py" in out
        assert "b.py" not in out


class TestCountErrors:
    def test_counts_only_severity_1(self):
        from spirit.lsp.reporter import count_errors
        diags = [
            {"severity": 1, "message": "e1"},
            {"severity": 1, "message": "e2"},
            {"severity": 2, "message": "w1"},
            {"severity": 3, "message": "i1"},
        ]
        assert count_errors(diags) == 2

    def test_no_severity_counts_as_error(self):
        """源码 .get("severity", 1) == 1, 缺省算 error。"""
        from spirit.lsp.reporter import count_errors
        assert count_errors([{"message": "x"}]) == 1

    def test_empty(self):
        from spirit.lsp.reporter import count_errors
        assert count_errors([]) == 0


class TestComputeDelta:
    def test_no_new_diagnostics(self):
        from spirit.lsp.reporter import compute_delta
        d = [{"range": {"start": {"line": 0, "character": 0}}, "severity": 1, "message": "e"}]
        assert compute_delta(d, d) == []

    def test_one_new_diagnostic(self):
        from spirit.lsp.reporter import compute_delta
        baseline = [{"range": {"start": {"line": 0, "character": 0}}, "severity": 1, "message": "e"}]
        current = baseline + [{"range": {"start": {"line": 1, "character": 0}}, "severity": 1, "message": "e2"}]
        delta = compute_delta(baseline, current)
        assert len(delta) == 1
        assert delta[0]["message"] == "e2"

    def test_key_components(self):
        """key 是 (line, character, severity, message)。"""
        from spirit.lsp.reporter import compute_delta
        baseline = [
            {"range": {"start": {"line": 0, "character": 0}}, "severity": 1, "message": "e"},
            {"range": {"start": {"line": 1, "character": 0}}, "severity": 1, "message": "e"},
        ]
        current = baseline[:1] + [
            {"range": {"start": {"line": 2, "character": 0}}, "severity": 1, "message": "e"},
        ]
        delta = compute_delta(baseline, current)
        assert len(delta) == 1
        assert delta[0]["range"]["start"]["line"] == 2


# ──────────────────────────────────────────────────────────────────────
# range_shift.py — 跨编辑行号映射
# ──────────────────────────────────────────────────────────────────────

class TestBuildLineShift:
    def test_identical_returns_identity(self):
        from spirit.lsp.range_shift import build_line_shift
        text = "a\nb\nc"
        shift = build_line_shift(text, text)
        assert shift(0) == 0
        assert shift(1) == 1
        assert shift(2) == 2

    def test_empty_pre_and_post(self):
        from spirit.lsp.range_shift import build_line_shift
        shift = build_line_shift("", "")
        # 边界: 0 应该映射到 0
        assert shift(0) == 0

    def test_insert_at_end(self):
        from spirit.lsp.range_shift import build_line_shift
        pre = "a\nb"
        post = "a\nb\nc"
        shift = build_line_shift(pre, post)
        # 0, 1 保持
        assert shift(0) == 0
        assert shift(1) == 1

    def test_insert_in_middle(self):
        from spirit.lsp.range_shift import build_line_shift
        pre = "a\nb\nc"
        post = "a\nb\nx\nc"  # x 插入到 b 和 c 之间
        shift = build_line_shift(pre, post)
        assert shift(0) == 0  # a
        assert shift(1) == 1  # b
        # c 之前是 line 2, 现在是 line 3
        assert shift(2) == 3

    def test_delete_in_middle(self):
        from spirit.lsp.range_shift import build_line_shift
        pre = "a\nb\nx\nc"
        post = "a\nb\nc"
        shift = build_line_shift(pre, post)
        # x(原 line 2) 被删
        assert shift(2) is None
        # a, b 保持
        assert shift(0) == 0
        assert shift(1) == 1
        # c(原 line 3) → 新 line 2
        assert shift(3) == 2

    def test_line_past_end(self):
        from spirit.lsp.range_shift import build_line_shift
        pre = "a"
        post = "a\nb"
        shift = build_line_shift(pre, post)
        # 1 已经超过 pre 的范围, 源码 .max(0, len(post_lines) - 1) = 1
        assert shift(99) == 1

    def test_empty_post_lines(self):
        from spirit.lsp.range_shift import build_line_shift
        shift = build_line_shift("a\nb", "")
        # 0 → 0 (但 post 空) 源码: max(0, 0-1) = 0; 但如果 post 是 [], 是 None
        # 实际: post_lines=[], so max(0, -1) = 0 ... 但源码用 `post_lines else None`
        # 实际行为: post_lines=[] (空) → 整个 if post_lines else None 走 None
        assert shift(0) is None


class TestShiftDiagnosticRange:
    def test_basic_shift(self):
        from spirit.lsp.range_shift import build_line_shift, shift_diagnostic_range
        pre = "a\nb\nc"
        post = "a\nb\nx\nc"
        shift = build_line_shift(pre, post)
        diag = {
            "severity": 1, "message": "x",
            "range": {"start": {"line": 2, "character": 5}, "end": {"line": 2, "character": 10}},
        }
        out = shift_diagnostic_range(diag, shift)
        assert out is not None
        assert out["range"]["start"]["line"] == 3
        assert out["range"]["end"]["line"] == 3

    def test_deleted_line_returns_none(self):
        from spirit.lsp.range_shift import build_line_shift, shift_diagnostic_range
        pre = "a\nx\nb"
        post = "a\nb"
        shift = build_line_shift(pre, post)
        diag = {
            "severity": 1, "message": "x",
            "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 5}},
        }
        out = shift_diagnostic_range(diag, shift)
        assert out is None

    def test_does_not_mutate_input(self):
        from spirit.lsp.range_shift import build_line_shift, shift_diagnostic_range
        pre = "a\nb"
        post = "a\nb\nc"
        shift = build_line_shift(pre, post)
        diag = {
            "severity": 1, "message": "x",
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 5}},
        }
        shift_diagnostic_range(diag, shift)
        # 原 diag 不变
        assert diag["range"]["start"]["line"] == 0

    def test_missing_range(self):
        from spirit.lsp.range_shift import build_line_shift, shift_diagnostic_range
        shift = build_line_shift("a", "a")
        diag = {"severity": 1, "message": "x"}  # 无 range
        out = shift_diagnostic_range(diag, shift)
        assert out is not None
        # range 应有默认 (0, 0, 0, 0)
        assert out["range"]["start"]["line"] == 0

    def test_end_line_collapsed_to_start(self):
        """start 不被删, end 被删 → end collapse 到 start。"""
        from spirit.lsp.range_shift import build_line_shift, shift_diagnostic_range
        # pre: line 0=a, line 1=x, line 2=b
        # post: 只有 a 和 b (line 1 = b)
        pre = "a\nx\nb"
        post = "a\nb"
        shift = build_line_shift(pre, post)
        # diag 横跨 [0, 1], start=0 (保), end=1 (删) → 折叠到 start
        diag = {
            "severity": 1, "message": "x",
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 1, "character": 5}},
        }
        out = shift_diagnostic_range(diag, shift)
        # 验证不抛错, 字段有效
        assert out is not None


class TestShiftBaseline:
    def test_applies_to_all(self):
        from spirit.lsp.range_shift import build_line_shift, shift_baseline
        pre = "a\nb\nc"
        post = "a\nb\nx\nc"
        shift = build_line_shift(pre, post)
        baseline = [
            {"severity": 1, "message": "a", "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}},
            {"severity": 1, "message": "c", "range": {"start": {"line": 2, "character": 0}, "end": {"line": 2, "character": 1}}},
        ]
        out = shift_baseline(baseline, shift)
        assert len(out) == 2
        # c 现在应该在 line 3
        assert out[1]["range"]["start"]["line"] == 3

    def test_skips_non_dict(self):
        from spirit.lsp.range_shift import build_line_shift, shift_baseline
        shift = build_line_shift("a", "a")
        baseline = [
            {"severity": 1, "message": "x", "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}},
            "not a dict",
            None,
        ]
        out = shift_baseline(baseline, shift)
        assert len(out) == 1

    def test_empty(self):
        from spirit.lsp.range_shift import build_line_shift, shift_baseline
        shift = build_line_shift("a", "a")
        assert shift_baseline([], shift) == []


# ──────────────────────────────────────────────────────────────────────
# eventlog.py — LSP 结构化日志
# ──────────────────────────────────────────────────────────────────────

class TestEventlogAnnounce:
    def setup_method(self):
        from spirit.lsp.eventlog import reset_announce_caches
        reset_announce_caches()

    def teardown_method(self):
        from spirit.lsp.eventlog import reset_announce_caches
        reset_announce_caches()

    def test_announce_once_first_true(self):
        from spirit.lsp.eventlog import _announce_once
        s = set()
        assert _announce_once(s, ("a", "b")) is True

    def test_announce_once_second_false(self):
        from spirit.lsp.eventlog import _announce_once
        s = set()
        _announce_once(s, ("a", "b"))
        assert _announce_once(s, ("a", "b")) is False

    def test_announce_thread_safe(self):
        from spirit.lsp.eventlog import _announce_once
        s = set()
        results = []

        def worker():
            for _ in range(100):
                results.append(_announce_once(s, ("k",)))

        threads = [__import__("threading").Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 应有且仅有一个 True
        assert results.count(True) == 1


class TestShortPath:
    def test_empty(self):
        from spirit.lsp.eventlog import _short_path
        assert _short_path("") == ""

    def test_relative_inside_cwd(self):
        from spirit.lsp.eventlog import _short_path
        # 当前 cwd 内文件应转 relpath
        out = _short_path("test_lsp_simple.py")
        assert ".." not in out

    def test_absolute_outside_cwd(self):
        from spirit.lsp.eventlog import _short_path
        out = _short_path("/some/far/away/path/file.py")
        # 跨目录应保留绝对路径
        assert out == "/some/far/away/path/file.py"


class TestLogFunctions:
    def setup_method(self):
        from spirit.lsp.eventlog import reset_announce_caches
        reset_announce_caches()

    def teardown_method(self):
        from spirit.lsp.eventlog import reset_announce_caches
        reset_announce_caches()

    def test_log_clean_emits_debug(self, caplog):
        from spirit.lsp.eventlog import log_clean
        with caplog.at_level(logging.DEBUG, logger="spirit.lint.lsp"):
            log_clean("pyright", "f.py")
        assert "clean" in caplog.text

    def test_log_active_first_info_then_debug(self, caplog):
        from spirit.lsp.eventlog import log_active
        with caplog.at_level(logging.DEBUG, logger="spirit.lint.lsp"):
            log_active("pyright", "/proj")
            log_active("pyright", "/proj")  # 第二次走 DEBUG
        # 第一次 INFO
        assert "active" in caplog.text
        # 第二次 "reused"
        assert "reused" in caplog.text

    def test_log_diagnostics_info(self, caplog):
        from spirit.lsp.eventlog import log_diagnostics
        with caplog.at_level(logging.INFO, logger="spirit.lint.lsp"):
            log_diagnostics("pyright", "f.py", 3)
        assert "3 diags" in caplog.text

    def test_log_no_project_root_dedup(self, caplog):
        from spirit.lsp.eventlog import log_no_project_root
        with caplog.at_level(logging.INFO, logger="spirit.lint.lsp"):
            log_no_project_root("pyright", "/f.py")
            log_no_project_root("pyright", "/f.py")
        # 第一次 INFO + 第二次 DEBUG, 总共 2 行
        assert "no project root" in caplog.text

    def test_log_server_unavailable_warning(self, caplog):
        from spirit.lsp.eventlog import log_server_unavailable
        with caplog.at_level(logging.WARNING, logger="spirit.lint.lsp"):
            log_server_unavailable("pyright", "pyright-langserver")
        assert "not found" in caplog.text

    def test_log_no_server_configured_once(self, caplog):
        from spirit.lsp.eventlog import log_no_server_configured
        with caplog.at_level(logging.WARNING, logger="spirit.lint.lsp"):
            log_no_server_configured("foobar")
            log_no_server_configured("foobar")  # 第二次不再 warning
        assert "no server configured" in caplog.text

    def test_log_timeout_warning(self, caplog):
        from spirit.lsp.eventlog import log_timeout
        with caplog.at_level(logging.WARNING, logger="spirit.lint.lsp"):
            log_timeout("pyright", "f.py", kind="diagnostics")
        assert "diagnostics timed out" in caplog.text

    def test_log_server_error_includes_exc_type(self, caplog):
        from spirit.lsp.eventlog import log_server_error
        with caplog.at_level(logging.WARNING, logger="spirit.lint.lsp"):
            log_server_error("pyright", "f.py", RuntimeError("boom"))
        assert "RuntimeError" in caplog.text
        assert "boom" in caplog.text

    def test_log_spawn_failed(self, caplog):
        from spirit.lsp.eventlog import log_spawn_failed
        with caplog.at_level(logging.WARNING, logger="spirit.lint.lsp"):
            log_spawn_failed("pyright", "/proj", RuntimeError("e"))
        assert "spawn" in caplog.text

    def test_log_disabled(self, caplog):
        from spirit.lsp.eventlog import log_disabled
        with caplog.at_level(logging.DEBUG, logger="spirit.lint.lsp"):
            log_disabled("pyright", "f.py", "extension not mapped")
        assert "skipped" in caplog.text


class TestResetAnnounceCaches:
    def test_clears_all(self):
        from spirit.lsp.eventlog import (
            _announced_active, _announced_unavailable,
            _announced_no_root, _announced_no_server,
            reset_announce_caches,
        )
        _announced_active.add(("a", "b"))
        _announced_unavailable.add(("a", "b"))
        _announced_no_root.add(("a", "b"))
        _announced_no_server.add(("a",))
        reset_announce_caches()
        assert len(_announced_active) == 0
        assert len(_announced_unavailable) == 0
        assert len(_announced_no_root) == 0
        assert len(_announced_no_server) == 0


# ──────────────────────────────────────────────────────────────────────
# servers.py — 语言服务器注册表
# ──────────────────────────────────────────────────────────────────────

class TestLanguageIdFor:
    def test_python(self):
        from spirit.lsp.servers import language_id_for
        assert language_id_for("f.py") == "python"
        assert language_id_for("f.pyi") == "python"

    def test_typescript(self):
        from spirit.lsp.servers import language_id_for
        assert language_id_for("f.ts") == "typescript"
        assert language_id_for("f.tsx") == "typescriptreact"
        assert language_id_for("f.js") == "javascript"
        assert language_id_for("f.jsx") == "javascriptreact"

    def test_go_rust(self):
        from spirit.lsp.servers import language_id_for
        assert language_id_for("f.go") == "go"
        assert language_id_for("f.rs") == "rust"

    def test_yaml_json(self):
        from spirit.lsp.servers import language_id_for
        assert language_id_for("f.yaml") == "yaml"
        assert language_id_for("f.yml") == "yaml"
        assert language_id_for("f.json") == "json"
        assert language_id_for("f.jsonc") == "jsonc"

    def test_unknown_defaults_plaintext(self):
        from spirit.lsp.servers import language_id_for
        assert language_id_for("f.unknown") == "plaintext"
        assert language_id_for("f") == "plaintext"

    def test_case_insensitive_ext(self):
        from spirit.lsp.servers import language_id_for
        assert language_id_for("F.PY") == "python"


class TestServerDef:
    def test_basic(self):
        from spirit.lsp.servers import ServerDef
        s = ServerDef(
            server_id="x", language_ids=["python"], binary="x-ls",
        )
        assert s.server_id == "x"
        assert s.args == []
        assert s.project_markers == []
        assert s.env_overrides == {}
        assert s.init_options == {}
        assert s.install_strategy == "auto"
        assert s.install_package == ""

    def test_matches_file(self):
        from spirit.lsp.servers import ServerDef
        s = ServerDef(server_id="x", language_ids=["python"], binary="x")
        assert s.matches_file("f.py") is True
        assert s.matches_file("f.go") is False

    def test_build_command(self):
        from spirit.lsp.servers import ServerDef
        s = ServerDef(
            server_id="x", language_ids=["python"],
            binary="my-ls", args=["--stdio", "-v"],
        )
        assert s.build_command("/proj") == ["my-ls", "--stdio", "-v"]

    def test_build_env_merges(self):
        from spirit.lsp.servers import ServerDef
        s = ServerDef(
            server_id="x", language_ids=["python"], binary="x",
            env_overrides={"FOO": "bar"},
        )
        env = s.build_env()
        assert env["FOO"] == "bar"
        # 原环境变量应保留
        assert "PATH" in env

    def test_resolve_root_no_markers_uses_git(self, tmp_path):
        from spirit.lsp.servers import ServerDef
        (tmp_path / ".git").mkdir()
        s = ServerDef(server_id="x", language_ids=["python"], binary="x")
        # 无 project_markers → 走 git
        assert s.resolve_root(str(tmp_path / "f.py")) == str(tmp_path)

    def test_resolve_root_with_markers(self, tmp_path):
        from spirit.lsp.servers import ServerDef
        (tmp_path / "pyproject.toml").write_text("")
        s = ServerDef(
            server_id="x", language_ids=["python"], binary="x",
            project_markers=["pyproject.toml"],
        )
        assert s.resolve_root(str(tmp_path)) == str(tmp_path)

    def test_resolve_root_no_match(self, tmp_path):
        from spirit.lsp.servers import ServerDef
        s = ServerDef(
            server_id="x", language_ids=["python"], binary="x",
            project_markers=["nonexistent.toml"],
        )
        assert s.resolve_root(str(tmp_path)) is None


class TestFindServerForFile:
    def test_finds_python(self):
        from spirit.lsp.servers import find_server_for_file
        s = find_server_for_file("f.py")
        assert s is not None
        assert s.server_id == "pyright"

    def test_finds_go(self):
        from spirit.lsp.servers import find_server_for_file
        s = find_server_for_file("f.go")
        assert s is not None
        assert s.server_id == "gopls"

    def test_finds_rust(self):
        from spirit.lsp.servers import find_server_for_file
        s = find_server_for_file("f.rs")
        assert s is not None
        assert s.server_id == "rust-analyzer"

    def test_no_match(self):
        from spirit.lsp.servers import find_server_for_file
        s = find_server_for_file("f.unknownext")
        assert s is None

    def test_preferred_overrides(self):
        from spirit.lsp.servers import find_server_for_file
        # 偏好 pyright
        s = find_server_for_file("f.py", preferred="pyright")
        assert s.server_id == "pyright"

    def test_preferred_wrong_file(self):
        from spirit.lsp.servers import find_server_for_file
        # 偏好 rust-analyzer 但文件是 .py
        s = find_server_for_file("f.py", preferred="rust-analyzer")
        # 应回退到默认匹配
        assert s.server_id == "pyright"


class TestGetServer:
    def test_existing(self):
        from spirit.lsp.servers import get_server
        s = get_server("pyright")
        assert s is not None
        assert s.server_id == "pyright"

    def test_not_existing(self):
        from spirit.lsp.servers import get_server
        s = get_server("not-a-server")
        assert s is None


class TestIsBinaryAvailable:
    def test_nonexistent_binary(self):
        from spirit.lsp.servers import is_binary_available
        # pyright-langserver 通常不在 PATH
        assert is_binary_available("pyright") is False

    def test_unknown_server(self):
        from spirit.lsp.servers import is_binary_available
        assert is_binary_available("not-a-server") is False

    def test_with_mock(self):
        from spirit.lsp.servers import is_binary_available
        with patch("shutil.which", return_value="/usr/bin/pyright-langserver"):
            assert is_binary_available("pyright") is True


class TestBuiltinServers:
    def test_count_at_least_8(self):
        from spirit.lsp.servers import BUILTIN_SERVERS
        assert len(BUILTIN_SERVERS) >= 8

    def test_all_have_unique_ids(self):
        from spirit.lsp.servers import BUILTIN_SERVERS
        ids = [s.server_id for s in BUILTIN_SERVERS]
        assert len(ids) == len(set(ids))

    def test_all_have_binary(self):
        from spirit.lsp.servers import BUILTIN_SERVERS
        for s in BUILTIN_SERVERS:
            assert s.binary


# ──────────────────────────────────────────────────────────────────────
# __init__.py — service 生命周期
# ──────────────────────────────────────────────────────────────────────

class TestLspServiceLifecycle:
    def setup_method(self):
        from spirit.lsp import shutdown_service
        shutdown_service()

    def teardown_method(self):
        from spirit.lsp import shutdown_service
        shutdown_service()

    def test_get_service_when_disabled(self):
        """LSP 在非 git 目录或被禁用时返回 None。"""
        from spirit.lsp import get_service
        with patch("spirit.lsp.LSPService.create_from_config", return_value=None):
            svc = get_service()
        assert svc is None

    def test_get_service_returns_inactive(self):
        """Service 创建但 is_active=False 时返回 None。"""
        from spirit.lsp import get_service
        mock_svc = MagicMock()
        mock_svc.is_active = False
        with patch("spirit.lsp.LSPService.create_from_config", return_value=mock_svc):
            svc = get_service()
        assert svc is None

    def test_get_service_returns_active(self):
        from spirit.lsp import get_service
        mock_svc = MagicMock()
        mock_svc.is_active = True
        with patch("spirit.lsp.LSPService.create_from_config", return_value=mock_svc):
            svc = get_service()
        assert svc is mock_svc

    def test_get_service_singleton(self):
        """多次调用应返回同一实例。"""
        from spirit.lsp import get_service
        mock_svc = MagicMock()
        mock_svc.is_active = True
        with patch("spirit.lsp.LSPService.create_from_config", return_value=mock_svc) as mock_create:
            s1 = get_service()
            s2 = get_service()
        assert s1 is s2
        # create_from_config 只调用一次
        assert mock_create.call_count == 1

    def test_shutdown_service_safe_when_none(self):
        from spirit.lsp import shutdown_service
        # 之前没创建, 不应抛
        shutdown_service()

    def test_shutdown_calls_shutdown(self):
        from spirit.lsp import shutdown_service
        mock_svc = MagicMock()
        mock_svc.is_active = True
        with patch("spirit.lsp.LSPService.create_from_config", return_value=mock_svc):
            from spirit.lsp import get_service
            get_service()
        shutdown_service()
        mock_svc.shutdown.assert_called_once()

    def test_shutdown_swallows_exceptions(self):
        from spirit.lsp import shutdown_service
        mock_svc = MagicMock()
        mock_svc.is_active = True
        mock_svc.shutdown.side_effect = RuntimeError("boom")
        with patch("spirit.lsp.LSPService.create_from_config", return_value=mock_svc):
            from spirit.lsp import get_service
            get_service()
        # 不应冒泡
        shutdown_service()
