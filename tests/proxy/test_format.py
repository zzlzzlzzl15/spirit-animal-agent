"""OpenAI 线格式构造器测试。"""

import json

from spirit.proxy import openai_format as fmt


def test_estimate_tokens():
    assert fmt.estimate_tokens("") == 0
    assert fmt.estimate_tokens("abc") == 1  # 至少 1
    assert fmt.estimate_tokens("a" * 40) == 10


def test_build_usage():
    usage = fmt.build_usage("a" * 40, "b" * 20)
    assert usage["prompt_tokens"] == 10
    assert usage["completion_tokens"] == 5
    assert usage["total_tokens"] == 15


def test_new_completion_id_prefix():
    cid = fmt.new_completion_id()
    assert cid.startswith("chatcmpl-")
    assert cid != fmt.new_completion_id()  # 唯一


def test_build_chat_completion_shape():
    resp = fmt.build_chat_completion(
        "hello", model="m1", completion_id="chatcmpl-x", created=100,
        usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    )
    assert resp["object"] == "chat.completion"
    assert resp["id"] == "chatcmpl-x"
    assert resp["created"] == 100
    assert resp["model"] == "m1"
    assert resp["choices"][0]["message"] == {"role": "assistant", "content": "hello"}
    assert resp["choices"][0]["finish_reason"] == "stop"
    assert resp["usage"]["total_tokens"] == 3


def test_build_chat_completion_defaults():
    resp = fmt.build_chat_completion("hi", model="m")
    assert resp["id"].startswith("chatcmpl-")
    assert isinstance(resp["created"], int)
    assert resp["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def test_build_models_list():
    resp = fmt.build_models_list("spirit-agent", created=42)
    assert resp["object"] == "list"
    assert len(resp["data"]) == 1
    entry = resp["data"][0]
    assert entry["id"] == "spirit-agent"
    assert entry["object"] == "model"
    assert entry["created"] == 42
    assert entry["owned_by"] == "spirit-agent"


def test_build_sse_chunks_single():
    lines = fmt.build_sse_chunks("hello", model="m", completion_id="id1", created=1)
    text = "".join(lines)
    assert text.endswith("data: [DONE]\n\n")
    # 首块 role，中间 content，末尾 finish
    assert '"role": "assistant"' in lines[0]
    assert any('"content": "hello"' in ln for ln in lines)
    assert any('"finish_reason": "stop"' in ln for ln in lines)
    # 每块都是合法 SSE
    for ln in lines[:-1]:
        assert ln.startswith("data: ")
        json.loads(ln[len("data: "):].strip())


def test_build_sse_chunks_split():
    lines = fmt.build_sse_chunks("abcdef", model="m", chunk_size=2)
    contents = [ln for ln in lines if '"content"' in ln]
    assert len(contents) == 3  # ab / cd / ef


def test_build_sse_chunks_empty_text():
    lines = fmt.build_sse_chunks("", model="m")
    # role 块 + finish 块 + DONE，无 content 块
    assert not any('"content"' in ln for ln in lines)
    assert lines[-1] == "data: [DONE]\n\n"


def test_build_error():
    err = fmt.build_error("boom", err_type="server_error", code="x", param="p")
    assert err["error"]["message"] == "boom"
    assert err["error"]["type"] == "server_error"
    assert err["error"]["code"] == "x"
    assert err["error"]["param"] == "p"
