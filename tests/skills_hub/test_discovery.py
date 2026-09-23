"""tests/skills_hub/test_discovery.py — 技能发现与门控。

对标 Hermes ``tests/agent/test_skill_utils.py`` + ``test_skill_commands.py`` 的发现层用例：
frontmatter 解析（含 BOM / 非法 YAML 回退）、平台门（硬兼容）、环境门（相关性、fail-open）、
禁用列表（全局 ∪ 平台）、外部技能目录、索引迭代（排除 VCS / .hub / 支持区）、标识符归一化、
``parse_skill_md`` → ``SkillMeta``、``find_all_skills`` 排序与门控。
"""

from __future__ import annotations

import os

from spirit.skills_hub import discovery, paths
from tests.skills_hub.conftest import write_skills_config


# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------

class TestParseFrontmatter:
    def test_valid(self):
        fm, body = discovery.parse_frontmatter(
            "---\nname: foo\ndescription: bar\n---\n\n# Body\n"
        )
        assert fm["name"] == "foo"
        assert fm["description"] == "bar"
        assert "# Body" in body

    def test_no_frontmatter(self):
        fm, body = discovery.parse_frontmatter("# Just a doc\n")
        assert fm == {}
        assert body == "# Just a doc\n"

    def test_no_closing_delimiter(self):
        fm, body = discovery.parse_frontmatter("---\nname: foo\n# never closed\n")
        assert fm == {}

    def test_empty(self):
        fm, body = discovery.parse_frontmatter("")
        assert fm == {}
        assert body == ""

    def test_nested_yaml(self):
        fm, _ = discovery.parse_frontmatter(
            "---\nname: foo\nmetadata:\n  spirit:\n    tags:\n      - a\n      - b\n---\nbody\n"
        )
        assert fm["metadata"]["spirit"]["tags"] == ["a", "b"]

    def test_strips_bom(self):
        """前导 UTF-8 BOM 会让 --- 栅栏检查失败；必须先剥离。"""
        fm, _ = discovery.parse_frontmatter("\ufeff---\nname: foo\n---\nbody\n")
        assert fm.get("name") == "foo"

    def test_invalid_yaml_falls_back_to_line_split(self):
        # YAML 解析失败时回退到简单 key: value 逐行切分。
        fm, _ = discovery.parse_frontmatter(
            "---\nname: foo\n\tbad: [unclosed\n---\nbody\n"
        )
        # 回退路径至少尽力解析出 name（不因坏 YAML 丢整个 frontmatter）。
        assert isinstance(fm, dict)


# ---------------------------------------------------------------------------
# 平台门（硬兼容门）
# ---------------------------------------------------------------------------

class TestPlatformGate:
    def test_empty_matches_all(self):
        assert discovery.skill_matches_platform_list(None) is True
        assert discovery.skill_matches_platform_list([]) is True

    def test_matching_current(self, monkeypatch):
        monkeypatch.setattr(discovery.sys, "platform", "linux")
        assert discovery.skill_matches_platform_list(["linux"]) is True

    def test_non_matching(self, monkeypatch):
        monkeypatch.setattr(discovery.sys, "platform", "win32")
        assert discovery.skill_matches_platform_list(["linux"]) is False

    def test_mapped_names(self, monkeypatch):
        monkeypatch.setattr(discovery.sys, "platform", "darwin")
        assert discovery.skill_matches_platform_list(["macos"]) is True
        monkeypatch.setattr(discovery.sys, "platform", "win32")
        assert discovery.skill_matches_platform_list(["windows"]) is True

    def test_string_not_list(self, monkeypatch):
        monkeypatch.setattr(discovery.sys, "platform", "linux")
        assert discovery.skill_matches_platform_list("linux") is True

    def test_any_of_multiple(self, monkeypatch):
        monkeypatch.setattr(discovery.sys, "platform", "win32")
        assert discovery.skill_matches_platform_list(["linux", "windows"]) is True

    def test_frontmatter_gate(self, monkeypatch):
        monkeypatch.setattr(discovery.sys, "platform", "linux")
        assert discovery.skill_matches_platform({"platforms": ["linux"]}) is True
        assert discovery.skill_matches_platform({"platforms": ["windows"]}) is False
        assert discovery.skill_matches_platform({}) is True


# ---------------------------------------------------------------------------
# 环境门（相关性门，fail-open）
# ---------------------------------------------------------------------------

class TestEnvironmentGate:
    def test_empty_matches_all(self):
        assert discovery.skill_matches_environment({}) is True
        assert discovery.skill_matches_environment({"environments": []}) is True

    def test_unknown_env_fails_open(self):
        # 看不懂的标签绝不藏技能。
        assert discovery.skill_matches_environment({"environments": ["quantum-realm"]}) is True

    def test_docker_active(self, monkeypatch):
        monkeypatch.setattr(discovery, "_is_container", lambda: True)
        discovery.clear_env_cache()
        assert discovery.skill_matches_environment({"environments": ["docker"]}) is True

    def test_docker_inactive(self, monkeypatch):
        monkeypatch.setattr(discovery, "_is_container", lambda: False)
        discovery.clear_env_cache()
        assert discovery.skill_matches_environment({"environments": ["docker"]}) is False

    def test_or_semantics(self, monkeypatch):
        # 任一声明环境活跃即匹配：docker 不活跃但未知标签 fail-open → True。
        monkeypatch.setattr(discovery, "_is_container", lambda: False)
        discovery.clear_env_cache()
        assert discovery.skill_matches_environment(
            {"environments": ["docker", "mystery"]}
        ) is True


# ---------------------------------------------------------------------------
# 禁用列表
# ---------------------------------------------------------------------------

class TestDisabledSkills:
    def test_empty_by_default(self, skills_home):
        # DEFAULT_CONFIG.skills.disabled = []。
        assert discovery.get_disabled_skill_names() == set()

    def test_global_disabled(self, skills_home):
        write_skills_config(skills_home, disabled=["foo", "bar"])
        assert discovery.get_disabled_skill_names() == {"foo", "bar"}

    def test_platform_disabled_union(self, skills_home, monkeypatch):
        write_skills_config(
            skills_home,
            disabled=["global-one"],
            platform_disabled={"telegram": ["tg-one"]},
        )
        monkeypatch.setenv("SPIRIT_PLATFORM", "telegram")
        assert discovery.get_disabled_skill_names() == {"global-one", "tg-one"}

    def test_platform_disabled_other_platform(self, skills_home, monkeypatch):
        write_skills_config(
            skills_home,
            disabled=["global-one"],
            platform_disabled={"telegram": ["tg-one"]},
        )
        monkeypatch.setenv("SPIRIT_PLATFORM", "discord")
        # 非 telegram 平台只保留全局禁用。
        assert discovery.get_disabled_skill_names() == {"global-one"}

    def test_explicit_platform_arg(self, skills_home):
        write_skills_config(skills_home, platform_disabled={"slack": ["s-one"]})
        assert discovery.get_disabled_skill_names(platform="slack") == {"s-one"}


# ---------------------------------------------------------------------------
# 外部技能目录
# ---------------------------------------------------------------------------

class TestExternalDirs:
    def test_none_by_default(self, skills_home):
        assert discovery.get_external_skills_dirs() == []

    def test_absolute_existing(self, skills_home, tmp_path_factory):
        ext = tmp_path_factory.mktemp("ext-skills")
        write_skills_config(skills_home, external_dirs=[str(ext)])
        result = discovery.get_external_skills_dirs()
        assert ext.resolve() in [p.resolve() for p in result]

    def test_relative_to_home(self, skills_home):
        rel = skills_home / "shared-vault"
        rel.mkdir()
        write_skills_config(skills_home, external_dirs=["shared-vault"])
        result = discovery.get_external_skills_dirs()
        assert rel.resolve() in [p.resolve() for p in result]

    def test_skips_nonexistent(self, skills_home):
        write_skills_config(skills_home, external_dirs=["does-not-exist"])
        assert discovery.get_external_skills_dirs() == []

    def test_skips_local_skills_dup(self, skills_home):
        write_skills_config(skills_home, external_dirs=[str(paths.skills_dir())])
        assert discovery.get_external_skills_dirs() == []

    def test_all_dirs_local_first(self, skills_home, tmp_path_factory):
        ext = tmp_path_factory.mktemp("ext2")
        write_skills_config(skills_home, external_dirs=[str(ext)])
        all_dirs = discovery.get_all_skills_dirs()
        assert all_dirs[0] == paths.skills_dir()
        assert ext.resolve() in [p.resolve() for p in all_dirs[1:]]


# ---------------------------------------------------------------------------
# normalize_skill_lookup_name
# ---------------------------------------------------------------------------

class TestNormalizeLookupName:
    def test_relative_unchanged(self, skills_home):
        assert discovery.normalize_skill_lookup_name("my-skill") == "my-skill"

    def test_strips_leading_slash(self, skills_home):
        assert discovery.normalize_skill_lookup_name("/my-skill") == "my-skill"

    def test_absolute_under_skills_becomes_relative(self, skills_home):
        abs_path = str(paths.skills_dir() / "cat" / "my-skill")
        result = discovery.normalize_skill_lookup_name(abs_path)
        assert result == os.path.join("cat", "my-skill")
        assert not os.path.isabs(result)

    def test_empty(self, skills_home):
        assert discovery.normalize_skill_lookup_name("") == ""

    def test_absolute_outside_trusted_passthrough(self, skills_home, tmp_path_factory):
        outside = tmp_path_factory.mktemp("outside") / "evil"
        # 受信根之外的绝对路径原样透传（调用方随后拒绝）。
        assert discovery.normalize_skill_lookup_name(str(outside)) == str(outside)


# ---------------------------------------------------------------------------
# iter_skill_index_files / 支持区 / 排除区
# ---------------------------------------------------------------------------

class TestIterIndexFiles:
    def test_finds_top_level(self, skills_home, make_skill):
        make_skill("alpha")
        make_skill("beta")
        found = {p.parent.name for p in discovery.iter_skill_index_files(paths.skills_dir(), "SKILL.md")}
        assert {"alpha", "beta"} <= found

    def test_finds_nested_category(self, skills_home, make_skill):
        make_skill("deep", category="productivity")
        found = {p.parent.name for p in discovery.iter_skill_index_files(paths.skills_dir(), "SKILL.md")}
        assert "deep" in found

    def test_excludes_hub_dir(self, skills_home, make_skill):
        make_skill("real")
        # .hub 下的 SKILL.md 不应被发现。
        hub_skill = paths.hub_dir() / "quarantine" / "q-skill"
        hub_skill.mkdir(parents=True, exist_ok=True)
        (hub_skill / "SKILL.md").write_text("---\nname: q\ndescription: d\n---\n", encoding="utf-8")
        found = {p.parent.name for p in discovery.iter_skill_index_files(paths.skills_dir(), "SKILL.md")}
        assert "real" in found
        assert "q-skill" not in found

    def test_excludes_support_dir_of_skill(self, skills_home, make_skill):
        make_skill("withrefs", supporting={"references/guide.md": "# guide\n"})
        found = {p.parent.name for p in discovery.iter_skill_index_files(paths.skills_dir(), "SKILL.md")}
        assert "withrefs" in found
        # references/ 下即使有 SKILL.md 也不作为独立技能（支持区）。
        ref = paths.skills_dir() / "withrefs" / "references"
        (ref / "SKILL.md").write_text("---\nname: hidden\ndescription: d\n---\n", encoding="utf-8")
        found2 = {p.parent.name for p in discovery.iter_skill_index_files(paths.skills_dir(), "SKILL.md")}
        assert "hidden" not in found2

    def test_support_path_detection(self, skills_home, make_skill):
        d = make_skill("sup", supporting={"scripts/run.py": "print(1)\n"})
        assert discovery.is_skill_support_path(d / "scripts" / "run.py") is True
        assert discovery.is_skill_support_path(d / "SKILL.md") is False

    def test_excluded_path_detection(self, skills_home):
        assert discovery.is_excluded_skill_path(paths.skills_dir() / ".git" / "x") is True
        assert discovery.is_excluded_skill_path(paths.skills_dir() / "normal" / "SKILL.md") is False


# ---------------------------------------------------------------------------
# parse_skill_md → SkillMeta
# ---------------------------------------------------------------------------

class TestParseSkillMd:
    def test_basic(self, skills_home, make_skill):
        d = make_skill("meta-skill", description="A description", body="hi")
        meta = discovery.parse_skill_md(d / "SKILL.md")
        assert meta is not None
        assert meta.name == "meta-skill"
        assert meta.description == "A description"
        assert meta.path == str(d)

    def test_spirit_tags(self, skills_home, make_skill):
        d = make_skill(
            "tagged",
            frontmatter_extra="metadata:\n  spirit:\n    tags:\n      - dev\n      - review",
        )
        meta = discovery.parse_skill_md(d / "SKILL.md")
        assert meta.tags == ["dev", "review"]

    def test_platforms_and_environments(self, skills_home, make_skill):
        d = make_skill(
            "gated",
            frontmatter_extra="platforms:\n  - linux\nenvironments:\n  - docker",
        )
        meta = discovery.parse_skill_md(d / "SKILL.md")
        assert meta.platforms == ["linux"]
        assert meta.environments == ["docker"]

    def test_missing_name_returns_none(self, skills_home):
        d = paths.skills_dir() / "noname"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\ndescription: only desc\n---\nbody\n", encoding="utf-8")
        assert discovery.parse_skill_md(d / "SKILL.md") is None

    def test_missing_description_returns_none(self, skills_home):
        d = paths.skills_dir() / "nodesc"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: nodesc\n---\nbody\n", encoding="utf-8")
        assert discovery.parse_skill_md(d / "SKILL.md") is None

    def test_no_frontmatter_returns_none(self, skills_home):
        d = paths.skills_dir() / "plain"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("# no frontmatter here\n", encoding="utf-8")
        assert discovery.parse_skill_md(d / "SKILL.md") is None


# ---------------------------------------------------------------------------
# find_all_skills
# ---------------------------------------------------------------------------

class TestFindAllSkills:
    def test_sorted_by_name(self, skills_home, make_skill):
        make_skill("zebra")
        make_skill("apple")
        make_skill("mango")
        names = [m.name for m in discovery.find_all_skills()]
        assert names == sorted(names, key=str.lower)
        assert names == ["apple", "mango", "zebra"]

    def test_dedup_by_name(self, skills_home, make_skill):
        make_skill("dup", category="a")
        make_skill("dup", category="b")
        names = [m.name for m in discovery.find_all_skills()]
        assert names.count("dup") == 1

    def test_apply_gates_filters_platform(self, skills_home, make_skill, monkeypatch):
        make_skill("linux-only", frontmatter_extra="platforms:\n  - linux")
        make_skill("any-os")
        monkeypatch.setattr(discovery.sys, "platform", "win32")
        gated = {m.name for m in discovery.find_all_skills(apply_gates=True)}
        assert "any-os" in gated
        assert "linux-only" not in gated
        # 不施加门时全部返回。
        ungated = {m.name for m in discovery.find_all_skills()}
        assert "linux-only" in ungated

    def test_apply_gates_filters_disabled(self, skills_home, make_skill):
        make_skill("enabled-one")
        make_skill("disabled-one")
        write_skills_config(skills_home, disabled=["disabled-one"])
        gated = {m.name for m in discovery.find_all_skills(apply_gates=True)}
        assert "enabled-one" in gated
        assert "disabled-one" not in gated
