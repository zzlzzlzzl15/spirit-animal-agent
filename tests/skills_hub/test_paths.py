"""tests/skills_hub/test_paths.py — 按调用解析的路径层。

对标 Hermes ``tools/skills_hub.py`` 顶部「按调用解析（而非 import 时冻结）」的路径设计：
验证 ``spirit_home()`` 读 ``config.SPIRIT_HOME`` 模块全局（monkeypatch 即时生效）、各目录
env 覆盖（``SPIRIT_SKILLS_DIR`` / ``SPIRIT_HUB_DIR`` / ``SPIRIT_BUNDLES_DIR``）、以及
``.hub`` 下各元数据文件的布局与排除 / 支持目录集合。
"""

from __future__ import annotations

from spirit.skills_hub import paths


class TestSpiritHome:
    def test_reads_config_global(self, skills_home):
        # skills_home fixture 把 config.SPIRIT_HOME 指到 tmp_path。
        assert paths.spirit_home() == skills_home

    def test_monkeypatch_takes_effect_immediately(self, skills_home, monkeypatch, tmp_path_factory):
        """按调用读取模块全局：改 SPIRIT_HOME 后下游路径立即跟随（无 import 期冻结）。"""
        import spirit.config as config

        other = tmp_path_factory.mktemp("other-home")
        monkeypatch.setattr(config, "SPIRIT_HOME", other, raising=False)
        assert paths.spirit_home() == other
        assert paths.skills_dir() == other / "skills"


class TestSkillsDir:
    def test_default_under_home(self, skills_home):
        assert paths.skills_dir() == skills_home / "skills"

    def test_env_override(self, skills_home, monkeypatch, tmp_path_factory):
        custom = tmp_path_factory.mktemp("custom-skills")
        monkeypatch.setenv("SPIRIT_SKILLS_DIR", str(custom))
        assert paths.skills_dir() == custom

    def test_blank_env_falls_back(self, skills_home, monkeypatch):
        monkeypatch.setenv("SPIRIT_SKILLS_DIR", "   ")
        assert paths.skills_dir() == skills_home / "skills"


class TestHubDir:
    def test_default_under_skills(self, skills_home):
        assert paths.hub_dir() == skills_home / "skills" / ".hub"

    def test_env_override(self, skills_home, monkeypatch, tmp_path_factory):
        custom = tmp_path_factory.mktemp("custom-hub")
        monkeypatch.setenv("SPIRIT_HUB_DIR", str(custom))
        assert paths.hub_dir() == custom

    def test_follows_skills_dir_env(self, skills_home, monkeypatch, tmp_path_factory):
        """hub_dir 回退到 skills_dir()/.hub，故 SPIRIT_SKILLS_DIR 覆盖会连带影响它。"""
        custom = tmp_path_factory.mktemp("sk")
        monkeypatch.setenv("SPIRIT_SKILLS_DIR", str(custom))
        assert paths.hub_dir() == custom / ".hub"


class TestHubFiles:
    def test_all_under_hub(self, skills_home):
        hub = skills_home / "skills" / ".hub"
        assert paths.lock_file() == hub / "lock.json"
        assert paths.quarantine_dir() == hub / "quarantine"
        assert paths.audit_log() == hub / "audit.jsonl"
        assert paths.taps_file() == hub / "taps.json"
        assert paths.index_cache_dir() == hub / "index-cache"
        assert paths.usage_file() == hub / "usage.json"
        assert paths.provenance_file() == hub / "provenance.json"

    def test_audit_log_is_jsonl(self, skills_home):
        assert paths.audit_log().suffix == ".jsonl"


class TestBundlesDir:
    def test_default_under_home(self, skills_home):
        assert paths.bundles_dir() == skills_home / "skill-bundles"

    def test_env_override(self, skills_home, monkeypatch, tmp_path_factory):
        custom = tmp_path_factory.mktemp("custom-bundles")
        monkeypatch.setenv("SPIRIT_BUNDLES_DIR", str(custom))
        assert paths.bundles_dir() == custom


class TestDirConstants:
    def test_excluded_includes_vcs_and_hub(self):
        for name in (".git", ".hub", ".venv", "node_modules", "__pycache__", ".archive"):
            assert name in paths.EXCLUDED_SKILL_DIRS

    def test_support_dirs_progressive_disclosure(self):
        assert paths.SKILL_SUPPORT_DIRS == frozenset(
            {"references", "templates", "assets", "scripts", "examples"}
        )

    def test_support_and_excluded_disjoint(self):
        # 支持区不是排除区（支持区经 is_skill_support_path 单独判定）。
        assert not (paths.SKILL_SUPPORT_DIRS & paths.EXCLUDED_SKILL_DIRS)
