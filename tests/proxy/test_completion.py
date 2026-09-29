"""补全 seam 的纯函数翻译测试（messages → 文本）。"""

from spirit.proxy import completion


def test_content_to_text_string():
    assert completion.content_to_text("hello") == "hello"


def test_content_to_text_none():
    assert completion.content_to_text(None) == ""


def test_content_to_text_multipart():
    parts = [
        {"type": "text", "text": "line1"},
        {"type": "image_url", "image_url": {"url": "http://x"}},
        {"type": "text", "text": "line2"},
    ]
    assert completion.content_to_text(parts) == "line1\nline2"


def test_content_to_text_list_of_str():
    assert completion.content_to_text(["a", "b"]) == "a\nb"


def test_extract_system_prompt():
    msgs = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "be concise"},
    ]
    assert completion.extract_system_prompt(msgs) == "you are helpful\nbe concise"


def test_extract_system_prompt_none():
    assert completion.extract_system_prompt([{"role": "user", "content": "hi"}]) == ""


def test_messages_to_user_prompt_single():
    msgs = [{"role": "user", "content": "just this"}]
    assert completion.messages_to_user_prompt(msgs) == "just this"


def test_messages_to_user_prompt_skips_system():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    result = completion.messages_to_user_prompt(msgs)
    assert "user: q1" in result
    assert "assistant: a1" in result
    assert result.endswith("q2")
    assert "sys" not in result


def test_messages_to_user_prompt_empty():
    assert completion.messages_to_user_prompt([]) == ""
    assert completion.messages_to_user_prompt([{"role": "system", "content": "s"}]) == ""


def test_default_completion_fn_empty_prompt_returns_empty(monkeypatch):
    """空对话不应触发 agent 构造（无 user 消息 → 直接返回空串）。"""
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("不应被调用")

    monkeypatch.setattr("spirit.agent.agent_init.initialize_agent", boom, raising=False)
    assert completion.default_completion_fn([{"role": "system", "content": "s"}]) == ""
    assert called["n"] == 0
