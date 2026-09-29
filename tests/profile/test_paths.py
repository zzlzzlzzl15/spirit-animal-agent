"""tests/profile/test_paths.py — 路径解析层。

覆盖 ``spirit.profile.paths``：default_root 的"从 profile 目录爬回根"语义、
profiles_root/profile_dir/active_profile_path 拼接、resolve_profile_env 子进程环境、
apply_active_profile 进程内切换。全部离线（monkeypatch SPIRIT_HOME）。
"""

from __future__ import annotations

import spirit.config as config
from spirit.profile import paths


def test_default_root_is_spirit_home(profile_home):
    """SPIRIT_HOME 不是 profile 目录时，default_root 原样返回它。"""
    assert paths.default_root() == profile_home


def test_profiles_root_under_default(profile_home):
    assert paths.profiles_root() == profile_home / "profiles"


def test_active_profile_path_under_default(profile_home):
    assert paths.active_profile_path() == profile_home / "active_profile"


def test_profile_dir_default_is_root(profile_home):
    """``default`` 解析为根本身（零迁移）。"""
    assert paths.profile_dir("default") == profile_home


def test_profile_dir_named_under_profiles(profile_home):
    assert paths.profile_dir("work") == profile_home / "profiles" / "work"


def test_default_root_climbs_back_from_profile_dir(profile_home, monkeypatch):
    """当 SPIRIT_HOME 已指向 ``<root>/profiles/<name>`` 时，default_root 爬回 ``<root>``。

    保证 profiles_root 不随活跃 profile 漂移。
    """
    nested = profile_home / "profiles" / "work"
    nested.mkdir(parents=True)
    monkeypatch.setattr(config, "SPIRIT_HOME", nested, raising=False)
    assert paths.default_root() == profile_home
    assert paths.profiles_root() == profile_home / "profiles"


def test_resolve_profile_env_sets_spirit_home(profile_home):
    """resolve_profile_env 产出子进程环境，SPIRIT_HOME 指向该 profile，且不改动 os.environ。"""
    base = {"PATH": "/usr/bin", "SPIRIT_HOME": "/original"}
    env = paths.resolve_profile_env("work", base_env=base)
    assert env["SPIRIT_HOME"] == str(profile_home / "profiles" / "work")
    assert env["PATH"] == "/usr/bin"
    # 原 base_env 不被就地修改
    assert base["SPIRIT_HOME"] == "/original"


def test_resolve_profile_env_default(profile_home):
    env = paths.resolve_profile_env("default", base_env={})
    assert env["SPIRIT_HOME"] == str(profile_home)


def test_apply_active_profile_switches_in_process(profile_home):
    """apply_active_profile 直接改写 config.SPIRIT_HOME 并返回新 HOME。"""
    target = paths.apply_active_profile("finance")
    assert target == profile_home / "profiles" / "finance"
    assert config.SPIRIT_HOME == target


def test_apply_active_profile_default_restores_root(profile_home):
    paths.apply_active_profile("finance")
    target = paths.apply_active_profile("default")
    assert target == profile_home
    assert config.SPIRIT_HOME == profile_home
