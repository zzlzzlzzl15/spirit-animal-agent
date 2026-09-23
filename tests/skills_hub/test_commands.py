"""tests/skills_hub/test_commands.py — 技能 slash 命令派发。

对标 Hermes ``tests/agent/test_skill_commands.py``：slug 归一化、扫描（平台 / 环境 / 禁用门、
保留命令避让、slug 冲突首个胜出、分类子目录、空 slug 跳过）、命令键解析（连字符 / 下划线
互通）、单技能调用消息构建（激活提示 + 技能目录头 + 支持文件 + 用户指令 + bump_use）、
会话级预加载、堆叠调用（至多 5、去重、指令回收）。
"""

from __future__ import annotations

import pytest

from spirit.skills_hub import commands, discovery, usage
from spirit.skills_hub.dispatch import RESERVED_SKILL_COMMANDS
from spirit.skills_hub import paths
from tests.skills_hub.conftest import write_skills_config


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------

class TestSlugify:
    def test_basic(self):
        assert commands.slugify("Backend Dev") == "backend-dev"

    def test_underscores(self):
        assert commands.slugify("backend_dev") == "backend-dev"

    def test_strips_invalid(self):
        assert commands.slugify("hello, world!") == "hello-world"

    def test_collapses_hyphens(self):
        assert commands.slugify("a--b---c") == "a-b-c"

    def test_strips_edge_hyphens(self):
        assert commands.slugify("--foo--") == "foo"

    def test_empty(self):
        assert commands.slugify("") == ""
        assert commands.slugify("!!!") == ""


# ---------------------------------------------------------------------------
# scan_skill_commands
# ---------------------------------------------------------------------------

class TestScanSkillCommands:
    def test_empty_dir(self, skills_home):
        assert commands.scan_skill_commands() == {}

    def test_finds_skills(self, skills_home, make_skill):
        make_skill("alpha")
        make_skill("beta")
        cmds = commands.scan_skill_commands()
        assert set(cmds) == {"/alpha", "/beta"}
        assert cmds["/alpha"]["name"] == "alpha"
        assert cmds["/alpha"]["skill_dir"] == str(paths.skills_dir() / "alpha")

    def test_description_from_frontmatter(self, skills_home, make_skill):
        make_skill("desc-skill", description="A helpful tool")
        cmds = commands.scan_skill_commands()
        assert cmds["/desc-skill"]["description"] == "A helpful tool"

    def test_description_falls_back_to_body(self, skills_home, make_skill):
        make_skill("nodesc", description="", body="First body line here.")
        cmds = commands.scan_skill_commands()
        assert cmds["/nodesc"]["description"] == "First body line here."

    def test_category_subdir_found(self, skills_home, make_skill):
        make_skill("nested", category="productivity")
        cmds = commands.scan_skill_commands()
        assert "/nested" in cmds

    def test_platform_gate(self, skills_home, make_skill, monkeypatch):
        make_skill("linux-only", frontmatter_extra="platforms:\n  - linux")
        make_skill("any-os")
        monkeypatch.setattr(discovery.sys, "platform", "win32")
        cmds = commands.scan_skill_commands()
        assert "/any-os" in cmds
        assert "/linux-only" not in cmds

    def test_disabled_gate(self, skills_home, make_skill):
        make_skill("enabled")
        make_skill("disabled")
        write_skills_config(skills_home, disabled=["disabled"])
        cmds = commands.scan_skill_commands()
        assert "/enabled" in cmds
        assert "/disabled" not in cmds

    def test_reserved_command_collision_skipped(self, skills_home, make_skill):
        make_skill("help")
        make_skill("real-skill")
        cmds = commands.scan_skill_commands()
        assert "/help" not in cmds
        assert "/real-skill" in cmds

    def test_reserved_set_covers_core_commands(self):
        for name in ("help", "status", "goal", "moa", "skill", "quit"):
            assert name in RESERVED_SKILL_COMMANDS

    def test_slug_collision_first_wins(self, skills_home, make_skill):
        # "web dev"(空格 0x20) 路径排序先于 "web-dev"(连字符 0x2D) → 首个胜出。
        make_skill("web dev")
        make_skill("web-dev")
        cmds = commands.scan_skill_commands()
        assert "/web-dev" in cmds
        assert cmds["/web-dev"]["name"] == "web dev"

    def test_empty_slug_skipped(self, skills_home):
        d = paths.skills_dir() / "weird"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            '---\nname: "!!!"\ndescription: d\n---\nbody\n', encoding="utf-8"
        )
        cmds = commands.scan_skill_commands()
        assert all(info["name"] != "!!!" for info in cmds.values())

    def test_get_skill_commands_caches(self, skills_home, make_skill):
        make_skill("cached")
        first = commands.get_skill_commands()
        second = commands.get_skill_commands()
        assert first is second

    def test_invalidate_forces_rescan(self, skills_home, make_skill):
        make_skill("s1")
        assert "/s1" in commands.get_skill_commands()
        make_skill("s2")  # make_skill 已 invalidate
        cmds = commands.get_skill_commands()
        assert "/s1" in cmds and "/s2" in cmds


# ---------------------------------------------------------------------------
# reload_skills
# ---------------------------------------------------------------------------

class TestReloadSkills:
    def test_reports_added_removed(self, skills_home, make_skill):
        make_skill("stale")
        commands.scan_skill_commands()  # 填充缓存 {stale} = before 快照
        # 直接改盘（不经 make_skill，避免中途失效缓存）：删 stale、加 fresh。
        (paths.skills_dir() / "stale" / "SKILL.md").unlink()
        fresh = paths.skills_dir() / "fresh"
        fresh.mkdir(parents=True)
        (fresh / "SKILL.md").write_text(
            "---\nname: fresh\ndescription: d\n---\nbody\n", encoding="utf-8"
        )
        diff = commands.reload_skills()
        added = {e["name"] for e in diff["added"]}
        removed = {e["name"] for e in diff["removed"]}
        assert "fresh" in added
        assert "stale" in removed
        assert diff["total"] == 1  # 只剩 "fresh"
        assert diff["total"] == diff["commands"]


# ---------------------------------------------------------------------------
# resolve_skill_command_key
# ---------------------------------------------------------------------------

class TestResolveKey:
    def test_hyphen_match(self, skills_home, make_skill):
        make_skill("my-skill")
        commands.scan_skill_commands()
        assert commands.resolve_skill_command_key("my-skill") == "/my-skill"

    def test_underscore_alias(self, skills_home, make_skill):
        make_skill("my-skill")
        commands.scan_skill_commands()
        assert commands.resolve_skill_command_key("my_skill") == "/my-skill"

    def test_unknown(self, skills_home, make_skill):
        make_skill("known")
        commands.scan_skill_commands()
        assert commands.resolve_skill_command_key("missing") is None

    def test_empty(self, skills_home):
        assert commands.resolve_skill_command_key("") is None


# ---------------------------------------------------------------------------
# build_skill_invocation_message
# ---------------------------------------------------------------------------

class TestBuildInvocation:
    def test_unknown_returns_none(self, skills_home):
        commands.scan_skill_commands()
        assert commands.build_skill_invocation_message("/nope") is None

    def test_contains_body_and_activation(self, skills_home, make_skill):
        make_skill("writer", body="Write the thing carefully.")
        commands.scan_skill_commands()
        msg = commands.build_skill_invocation_message("/writer")
        assert msg is not None
        assert "Write the thing carefully." in msg
        assert 'invoked the "writer" skill' in msg

    def test_skill_directory_header(self, skills_home, make_skill):
        d = make_skill("dirhead", body="body")
        commands.scan_skill_commands()
        msg = commands.build_skill_invocation_message("/dirhead")
        assert f"[Skill directory: {d}]" in msg

    def test_user_instruction_included(self, skills_home, make_skill):
        make_skill("instr", body="body")
        commands.scan_skill_commands()
        msg = commands.build_skill_invocation_message("/instr", user_instruction="do XYZ now")
        assert "do XYZ now" in msg

    def test_supporting_files_listed(self, skills_home, make_skill):
        make_skill("withsup", body="b", supporting={"scripts/run.py": "print(1)\n"})
        commands.scan_skill_commands()
        msg = commands.build_skill_invocation_message("/withsup")
        assert "supporting files" in msg.lower()
        assert "scripts/run.py" in msg

    def test_bumps_usage(self, skills_home, make_skill):
        make_skill("used", body="b")
        commands.scan_skill_commands()
        commands.build_skill_invocation_message("/used")
        rec = usage.get_usage("used")
        assert rec is not None and rec["use_count"] >= 1

    def test_template_vars_substituted(self, skills_home, make_skill):
        d = make_skill("tmpl", body="run ${SPIRIT_SKILL_DIR}/x.py")
        commands.scan_skill_commands()
        msg = commands.build_skill_invocation_message("/tmpl")
        assert str(d) in msg
        assert "${SPIRIT_SKILL_DIR}" not in msg


# ---------------------------------------------------------------------------
# build_preloaded_skills_prompt
# ---------------------------------------------------------------------------

class TestPreloaded:
    def test_loads_by_name(self, skills_home, make_skill):
        make_skill("pre", body="preloaded body")
        prompt, loaded, missing = commands.build_preloaded_skills_prompt(["pre"])
        assert loaded == ["pre"]
        assert missing == []
        assert "preloaded body" in prompt
        assert "preloaded" in prompt.lower()

    def test_missing_skill(self, skills_home, make_skill):
        make_skill("real")
        prompt, loaded, missing = commands.build_preloaded_skills_prompt(["real", "ghost"])
        assert loaded == ["real"]
        assert missing == ["ghost"]

    def test_disabled_treated_as_missing(self, skills_home, make_skill):
        make_skill("off-limits", body="secret")
        write_skills_config(skills_home, disabled=["off-limits"])
        prompt, loaded, missing = commands.build_preloaded_skills_prompt(["off-limits"])
        assert loaded == []
        assert "off-limits" in missing
        assert "secret" not in prompt

    def test_dedup(self, skills_home, make_skill):
        make_skill("dup")
        prompt, loaded, missing = commands.build_preloaded_skills_prompt(["dup", "dup"])
        assert loaded == ["dup"]

    def test_multiple(self, skills_home, make_skill):
        make_skill("a", body="AAA")
        make_skill("b", body="BBB")
        prompt, loaded, missing = commands.build_preloaded_skills_prompt(["a", "b"])
        assert set(loaded) == {"a", "b"}
        assert "AAA" in prompt and "BBB" in prompt


# ---------------------------------------------------------------------------
# 堆叠 slash 调用
# ---------------------------------------------------------------------------

class TestStacked:
    def test_split_consumes_leading_skills(self, skills_home, make_skill):
        make_skill("sa")
        make_skill("sb")
        commands.scan_skill_commands()
        keys, remaining = commands.split_stacked_skill_commands("/sb do the task")
        assert keys == ["/sb"]
        assert remaining == "do the task"

    def test_split_stops_at_non_skill(self, skills_home, make_skill):
        make_skill("sa")
        commands.scan_skill_commands()
        keys, remaining = commands.split_stacked_skill_commands("/unknown blah")
        assert keys == []
        assert remaining == "/unknown blah"

    def test_split_multiple(self, skills_home, make_skill):
        for n in ("s1", "s2", "s3"):
            make_skill(n)
        commands.scan_skill_commands()
        keys, remaining = commands.split_stacked_skill_commands("/s2 /s3 go")
        assert keys == ["/s2", "/s3"]
        assert remaining == "go"

    def test_split_caps_at_max(self, skills_home, make_skill):
        # 至多 _MAX_STACKED_SKILLS - 1 个额外 key。
        for i in range(commands._MAX_STACKED_SKILLS + 2):
            make_skill(f"cap{i}")
        commands.scan_skill_commands()
        rest = " ".join(f"/cap{i}" for i in range(commands._MAX_STACKED_SKILLS + 2))
        keys, _ = commands.split_stacked_skill_commands(rest)
        assert len(keys) == commands._MAX_STACKED_SKILLS - 1

    def test_split_dedup_stops(self, skills_home, make_skill):
        make_skill("dx")
        commands.scan_skill_commands()
        keys, remaining = commands.split_stacked_skill_commands("/dx /dx tail")
        # 第二次遇到同一 key 即停，剩余原样返回。
        assert keys == ["/dx"]
        assert remaining.startswith("/dx")

    def test_build_stacked_message(self, skills_home, make_skill):
        make_skill("one", body="BODY-ONE")
        make_skill("two", body="BODY-TWO")
        commands.scan_skill_commands()
        built = commands.build_stacked_skill_invocation_message(
            ["/one", "/two"], user_instruction="combine them"
        )
        assert built is not None
        msg, loaded, missing = built
        assert set(loaded) == {"one", "two"}
        assert missing == []
        assert "BODY-ONE" in msg and "BODY-TWO" in msg
        assert "stacked skill bundle" in msg
        assert "combine them" in msg

    def test_build_stacked_none_when_all_missing(self, skills_home):
        commands.scan_skill_commands()
        assert commands.build_stacked_skill_invocation_message(["/ghost"]) is None


# ---------------------------------------------------------------------------
# extract_user_instruction_from_skill_message
# ---------------------------------------------------------------------------

class TestExtractInstruction:
    def test_single_marker(self, skills_home, make_skill):
        make_skill("ex", body="b")
        commands.scan_skill_commands()
        msg = commands.build_skill_invocation_message("/ex", user_instruction="the real ask")
        assert commands.extract_user_instruction_from_skill_message(msg) == "the real ask"

    def test_bundle_marker(self, skills_home, make_skill):
        make_skill("bs", body="b")
        commands.scan_skill_commands()
        built = commands.build_stacked_skill_invocation_message(
            ["/bs"], user_instruction="bundled ask"
        )
        msg, _, _ = built
        assert commands.extract_user_instruction_from_skill_message(msg) == "bundled ask"

    def test_from_message_list(self, skills_home, make_skill):
        make_skill("ml", body="b")
        commands.scan_skill_commands()
        msg = commands.build_skill_invocation_message("/ml", user_instruction="list ask")
        content = [{"role": "user", "content": msg}]
        assert commands.extract_user_instruction_from_skill_message(content) == "list ask"

    def test_no_instruction_returns_none(self, skills_home, make_skill):
        make_skill("ni", body="b")
        commands.scan_skill_commands()
        msg = commands.build_skill_invocation_message("/ni")
        assert commands.extract_user_instruction_from_skill_message(msg) is None

    def test_non_message_returns_none(self):
        assert commands.extract_user_instruction_from_skill_message("plain text") is None


# ---------------------------------------------------------------------------
# _resolve_skill_dir
# ---------------------------------------------------------------------------

class TestResolveSkillDir:
    def test_by_relative_name(self, skills_home, make_skill):
        d = make_skill("direct")
        assert commands._resolve_skill_dir("direct") == d

    def test_by_leaf_name(self, skills_home, make_skill):
        d = make_skill("leafy", category="cat")
        assert commands._resolve_skill_dir("leafy") == d

    def test_missing_returns_none(self, skills_home):
        assert commands._resolve_skill_dir("nope") is None

    def test_empty_returns_none(self, skills_home):
        assert commands._resolve_skill_dir("") is None
