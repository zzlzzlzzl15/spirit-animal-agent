"""X/Twitter 搜索集成测试 —— payload 构造 / 响应解析 / 降级。"""

from .conftest import FakeTransport, json_response

from spirit.integrations.x_search import (
    XSearchIntegration,
    build_search_payload,
    parse_search_response,
)


# --------------------------------------------------------------------------
# 纯函数
# --------------------------------------------------------------------------

def test_build_search_payload_basic():
    p = build_search_payload("cats")
    assert p["model"] == "grok-4.20-reasoning"
    assert p["messages"][0]["content"] == "cats"
    assert p["tools"] == [{"type": "x_search"}]


def test_build_search_payload_with_filters():
    p = build_search_payload(
        "ai", from_date="2026-01-01", to_date="2026-02-01",
        handles=["@a", "@b"], max_results=5,
    )
    content = p["messages"][0]["content"]
    assert "from:2026-01-01" in content
    assert "to:2026-02-01" in content
    assert "handles:@a,@b" in content
    assert p["max_results"] == 5


def test_parse_search_response_full():
    payload = {
        "choices": [{"message": {"content": "the answer"}}],
        "citations": [{"url": "http://x"}],
    }
    out = parse_search_response(payload)
    assert out["answer"] == "the answer"
    assert out["citations"] == [{"url": "http://x"}]


def test_parse_search_response_missing_fields():
    assert parse_search_response({}) == {"answer": "", "citations": []}
    assert parse_search_response(None) == {"answer": "", "citations": []}
    assert parse_search_response("nope") == {"answer": "", "citations": []}


def test_parse_search_response_empty_choices():
    assert parse_search_response({"choices": []})["answer"] == ""


# --------------------------------------------------------------------------
# 集成
# --------------------------------------------------------------------------

def test_not_configured():
    t = FakeTransport()
    integ = XSearchIntegration(transport=t, env={})
    assert integ.search("q").ok is False
    assert t.calls == []


def test_empty_query():
    integ = XSearchIntegration(transport=FakeTransport(), env={"XAI_API_KEY": "k"})
    assert integ.search("").ok is False


def test_search_ok():
    resp = json_response({
        "choices": [{"message": {"content": "result text"}}],
        "citations": [],
    })
    t = FakeTransport([resp])
    integ = XSearchIntegration(transport=t, env={"XAI_API_KEY": "KEY"})
    res = integ.search("ai news")
    assert res.ok is True
    assert res.data["answer"] == "result text"
    assert t.last_call["url"] == "https://api.x.ai/v1/chat/completions"
    assert t.last_call["headers"]["Authorization"] == "Bearer KEY"


def test_search_custom_base_and_model():
    t = FakeTransport([json_response({"choices": [{"message": {"content": "x"}}]})])
    integ = XSearchIntegration(
        transport=t, env={"XAI_API_KEY": "k", "XAI_BASE_URL": "http://local/",
                          "X_SEARCH_MODEL": "custom-model"},
    )
    integ.search("q")
    assert t.last_call["url"] == "http://local/chat/completions"
    assert t.last_call["json_body"]["model"] == "custom-model"


def test_search_error_status():
    t = FakeTransport([json_response({}, status=429)])
    integ = XSearchIntegration(transport=t, env={"XAI_API_KEY": "k"})
    res = integ.search("q")
    assert res.ok is False
    assert "429" in res.error


def test_search_transport_exception():
    t = FakeTransport([RuntimeError("timeout")])
    integ = XSearchIntegration(transport=t, env={"XAI_API_KEY": "k"})
    assert "timeout" in integ.search("q").error
