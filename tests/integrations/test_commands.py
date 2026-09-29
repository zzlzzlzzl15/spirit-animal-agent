"""``/integrations`` 命令分发 + 注册表单例测试。"""

import spirit.integrations as integ_pkg
from spirit.integrations.base import IntegrationRegistry
from spirit.integrations.commands import handle_integrations_command
from spirit.integrations.discord import DiscordIntegration
from spirit.integrations.homeassistant import HomeAssistantIntegration


def _registry(env=None):
    reg = IntegrationRegistry()
    reg.register(DiscordIntegration(env=env or {}))
    reg.register(HomeAssistantIntegration(env=env or {}))
    return reg


# --------------------------------------------------------------------------
# 默认注册表 / 单例
# --------------------------------------------------------------------------

def test_build_default_registry_has_all():
    reg = integ_pkg.build_default_registry()
    names = reg.names()
    for expected in ("discord", "homeassistant", "telegram", "x_search",
                     "feishu", "yuanbao"):
        assert expected in names


def test_build_default_registry_env_injection():
    reg = integ_pkg.build_default_registry(env={"DISCORD_BOT_TOKEN": "x"})
    assert reg.get("discord").is_configured() is True


def test_get_registry_singleton_and_reset():
    integ_pkg.reset_registry()
    r1 = integ_pkg.get_registry()
    r2 = integ_pkg.get_registry()
    assert r1 is r2
    integ_pkg.reset_registry()
    r3 = integ_pkg.get_registry()
    assert r3 is not r1
    integ_pkg.reset_registry()


# --------------------------------------------------------------------------
# 命令：list
# --------------------------------------------------------------------------

def test_list_all():
    res = handle_integrations_command("list", _registry())
    assert res["ok"] is True
    assert res["action"] == "list"
    assert len(res["lines"]) == 2


def test_list_default_verb_when_empty_arg():
    res = handle_integrations_command("", _registry())
    assert res["action"] == "list"


def test_list_by_category():
    res = handle_integrations_command("list smart_home", _registry())
    assert res["ok"] is True
    assert len(res["lines"]) == 1
    assert "Home Assistant" in res["lines"][0]


def test_list_unknown_category_empty():
    res = handle_integrations_command("list nonexistent_cat", _registry())
    assert res["ok"] is True
    assert res["lines"] == []


def test_list_alias_ls():
    assert handle_integrations_command("ls", _registry())["action"] == "list"


def test_list_shows_configured_count():
    res = handle_integrations_command("list", _registry({"DISCORD_BOT_TOKEN": "x"}))
    assert "1 个已配置" in res["message"]


# --------------------------------------------------------------------------
# 命令：status
# --------------------------------------------------------------------------

def test_status_overview():
    res = handle_integrations_command("status", _registry())
    assert res["ok"] is True
    assert len(res["lines"]) >= 2


def test_status_single():
    res = handle_integrations_command("status discord", _registry())
    assert res["ok"] is True
    assert res["integration"]["name"] == "discord"


def test_status_shows_missing():
    res = handle_integrations_command("status discord", _registry())
    assert "DISCORD_BOT_TOKEN" in res["integration"]["missing"]


def test_status_unknown_integration():
    res = handle_integrations_command("status nope", _registry())
    assert res["ok"] is False


# --------------------------------------------------------------------------
# 命令：test
# --------------------------------------------------------------------------

def test_test_configured_passes():
    res = handle_integrations_command(
        "test discord", _registry({"DISCORD_BOT_TOKEN": "x"})
    )
    assert res["ok"] is True
    assert res["result"]["ok"] is True


def test_test_unconfigured_fails():
    res = handle_integrations_command("test discord", _registry())
    assert res["ok"] is False
    assert res["result"]["ok"] is False


def test_test_requires_name():
    res = handle_integrations_command("test", _registry())
    assert res["ok"] is False


def test_test_unknown_integration():
    res = handle_integrations_command("test nope", _registry())
    assert res["ok"] is False


def test_test_alias_check():
    res = handle_integrations_command(
        "check discord", _registry({"DISCORD_BOT_TOKEN": "x"})
    )
    assert res["action"] == "test"


# --------------------------------------------------------------------------
# 命令：help / unknown
# --------------------------------------------------------------------------

def test_help():
    res = handle_integrations_command("help", _registry())
    assert res["ok"] is True
    assert res["action"] == "help"
    assert any("/integrations list" in ln for ln in res["lines"])


def test_unknown_verb():
    res = handle_integrations_command("frobnicate", _registry())
    assert res["ok"] is False
    assert res["action"] == "unknown"


def test_command_never_raises():
    """即使注册表损坏，命令层也应捕获并返回结构化错误。"""

    class BadRegistry:
        def list(self, *a, **k):
            raise RuntimeError("boom")

        def categories(self):
            return []

    res = handle_integrations_command("list", BadRegistry())
    assert res["ok"] is False
    assert "执行失败" in res["message"]
