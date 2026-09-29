"""ProxyServer stdlib HTTP 集成测试 —— 真实 socket（localhost 临时端口）。

用 ``port=0`` 让 OS 分配临时端口，通过 ``urllib`` 打真实 HTTP 请求验证 stdlib 适配层
把 :class:`ProxyHandler` 的纯数据响应正确落到线上。绑定失败时跳过（CI/离线友好）。
"""

import json
import urllib.error
import urllib.request

import pytest

from spirit.proxy.config import ProxyConfig
from spirit.proxy.server import ProxyServer


def _fake(messages, *, model=None):
    from spirit.proxy.completion import messages_to_user_prompt
    return f"echo:{messages_to_user_prompt(messages)}"


@pytest.fixture
def live_server():
    """启动一个绑定临时端口的真实服务器；测试后停止。"""
    cfg = ProxyConfig(enabled=True, host="127.0.0.1", port=0, model="spirit-agent")
    server = ProxyServer(config=cfg, completion_fn=_fake)
    try:
        server.start()
    except OSError as exc:  # pragma: no cover - 环境不允许绑定时跳过
        pytest.skip(f"无法绑定本地端口: {exc}")
    yield server
    server.stop()


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8"), dict(resp.headers)


def _post(url, payload, headers=None):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8"), dict(e.headers)


def test_server_url_reflects_bound_port(live_server):
    assert live_server.url.startswith("http://127.0.0.1:")
    # port=0 应已被替换为真实端口
    assert not live_server.url.endswith(":0")


def test_health_over_http(live_server):
    status, body, _ = _get(f"{live_server.url}/health")
    assert status == 200
    assert json.loads(body)["status"] == "ok"


def test_models_over_http(live_server):
    status, body, _ = _get(f"{live_server.url}/v1/models")
    assert status == 200
    assert json.loads(body)["data"][0]["id"] == "spirit-agent"


def test_chat_over_http(live_server):
    status, body, headers = _post(
        f"{live_server.url}/v1/chat/completions",
        {"model": "spirit-agent", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert status == 200
    data = json.loads(body)
    assert data["choices"][0]["message"]["content"] == "echo:ping"
    assert headers.get("Content-Type") == "application/json"


def test_chat_streaming_over_http(live_server):
    status, body, headers = _post(
        f"{live_server.url}/v1/chat/completions",
        {
            "model": "spirit-agent",
            "messages": [{"role": "user", "content": "ping"}],
            "stream": True,
        },
    )
    assert status == 200
    assert headers.get("Content-Type") == "text/event-stream"
    assert body.endswith("data: [DONE]\n\n")


def test_unauthorized_over_http():
    cfg = ProxyConfig(
        enabled=True, host="127.0.0.1", port=0, model="spirit-agent", api_key="k"
    )
    server = ProxyServer(config=cfg, completion_fn=_fake)
    try:
        server.start()
    except OSError as exc:  # pragma: no cover
        pytest.skip(f"无法绑定本地端口: {exc}")
    status = None
    try:
        # urllib 对 401 抛 HTTPError，需捕获后读取状态码
        try:
            status, _body, _ = _get(f"{server.url}/v1/models")
        except urllib.error.HTTPError as e:
            status = e.code
        assert status == 401
    finally:
        server.stop()


def test_stop_is_idempotent(live_server):
    live_server.stop()
    live_server.stop()  # 第二次不应抛
