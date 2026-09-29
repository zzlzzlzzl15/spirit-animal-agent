"""检查点路径解析测试 —— SPIRIT_HOME 感知 + 无导入期副作用。"""

from spirit.checkpoint import paths


def test_checkpoints_root_follows_spirit_home(spirit_home):
    """checkpoints_root 应按调用反映被 monkeypatch 的 SPIRIT_HOME。"""
    root = paths.checkpoints_root()
    assert root == spirit_home / "checkpoints"


def test_blobs_dir_and_path(spirit_home):
    assert paths.blobs_dir() == spirit_home / "checkpoints" / "blobs"
    assert paths.blob_path("abc123") == spirit_home / "checkpoints" / "blobs" / "abc123"


def test_index_path(spirit_home):
    assert paths.index_path() == spirit_home / "checkpoints" / "index.json"


def test_no_import_side_effects(spirit_home):
    """路径解析不应创建任何目录（惰性建目录只在 ensure_dirs 里）。"""
    paths.checkpoints_root()
    paths.blobs_dir()
    paths.index_path()
    assert not paths.checkpoints_root().exists()


def test_ensure_dirs_creates_blob_dir(spirit_home):
    paths.ensure_dirs()
    assert paths.blobs_dir().is_dir()


def test_path_functions_reflect_home_switch(tmp_path, monkeypatch):
    """切换 SPIRIT_HOME 后，路径函数应立即跟随（按调用解析）。"""
    from spirit import config

    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    monkeypatch.setattr(config, "SPIRIT_HOME", home_a, raising=False)
    assert paths.checkpoints_root() == home_a / "checkpoints"
    monkeypatch.setattr(config, "SPIRIT_HOME", home_b, raising=False)
    assert paths.checkpoints_root() == home_b / "checkpoints"
