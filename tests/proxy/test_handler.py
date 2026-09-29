"""ProxyHandler 传输无关分发测试 —— 路由 / 鉴权 / 流式 / 错误。"""

import json

from spirit.proxy.config import ProxyConfig
from spirit.proxy.handler import ProxyHandler


def _body_json(resp):
    return json.loads(resp.body.decode("utf-8"))


def _chat_body(messages, **extra):
    payload = {"model": "spirit-agent", "messages": messages}
    payload.update(extra)
    return json.dumps(payload).encode("utf-8")


# --------------------------------------------------------------------------
# health / models / CORS
# --------------------------------------------------------------------------

def test_health_no_auth_required(handler):
    resp = handler.handle("GET", "/health")
    assert resp.status == 200
    assert _body_json(resp)["status"] == "ok"


def test_health_open_even_with_auth_config(auth_handler):
    """/health 不需要鉴权（探活用）。"""
    resp = auth_handler.handle("GET", "/health")
    assert resp.status == 200


def test_models_list(handler):
    resp = handler.handle("GET", "/v1/models")
    assert resp.status == 200
    data = _body_json(resp)
    assert data["object"] == "list"
    assert data["data"][0]["id"] == "spirit-agent"


def test_models_wrong_method(handler):
    resp = handler.handle("POST", "/v1/models")
    assert resp.status == 405


def test_options_returns_cors_preflight(handler):
    resp = handler.handle("OPTIONS", "/v1/chat/completions")
    assert resp.status == 204
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"


def test_unknown_endpoint_404(handler):
    resp = handler.handle("GET", "/v1/nope")
    assert resp.status == 404
    assert _body_json(resp)["error"]["code"] == "not_found"


def test_path_normalization_strips_query_and_slash(handler):
    resp = handler.handle("GET", "/v1/models/?foo=bar")
    assert resp.status == 200


# --------------------------------------------------------------------------
# 鉴权
# --------------------------------------------------------------------------

def test_chat_requires_auth_when_key_set(auth_handler):
    resp = auth_handler.handle("POST", "/v1/chat/completions", {}, _chat_body(
        [{"role": "user", "content": "hi"}]
    ))
    assert resp.status == 401
    assert _body_json(resp)["error"]["type"] == "authentication_error"


def test_chat_accepts_valid_bearer(auth_handler):
    resp = auth_handler.handle(
        "POST", "/v1/chat/completions",
        {"Authorization": "Bearer secret-key"},
        _chat_body([{"role": "user", "content": "hi"}]),
    )
    assert resp.status == 200


def test_chat_rejects_wrong_bearer(auth_handler):
    resp = auth_handler.handle(
        "POST", "/v1/chat/completions",
        {"Authorization": "Bearer wrong"},
        _chat_body([{"role": "user", "content": "hi"}]),
    )
    assert resp.status == 401


def test_models_requires_auth(auth_handler):
    assert auth_handler.handle("GET", "/v1/models").status == 401


# --------------------------------------------------------------------------
# chat completions —— 非流式
# --------------------------------------------------------------------------

def test_chat_non_streaming(handler):
    resp = handler.handle("POST", "/v1/chat/completions", {}, _chat_body(
        [{"role": "user", "content": "hello"}]
    ))
    assert resp.status == 200
    data = _body_json(resp)
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert data["choices"][0]["message"]["content"] == "echo:hello"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"]["total_tokens"] > 0


def test_chat_wrong_method(handler):
    resp = handler.handle("GET", "/v1/chat/completions")
    assert resp.status == 405


def test_chat_invalid_json(handler):
    resp = handler.handle("POST", "/v1/chat/completions", {}, b"{not json")
    assert resp.status == 400
    assert _body_json(resp)["error"]["code"] == "invalid_json"


def test_chat_missing_messages(handler):
    resp = handler.handle("POST", "/v1/chat/completions", {}, b'{"model":"m"}')
    assert resp.status == 400
    assert _body_json(resp)["error"]["code"] == "missing_messages"


def test_chat_empty_messages(handler):
    resp = handler.handle("POST", "/v1/chat/completions", {}, _chat_body([]))
    assert resp.status == 400


def test_chat_json_body_not_object(handler):
    resp = handler.handle("POST", "/v1/chat/completions", {}, b"[1,2,3]")
    assert resp.status == 400


# --------------------------------------------------------------------------
# chat completions —— 流式
# --------------------------------------------------------------------------

def test_chat_streaming_sse(handler):
    resp = handler.handle("POST", "/v1/chat/completions", {}, _chat_body(
        [{"role": "user", "content": "hi"}], stream=True
    ))
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "text/event-stream"
    text = resp.body.decode("utf-8")
    assert text.endswith("data: [DONE]\n\n")
    assert "chat.completion.chunk" in text
    assert "echo:hi" in text


# --------------------------------------------------------------------------
# model 映射 / 覆盖
# --------------------------------------------------------------------------

def test_model_mapped_to_configured(handler):
    """客户端请求任意 model，都映射到服务端配置的模型。"""
    resp = handler.handle("POST", "/v1/chat/completions", {}, _chat_body(
        [{"role": "user", "content": "x"}], model="gpt-4"
    ))
    assert _body_json(resp)["model"] == "spirit-agent"


def test_model_override_when_allowed(fake_completion):
    cfg = ProxyConfig(model="spirit-agent", allow_model_override=True)
    h = ProxyHandler(config=cfg, completion_fn=fake_completion)
    resp = h.handle("POST", "/v1/chat/completions", {}, _chat_body(
        [{"role": "user", "content": "x"}], model="custom-llm"
    ))
    assert _body_json(resp)["model"] == "custom-llm"


# --------------------------------------------------------------------------
# 补全失败 / handler 绝不抛
# --------------------------------------------------------------------------

def test_completion_failure_returns_502(config):
    def broken(messages, *, model=None):
        raise RuntimeError("upstream down")

    h = ProxyHandler(config=config, completion_fn=broken)
    resp = h.handle("POST", "/v1/chat/completions", {}, _chat_body(
        [{"role": "user", "content": "x"}]
    ))
    assert resp.status == 502
    assert _body_json(resp)["error"]["code"] == "completion_failed"


def test_empty_completion_finish_reason_length(config):
    h = ProxyHandler(config=config, completion_fn=lambda m, *, model=None: "")
    resp = h.handle("POST", "/v1/chat/completions", {}, _chat_body(
        [{"role": "user", "content": "x"}]
    ))
    assert _body_json(resp)["choices"][0]["finish_reason"] == "length"


def test_handler_never_raises_on_internal_bug():
    """即使 config 对象损坏，handler 也应返回 500 而非抛出。"""

    class BadConfig:
        model = "m"

        def requires_auth(self):
            raise RuntimeError("boom")

    h = ProxyHandler(config=BadConfig(), completion_fn=lambda m, *, model=None: "")
    resp = h.handle("GET", "/v1/models")
    assert resp.status == 500
    assert json.loads(resp.body.decode())["error"]["code"] == "internal_error"


# --------------------------------------------------------------------------
# CORS 头存在于 JSON 响应
# --------------------------------------------------------------------------

def test_json_responses_carry_cors(handler):
    resp = handler.handle("GET", "/health")
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"
    assert resp.headers.get("Content-Type") == "application/json"
