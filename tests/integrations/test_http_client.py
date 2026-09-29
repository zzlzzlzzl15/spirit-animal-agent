"""HTTP 传输测试 —— build_url / safe_json 纯函数 + UrllibTransport（打桩 urlopen）。"""

import io
import json

from spirit.integrations import http_client


# --------------------------------------------------------------------------
# build_url
# --------------------------------------------------------------------------

def test_build_url_base_only():
    assert http_client.build_url("http://x/api") == "http://x/api"


def test_build_url_joins_path():
    assert http_client.build_url("http://x/api/", "/v1") == "http://x/api/v1"
    assert http_client.build_url("http://x/api", "v1") == "http://x/api/v1"


def test_build_url_with_params():
    url = http_client.build_url("http://x", "/q", {"a": 1, "b": "two"})
    assert url.startswith("http://x/q?")
    assert "a=1" in url and "b=two" in url


def test_build_url_drops_none_params():
    url = http_client.build_url("http://x", "/q", {"a": 1, "b": None})
    assert "a=1" in url
    assert "b=" not in url


def test_build_url_appends_with_amp_when_query_exists():
    url = http_client.build_url("http://x?q=1", "", {"a": 2})
    assert url == "http://x?q=1&a=2"


def test_build_url_empty_params_no_question():
    assert http_client.build_url("http://x", "/q", {}) == "http://x/q"


# --------------------------------------------------------------------------
# safe_json
# --------------------------------------------------------------------------

def test_safe_json_from_str_and_bytes():
    assert http_client.safe_json('{"a": 1}') == {"a": 1}
    assert http_client.safe_json(b'{"a": 2}') == {"a": 2}


def test_safe_json_invalid_returns_default():
    assert http_client.safe_json("not json", default={"d": 1}) == {"d": 1}
    assert http_client.safe_json(None, default=[]) == []


def test_safe_json_passthrough_non_str():
    assert http_client.safe_json({"already": "dict"}) == {"already": "dict"}


# --------------------------------------------------------------------------
# UrllibTransport（打桩 urlopen）
# --------------------------------------------------------------------------

class _FakeResp(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_urllib_transport_get(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResp(b'{"ok": true}')

    monkeypatch.setattr(http_client.urllib.request, "urlopen", fake_urlopen)
    t = http_client.UrllibTransport()
    resp = t.request("GET", "http://x/api", params={"a": 1})
    assert resp.status == 200
    assert resp.json() == {"ok": True}
    assert captured["url"] == "http://x/api?a=1"
    assert captured["method"] == "GET"


def test_urllib_transport_post_json(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data
        captured["ct"] = req.get_header("Content-type")
        return _FakeResp(b"{}")

    monkeypatch.setattr(http_client.urllib.request, "urlopen", fake_urlopen)
    t = http_client.UrllibTransport()
    t.request("POST", "http://x", json_body={"k": "v"})
    assert json.loads(captured["data"].decode()) == {"k": "v"}
    assert captured["ct"] == "application/json"


def test_urllib_transport_http_error(monkeypatch):
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            "http://x", 404, "Not Found", {}, io.BytesIO(b'{"err":"nf"}')
        )

    monkeypatch.setattr(http_client.urllib.request, "urlopen", fake_urlopen)
    resp = http_client.UrllibTransport().request("GET", "http://x")
    assert resp.status == 404
    assert resp.ok is False


def test_urllib_transport_network_error_becomes_status_0(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(http_client.urllib.request, "urlopen", fake_urlopen)
    resp = http_client.UrllibTransport().request("GET", "http://x")
    assert resp.status == 0
    assert b"connection refused" in resp.body
