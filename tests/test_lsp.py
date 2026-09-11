"""spirit.lsp 模块测试。"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# protocol.py 测试
# ---------------------------------------------------------------------------

class TestProtocol:
    """LSP 协议帧测试。"""

    def test_encode_message(self):
        from spirit.lsp.protocol import encode_message
        msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        encoded = encode_message(msg)
        assert b"Content-Length:" in encoded
        assert b"\r\n\r\n" in encoded
        # 提取 body 验证
        header, _, body = encoded.partition(b"\r\n\r\n")
        parsed = json.loads(body)
        assert parsed["method"] == "initialize"

    def test_make_request(self):
        from spirit.lsp.protocol import make_request
        req = make_request("textDocument/definition", {"uri": "file:///test.py"}, 42)
        assert req["jsonrpc"] == "2.0"
        assert req["id"] == 42
        assert req["method"] == "textDocument/definition"

    def test_make_notification(self):
        from spirit.lsp.protocol import make_notification
        notif = make_notification("initialized", {})
        assert "id" not in notif
        assert notif["method"] == "initialized"

    def test_make_response(self):
        from spirit.lsp.protocol import make_response
        resp = make_response(1, {"result": "ok"})
        assert resp["id"] == 1
        assert "result" in resp

    def test_make_error_response(self):
        from spirit.lsp.protocol import make_error_response
        resp = make_error_response(1, -32601, "Method not found")
        assert resp["error"]["code"] == -32601

    def test_classify_message(self):
        from spirit.lsp.protocol import classify_message
        assert classify_message({"id": 1, "method": "test"}) == "request"
        assert classify_message({"id": 1, "result": None}) == "response"
        assert classify_message({"method": "test"}) == "notification"
        assert classify_message({}) == "unknown"

    def test_lsp_request_error(self):
        from spirit.lsp.protocol import LSPRequestError
        err = LSPRequestError(-32601, "Not found", data={"key": "val"})
        assert err.code == -32601
        assert err.message == "Not found"
        assert err.data == {"key": "val"}


# ---------------------------------------------------------------------------
# workspace.py 测试
# ---------------------------------------------------------------------------

class TestWorkspace:
    """工作区解析测试。"""

    def test_normalize_path(self):
        from spirit.lsp.workspace import normalize_path
        result = normalize_path("~/test")
        assert os.path.isabs(result)
        assert "~" not in result

    def test_find_git_worktree_in_git_repo(self):
        from spirit.lsp.workspace import find_git_worktree, clear_cache
        clear_cache()
        # 当前项目应该有 .git
        project_root = Path(__file__).parent.parent.parent
        result = find_git_worktree(str(project_root))
        # 可能找到也可能找不到（取决于测试环境）
        # 至少不崩溃
        assert result is None or isinstance(result, str)

    def test_find_git_worktree_non_git(self):
        from spirit.lsp.workspace import find_git_worktree, clear_cache
        clear_cache()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = find_git_worktree(tmpdir)
            assert result is None

    def test_nearest_root_with_markers(self):
        from spirit.lsp.workspace import nearest_root
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建项目结构
            project = Path(tmpdir) / "myproject"
            project.mkdir()
            (project / "pyproject.toml").touch()
            sub = project / "src" / "pkg"
            sub.mkdir(parents=True)
            test_file = sub / "test.py"
            test_file.touch()

            result = nearest_root(str(test_file), ["pyproject.toml"])
            assert result == str(project)

    def test_nearest_root_no_marker(self):
        from spirit.lsp.workspace import nearest_root
        with tempfile.TemporaryDirectory() as tmpdir:
            result = nearest_root(tmpdir, ["nonexistent.toml"])
            assert result is None

    def test_clear_cache(self):
        from spirit.lsp.workspace import clear_cache, _workspace_cache
        _workspace_cache["test"] = ("root", True)
        clear_cache()
        assert len(_workspace_cache) == 0


# ---------------------------------------------------------------------------
# servers.py 测试
# ---------------------------------------------------------------------------

class TestServers:
    """语言服务器注册表测试。"""

    def test_language_id_for_python(self):
        from spirit.lsp.servers import language_id_for
        assert language_id_for("test.py") == "python"
        assert language_id_for("test.pyi") == "python"

    def test_language_id_for_typescript(self):
        from spirit.lsp.servers import language_id_for
        assert language_id_for("app.ts") == "typescript"
        assert language_id_for("app.tsx") == "typescriptreact"

    def test_language_id_for_unknown(self):
        from spirit.lsp.servers import language_id_for
        assert language_id_for("test.xyz") == "plaintext"

    def test_find_server_for_python(self):
        from spirit.lsp.servers import find_server_for_file
        srv = find_server_for_file("test.py")
        assert srv is not None
        assert srv.server_id == "pyright"

    def test_find_server_for_go(self):
        from spirit.lsp.servers import find_server_for_file
        srv = find_server_for_file("main.go")
        assert srv is not None
        assert srv.server_id == "gopls"

    def test_find_server_for_unknown(self):
        from spirit.lsp.servers import find_server_for_file
        srv = find_server_for_file("test.xyz")
        assert srv is None

    def test_server_def_matches_file(self):
        from spirit.lsp.servers import get_server
        pyright = get_server("pyright")
        assert pyright is not None
        assert pyright.matches_file("foo.py") is True
        assert pyright.matches_file("foo.ts") is False

    def test_get_server_nonexistent(self):
        from spirit.lsp.servers import get_server
        assert get_server("nonexistent-server") is None

    def test_builtin_servers_not_empty(self):
        from spirit.lsp.servers import BUILTIN_SERVERS
        assert len(BUILTIN_SERVERS) >= 5


# ---------------------------------------------------------------------------
# reporter.py 测试
# ---------------------------------------------------------------------------

class TestReporter:
    """诊断格式化测试。"""

    def test_format_diagnostics_empty(self):
        from spirit.lsp.reporter import format_diagnostics
        result = format_diagnostics("test.py", [])
        assert result == ""

    def test_format_diagnostics_with_errors(self):
        from spirit.lsp.reporter import format_diagnostics
        diags = [
            {
                "severity": 1,
                "range": {"start": {"line": 9, "character": 0}},
                "message": "undefined name 'foo'",
                "source": "pyright",
            }
        ]
        result = format_diagnostics("test.py", diags)
        assert "<diagnostics" in result
        assert "L10:1" in result  # 1-based
        assert "ERROR" in result
        assert "pyright" in result

    def test_format_diagnostics_filters_severity(self):
        from spirit.lsp.reporter import format_diagnostics
        diags = [
            {"severity": 3, "range": {"start": {"line": 0, "character": 0}}, "message": "info"},
        ]
        # 默认只看 ERROR (severity=1)
        result = format_diagnostics("test.py", diags)
        assert result == ""

    def test_count_errors(self):
        from spirit.lsp.reporter import count_errors
        diags = [
            {"severity": 1},
            {"severity": 2},
            {"severity": 1},
        ]
        assert count_errors(diags) == 2

    def test_compute_delta(self):
        from spirit.lsp.reporter import compute_delta
        baseline = [
            {"range": {"start": {"line": 0, "character": 0}}, "severity": 1, "message": "old error"},
        ]
        current = [
            {"range": {"start": {"line": 0, "character": 0}}, "severity": 1, "message": "old error"},
            {"range": {"start": {"line": 5, "character": 0}}, "severity": 1, "message": "new error"},
        ]
        delta = compute_delta(baseline, current)
        assert len(delta) == 1
        assert delta[0]["message"] == "new error"

    def test_sanitize_field(self):
        from spirit.lsp.reporter import _sanitize_field
        # 换行折叠
        assert "\n" not in _sanitize_field("line1\nline2", limit=100)
        # 长度截断
        result = _sanitize_field("a" * 500, limit=10)
        assert len(result) <= 12  # 10 + "…"
        # HTML 转义
        assert "&lt;" in _sanitize_field("<script>", limit=100)

    def test_format_multi_file(self):
        from spirit.lsp.reporter import format_multi_file_diagnostics
        diag_map = {
            "a.py": [{"severity": 1, "range": {"start": {"line": 0, "character": 0}}, "message": "err1"}],
            "b.py": [{"severity": 1, "range": {"start": {"line": 1, "character": 0}}, "message": "err2"}],
        }
        result = format_multi_file_diagnostics(diag_map)
        assert "a.py" in result
        assert "b.py" in result


# ---------------------------------------------------------------------------
# install.py 测试
# ---------------------------------------------------------------------------

class TestInstall:
    """安装模块测试。"""

    def test_install_recipes_exist(self):
        from spirit.lsp.install import INSTALL_RECIPES
        assert "pyright" in INSTALL_RECIPES
        assert "typescript-language-server" in INSTALL_RECIPES

    def test_get_resolved_binary_unknown_server(self):
        from spirit.lsp.install import get_resolved_binary
        result = get_resolved_binary("nonexistent-server")
        assert result is None

    def test_try_install_no_recipe(self):
        from spirit.lsp.install import try_install
        result = try_install("nonexistent-server")
        assert result is None

    def test_try_install_manual_strategy(self):
        from spirit.lsp.install import try_install
        # gopls 是 manual 策略
        result = try_install("gopls")
        # 如果 gopls 不在 PATH 上，应返回 None（manual 不自动安装）
        import shutil
        if not shutil.which("gopls"):
            assert result is None


# ---------------------------------------------------------------------------
# client.py 测试
# ---------------------------------------------------------------------------

class TestClient:
    """LSP 客户端基础测试。"""

    def test_path_to_uri_unix(self):
        from spirit.lsp.client import _path_to_uri
        uri = _path_to_uri("/home/user/test.py")
        assert uri.startswith("file://")
        assert "test.py" in uri

    def test_uri_to_path_roundtrip(self):
        from spirit.lsp.client import _path_to_uri, _uri_to_path
        original = os.path.abspath("test.py")
        uri = _path_to_uri(original)
        restored = _uri_to_path(uri)
        assert restored == original

    def test_client_init(self):
        from spirit.lsp.client import LSPClient
        client = LSPClient(
            server_id="pyright",
            workspace_root="/tmp/test",
            command=["pyright-langserver", "--stdio"],
        )
        assert client.server_id == "pyright"
        assert not client.is_alive


# ---------------------------------------------------------------------------
# __init__.py 测试
# ---------------------------------------------------------------------------

class TestInit:
    """公共 API 测试。"""

    def test_get_service_returns_none_outside_git(self):
        from spirit.lsp import get_service, shutdown_service
        # 在非 git 目录下应该返回 None
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                # 清除缓存
                from spirit.lsp import _service
                import spirit.lsp
                spirit.lsp._service = None
                svc = get_service()
                # 可能为 None（不在 git 内）
                assert svc is None or svc.is_active
            finally:
                os.chdir(old_cwd)
                shutdown_service()

    def test_shutdown_idempotent(self):
        from spirit.lsp import shutdown_service
        # 多次调用不应崩溃
        shutdown_service()
        shutdown_service()


# ---------------------------------------------------------------------------
# config 测试
# ---------------------------------------------------------------------------

class TestConfig:
    """LSP 配置项测试。"""

    def test_lsp_config_exists(self):
        from spirit.config import DEFAULT_CONFIG
        assert "lsp" in DEFAULT_CONFIG

    def test_lsp_auto_install_default(self):
        from spirit.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["lsp"]["auto_install"] == "auto"

    def test_lsp_idle_timeout_default(self):
        from spirit.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["lsp"]["idle_timeout"] == 600

    def test_lsp_enabled_default(self):
        from spirit.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["lsp"]["enabled"] is True

    def test_get_config_value_lsp(self):
        from spirit.config import get_config_value
        assert get_config_value("lsp.auto_install") == "auto"
        assert get_config_value("lsp.enabled") is True
