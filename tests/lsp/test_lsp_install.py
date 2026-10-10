"""spirit/lsp/install.py 回归测试。

覆盖 INSTALL_RECIPES / _get_staging_dir / _get_lock / _find_npm / try_install / get_resolved_binary。
通过 monkeypatch subprocess + shutil.which + SPIRIT_HOME 隔离真实环境。
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ──────────────────────────────────────────────────────────────────────
# INSTALL_RECIPES
# ──────────────────────────────────────────────────────────────────────

class TestInstallRecipes:
    def test_recipes_is_dict(self):
        from spirit.lsp.install import INSTALL_RECIPES
        assert isinstance(INSTALL_RECIPES, dict)

    def test_has_common_servers(self):
        from spirit.lsp.install import INSTALL_RECIPES
        for srv in ["pyright", "typescript-language-server", "gopls", "rust-analyzer"]:
            assert srv in INSTALL_RECIPES, f"missing {srv}"

    def test_recipes_have_required_keys(self):
        from spirit.lsp.install import INSTALL_RECIPES
        for srv, recipe in INSTALL_RECIPES.items():
            assert "strategy" in recipe, f"{srv} missing strategy"
            assert "pkg" in recipe, f"{srv} missing pkg"
            assert "bin" in recipe, f"{srv} missing bin"

    def test_npm_strategy_servers(self):
        from spirit.lsp.install import INSTALL_RECIPES
        npm_servers = [s for s, r in INSTALL_RECIPES.items() if r["strategy"] == "npm"]
        assert len(npm_servers) >= 4
        assert "pyright" in npm_servers

    def test_manual_strategy_servers(self):
        from spirit.lsp.install import INSTALL_RECIPES
        manual = [s for s, r in INSTALL_RECIPES.items() if r["strategy"] == "manual"]
        assert "gopls" in manual
        assert "rust-analyzer" in manual

    def test_extra_pkgs_is_list_when_present(self):
        from spirit.lsp.install import INSTALL_RECIPES
        for srv, recipe in INSTALL_RECIPES.items():
            if "extra_pkgs" in recipe:
                assert isinstance(recipe["extra_pkgs"], list)


# ──────────────────────────────────────────────────────────────────────
# _get_staging_dir
# ──────────────────────────────────────────────────────────────────────

class TestGetStagingDir:
    def test_creates_lsp_bin_under_spirit_home(self, tmp_path, monkeypatch):
        from spirit.lsp import install
        # monkeypatch SPIRIT_HOME
        monkeypatch.setattr("spirit.config.SPIRIT_HOME", tmp_path)
        staging = install._get_staging_dir()
        assert staging == tmp_path / "lsp" / "bin"
        assert staging.exists()

    def test_idempotent(self, tmp_path, monkeypatch):
        from spirit.lsp import install
        monkeypatch.setattr("spirit.config.SPIRIT_HOME", tmp_path)
        s1 = install._get_staging_dir()
        s2 = install._get_staging_dir()
        assert s1 == s2


# ──────────────────────────────────────────────────────────────────────
# _get_lock
# ──────────────────────────────────────────────────────────────────────

class TestGetLock:
    def test_returns_lock(self):
        from spirit.lsp import install
        # threading.Lock 是工厂函数, 不是类, 用 duck-typing 验证
        lock = install._get_lock("pyright")
        assert hasattr(lock, "acquire")
        assert hasattr(lock, "release")
        assert hasattr(lock, "locked")
        assert lock.acquire(blocking=False) is True
        lock.release()

    def test_same_server_returns_same_lock(self):
        from spirit.lsp import install
        lock1 = install._get_lock("pyright")
        lock2 = install._get_lock("pyright")
        assert lock1 is lock2

    def test_different_servers_different_locks(self):
        from spirit.lsp import install
        lock1 = install._get_lock("pyright")
        lock2 = install._get_lock("gopls")
        assert lock1 is not lock2

    def test_thread_safe(self):
        import threading
        from spirit.lsp import install
        locks = []
        results = []

        def worker():
            for _ in range(50):
                l = install._get_lock("concurrent")
                locks.append(l)
                results.append(l is locks[0] if locks else True)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 所有 lock 应是同一对象
        assert all(l is locks[0] for l in locks)


# ──────────────────────────────────────────────────────────────────────
# _find_npm
# ──────────────────────────────────────────────────────────────────────

class TestFindNpm:
    def test_finds_npm(self):
        from spirit.lsp.install import _find_npm
        with patch("shutil.which", side_effect=lambda x: "/usr/bin/npm" if x == "npm" else None):
            assert _find_npm() == "/usr/bin/npm"

    def test_falls_back_to_npx(self):
        from spirit.lsp.install import _find_npm
        def which(cmd):
            if cmd == "npm":
                return None
            if cmd == "npx":
                return "/usr/bin/npx"
            return None
        with patch("shutil.which", side_effect=which):
            assert _find_npm() == "/usr/bin/npx"

    def test_returns_none_when_neither(self):
        from spirit.lsp.install import _find_npm
        with patch("shutil.which", return_value=None):
            assert _find_npm() is None


# ──────────────────────────────────────────────────────────────────────
# _npm_install
# ──────────────────────────────────────────────────────────────────────

class TestNpmInstall:
    def test_no_npm_returns_false(self, tmp_path, monkeypatch):
        from spirit.lsp.install import _npm_install
        monkeypatch.setattr("spirit.lsp.install._find_npm", lambda: None)
        assert _npm_install({"pkg": "x", "bin": "x"}, tmp_path) is False

    def test_successful_install(self, tmp_path, monkeypatch):
        from spirit.lsp.install import _npm_install
        # 预创建 node_modules/.bin
        bin_dir = tmp_path / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "my-ls").touch()

        # mock subprocess.run
        mock_result = MagicMock()
        mock_result.returncode = 0

        monkeypatch.setattr("spirit.lsp.install._find_npm", lambda: "/usr/bin/npm")
        with patch("subprocess.run", return_value=mock_result):
            ok = _npm_install({"pkg": "mypkg", "bin": "my-ls"}, tmp_path)
        assert ok is True

    def test_npm_returns_nonzero(self, tmp_path, monkeypatch):
        from spirit.lsp.install import _npm_install
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "some error"

        monkeypatch.setattr("spirit.lsp.install._find_npm", lambda: "/usr/bin/npm")
        with patch("subprocess.run", return_value=mock_result):
            ok = _npm_install({"pkg": "x", "bin": "x"}, tmp_path)
        assert ok is False

    def test_timeout_returns_false(self, tmp_path, monkeypatch):
        from spirit.lsp.install import _npm_install
        import subprocess
        monkeypatch.setattr("spirit.lsp.install._find_npm", lambda: "/usr/bin/npm")
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("npm", 120)):
            ok = _npm_install({"pkg": "x", "bin": "x"}, tmp_path)
        assert ok is False

    def test_exception_returns_false(self, tmp_path, monkeypatch):
        from spirit.lsp.install import _npm_install
        monkeypatch.setattr("spirit.lsp.install._find_npm", lambda: "/usr/bin/npm")
        with patch("subprocess.run", side_effect=RuntimeError("boom")):
            ok = _npm_install({"pkg": "x", "bin": "x"}, tmp_path)
        assert ok is False

    def test_init_creates_node_modules(self, tmp_path, monkeypatch):
        """首次安装时会先 npm init -y。"""
        from spirit.lsp.install import _npm_install

        # 预创建 bin 以便最终检查
        bin_dir = tmp_path / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "my-ls").touch()

        call_count = {"init": 0, "install": 0}

        def fake_run(cmd, **kw):
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            if "init" in cmd:
                call_count["init"] += 1
                # 第一次 init 后, 创建 node_modules 模拟 npm 行为
                (tmp_path / "node_modules").mkdir(exist_ok=True)
            else:
                call_count["install"] += 1
            return r

        monkeypatch.setattr("spirit.lsp.install._find_npm", lambda: "/usr/bin/npm")
        with patch("subprocess.run", side_effect=fake_run):
            ok = _npm_install({"pkg": "x", "bin": "my-ls"}, tmp_path)
        assert ok is True
        assert call_count["install"] == 1

    def test_skips_init_if_node_modules_exists(self, tmp_path, monkeypatch):
        """已有 node_modules 时跳过 init。"""
        from spirit.lsp.install import _npm_install

        # 预创建 node_modules 和 bin
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / ".bin").mkdir()
        (tmp_path / "node_modules" / ".bin" / "my-ls").touch()

        commands = []

        def fake_run(cmd, **kw):
            commands.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            return r

        monkeypatch.setattr("spirit.lsp.install._find_npm", lambda: "/usr/bin/npm")
        with patch("subprocess.run", side_effect=fake_run):
            ok = _npm_install({"pkg": "x", "bin": "my-ls"}, tmp_path)
        assert ok is True
        # 没有 init 命令
        assert not any("init" in c for c in commands)

    def test_fallback_to_pkg_bin_dir(self, tmp_path, monkeypatch):
        """.bin 找不到, 但 pkg/bin/ 存在 → True。"""
        from spirit.lsp.install import _npm_install
        pkg_bin = tmp_path / "node_modules" / "mypkg" / "bin"
        pkg_bin.mkdir(parents=True)
        (pkg_bin / "x").touch()

        mock_result = MagicMock()
        mock_result.returncode = 0
        monkeypatch.setattr("spirit.lsp.install._find_npm", lambda: "/usr/bin/npm")
        with patch("subprocess.run", return_value=mock_result):
            ok = _npm_install({"pkg": "mypkg", "bin": "x"}, tmp_path)
        assert ok is True

    def test_no_bin_found_returns_false(self, tmp_path, monkeypatch):
        from spirit.lsp.install import _npm_install
        # 不预创建任何 bin
        (tmp_path / "node_modules").mkdir()
        mock_result = MagicMock()
        mock_result.returncode = 0
        monkeypatch.setattr("spirit.lsp.install._find_npm", lambda: "/usr/bin/npm")
        with patch("subprocess.run", return_value=mock_result):
            ok = _npm_install({"pkg": "mypkg", "bin": "my-ls"}, tmp_path)
        assert ok is False

    def test_extra_pkgs_included(self, tmp_path, monkeypatch):
        from spirit.lsp.install import _npm_install

        bin_dir = tmp_path / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "x").touch()

        mock_result = MagicMock()
        mock_result.returncode = 0
        monkeypatch.setattr("spirit.lsp.install._find_npm", lambda: "/usr/bin/npm")

        seen_cmd = []

        def fake_run(cmd, **kw):
            seen_cmd.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            return r

        with patch("subprocess.run", side_effect=fake_run):
            _npm_install(
                {"pkg": "main", "bin": "x", "extra_pkgs": ["extra1", "extra2"]},
                tmp_path,
            )
        # 找 install 命令
        install_cmd = next(c for c in seen_cmd if "install" in c)
        assert "main" in install_cmd
        assert "extra1" in install_cmd
        assert "extra2" in install_cmd


# ──────────────────────────────────────────────────────────────────────
# try_install
# ──────────────────────────────────────────────────────────────────────

class TestTryInstall:
    def test_unknown_server_returns_none(self):
        from spirit.lsp.install import try_install
        assert try_install("not-a-real-server") is None

    def test_manual_strategy_returns_none(self):
        """gopls 是 manual, 不自动安装。"""
        from spirit.lsp.install import try_install
        with patch("shutil.which", return_value=None):
            assert try_install("gopls") is None

    def test_off_strategy_returns_none(self):
        from spirit.lsp.install import try_install, INSTALL_RECIPES
        # 临时把一个 npm 改成 off
        original = INSTALL_RECIPES["pyright"]["strategy"]
        INSTALL_RECIPES["pyright"]["strategy"] = "off"
        try:
            assert try_install("pyright") is None
        finally:
            INSTALL_RECIPES["pyright"]["strategy"] = original

    def test_existing_in_path_returns_path(self):
        from spirit.lsp.install import try_install
        with patch("shutil.which", return_value="/usr/bin/pyright-langserver"):
            result = try_install("pyright")
        assert result == "/usr/bin/pyright-langserver"

    def test_staging_bin_exists_returns_path(self, tmp_path, monkeypatch):
        from spirit.lsp.install import try_install
        # staging_bin 是 staging/node_modules/.bin/<bin_name> 指向一个**文件**
        bin_path = tmp_path / "node_modules" / ".bin" / "pyright-langserver"
        bin_path.parent.mkdir(parents=True)
        bin_path.touch()

        monkeypatch.setattr("spirit.lsp.install._get_staging_dir", lambda: tmp_path)
        with patch("shutil.which", return_value=None):
            result = try_install("pyright")
        assert result == str(bin_path)

    def test_npm_install_success_returns_path(self, tmp_path, monkeypatch):
        from spirit.lsp.install import try_install

        bin_path = tmp_path / "node_modules" / ".bin" / "pyright-langserver"
        bin_path.mkdir(parents=True)
        # 先放一个空文件 (代表 npm 后已存在)
        (bin_path / "pyright-langserver").touch()

        monkeypatch.setattr("spirit.lsp.install._get_staging_dir", lambda: tmp_path)

        # shutil.which 先 None 触发安装, 第二次 (锁内) 仍 None
        with patch("shutil.which", return_value=None), \
             patch("spirit.lsp.install._npm_install", return_value=True):
            result = try_install("pyright")
        # staging_bin 已存在 → 返回
        assert result is not None

    def test_npm_install_failure_returns_none(self, tmp_path, monkeypatch):
        from spirit.lsp.install import try_install
        monkeypatch.setattr("spirit.lsp.install._get_staging_dir", lambda: tmp_path)
        with patch("shutil.which", return_value=None), \
             patch("spirit.lsp.install._npm_install", return_value=False):
            result = try_install("pyright")
        assert result is None


# ──────────────────────────────────────────────────────────────────────
# get_resolved_binary
# ──────────────────────────────────────────────────────────────────────

class TestGetResolvedBinary:
    def test_unknown_server_returns_none(self):
        from spirit.lsp.install import get_resolved_binary
        assert get_resolved_binary("not-real") is None

    def test_path_wins_over_staging(self, tmp_path, monkeypatch):
        from spirit.lsp.install import get_resolved_binary
        # staging 也有, 但 PATH 优先
        bin_dir = tmp_path / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "pyright-langserver").touch()

        monkeypatch.setattr("spirit.lsp.install._get_staging_dir", lambda: tmp_path)
        with patch("shutil.which", return_value="/usr/bin/pyright-langserver"):
            result = get_resolved_binary("pyright")
        assert result == "/usr/bin/pyright-langserver"

    def test_staging_fallback(self, tmp_path, monkeypatch):
        from spirit.lsp.install import get_resolved_binary
        bin_dir = tmp_path / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "pyright-langserver").touch()

        monkeypatch.setattr("spirit.lsp.install._get_staging_dir", lambda: tmp_path)
        with patch("shutil.which", return_value=None):
            result = get_resolved_binary("pyright")
        assert result == str(bin_dir / "pyright-langserver")

    def test_neither_returns_none(self, tmp_path, monkeypatch):
        from spirit.lsp.install import get_resolved_binary
        monkeypatch.setattr("spirit.lsp.install._get_staging_dir", lambda: tmp_path)
        with patch("shutil.which", return_value=None):
            result = get_resolved_binary("pyright")
        assert result is None
