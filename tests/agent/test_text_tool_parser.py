"""文本工具调用解析器测试 — text_tool_parser 全格式覆盖。

背景：MiniMax 等 provider 以 in-band 文本形式输出工具调用，主循环依赖
parse_text_tool_calls 提取。此前该模块零测试覆盖，导致两类回归长期潜伏：
1. 零参数 invoke 被 `if not params: return None` 静默丢弃
2. 包装标签（tool_calls / tool_response / function_calls）内的 JSON 不识别

本测试锁定全部支持格式与清洗行为。
"""

import json

from spirit.agent.text_tool_parser import parse_text_tool_calls


LT = "\x3c"   # <
GT = "\x3e"   # >


def tag(name, body, close=True):
    """构造 XML 风格标签块（避免本文件源码出现字面标签）。"""
    end = f"{LT}/{name}{GT}" if close else ""
    return f"{LT}{name}{GT}{body}{end}"


def fn(call):
    return call["function"]


# ---------------------------------------------------------------------------
# tool_call 块（Hermes/Copilot 风格）
# ---------------------------------------------------------------------------

class TestToolCallBlock:
    def test_single_json_object(self):
        payload = json.dumps({
            "id": "c1", "type": "function",
            "function": {"name": "web_search", "arguments": "{\"q\": \"news\"}"},
        })
        calls, cleaned = parse_text_tool_calls(tag("tool_call", payload))
        assert len(calls) == 1
        assert fn(calls[0])["name"] == "web_search"
        assert calls[0]["id"] == "c1"
        assert cleaned == ""

    def test_json_array(self):
        payload = json.dumps([
            {"id": "a", "type": "function", "function": {"name": "t1", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "t2", "arguments": "{}"}},
        ])
        calls, _ = parse_text_tool_calls(tag("tool_call", payload))
        assert [fn(c)["name"] for c in calls] == ["t1", "t2"]

    def test_consecutive_json_objects(self):
        o1 = json.dumps({"id": "x1", "type": "function",
                         "function": {"name": "t1", "arguments": "{}"}})
        o2 = json.dumps({"id": "x2", "type": "function",
                         "function": {"name": "t2", "arguments": "{}"}})
        calls, _ = parse_text_tool_calls(tag("tool_call", o1 + o2))
        assert [fn(c)["name"] for c in calls] == ["t1", "t2"]

    def test_visible_text_preserved(self):
        payload = json.dumps({"id": "c1", "type": "function",
                              "function": {"name": "t", "arguments": "{}"}})
        text = "我先说明一下。\n" + tag("tool_call", payload)
        calls, cleaned = parse_text_tool_calls(text)
        assert len(calls) == 1
        assert cleaned == "我先说明一下。"


# ---------------------------------------------------------------------------
# MiniMax invoke 风格
# ---------------------------------------------------------------------------

class TestMinimaxInvoke:
    def test_invoke_with_params(self):
        body = f'{LT}parameter name="url"{GT}"https://example.com"{LT}/parameter{GT}'
        text = f'{LT}invoke name="web_fetch"{GT}{body}{LT}/invoke{GT}'
        calls, cleaned = parse_text_tool_calls(text)
        assert len(calls) == 1
        assert fn(calls[0])["name"] == "web_fetch"
        assert json.loads(fn(calls[0])["arguments"])["url"] == "https://example.com"
        assert cleaned == ""

    def test_zero_param_invoke_not_dropped(self):
        """回归锁定：零参数 invoke 曾被静默丢弃。"""
        text = f'{LT}invoke name="get_date"{GT}{LT}/invoke{GT}'
        calls, _ = parse_text_tool_calls(text)
        assert len(calls) == 1
        assert fn(calls[0])["name"] == "get_date"
        assert json.loads(fn(calls[0])["arguments"]) == {}

    def test_multiple_invokes(self):
        t1 = f'{LT}invoke name="t1"{GT}{LT}/invoke{GT}'
        t2 = f'{LT}invoke name="t2"{GT}{LT}/invoke{GT}'
        calls, _ = parse_text_tool_calls(t1 + t2)
        assert [fn(c)["name"] for c in calls] == ["t1", "t2"]
        # call_id 递增不冲突
        assert calls[0]["id"] != calls[1]["id"]


# ---------------------------------------------------------------------------
# 包装标签（回归锁定：曾完全不识别）
# ---------------------------------------------------------------------------

class TestWrapperTags:
    def _payload(self, name="t1", cid="w1"):
        return json.dumps({"id": cid, "type": "function",
                           "function": {"name": name, "arguments": "{}"}})

    def test_tool_calls_wrapper_array(self):
        text = tag("tool_calls", json.dumps([
            {"id": "w1", "type": "function", "function": {"name": "t1", "arguments": "{}"}},
            {"id": "w2", "type": "function", "function": {"name": "t2", "arguments": "{}"}},
        ]))
        calls, cleaned = parse_text_tool_calls(text)
        assert [fn(c)["name"] for c in calls] == ["t1", "t2"]
        assert cleaned == ""

    def test_tool_response_wrapper(self):
        calls, _ = parse_text_tool_calls(tag("tool_response", self._payload("t9")))
        assert len(calls) == 1
        assert fn(calls[0])["name"] == "t9"

    def test_wrapper_with_visible_text(self):
        text = "我来帮你搜索今天的财经新闻。\n" + tag("tool_calls", self._payload("web_search"))
        calls, cleaned = parse_text_tool_calls(text)
        assert len(calls) == 1
        assert cleaned == "我来帮你搜索今天的财经新闻。"

    def test_wrapper_non_json_not_consumed(self):
        """包装块内不是合法工具 JSON 时不消费、不误报。"""
        text = tag("tool_calls", "这不是 JSON")
        calls, cleaned = parse_text_tool_calls(text)
        assert calls == []
        assert "这不是 JSON" in cleaned


# ---------------------------------------------------------------------------
# Bare JSON fallback
# ---------------------------------------------------------------------------

class TestBareJson:
    def test_bare_tool_call_json(self):
        text = '前置说明 {"id": "b1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}} 后置'
        calls, cleaned = parse_text_tool_calls(text)
        assert len(calls) == 1
        assert fn(calls[0])["name"] == "read_file"
        assert "b1" not in cleaned

    def test_plain_json_not_matched(self):
        text = '结果是 {"foo": "bar"} 这样'
        calls, cleaned = parse_text_tool_calls(text)
        assert calls == []
        assert cleaned == text.strip()


# ---------------------------------------------------------------------------
# 边界输入
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_and_invalid(self):
        assert parse_text_tool_calls("") == ([], "")
        assert parse_text_tool_calls("   ") == ([], "")
        assert parse_text_tool_calls(None) == ([], "")
        assert parse_text_tool_calls(123) == ([], "")

    def test_plain_text_untouched(self):
        text = "今天天气不错，没有任何工具调用。"
        calls, cleaned = parse_text_tool_calls(text)
        assert calls == []
        assert cleaned == text
