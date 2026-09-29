"""tests/profile/test_manager.py — Profile 管理器核心。

覆盖 ``spirit.profile.manager``：名字规范化/校验、CRUD（create/list/delete/rename）、
活跃 profile 粘性状态、profile.yaml meta 读写、技能计数、clone-config/clone-all、
以及对损坏 profile 的容错。全部离线。
"""

from __future__ import annotations

import pytest

from spirit.profile import manager
from spirit.profile.manager import (
    ProfileInfo,
    create_profile,
    delete_profile,
    get_active_profile,
    list_profiles,
    normalize_profile_name,
    profile_exists,
    read_profile_meta,
    rename_profile,
    set_active_profile,
    validate_profile_name,
    write_profile_meta,
)
from tests.profile.conftest import add_flat_skill, add_skill, write_config_yaml


# ---------------------------------------------------------------------------
# 名字规范化与校验
# ---------------------------------------------------------------------------

def test_normalize_lowercases():
    assert normalize_profile_name("Work") == "work"
    assert normalize_profile_name("  Finance-1  ") == "finance-1"


def test_normalize_default_case_insensitive():
    assert normalize_profile_name("Default") == "default"
    assert normalize_profile_name("DEFAULT") == "default"


def test_normalize_empty_raises():
    with pytest.raises(ValueError):
        normalize_profile_name("   ")


def test_validate_accepts_legal_names():
    for n in ("work", "finance-1", "a_b", "x0", "default"):
        validate_profile_name(n)  # 不抛即通过


def test_validate_rejects_illegal():
    for bad in ("Work", "-lead", "1a b", "a" * 65, "up/../down", "with.dot"):
        with pytest.raises(ValueError):
            validate_profile_name(bad)


def test_validate_rejects_reserved():
    for r in ("spirit", "root", "sudo", "profiles"):
        with pytest.raises(ValueError):
            validate_profile_name(r)


# ---------------------------------------------------------------------------
# create / list / exists
# ---------------------------------------------------------------------------

def test_create_profile_bootstraps_dirs(profile_home):
    path = create_profile("work")
    assert path.is_dir()
    assert (path / ".env").is_file()
    for sub in ("memories", "sessions", "skills", "logs"):
        assert (path / sub).is_dir()


def test_create_default_rejected(profile_home):
    with pytest.raises(ValueError):
        create_profile("default")


def test_create_duplicate_rejected(profile_home):
    create_profile("work")
    with pytest.raises(FileExistsError):
        create_profile("work")


def test_create_with_description_writes_meta(profile_home):
    path = create_profile("work", description="工作身份")
    meta = read_profile_meta(path)
    assert meta["description"] == "工作身份"
    assert meta["description_auto"] is False


def test_profile_exists(profile_home):
    assert profile_exists("default") is True  # 虚拟根，恒存在
    assert profile_exists("work") is False
    create_profile("work")
    assert profile_exists("work") is True


def test_list_profiles_includes_default_and_named(profile_home):
    create_profile("work")
    create_profile("finance")
    names = {p.name for p in list_profiles()}
    assert names == {"default", "work", "finance"}
    by_name = {p.name: p for p in list_profiles()}
    assert by_name["default"].is_default is True
    assert by_name["work"].is_default is False


def test_list_profiles_tolerates_corrupt_meta(profile_home):
    """一个坏 profile.yaml 不该拖垮 list_profiles。"""
    path = create_profile("work")
    (path / "profile.yaml").write_text("::: not yaml :::\n\t- broken", encoding="utf-8")
    create_profile("finance")
    profiles = list_profiles()  # 不抛
    assert {p.name for p in profiles} == {"default", "work", "finance"}


# ---------------------------------------------------------------------------
# active profile 粘性状态
# ---------------------------------------------------------------------------

def test_active_defaults_to_default(profile_home):
    assert get_active_profile() == "default"


def test_set_and_get_active(profile_home):
    create_profile("work")
    set_active_profile("work")
    assert get_active_profile() == "work"
    infos = {p.name: p for p in list_profiles()}
    assert infos["work"].is_active is True
    assert infos["default"].is_active is False


def test_set_active_default_clears(profile_home):
    create_profile("work")
    set_active_profile("work")
    set_active_profile("default")
    assert get_active_profile() == "default"


def test_set_active_missing_raises(profile_home):
    with pytest.raises(FileNotFoundError):
        set_active_profile("ghost")


# ---------------------------------------------------------------------------
# delete / rename
# ---------------------------------------------------------------------------

def test_delete_profile(profile_home):
    path = create_profile("work")
    deleted = delete_profile("work")
    assert deleted == path
    assert not path.exists()
    assert profile_exists("work") is False


def test_delete_default_rejected(profile_home):
    with pytest.raises(ValueError):
        delete_profile("default")


def test_delete_active_clears_sticky(profile_home):
    create_profile("work")
    set_active_profile("work")
    delete_profile("work")
    assert get_active_profile() == "default"


def test_delete_missing_raises(profile_home):
    with pytest.raises(FileNotFoundError):
        delete_profile("ghost")


def test_rename_profile(profile_home):
    old = create_profile("work")
    (old / "marker.txt").write_text("keep me", encoding="utf-8")
    new = rename_profile("work", "job")
    assert new.is_dir()
    assert (new / "marker.txt").read_text(encoding="utf-8") == "keep me"
    assert not old.exists()
    assert profile_exists("job") is True


def test_rename_active_follows(profile_home):
    create_profile("work")
    set_active_profile("work")
    rename_profile("work", "job")
    assert get_active_profile() == "job"


def test_rename_to_existing_rejected(profile_home):
    create_profile("work")
    create_profile("job")
    with pytest.raises(FileExistsError):
        rename_profile("work", "job")


def test_rename_default_rejected(profile_home):
    with pytest.raises(ValueError):
        rename_profile("default", "other")


# ---------------------------------------------------------------------------
# meta / skill 计数 / config 探测
# ---------------------------------------------------------------------------

def test_write_meta_preserves_other_fields(profile_home):
    path = create_profile("work")
    write_profile_meta(path, description="v1", description_auto=False)
    write_profile_meta(path, description_auto=True)  # 只改 auto，保留 description
    meta = read_profile_meta(path)
    assert meta["description"] == "v1"
    assert meta["description_auto"] is True


def test_read_meta_missing_returns_defaults(profile_home):
    path = create_profile("work")
    meta = read_profile_meta(path)
    assert meta == {"description": "", "description_auto": False}


def test_skill_count(profile_home):
    path = create_profile("work")
    add_skill(path, "devops", "deploy")
    add_skill(path, "research", "search")
    add_flat_skill(path, "solo")
    info = {p.name: p for p in list_profiles()}["work"]
    assert info.skill_count == 3


def test_config_model_provider(profile_home):
    path = create_profile("work")
    write_config_yaml(path, "qwen3-max", "alibaba")
    info = {p.name: p for p in list_profiles()}["work"]
    assert info.model == "qwen3-max"
    assert info.provider == "alibaba"


# ---------------------------------------------------------------------------
# clone
# ---------------------------------------------------------------------------

def test_clone_config_copies_config_and_skills(profile_home):
    src = create_profile("work", description="源")
    write_config_yaml(src, "gpt-x", "openai")
    add_skill(src, "devops", "deploy")
    (src / "SOUL.md").write_text("identity", encoding="utf-8")

    dst = create_profile("job", clone_from="work", clone_config=True)
    assert (dst / "config.yaml").is_file()
    assert (dst / "SOUL.md").read_text(encoding="utf-8") == "identity"
    assert (dst / "skills" / "devops" / "deploy" / "SKILL.md").is_file()


def test_clone_all_copies_tree_but_strips_runtime(profile_home):
    src = create_profile("work")
    (src / "config.yaml").write_text("llm: {}\n", encoding="utf-8")
    (src / "gateway.pid").write_text("1234", encoding="utf-8")  # 运行时文件，应被剥掉

    dst = create_profile("job", clone_from="work", clone_all=True)
    assert (dst / "config.yaml").is_file()
    assert not (dst / "gateway.pid").exists()


def test_clone_from_missing_source_raises(profile_home):
    with pytest.raises(FileNotFoundError):
        create_profile("job", clone_from="ghost", clone_config=True)


def test_profile_info_is_dataclass(profile_home):
    p = create_profile("work")
    info = ProfileInfo(name="work", path=p, is_default=False)
    assert info.name == "work"
    assert info.is_active is False
    assert info.skill_count == 0
