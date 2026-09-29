"""Discord 集成测试 —— 请求构造 / 响应解析 / 未配置降级（FakeTransport）。"""

from .conftest import FakeTransport, json_response

from spirit.integrations.discord import DiscordIntegration


def _discord(env, responses=None):
    t = FakeTransport(responses)
    return DiscordIntegration(transport=t, env=env), t


def test_not_configured_fails_without_network():
    integ, t = _discord({})
    res = integ.list_guilds()
    assert res.ok is False
    assert "DISCORD_BOT_TOKEN" in res.error
    assert t.calls == []  # 未配置绝不发请求


def test_list_guilds_builds_auth_header():
    integ, t = _discord({"DISCORD_BOT_TOKEN": "tok"}, [json_response([{"id": "1"}])])
    res = integ.list_guilds()
    assert res.ok is True
    assert res.data["guilds"] == [{"id": "1"}]
    call = t.last_call
    assert call["url"] == "https://discord.com/api/v10/users/@me/guilds"
    assert call["headers"]["Authorization"] == "Bot tok"


def test_list_guilds_applies_limit():
    integ, _ = _discord(
        {"DISCORD_BOT_TOKEN": "t"},
        [json_response([{"id": str(i)} for i in range(10)])],
    )
    res = integ.list_guilds(limit=3)
    assert len(res.data["guilds"]) == 3


def test_list_channels_requires_guild_id():
    integ, _ = _discord({"DISCORD_BOT_TOKEN": "t"})
    assert integ.list_channels("").ok is False


def test_list_channels_ok():
    integ, t = _discord({"DISCORD_BOT_TOKEN": "t"}, [json_response([{"id": "c1"}])])
    res = integ.list_channels("g1")
    assert res.ok is True
    assert t.last_call["url"].endswith("/guilds/g1/channels")


def test_fetch_messages_caps_limit_param():
    integ, t = _discord({"DISCORD_BOT_TOKEN": "t"}, [json_response([])])
    integ.fetch_messages("ch1", limit=500)
    assert t.last_call["params"] == {"limit": 100}


def test_send_message_posts_content():
    integ, t = _discord({"DISCORD_BOT_TOKEN": "t"}, [json_response({"id": "m1"})])
    res = integ.send_message("ch1", "hello")
    assert res.ok is True
    assert res.data["sent"] is True
    assert t.last_call["method"] == "POST"
    assert t.last_call["json_body"] == {"content": "hello"}


def test_send_message_requires_args():
    integ, _ = _discord({"DISCORD_BOT_TOKEN": "t"})
    assert integ.send_message("", "x").ok is False
    assert integ.send_message("ch", "").ok is False


def test_api_error_surfaces_message():
    integ, _ = _discord({"DISCORD_BOT_TOKEN": "t"}, [json_response({"e": 1}, status=403)])
    res = integ.list_guilds()
    assert res.ok is False
    assert "403" in res.error


def test_transport_exception_caught():
    integ, _ = _discord({"DISCORD_BOT_TOKEN": "t"}, [RuntimeError("boom")])
    res = integ.list_guilds()
    assert res.ok is False
    assert "boom" in res.error


def test_204_no_content_handled():
    from spirit.integrations.base import HttpResponse
    integ, _ = _discord({"DISCORD_BOT_TOKEN": "t"}, [HttpResponse(status=204)])
    res = integ.list_guilds()
    assert res.ok is True
    assert res.data["guilds"] is None
