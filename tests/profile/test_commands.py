"""tests/profile/test_commands.py — /profile 命令分发层。

覆盖 ``spirit.profile.commands.handle_profile_command``：list/current/create/use/
delete/rename/info/describe 各子命令的解析与结果 dict 契约、参数错误路径、
未知子命令、以及 describe 经 agent 注入 llm_caller 的成功/降级路径。全部离线。
"""

from __future__ import annotations

from spirit.profile.commands import _parse_flags, handle_profile_command
from spirit.profile.manager import create_profile, get_active_profile, profile_exists


# ---------------------------------------------------------------------------
# _parse_flags
# ---------------------------------------------------------------------------

def test_parse_flags_positional_and_bool():
    parsed = _parse_flags(["work", "--clone-all"])
    assert parsed["positional"] == ["work"]
    assert parsed["flags"]["clone-all"] is True


def test_parse_flags_value():
    parsed = _parse_flags(["job", "--from", "work", "--desc", "hello"])
    assert parsed["positional"] == ["job"]
    assert parsed["flags"]["from"] == "work"
    assert parsed["flags"]["desc"] == "hello"


def test_parse_flags_missing_value():
    parsed = _parse_flags(["job", "--desc"])
    assert parsed["flags"]["desc"] == ""


# ---------------------------------------------------------------------------
# list / current
# ---------------------------------------------------------------------------

def test_bare_command_lists(profile_home):
    r = handle_profile_command(None, "")
    assert r["ok"] is True
    assert r["action"] == "list"


def test_list_shows_default(profile_home):
    r = handle_profile_command(None, "list")
    assert r["ok"] is True
    assert any("default" in ln for ln in r["lines"])


def test_list_after_create(profile_home):
    create_profile("work")
    r = handle_profile_command(None, "list")
    assert any("work" in ln for ln in r["lines"])


def test_current(profile_home):
    r = handle_profile_command(None, "current")
    assert r["ok"] is True
    assert r["action"] == "current"
    assert r["active"] == "default"


def test_unknown_subcommand(profile_home):
    r = handle_profile_command(None, "frobnicate")
    assert r["ok"] is False
    assert r["action"] == "unknown"


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def test_create(profile_home):
    r = handle_profile_command(None, "create work")
    assert r["ok"] is True
    assert r["action"] == "create"
    assert profile_exists("work")


def test_create_with_desc(profile_home):
    r = handle_profile_command(None, 'create work --desc 工作身份')
    assert r["ok"] is True
    assert profile_exists("work")


def test_create_clone_from(profile_home):
    create_profile("work")
    r = handle_profile_command(None, "create job --from work --clone-config")
    assert r["ok"] is True
    assert profile_exists("job")


def test_create_missing_name(profile_home):
    r = handle_profile_command(None, "create")
    assert r["ok"] is False


def test_create_duplicate(profile_home):
    create_profile("work")
    r = handle_profile_command(None, "create work")
    assert r["ok"] is False
    assert "已存在" in r["message"]


# ---------------------------------------------------------------------------
# use
# ---------------------------------------------------------------------------

def test_use_switches_active(profile_home):
    create_profile("work")
    r = handle_profile_command(None, "use work")
    assert r["ok"] is True
    assert r["action"] == "use"
    assert r["active"] == "work"
    assert r["restart_hint"] is True
    assert get_active_profile() == "work"


def test_use_default(profile_home):
    r = handle_profile_command(None, "use default")
    assert r["ok"] is True
    assert get_active_profile() == "default"


def test_use_missing(profile_home):
    r = handle_profile_command(None, "use ghost")
    assert r["ok"] is False


def test_use_no_arg(profile_home):
    r = handle_profile_command(None, "use")
    assert r["ok"] is False


# ---------------------------------------------------------------------------
# delete / rename / info
# ---------------------------------------------------------------------------

def test_delete(profile_home):
    create_profile("work")
    r = handle_profile_command(None, "delete work --yes")
    assert r["ok"] is True
    assert not profile_exists("work")


def test_delete_default_rejected(profile_home):
    r = handle_profile_command(None, "delete default")
    assert r["ok"] is False


def test_rename(profile_home):
    create_profile("work")
    r = handle_profile_command(None, "rename work job")
    assert r["ok"] is True
    assert profile_exists("job")
    assert not profile_exists("work")


def test_rename_missing_arg(profile_home):
    r = handle_profile_command(None, "rename work")
    assert r["ok"] is False


def test_info(profile_home):
    create_profile("work")
    r = handle_profile_command(None, "info work")
    assert r["ok"] is True
    assert r["action"] == "info"
    assert any("路径" in ln for ln in r["lines"])


def test_info_missing_profile(profile_home):
    r = handle_profile_command(None, "info ghost")
    assert r["ok"] is False


# ---------------------------------------------------------------------------
# describe（经 agent 注入 llm_caller）
# ---------------------------------------------------------------------------

class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, content):
        self._content = content

    def create(self, **kwargs):
        return _FakeResponse(self._content)


class _FakeChat:
    def __init__(self, content):
        self.completions = _FakeCompletions(content)


class FakeAgent:
    """最小 SpiritAgent 桩：提供 build_agent_llm_caller 所需的 client/model。"""

    def __init__(self, content='{"description": "auto from agent"}'):
        self.model = "test-model"
        self.client = type("C", (), {})()
        self.client.chat = _FakeChat(content)


def test_describe_via_agent(profile_home):
    create_profile("work")
    agent = FakeAgent('{"description": "auto from agent"}')
    r = handle_profile_command(agent, "describe work")
    assert r["ok"] is True
    assert r["action"] == "describe"
    assert any("auto from agent" in ln for ln in r["lines"])


def test_describe_no_agent_degrades(profile_home):
    """agent=None → 无 llm_caller → 优雅失败，不抛。"""
    create_profile("work")
    r = handle_profile_command(None, "describe work")
    assert r["ok"] is False
    assert "no llm_caller" in r["message"] or "unavailable" in r["message"]


def test_describe_missing_name(profile_home):
    r = handle_profile_command(None, "describe")
    assert r["ok"] is False


def test_describe_overwrite_flag(profile_home):
    create_profile("work", description="手写")
    agent = FakeAgent('{"description": "auto wins"}')
    # 不带 --overwrite → 保护手写描述
    r1 = handle_profile_command(agent, "describe work")
    assert r1["ok"] is False
    # 带 --overwrite → 替换
    r2 = handle_profile_command(agent, "describe work --overwrite")
    assert r2["ok"] is True
