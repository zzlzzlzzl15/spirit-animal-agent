"""检查点系统测试共享 fixture —— 隔离 SPIRIT_HOME + 工作区文件。"""

import pytest

from spirit import config
from spirit import checkpoint as cp_pkg


@pytest.fixture
def spirit_home(tmp_path, monkeypatch):
    """把 config.SPIRIT_HOME 指向 tmp，隔离检查点存储；并重置全局 manager 缓存。"""
    monkeypatch.setattr(config, "SPIRIT_HOME", tmp_path, raising=False)
    cp_pkg.reset_manager()
    yield tmp_path
    cp_pkg.reset_manager()


@pytest.fixture
def workspace(tmp_path):
    """一个含若干文件的工作区目录。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def sample_file(workspace):
    f = workspace / "a.py"
    f.write_text("print('v1')\n", encoding="utf-8")
    return f


@pytest.fixture
def manager(spirit_home):
    """一个绑定到隔离 SPIRIT_HOME 的新 CheckpointManager。"""
    return cp_pkg.CheckpointManager()
