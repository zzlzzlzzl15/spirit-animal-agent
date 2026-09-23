"""tests/skills_hub/test_dispatch.py — 传输无关的 /skill 命令分发层。

对标 Hermes ``tests/agent/test_skill_commands.py`` 的会话内命令管理 + ``tools/skills_hub.py``
的市场操作，验证 Spirit 把「解析 + 派发 + 结果」从 UI 剥离后吐出的结构化 dict：
``handle_skill_command`` 各子命令（list/reload/browse/install/uninstall/scan/bundles/info/
audit/help/unknown）的 ok/action/message/lines/status_line 语义，以及
``resolve_slash_skill_or_bundle`` 的捆绑优先、堆叠调用、未知回退与 ``RESERVED_SKILL_COMMANDS``。

市场流走 :mod:`spirit.skills_hub.hub` 的离线 fetch seam，无需真实终端 / LLM / 网络。
"""

from __future__ import annotations

from spirit.skills_hub import commands, hub, paths
from spirit.skills_hub.dispatch import (
    RESERVED_SKILL_COMMANDS,
    handle_skill_command,
    resolve_slash_skill_or_bundle,
    skill_usage,
)
from tests.skills_hub.conftest import make_market_bundle

RESULT_KEYS = {"ok", "action", "message", "lines", "status_line", "response"}


def _assert_shape(result):
    assert isinstance(result, dict)
    assert set(result) == RESULT_KEYS
    assert isinstance(result["ok"], bool)
    assert isinstance(result["lines"], list)
    assert result["response"] is None  # 技能命令不产 LLM 响应


# ---------------------------------------------------------------------------
# RESERVED_SKILL_COMMANDS
# ---------------------------------------------------------------------------

class TestReserved:
    def test_covers_core_commands(self):
        for name in ("help", "new", "model", "status", "tools", "goal", "moa", "skill", "quit"):
            assert name in RESERVED_SKILL_COMMANDS

    def test_is_frozenset(self):
        assert isinstance(RESERVED_SKILL_COMMANDS, frozenset)


# ---------------------------------------------------------------------------
# list / help / unknown
# ---------------------------------------------------------------------------

class TestListHelpUnknown:
    def test_bare_skill_lists(self, skills_home, make_skill):
        make_skill("alpha")
        result = handle_skill_command(None, "")
        _assert_shape(result)
        assert result["ok"] is True
        assert result["action"] == "list"
        assert any("/alpha" in ln for ln in result["lines"])

    def test_list_alias(self, skills_home):
        assert handle_skill_command(None, "list")["action"] == "list"
        assert handle_skill_command(None, "ls")["action"] == "list"

    def test_empty_skills_hint(self, skills_home):
        result = handle_skill_command(None, "")
        assert result["ok"] is True
        assert "没有可用技能" in result["message"]

    def test_status_line_format(self, skills_home, make_skill):
        make_skill("alpha")
        result = handle_skill_command(None, "")
        assert result["status_line"].startswith("技能中心:")
        assert "个可用技能" in result["status_line"]

    def test_help(self, skills_home):
        for verb in ("help", "-h", "--help"):
            result = handle_skill_command(None, verb)
            assert result["ok"] is True
            assert result["action"] == "help"
            assert "Usage:" in result["message"]

    def test_unknown_subcommand(self, skills_home):
        result = handle_skill_command(None, "frobnicate now")
        _assert_shape(result)
        assert result["ok"] is False
        assert result["action"] == "unknown"
        assert "frobnicate" in result["message"]
        assert result["lines"]  # 附带用法提示

    def test_skill_usage_mentions_subcommands(self):
        text = skill_usage()
        for token in ("install", "uninstall", "scan", "browse", "audit"):
            assert token in text


# ---------------------------------------------------------------------------
# reload
# ---------------------------------------------------------------------------

class TestReload:
    def test_reload_reports(self, skills_home, make_skill):
        make_skill("alpha")
        commands.scan_skill_commands()  # before 快照
        result = handle_skill_command(None, "reload")
        _assert_shape(result)
        assert result["ok"] is True
        assert result["action"] == "reload"
        assert "重扫" in result["message"]


# ---------------------------------------------------------------------------
# browse
# ---------------------------------------------------------------------------

class TestBrowse:
    def test_browse_empty(self, skills_home):
        result = handle_skill_command(None, "browse")
        assert result["ok"] is True
        assert result["action"] == "browse"
        assert "没有可浏览" in result["message"]

    def test_browse_lists_cached_index(self, skills_home):
        hub.write_index_cache("index-community", [{"name": "pdf-kit", "description": "work with pdf"}])
        result = handle_skill_command(None, "browse")
        assert result["ok"] is True
        assert any("pdf-kit" in ln for ln in result["lines"])

    def test_browse_with_query_searches(self, skills_home):
        hub.write_index_cache("index-community", [
            {"name": "pdf-kit", "description": "pdf stuff"},
            {"name": "img-gen", "description": "images"},
        ])
        result = handle_skill_command(None, "browse pdf")
        assert result["ok"] is True
        assert any("pdf-kit" in ln for ln in result["lines"])
        assert not any("img-gen" in ln for ln in result["lines"])


# ---------------------------------------------------------------------------
# install / uninstall
# ---------------------------------------------------------------------------

class TestInstallUninstall:
    def test_install_requires_identifier(self, skills_home):
        result = handle_skill_command(None, "install")
        assert result["ok"] is False
        assert result["action"] == "install"
        assert "用法" in result["message"]

    def test_install_no_source_fails(self, skills_home):
        result = handle_skill_command(None, "install owner/thing")
        assert result["ok"] is False

    def test_install_via_fetcher(self, skills_home):
        hub.set_fetcher(lambda ident, source: make_market_bundle("fetched", identifier=ident))
        result = handle_skill_command(None, "install owner/fetched")
        assert result["ok"] is True
        assert (paths.skills_dir() / "fetched").exists()
        # 安装后新技能立即可经 /<name> 解析（缓存已失效重扫）。
        assert commands.resolve_skill_command_key("fetched") == "/fetched"

    def test_uninstall_requires_name(self, skills_home):
        result = handle_skill_command(None, "uninstall")
        assert result["ok"] is False
        assert "用法" in result["message"]

    def test_uninstall_market_skill(self, skills_home):
        hub.install_bundle(make_market_bundle("to-remove"))
        result = handle_skill_command(None, "uninstall to-remove")
        assert result["ok"] is True
        assert not (paths.skills_dir() / "to-remove").exists()

    def test_uninstall_non_market(self, skills_home, make_skill):
        make_skill("local-only")
        result = handle_skill_command(None, "uninstall local-only")
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

class TestScan:
    def test_scan_named_skill(self, skills_home, make_skill):
        make_skill("writer", body="Just prose, no code.")
        result = handle_skill_command(None, "scan writer")
        _assert_shape(result)
        assert result["action"] == "scan"
        assert result["ok"] is True  # safe → 非 dangerous
        assert "SAFE" in result["message"]

    def test_scan_named_dangerous(self, skills_home):
        d = paths.skills_dir() / "evil"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: evil\n---\nbody\n", encoding="utf-8")
        (d / "run.py").write_text("eval('boom')\n", encoding="utf-8")
        commands.invalidate_skill_commands()
        result = handle_skill_command(None, "scan evil")
        assert result["ok"] is False
        assert "DANGEROUS" in result["message"]

    def test_scan_missing_skill(self, skills_home):
        result = handle_skill_command(None, "scan ghost")
        assert result["ok"] is False
        assert "未找到" in result["message"]

    def test_scan_all(self, skills_home, make_skill):
        make_skill("a", body="prose")
        make_skill("b", body="prose")
        result = handle_skill_command(None, "scan")
        assert result["action"] == "scan"
        assert result["ok"] is True
        assert "已扫描" in result["message"]

    def test_scan_all_empty(self, skills_home):
        result = handle_skill_command(None, "scan")
        assert result["ok"] is True
        assert "没有可扫描" in result["message"]


# ---------------------------------------------------------------------------
# bundles / info / audit
# ---------------------------------------------------------------------------

class TestBundlesInfoAudit:
    def test_bundles_empty(self, skills_home):
        result = handle_skill_command(None, "bundles")
        assert result["ok"] is True
        assert result["action"] == "bundles"
        assert "没有捆绑包" in result["message"]

    def test_bundles_lists(self, skills_home, make_bundle):
        make_bundle("combo", ["skill-a", "skill-b"])
        result = handle_skill_command(None, "bundles")
        assert any("/combo" in ln for ln in result["lines"])

    def test_info_requires_name(self, skills_home):
        result = handle_skill_command(None, "info")
        assert result["ok"] is False
        assert "用法" in result["message"]

    def test_info_missing(self, skills_home):
        result = handle_skill_command(None, "info ghost")
        assert result["ok"] is False
        assert "未找到" in result["message"]

    def test_info_shows_metadata_and_usage(self, skills_home, make_skill):
        make_skill("writer", description="Writes things.", body="prose")
        commands.build_skill_invocation_message("/writer")  # 触发一次使用统计
        result = handle_skill_command(None, "info writer")
        _assert_shape(result)
        assert result["ok"] is True
        joined = "\n".join(result["lines"])
        assert "Writes things." in joined
        assert "使用" in joined
        assert "来源" in joined

    def test_audit_empty(self, skills_home):
        result = handle_skill_command(None, "audit")
        assert result["ok"] is True
        assert result["action"] == "audit"
        assert "审计日志为空" in result["message"]

    def test_audit_shows_records(self, skills_home):
        hub.append_audit_log("INSTALL", "s", verdict="safe")
        result = handle_skill_command(None, "audit")
        assert result["ok"] is True
        assert any("INSTALL" in ln for ln in result["lines"])


# ---------------------------------------------------------------------------
# resolve_slash_skill_or_bundle
# ---------------------------------------------------------------------------

class TestResolveSlash:
    def test_empty_command(self, skills_home):
        assert resolve_slash_skill_or_bundle("") is None
        assert resolve_slash_skill_or_bundle("   ") is None

    def test_unknown_command(self, skills_home, make_skill):
        make_skill("known")
        commands.scan_skill_commands()
        assert resolve_slash_skill_or_bundle("nope") is None

    def test_resolves_skill(self, skills_home, make_skill):
        make_skill("writer", body="Write carefully.")
        commands.scan_skill_commands()
        msg = resolve_slash_skill_or_bundle("writer")
        assert msg is not None
        assert "Write carefully." in msg

    def test_leading_slash_stripped(self, skills_home, make_skill):
        make_skill("writer", body="Body here.")
        commands.scan_skill_commands()
        msg = resolve_slash_skill_or_bundle("/writer")
        assert msg is not None and "Body here." in msg

    def test_user_instruction_forwarded(self, skills_home, make_skill):
        make_skill("writer")
        commands.scan_skill_commands()
        msg = resolve_slash_skill_or_bundle("writer", "do XYZ now")
        assert msg is not None and "do XYZ now" in msg

    def test_bundle_priority_over_skill(self, skills_home, make_skill, make_bundle):
        # 同名技能与捆绑并存 → 捆绑胜出。
        make_skill("research", body="SKILL body research.")
        make_skill("member", body="Member body.")
        make_bundle("research", ["member"])
        commands.scan_skill_commands()
        msg = resolve_slash_skill_or_bundle("research")
        assert msg is not None
        assert "Member body." in msg
        assert "SKILL body research." not in msg

    def test_stacked_skills(self, skills_home, make_skill):
        make_skill("skill-a", body="A body.")
        make_skill("skill-b", body="B body.")
        commands.scan_skill_commands()
        msg = resolve_slash_skill_or_bundle("skill-a", "/skill-b run the pipeline")
        assert msg is not None
        assert "A body." in msg and "B body." in msg
        assert "run the pipeline" in msg
