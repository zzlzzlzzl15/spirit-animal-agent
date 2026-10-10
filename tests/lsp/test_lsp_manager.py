"""spirit/lsp/manager.py 回归测试。

LSPService + _BackgroundLoop（同步/异步桥接层）。
client.py / install.py 通过 mock 隔离。
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ──────────────────────────────────────────────────────────────────────
# _BackgroundLoop
# ──────────────────────────────────────────────────────────────────────

class TestBackgroundLoop:
    def test_init(self):
        from spirit.lsp.manager import _BackgroundLoop
        loop = _BackgroundLoop()
        assert loop._loop is None
        assert loop._thread is None
        assert loop._ready.is_set() is False

    def test_start_creates_thread(self):
        from spirit.lsp.manager import _BackgroundLoop
        loop = _BackgroundLoop()
        try:
            loop.start()
            assert loop._thread is not None
            assert loop._thread.daemon is True
            assert loop._thread.name == "spirit-lsp-loop"
        finally:
            loop.stop()

    def test_start_idempotent(self):
        """第二次 start 不会重启线程。"""
        from spirit.lsp.manager import _BackgroundLoop
        loop = _BackgroundLoop()
        try:
            loop.start()
            first_thread = loop._thread
            loop.start()  # 第二次
            assert loop._thread is first_thread
        finally:
            loop.stop()

    def test_run_simple_coro(self):
        from spirit.lsp.manager import _BackgroundLoop
        loop = _BackgroundLoop()
        try:
            loop.start()
            result = loop.run(asyncio.sleep(0, result=42), timeout=2.0)
            assert result == 42
        finally:
            loop.stop()

    def test_run_without_start_raises(self):
        from spirit.lsp.manager import _BackgroundLoop
        loop = _BackgroundLoop()
        with pytest.raises(RuntimeError, match="未启动"):
            loop.run(asyncio.sleep(0, result=1))

    def test_stop_joins_thread(self):
        from spirit.lsp.manager import _BackgroundLoop
        loop = _BackgroundLoop()
        loop.start()
        loop.stop()
        # 线程已 join
        assert loop._thread is None


# ──────────────────────────────────────────────────────────────────────
# LSPService.__init__
# ──────────────────────────────────────────────────────────────────────

class TestLSPServiceInit:
    def test_defaults(self):
        from spirit.lsp.manager import LSPService
        svc = LSPService()
        assert svc._auto_install == "auto"
        assert svc._clients == {}
        assert svc._broken == set()
        assert svc._baselines == {}
        assert svc._active is False
        assert svc._loop is not None

    def test_custom_auto_install(self):
        from spirit.lsp.manager import LSPService
        svc = LSPService(auto_install="manual")
        assert svc._auto_install == "manual"

    def test_is_active_property(self):
        from spirit.lsp.manager import LSPService
        svc = LSPService()
        assert svc.is_active is False
        svc._active = True
        assert svc.is_active is True


# ──────────────────────────────────────────────────────────────────────
# create_from_config
# ──────────────────────────────────────────────────────────────────────

class TestCreateFromConfig:
    def test_not_in_git_returns_none(self):
        from spirit.lsp.manager import LSPService
        with patch("spirit.lsp.manager.find_git_worktree", return_value=None):
            with patch("spirit.config.get_config_value", return_value="auto"):
                svc = LSPService.create_from_config()
        assert svc is None

    def test_in_git_creates_active_service(self):
        from spirit.lsp.manager import LSPService
        with patch("spirit.lsp.manager.find_git_worktree", return_value="/repo"):
            with patch("spirit.config.get_config_value", return_value="auto"):
                svc = LSPService.create_from_config()
        try:
            assert svc is not None
            assert svc.is_active is True
            assert svc._auto_install == "auto"
        finally:
            svc.shutdown()


# ──────────────────────────────────────────────────────────────────────
# _get_or_create_client
# ──────────────────────────────────────────────────────────────────────

class TestGetOrCreateClient:
    def _make_active(self):
        from spirit.lsp.manager import LSPService
        svc = LSPService()
        svc._active = True
        return svc

    def _isolated_repo(self, tmp_path):
        """在 tmp_path 建一个独立 git repo, 避免干扰当前 cwd 的 pyproject.toml。"""
        import subprocess
        (tmp_path / ".git").mkdir()
        return str(tmp_path)

    def test_no_server_for_file(self):
        """无法识别的扩展名 → None。"""
        svc = self._make_active()
        client = svc._get_or_create_client("f.unknownext")
        assert client is None

    def test_broken_key_returns_none(self):
        """已损坏的 key 不再尝试。"""
        svc = self._make_active()
        from spirit.lsp.servers import find_server_for_file
        server = find_server_for_file("f.py")
        if not server:
            pytest.skip("no python server in builtin list")
        fake_root = "/fake/repo"
        svc._broken.add((server.server_id, fake_root))
        with patch("spirit.lsp.servers.find_server_for_file", return_value=server), \
             patch.object(server, "resolve_root", return_value=fake_root):
            client = svc._get_or_create_client("f.py")
        assert client is None

    def test_reuse_existing_alive_client(self):
        svc = self._make_active()
        from spirit.lsp.servers import find_server_for_file
        server = find_server_for_file("f.py")
        if not server:
            pytest.skip("no python server")
        mock_client = MagicMock()
        mock_client.is_alive = True
        with patch("spirit.lsp.servers.find_server_for_file", return_value=server), \
             patch.object(server, "resolve_root", return_value="/repo"):
            key = (server.server_id, "/repo")
            svc._clients[key] = mock_client
            result = svc._get_or_create_client("f.py")
        assert result is mock_client
        # _last_access 被更新
        assert key in svc._last_access

    def test_dead_client_is_replaced(self):
        """已存在的 client 不 alive → 删掉, 重新创建路径。"""
        svc = self._make_active()
        from spirit.lsp.servers import find_server_for_file
        server = find_server_for_file("f.py")
        if not server:
            pytest.skip("no python server")
        mock_dead = MagicMock()
        mock_dead.is_alive = False
        key = (server.server_id, "/repo")
        svc._clients[key] = mock_dead

        with patch("spirit.lsp.servers.find_server_for_file", return_value=server), \
             patch.object(server, "resolve_root", return_value="/repo"), \
             patch("spirit.lsp.manager.get_resolved_binary", return_value=None), \
             patch("spirit.lsp.manager.try_install", return_value=None):
            result = svc._get_or_create_client("f.py")
        # 没有 binary → 标记 broken, 返回 None
        assert result is None
        assert key in svc._broken
        # 死 client 被删除
        assert key not in svc._clients

    def test_no_binary_no_install_marks_broken(self):
        svc = self._make_active()
        from spirit.lsp.servers import find_server_for_file
        server = find_server_for_file("f.py")
        if not server:
            pytest.skip("no python server")

        with patch("spirit.lsp.servers.find_server_for_file", return_value=server), \
             patch.object(server, "resolve_root", return_value="/repo"), \
             patch("spirit.lsp.manager.get_resolved_binary", return_value=None), \
             patch("spirit.lsp.manager.try_install", return_value=None):
            result = svc._get_or_create_client("f.py")
        assert result is None
        assert (server.server_id, "/repo") in svc._broken

    def test_auto_install_strategy_triggers_install(self):
        svc = self._make_active()
        from spirit.lsp.servers import find_server_for_file
        server = find_server_for_file("f.py")
        if not server:
            pytest.skip("no python server")
        svc._auto_install = "auto"

        with patch("spirit.lsp.servers.find_server_for_file", return_value=server), \
             patch.object(server, "resolve_root", return_value="/repo"), \
             patch("spirit.lsp.manager.get_resolved_binary", side_effect=[None, "/installed/bin"]), \
             patch("spirit.lsp.manager.try_install", return_value="/installed/bin"), \
             patch("spirit.lsp.manager.LSPClient") as mock_client_cls, \
             patch.object(svc._loop, "run", return_value=True):
            result = svc._get_or_create_client("f.py")
        assert result is not None
        assert mock_client_cls.called

    def test_initialize_failure_marks_broken(self):
        svc = self._make_active()
        from spirit.lsp.servers import find_server_for_file
        server = find_server_for_file("f.py")
        if not server:
            pytest.skip("no python server")

        with patch("spirit.lsp.servers.find_server_for_file", return_value=server), \
             patch.object(server, "resolve_root", return_value="/repo"), \
             patch("spirit.lsp.manager.get_resolved_binary", return_value="/bin"), \
             patch("spirit.lsp.manager.LSPClient") as mock_client_cls, \
             patch.object(svc._loop, "run", return_value=False):
            result = svc._get_or_create_client("f.py")
        assert result is None
        assert (server.server_id, "/repo") in svc._broken

    def test_initialize_exception_marks_broken(self):
        svc = self._make_active()
        from spirit.lsp.servers import find_server_for_file
        server = find_server_for_file("f.py")
        if not server:
            pytest.skip("no python server")

        with patch("spirit.lsp.servers.find_server_for_file", return_value=server), \
             patch.object(server, "resolve_root", return_value="/repo"), \
             patch("spirit.lsp.manager.get_resolved_binary", return_value="/bin"), \
             patch("spirit.lsp.manager.LSPClient") as mock_client_cls, \
             patch.object(svc._loop, "run", side_effect=RuntimeError("boom")):
            result = svc._get_or_create_client("f.py")
        assert result is None
        assert (server.server_id, "/repo") in svc._broken

    def test_no_workspace_root_returns_none(self):
        """project_markers 和 git 都找不到 → None。"""
        svc = self._make_active()
        from spirit.lsp.servers import find_server_for_file
        server = find_server_for_file("f.py")
        if not server:
            pytest.skip("no python server")

        with patch("spirit.lsp.servers.find_server_for_file", return_value=server), \
             patch("spirit.lsp.manager.find_git_worktree", return_value=None), \
             patch.object(server, "resolve_root", return_value=None):
            result = svc._get_or_create_client("f.py")
        assert result is None


# ──────────────────────────────────────────────────────────────────────
# enabled_for / touch_file / open_file
# ──────────────────────────────────────────────────────────────────────

class TestPublicApiInactive:
    """_active=False 时所有公开方法都是 no-op。"""

    def _make(self):
        from spirit.lsp.manager import LSPService
        return LSPService()

    def test_enabled_for_false(self):
        svc = self._make()
        assert svc.enabled_for("f.py") is False

    def test_touch_file_noop(self):
        svc = self._make()
        svc.touch_file("f.py")  # 不抛

    def test_open_file_noop(self):
        svc = self._make()
        svc.open_file("f.py")

    def test_snapshot_baseline_noop(self):
        svc = self._make()
        svc.snapshot_baseline("f.py")

    def test_get_diagnostics_sync_empty(self):
        svc = self._make()
        assert svc.get_diagnostics_sync("f.py") == []

    def test_get_diagnostics_formatted_empty(self):
        svc = self._make()
        assert svc.get_diagnostics_formatted("f.py") == ""

    def test_go_to_definition_none(self):
        svc = self._make()
        assert svc.go_to_definition("f.py", 0, 0) is None

    def test_find_references_none(self):
        svc = self._make()
        assert svc.find_references("f.py", 0, 0) is None

    def test_hover_none(self):
        svc = self._make()
        assert svc.hover("f.py", 0, 0) is None

    def test_document_symbols_empty(self):
        svc = self._make()
        assert svc.document_symbols("f.py") == []

    def test_workspace_symbols_empty(self):
        svc = self._make()
        assert svc.workspace_symbols("query") == []


class TestPublicApiActive:
    """_active=True 但无 client → 各种边界。"""

    def _make(self):
        from spirit.lsp.manager import LSPService
        svc = LSPService()
        svc._active = True
        return svc

    def test_enabled_for_no_client(self):
        svc = self._make()
        with patch.object(svc, "_get_or_create_client", return_value=None):
            assert svc.enabled_for("f.unknown") is False

    def test_touch_file_swallows_exception(self):
        svc = self._make()
        mock_client = MagicMock()
        with patch.object(svc, "_get_or_create_client", return_value=mock_client), \
             patch.object(svc._loop, "run", side_effect=RuntimeError("boom")):
            svc.touch_file("f.py")  # 不应抛

    def test_open_file_swallows_exception(self):
        svc = self._make()
        mock_client = MagicMock()
        with patch.object(svc, "_get_or_create_client", return_value=mock_client), \
             patch.object(svc._loop, "run", side_effect=RuntimeError("boom")):
            svc.open_file("f.py")

    def test_snapshot_baseline_swallows_exception(self):
        svc = self._make()
        mock_client = MagicMock()
        with patch.object(svc, "_get_or_create_client", return_value=mock_client), \
             patch.object(svc._loop, "run", side_effect=RuntimeError("boom")):
            svc.snapshot_baseline("f.py")
        # baselines 仍为空
        assert svc._baselines == {}

    def test_snapshot_baseline_stores_diagnostics(self):
        svc = self._make()
        mock_client = MagicMock()
        from spirit.lsp.client import _path_to_uri
        uri = _path_to_uri("f.py")
        with patch.object(svc, "_get_or_create_client", return_value=mock_client), \
             patch.object(svc._loop, "run", return_value=[{"severity": 1, "message": "e"}]):
            svc.snapshot_baseline("f.py")
        assert uri in svc._baselines
        assert svc._baselines[uri] == [{"severity": 1, "message": "e"}]

    def test_get_diagnostics_sync_no_client_empty(self):
        svc = self._make()
        with patch.object(svc, "_get_or_create_client", return_value=None):
            assert svc.get_diagnostics_sync("f.py") == []

    def test_get_diagnostics_sync_uses_delta(self):
        """应使用 compute_delta 比较 baseline vs current。"""
        svc = self._make()
        mock_client = MagicMock()
        from spirit.lsp.client import _path_to_uri
        uri = _path_to_uri("f.py")
        # 基线已有 1 条, 当前有 2 条
        svc._baselines[uri] = [
            {"range": {"start": {"line": 0, "character": 0}}, "severity": 1, "message": "old"},
        ]
        current = svc._baselines[uri] + [
            {"range": {"start": {"line": 1, "character": 0}}, "severity": 1, "message": "new"},
        ]
        with patch.object(svc, "_get_or_create_client", return_value=mock_client), \
             patch.object(svc._loop, "run", return_value=current):
            delta = svc.get_diagnostics_sync("f.py")
        assert len(delta) == 1
        assert delta[0]["message"] == "new"

    def test_get_diagnostics_sync_exception_empty(self):
        svc = self._make()
        mock_client = MagicMock()
        with patch.object(svc, "_get_or_create_client", return_value=mock_client), \
             patch.object(svc._loop, "run", side_effect=RuntimeError("boom")):
            assert svc.get_diagnostics_sync("f.py") == []

    def test_get_diagnostics_formatted_uses_delta(self):
        svc = self._make()
        mock_client = MagicMock()
        from spirit.lsp.client import _path_to_uri
        uri = _path_to_uri("f.py")
        svc._baselines[uri] = []
        new_diag = {
            "range": {"start": {"line": 0, "character": 0}},
            "severity": 1, "message": "e",
        }
        with patch.object(svc, "_get_or_create_client", return_value=mock_client), \
             patch.object(svc._loop, "run", return_value=[new_diag]):
            out = svc.get_diagnostics_formatted("f.py")
        assert "ERROR" in out
        assert "e" in out

    def test_get_diagnostics_formatted_empty_returns_empty(self):
        svc = self._make()
        mock_client = MagicMock()
        with patch.object(svc, "_get_or_create_client", return_value=mock_client), \
             patch.object(svc._loop, "run", return_value=[]):
            assert svc.get_diagnostics_formatted("f.py") == ""

    def test_go_to_definition_swallows_exception(self):
        svc = self._make()
        mock_client = MagicMock()
        with patch.object(svc, "_get_or_create_client", return_value=mock_client), \
             patch.object(svc._loop, "run", side_effect=RuntimeError("boom")):
            assert svc.go_to_definition("f.py", 0, 0) is None

    def test_find_references_swallows_exception(self):
        svc = self._make()
        mock_client = MagicMock()
        with patch.object(svc, "_get_or_create_client", return_value=mock_client), \
             patch.object(svc._loop, "run", side_effect=RuntimeError("boom")):
            assert svc.find_references("f.py", 0, 0) is None

    def test_hover_swallows_exception(self):
        svc = self._make()
        mock_client = MagicMock()
        with patch.object(svc, "_get_or_create_client", return_value=mock_client), \
             patch.object(svc._loop, "run", side_effect=RuntimeError("boom")):
            assert svc.hover("f.py", 0, 0) is None

    def test_document_symbols_swallows_exception(self):
        svc = self._make()
        mock_client = MagicMock()
        with patch.object(svc, "_get_or_create_client", return_value=mock_client), \
             patch.object(svc._loop, "run", side_effect=RuntimeError("boom")):
            assert svc.document_symbols("f.py") == []

    def test_workspace_symbols_no_client(self):
        """无 client, 无 _clients → 返回 []。"""
        svc = self._make()
        assert svc.workspace_symbols("query") == []

    def test_workspace_symbols_fallback_to_any_client(self):
        """无 file_path 但有 _clients → 用第一个。"""
        svc = self._make()
        mock_client = MagicMock()
        svc._clients[("pyright", "/repo")] = mock_client
        with patch.object(svc._loop, "run", return_value=[{"name": "x"}]):
            result = svc.workspace_symbols("query")
        assert result == [{"name": "x"}]

    def test_workspace_symbols_swallows_exception(self):
        svc = self._make()
        mock_client = MagicMock()
        svc._clients[("pyright", "/repo")] = mock_client
        with patch.object(svc._loop, "run", side_effect=RuntimeError("boom")):
            assert svc.workspace_symbols("query") == []


# ──────────────────────────────────────────────────────────────────────
# reap_idle + shutdown
# ──────────────────────────────────────────────────────────────────────

class TestReapIdle:
    def test_no_clients(self):
        from spirit.lsp.manager import LSPService
        svc = LSPService()
        assert svc.reap_idle() == 0

    def test_reaps_old_clients(self):
        from spirit.lsp.manager import LSPService, DEFAULT_IDLE_TIMEOUT
        svc = LSPService()
        mock_client = MagicMock()
        # 设置一个很旧的 access time
        svc._clients[("pyright", "/repo")] = mock_client
        svc._last_access[("pyright", "/repo")] = time.time() - DEFAULT_IDLE_TIMEOUT - 100
        with patch.object(svc._loop, "run", return_value=None):
            count = svc.reap_idle()
        assert count == 1
        assert ("pyright", "/repo") not in svc._clients
        assert ("pyright", "/repo") not in svc._last_access

    def test_keeps_fresh_clients(self):
        from spirit.lsp.manager import LSPService
        svc = LSPService()
        mock_client = MagicMock()
        svc._clients[("pyright", "/repo")] = mock_client
        svc._last_access[("pyright", "/repo")] = time.time()  # 刚用过
        count = svc.reap_idle()
        assert count == 0
        assert ("pyright", "/repo") in svc._clients

    def test_swallows_shutdown_exception(self):
        from spirit.lsp.manager import LSPService, DEFAULT_IDLE_TIMEOUT
        svc = LSPService()
        mock_client = MagicMock()
        svc._clients[("pyright", "/repo")] = mock_client
        svc._last_access[("pyright", "/repo")] = time.time() - DEFAULT_IDLE_TIMEOUT - 100
        with patch.object(svc._loop, "run", side_effect=RuntimeError("boom")):
            count = svc.reap_idle()
        assert count == 1  # 已从 _clients 删除


class TestShutdown:
    def test_sets_inactive(self):
        from spirit.lsp.manager import LSPService
        svc = LSPService()
        svc._active = True
        with patch.object(svc._loop, "stop"):
            svc.shutdown()
        assert svc._active is False

    def test_clears_clients(self):
        from spirit.lsp.manager import LSPService
        svc = LSPService()
        svc._active = True
        mock_client = MagicMock()
        svc._clients[("pyright", "/repo")] = mock_client
        svc._baselines["uri"] = [{"x": 1}]
        svc._last_access[("pyright", "/repo")] = time.time()
        with patch.object(svc._loop, "run", return_value=None), \
             patch.object(svc._loop, "stop"):
            svc.shutdown()
        assert svc._clients == {}
        assert svc._baselines == {}
        assert svc._last_access == {}

    def test_swallows_client_shutdown_exception(self):
        from spirit.lsp.manager import LSPService
        svc = LSPService()
        svc._active = True
        mock_client = MagicMock()
        svc._clients[("pyright", "/repo")] = mock_client
        with patch.object(svc._loop, "run", side_effect=RuntimeError("boom")), \
             patch.object(svc._loop, "stop"):
            svc.shutdown()
        # 不应抛
