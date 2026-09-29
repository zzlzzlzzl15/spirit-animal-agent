"""集成测试共享 fixture —— FakeTransport（注入式 HTTP seam）+ env 映射。"""

import json as _json

import pytest

from spirit.integrations.base import HttpResponse


def json_response(obj, status=200, headers=None):
    """构造一个 body 为 JSON 的 HttpResponse。"""
    return HttpResponse(
        status=status,
        body=_json.dumps(obj).encode("utf-8"),
        headers=headers or {"Content-Type": "application/json"},
    )


class FakeTransport:
    """记录调用、回放预置响应的假 transport。"""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def request(self, method, url, *, headers=None, json_body=None,
                params=None, timeout=15.0):
        self.calls.append({
            "method": method, "url": url, "headers": headers,
            "json_body": json_body, "params": params, "timeout": timeout,
        })
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return HttpResponse(status=200, body=b"{}", headers={})

    @property
    def last_call(self):
        return self.calls[-1] if self.calls else None


@pytest.fixture
def fake_transport():
    return FakeTransport()


@pytest.fixture
def discord_env():
    return {"DISCORD_BOT_TOKEN": "bot-token-xyz"}


@pytest.fixture
def ha_env():
    return {"HASS_TOKEN": "ha-token", "HASS_URL": "http://ha.local:8123"}


@pytest.fixture
def telegram_env():
    return {"TELEGRAM_BOT_TOKEN": "tg-token"}


@pytest.fixture
def xai_env():
    return {"XAI_API_KEY": "xai-key"}
