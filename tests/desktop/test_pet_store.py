"""PetStore 测试。"""

import json
import pytest
from pathlib import Path
from spirit.desktop import pet_store
from spirit.desktop.pet_store import (
    InstalledPet, PetStoreError, slugify, _safe_slug,
)


@pytest.fixture
def temp_pet_dir(tmp_path, monkeypatch):
    """使用临时目录作为宠物存储。"""
    monkeypatch.setenv("SPIRIT_HOME", str(tmp_path))
    return tmp_path


class TestSlugify:
    def test_basic(self):
        assert slugify("Spirit Fox") == "spirit-fox"

    def test_special_chars(self):
        assert slugify("Hello! World@#") == "hello-world"

    def test_empty(self):
        assert slugify("") == "pet"

    def test_chinese(self):
        result = slugify("小灵狐")
        assert result == "pet"  # 中文字符被去除


class TestSafeSlug:
    def test_normal(self):
        assert _safe_slug("spirit-fox") == "spirit-fox"

    def test_path_traversal(self):
        assert _safe_slug("../etc/passwd") == "passwd"

    def test_dot(self):
        assert _safe_slug(".") == ""

    def test_dotdot(self):
        assert _safe_slug("..") == ""

    def test_empty(self):
        assert _safe_slug("") == ""


class TestPetStore:
    def test_pets_dir_created(self, temp_pet_dir):
        d = pet_store.pets_dir()
        assert d.is_dir()

    def test_load_nonexistent(self, temp_pet_dir):
        result = pet_store.load_pet("nonexistent")
        assert result is None

    def test_register_and_load(self, temp_pet_dir):
        # 创建一个假精灵图
        sprite_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        pet = pet_store.register_pet(
            sprite_data,
            slug="test-fox",
            display_name="Test Fox",
            description="A test fox",
        )
        assert pet.slug == "test-fox"
        assert pet.display_name == "Test Fox"
        assert pet.exists

        # 重新加载
        loaded = pet_store.load_pet("test-fox")
        assert loaded is not None
        assert loaded.slug == "test-fox"

    def test_installed_pets(self, temp_pet_dir):
        # 注册两个宠物
        pet_store.register_pet(b"\x89PNG" + b"\x00" * 50, slug="fox")
        pet_store.register_pet(b"\x89PNG" + b"\x00" * 50, slug="cat")

        pets = pet_store.installed_pets()
        slugs = [p.slug for p in pets]
        assert "cat" in slugs
        assert "fox" in slugs

    def test_remove_pet(self, temp_pet_dir):
        pet_store.register_pet(b"\x89PNG" + b"\x00" * 50, slug="temp")
        assert pet_store.load_pet("temp") is not None

        result = pet_store.remove_pet("temp")
        assert result is True
        assert pet_store.load_pet("temp") is None

    def test_remove_nonexistent(self, temp_pet_dir):
        result = pet_store.remove_pet("nonexistent")
        assert result is False

    def test_resolve_active_pet(self, temp_pet_dir):
        # 无宠物时返回 None
        assert pet_store.resolve_active_pet() is None

        # 注册后返回第一个
        pet_store.register_pet(b"\x89PNG" + b"\x00" * 50, slug="alpha")
        pet_store.register_pet(b"\x89PNG" + b"\x00" * 50, slug="beta")

        active = pet_store.resolve_active_pet()
        assert active is not None
        assert active.slug == "alpha"  # 字母序第一个

    def test_resolve_active_with_config(self, temp_pet_dir):
        pet_store.register_pet(b"\x89PNG" + b"\x00" * 50, slug="alpha")
        pet_store.register_pet(b"\x89PNG" + b"\x00" * 50, slug="beta")

        active = pet_store.resolve_active_pet("beta")
        assert active is not None
        assert active.slug == "beta"

    def test_get_pet_info(self, temp_pet_dir):
        pet_store.register_pet(b"\x89PNG" + b"\x00" * 50, slug="info-test", display_name="Info")
        info = pet_store.get_pet_info("info-test")
        assert info["slug"] == "info-test"
        assert info["displayName"] == "Info"
        assert info["spritesheetExists"] is True

    def test_get_pet_info_empty(self, temp_pet_dir):
        info = pet_store.get_pet_info("nonexistent")
        assert info == {}

    def test_export_pet(self, temp_pet_dir):
        pet_store.register_pet(b"\x89PNG" + b"\x00" * 50, slug="export-test")
        filename, data = pet_store.export_pet("export-test")
        assert filename == "export-test.zip"
        assert len(data) > 0

    def test_export_nonexistent(self, temp_pet_dir):
        with pytest.raises(PetStoreError):
            pet_store.export_pet("nonexistent")
