"""tests/skills_hub/test_bundles.py — YAML 定义的技能捆绑包。

对标 Hermes ``tests/agent/test_skill_bundles.py``：扫描（跳过非法 YAML / 无 skills / 重复
slug 首个胜出 / 文件名回退名）、缓存与按 mtime 重扫、命令键解析（连字符 / 下划线互通）、
调用消息构建（载全部成员、跳过缺失、跳过禁用、全禁返回 None、去重、用户 / 捆绑指令）、
文件级 CRUD（save/delete/overwrite 边界）、reload diff、list 排序。
"""

from __future__ import annotations

import os
import time

import pytest

from spirit.skills_hub import bundles, discovery, paths
from tests.skills_hub.conftest import write_skills_config


class TestScanBundles:
    def test_empty_dir(self, skills_home):
        assert bundles.scan_bundles() == {}

    def test_finds_bundle(self, skills_home, make_bundle):
        make_bundle("backend", ["skill-a", "skill-b"])
        result = bundles.scan_bundles()
        assert "/backend" in result
        assert result["/backend"]["name"] == "backend"
        assert result["/backend"]["skills"] == ["skill-a", "skill-b"]

    def test_skips_invalid_yaml(self, skills_home, make_bundle):
        bdir = paths.bundles_dir()
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "broken.yaml").write_text("{not: valid yaml: [", encoding="utf-8")
        make_bundle("good", ["skill-a"])
        result = bundles.scan_bundles()
        assert "/good" in result
        assert "/broken" not in result

    def test_skips_bundle_without_skills(self, skills_home):
        bdir = paths.bundles_dir()
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "noskills.yaml").write_text("name: noskills\nskills: []\n", encoding="utf-8")
        assert "/noskills" not in bundles.scan_bundles()

    def test_duplicate_slug_first_wins(self, skills_home, make_bundle):
        # alpha-dup.yaml 排序先于 alpha.yaml（'-' 0x2D < '.' 0x2E）→ 首个胜出。
        make_bundle("alpha", ["s1"], name="alpha")
        make_bundle("alpha-dup", ["s2"], name="ALPHA")
        result = bundles.scan_bundles()
        assert "/alpha" in result
        assert result["/alpha"]["skills"] == ["s2"]

    def test_uses_filename_as_fallback_name(self, skills_home):
        bdir = paths.bundles_dir()
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "fallback.yaml").write_text("skills:\n  - foo\n", encoding="utf-8")
        result = bundles.scan_bundles()
        assert "/fallback" in result
        assert result["/fallback"]["name"] == "fallback"


class TestGetSkillBundles:
    def test_returns_cache(self, skills_home, make_bundle):
        make_bundle("a", ["s1"])
        first = bundles.get_skill_bundles()
        second = bundles.get_skill_bundles()
        assert first == second

    def test_rescans_on_change(self, skills_home, make_bundle):
        make_bundle("a", ["s1"])
        assert "/a" in bundles.get_skill_bundles()
        time.sleep(0.05)
        make_bundle("b", ["s2"])
        os.utime(paths.bundles_dir(), None)
        result = bundles.get_skill_bundles()
        assert "/a" in result and "/b" in result


class TestResolveBundleCommandKey:
    def test_exact(self, skills_home, make_bundle):
        make_bundle("my-bundle", ["s1"])
        bundles.scan_bundles()
        assert bundles.resolve_bundle_command_key("my-bundle") == "/my-bundle"

    def test_underscore_alias(self, skills_home, make_bundle):
        make_bundle("my-bundle", ["s1"])
        bundles.scan_bundles()
        assert bundles.resolve_bundle_command_key("my_bundle") == "/my-bundle"

    def test_unknown(self, skills_home):
        bundles.scan_bundles()
        assert bundles.resolve_bundle_command_key("missing") is None

    def test_empty(self, skills_home):
        assert bundles.resolve_bundle_command_key("") is None


class TestBuildBundleInvocation:
    def test_loads_all_skills(self, skills_home, make_skill, make_bundle):
        make_skill("skill-a", body="Skill A content.")
        make_skill("skill-b", body="Skill B content.")
        make_bundle("combo", ["skill-a", "skill-b"])
        bundles.scan_bundles()
        result = bundles.build_bundle_invocation_message("/combo")
        assert result is not None
        msg, loaded, missing = result
        assert set(loaded) == {"skill-a", "skill-b"}
        assert missing == []
        assert "Skill A content." in msg and "Skill B content." in msg
        assert "combo" in msg

    def test_skips_missing_skills(self, skills_home, make_skill, make_bundle):
        make_skill("skill-a")
        make_bundle("combo", ["skill-a", "skill-ghost"])
        bundles.scan_bundles()
        msg, loaded, missing = bundles.build_bundle_invocation_message("/combo")
        assert loaded == ["skill-a"]
        assert missing == ["skill-ghost"]
        assert "skill-ghost" in msg  # header 点名

    def test_skips_disabled_skills(self, skills_home, make_skill, make_bundle):
        make_skill("skill-a", body="Skill A content.")
        make_skill("skill-b", body="SECRET DISABLED CONTENT.")
        make_bundle("combo", ["skill-a", "skill-b"])
        write_skills_config(skills_home, disabled=["skill-b"])
        bundles.scan_bundles()
        msg, loaded, missing = bundles.build_bundle_invocation_message("/combo")
        assert loaded == ["skill-a"]
        assert "SECRET DISABLED CONTENT." not in msg
        assert "skill-b" in msg
        assert "disabled" in msg.lower()

    def test_all_disabled_returns_none(self, skills_home, make_skill, make_bundle):
        make_skill("skill-a")
        make_bundle("solo", ["skill-a"])
        write_skills_config(skills_home, disabled=["skill-a"])
        bundles.scan_bundles()
        assert bundles.build_bundle_invocation_message("/solo") is None

    def test_unknown_bundle_returns_none(self, skills_home):
        bundles.scan_bundles()
        assert bundles.build_bundle_invocation_message("/nope") is None

    def test_no_loadable_skills_returns_none(self, skills_home, make_bundle):
        make_bundle("ghost", ["nonexistent-skill"])
        bundles.scan_bundles()
        assert bundles.build_bundle_invocation_message("/ghost") is None

    def test_includes_user_instruction(self, skills_home, make_skill, make_bundle):
        make_skill("skill-a")
        make_bundle("combo", ["skill-a"])
        bundles.scan_bundles()
        msg, _, _ = bundles.build_bundle_invocation_message("/combo", user_instruction="extra ctx")
        assert "extra ctx" in msg

    def test_includes_bundle_instruction(self, skills_home, make_skill, make_bundle):
        make_skill("skill-a")
        make_bundle("combo", ["skill-a"], instruction="Always check tests first.")
        bundles.scan_bundles()
        msg, _, _ = bundles.build_bundle_invocation_message("/combo")
        assert "Always check tests first." in msg

    def test_dedupes_skills(self, skills_home, make_skill, make_bundle):
        make_skill("skill-a")
        make_bundle("combo", ["skill-a", "skill-a"])
        bundles.scan_bundles()
        _, loaded, _ = bundles.build_bundle_invocation_message("/combo")
        assert loaded == ["skill-a"]


class TestSaveDeleteBundle:
    def test_save_creates_file(self, skills_home):
        path = bundles.save_bundle("test-bundle", ["s1", "s2"], description="d", instruction="i")
        assert path.exists()
        assert path.parent == paths.bundles_dir()
        content = path.read_text(encoding="utf-8")
        assert "test-bundle" in content
        assert "s1" in content and "s2" in content
        assert "description: d" in content

    def test_save_refuses_overwrite_by_default(self, skills_home):
        bundles.save_bundle("dup", ["s1"])
        with pytest.raises(FileExistsError):
            bundles.save_bundle("dup", ["s2"])

    def test_save_overwrites_with_force(self, skills_home):
        bundles.save_bundle("dup", ["s1"])
        bundles.save_bundle("dup", ["s2"], overwrite=True)
        info = bundles.get_bundle("dup")
        assert info is not None and info["skills"] == ["s2"]

    def test_save_requires_skills(self, skills_home):
        with pytest.raises(ValueError):
            bundles.save_bundle("empty", [])

    def test_save_requires_name(self, skills_home):
        with pytest.raises(ValueError):
            bundles.save_bundle("", ["s1"])

    def test_delete_removes(self, skills_home):
        bundles.save_bundle("doomed", ["s1"])
        assert bundles.get_bundle("doomed") is not None
        bundles.delete_bundle("doomed")
        assert bundles.get_bundle("doomed") is None

    def test_delete_missing_raises(self, skills_home):
        with pytest.raises(FileNotFoundError):
            bundles.delete_bundle("ghost")

    def test_bundle_path_for(self, skills_home):
        assert bundles.bundle_path_for("My Bundle") == paths.bundles_dir() / "my-bundle.yaml"

    def test_bundle_path_for_empty_raises(self, skills_home):
        with pytest.raises(ValueError):
            bundles.bundle_path_for("!!!")


class TestReloadAndList:
    def test_reload_reports_added_removed(self, skills_home, make_bundle):
        make_bundle("old", ["s1"])
        bundles.scan_bundles()  # 缓存 {old}
        # 直接改盘（不经 save/delete，避免中途刷新缓存）。
        (paths.bundles_dir() / "old.yaml").unlink()
        (paths.bundles_dir() / "new.yaml").write_text(
            "name: new\nskills:\n  - s2\n", encoding="utf-8"
        )
        diff = bundles.reload_bundles()
        added = {e["name"] for e in diff["added"]}
        removed = {e["name"] for e in diff["removed"]}
        assert "new" in added
        assert "old" in removed
        assert diff["total"] == 1

    def test_list_sorted_by_slug(self, skills_home, make_bundle):
        make_bundle("zebra", ["s1"])
        make_bundle("apple", ["s2"])
        make_bundle("mango", ["s3"])
        bundles.scan_bundles()
        slugs = [b["slug"] for b in bundles.list_bundles()]
        assert slugs == sorted(slugs)
