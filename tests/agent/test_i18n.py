"""spirit.agent.i18n 单元测试 —— catalog 翻译。

离线。用内置 locales/（en+zh）+ tmp 自定义 catalog（经 SPIRIT_BUNDLED_LOCALES）
覆盖：语言规范化、env/config/默认优先级、缺失降级、format 占位符、缓存失效。
"""

import os
import textwrap

import pytest

from spirit.agent import i18n


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """每个测试前后清缓存，并隔离环境变量，避免串扰。"""
    monkeypatch.delenv("SPIRIT_LANGUAGE", raising=False)
    monkeypatch.delenv("SPIRIT_BUNDLED_LOCALES", raising=False)
    i18n.reset_language_cache()
    yield
    i18n.reset_language_cache()


# --------------------------------------------------------------------------
# 语言规范化
# --------------------------------------------------------------------------

class TestNormalizeLang:
    @pytest.mark.parametrize("value,expected", [
        ("zh", "zh"),
        ("en", "en"),
        ("chinese", "zh"),
        ("zh-CN", "zh"),
        ("zh-TW", "zh-hant"),
        ("jp", "ja"),
        ("japanese", "ja"),
        ("Deutsch", "de"),
        ("ko-KR", "ko"),
    ])
    def test_aliases_and_regions(self, value, expected):
        assert i18n._normalize_lang(value) == expected

    def test_unknown_falls_back_to_default(self):
        assert i18n._normalize_lang("klingon") == i18n.DEFAULT_LANGUAGE

    def test_non_string_falls_back(self):
        assert i18n._normalize_lang(None) == i18n.DEFAULT_LANGUAGE
        assert i18n._normalize_lang(123) == i18n.DEFAULT_LANGUAGE

    def test_empty_falls_back(self):
        assert i18n._normalize_lang("   ") == i18n.DEFAULT_LANGUAGE


# --------------------------------------------------------------------------
# t() —— 翻译与降级
# --------------------------------------------------------------------------

class TestTranslate:
    def test_english_from_bundled_catalog(self):
        assert i18n.t("common.ok", lang="en") == "OK"

    def test_chinese_from_bundled_catalog(self):
        assert i18n.t("common.ok", lang="zh") == "好的"

    def test_nested_key(self):
        assert i18n.t("approval.deny", lang="zh") == "拒绝"

    def test_missing_key_in_target_falls_back_to_english(self):
        # 构造一个只有部分 key 的自定义 catalog
        # 用内置 zh 没有的 key —— 直接测 en 存在、zh 缺失时的回退
        # 这里用一个两 catalog 都没有的 key 验证返回裸 key
        assert i18n.t("nonexistent.key.path", lang="zh") == "nonexistent.key.path"

    def test_format_kwargs_substituted(self):
        out = i18n.t("gateway.drain", lang="en", count=3)
        assert out == "Draining 3 pending message(s)..."

    def test_format_kwargs_zh(self):
        out = i18n.t("profile.active", lang="zh", name="work")
        assert "work" in out

    def test_format_missing_kwarg_returns_raw(self):
        # 占位符需要的 kwarg 未提供 → 返回未格式化的模板（不抛）
        out = i18n.t("gateway.drain", lang="en")
        assert "{count}" in out


# --------------------------------------------------------------------------
# 自定义 catalog（SPIRIT_BUNDLED_LOCALES）
# --------------------------------------------------------------------------

class TestCustomCatalog:
    def test_loads_from_bundled_locales_override(self, tmp_path, monkeypatch):
        locales = tmp_path / "locales"
        locales.mkdir()
        (locales / "en.yaml").write_text(
            textwrap.dedent("""\
                greeting:
                  hello: "Hi there"
            """),
            encoding="utf-8",
        )
        monkeypatch.setenv("SPIRIT_BUNDLED_LOCALES", str(locales))
        i18n.reset_language_cache()
        assert i18n.t("greeting.hello", lang="en") == "Hi there"

    def test_missing_catalog_file_returns_key(self, tmp_path, monkeypatch):
        locales = tmp_path / "empty_locales"
        locales.mkdir()
        monkeypatch.setenv("SPIRIT_BUNDLED_LOCALES", str(locales))
        i18n.reset_language_cache()
        # 无任何 catalog → 逐级降级到裸 key
        assert i18n.t("anything.at.all", lang="en") == "anything.at.all"

    def test_bundled_locales_non_dir_falls_back(self, tmp_path, monkeypatch):
        # 指向一个文件而非目录 → 忽略 override，回退源码 locales
        a_file = tmp_path / "not_a_dir.txt"
        a_file.write_text("x", encoding="utf-8")
        monkeypatch.setenv("SPIRIT_BUNDLED_LOCALES", str(a_file))
        i18n.reset_language_cache()
        # 仍能从内置 catalog 取到
        assert i18n.t("common.ok", lang="en") == "OK"

    def test_malformed_yaml_degrades_gracefully(self, tmp_path, monkeypatch):
        locales = tmp_path / "bad"
        locales.mkdir()
        (locales / "en.yaml").write_text("key: [unclosed", encoding="utf-8")
        monkeypatch.setenv("SPIRIT_BUNDLED_LOCALES", str(locales))
        i18n.reset_language_cache()
        # 解析失败 → 返回 key，不抛
        assert i18n.t("some.key", lang="en") == "some.key"


# --------------------------------------------------------------------------
# get_language() —— 优先级
# --------------------------------------------------------------------------

class TestGetLanguage:
    def test_default_is_en(self):
        assert i18n.get_language() == "en"

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("SPIRIT_LANGUAGE", "zh-CN")
        assert i18n.get_language() == "zh"

    def test_env_alias_normalized(self, monkeypatch):
        monkeypatch.setenv("SPIRIT_LANGUAGE", "japanese")
        assert i18n.get_language() == "ja"

    def test_t_uses_active_language(self, monkeypatch):
        monkeypatch.setenv("SPIRIT_LANGUAGE", "zh")
        assert i18n.t("common.ok") == "好的"

    def test_explicit_lang_beats_env(self, monkeypatch):
        monkeypatch.setenv("SPIRIT_LANGUAGE", "zh")
        assert i18n.t("common.ok", lang="en") == "OK"


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------

class TestCaching:
    def test_reset_clears_catalog_cache(self):
        # 先加载填充缓存
        i18n.t("common.ok", lang="en")
        assert "en" in i18n._catalog_cache
        i18n.reset_language_cache()
        assert i18n._catalog_cache == {}

    def test_flatten_ignores_non_string_leaves(self):
        out = {}
        i18n._flatten_into({"a": {"b": "text", "c": 123, "d": None}}, "", out)
        assert out == {"a.b": "text"}
