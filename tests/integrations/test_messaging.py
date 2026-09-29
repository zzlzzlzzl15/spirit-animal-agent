"""消息集成测试 —— Telegram 发送 + MessageRouter 跨平台路由。"""

from .conftest import FakeTransport, json_response

from spirit.integrations.base import IntegrationResult
from spirit.integrations.messaging import (
    MessageRouter,
    TelegramIntegration,
    build_telegram_url,
)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def test_build_telegram_url():
    url = build_telegram_url("TOK", "sendMessage")
    assert url == "https://api.telegram.org/botTOK/sendMessage"


def test_telegram_not_configured():
    t = FakeTransport()
    integ = TelegramIntegration(transport=t, env={})
    assert integ.send_message("1", "hi").ok is False
    assert t.calls == []


def test_telegram_send_ok():
    t = FakeTransport([json_response({"ok": True})])
    integ = TelegramIntegration(transport=t, env={"TELEGRAM_BOT_TOKEN": "TG"})
    res = integ.send_message("chat1", "hello")
    assert res.ok is True
    assert res.data["platform"] == "telegram"
    assert t.last_call["url"].endswith("/botTG/sendMessage")
    assert t.last_call["json_body"] == {"chat_id": "chat1", "text": "hello"}


def test_telegram_requires_args():
    integ = TelegramIntegration(transport=FakeTransport(), env={"TELEGRAM_BOT_TOKEN": "TG"})
    assert integ.send_message("", "x").ok is False
    assert integ.send_message("c", "").ok is False


def test_telegram_error_status():
    t = FakeTransport([json_response({}, status=400)])
    integ = TelegramIntegration(transport=t, env={"TELEGRAM_BOT_TOKEN": "TG"})
    assert integ.send_message("c", "x").ok is False


# --------------------------------------------------------------------------
# MessageRouter
# --------------------------------------------------------------------------

class _FakeSender:
    def __init__(self, configured=True, ok=True, raises=False):
        self._configured = configured
        self._ok = ok
        self._raises = raises
        self.sent = []

    def is_configured(self):
        return self._configured

    def send_message(self, to, message):
        if self._raises:
            raise RuntimeError("send boom")
        self.sent.append((to, message))
        if self._ok:
            return IntegrationResult.success({"sent": True})
        return IntegrationResult.failure("nope")


def test_router_available_platforms():
    r = MessageRouter({
        "telegram": _FakeSender(configured=True),
        "discord": _FakeSender(configured=False),
        "slack": _FakeSender(configured=True),
    })
    assert r.available_platforms() == ["slack", "telegram"]


def test_router_send_explicit_platform():
    tg = _FakeSender()
    r = MessageRouter({"telegram": tg})
    res = r.send("c", "hi", platform="telegram")
    assert res.ok is True
    assert tg.sent == [("c", "hi")]


def test_router_send_unknown_platform():
    r = MessageRouter({"telegram": _FakeSender()})
    assert r.send("c", "hi", platform="nope").ok is False


def test_router_send_picks_first_configured():
    tg = _FakeSender(configured=True)
    dc = _FakeSender(configured=False)
    r = MessageRouter({"telegram": tg, "discord": dc})
    res = r.send("c", "hi")
    assert res.ok is True
    assert tg.sent == [("c", "hi")]
    assert dc.sent == []


def test_router_falls_through_on_failure():
    first = _FakeSender(configured=True, ok=False)
    second = _FakeSender(configured=True, ok=True)
    # available_platforms 排序：a_first < b_second
    r = MessageRouter({"a_first": first, "b_second": second})
    res = r.send("c", "hi")
    assert res.ok is True
    assert second.sent == [("c", "hi")]


def test_router_no_platform_available():
    r = MessageRouter({"telegram": _FakeSender(configured=False)})
    res = r.send("c", "hi")
    assert res.ok is False
    assert "没有可用的消息平台" in res.error


def test_router_requires_args():
    r = MessageRouter({"telegram": _FakeSender()})
    assert r.send("", "hi").ok is False
    assert r.send("c", "").ok is False


def test_router_sender_exception_caught():
    r = MessageRouter({"telegram": _FakeSender(raises=True)})
    res = r.send("c", "hi", platform="telegram")
    assert res.ok is False
    assert "send boom" in res.error


def test_router_add():
    r = MessageRouter()
    r.add("telegram", _FakeSender())
    assert r.available_platforms() == ["telegram"]
