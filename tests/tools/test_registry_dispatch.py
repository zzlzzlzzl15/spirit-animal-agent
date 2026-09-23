"""tests/tools/test_registry_dispatch.py — handler 签名约定适配器测试。

Spirit 工具 handler 有两种历史签名约定：
- args-dict：``handler(args: Dict, **kwargs)``
- 显式 kwargs：``handler(**args)``

registry.dispatch 必须正确适配两者，否则 args-dict 工具会因缺少位置参数
``args`` 而 TypeError（曾导致 browser / platforms / skills / terminal_ext /
voice 等约 21 个工具经 dispatch 全部损坏）。
"""

import json
from typing import Any, Dict

import pytest

from spirit.tools.registry import ToolRegistry, _detect_args_dict_handler


# ---------------------------------------------------------------------------
# 签名探测
# ---------------------------------------------------------------------------

class TestDetectArgsDictHandler:
    def test_args_dict_plain(self):
        def handler(args, **kwargs):
            return "ok"
        assert _detect_args_dict_handler(handler) is True

    def test_args_dict_typed(self):
        def handler(args: Dict[str, Any], **kwargs) -> str:
            return "ok"
        assert _detect_args_dict_handler(handler) is True

    def test_args_dict_lambda(self):
        assert _detect_args_dict_handler(lambda args, **kw: "ok") is True

    def test_explicit_kwargs(self):
        def handler(path: str, offset: int = 0):
            return "ok"
        assert _detect_args_dict_handler(handler) is False

    def test_single_positional_not_named_args(self):
        def handler(command: str):
            return "ok"
        assert _detect_args_dict_handler(handler) is False

    def test_no_params(self):
        def handler():
            return "ok"
        assert _detect_args_dict_handler(handler) is False

    def test_kwargs_only(self):
        def handler(**kwargs):
            return "ok"
        assert _detect_args_dict_handler(handler) is False

    def test_star_args_first(self):
        def handler(*args, **kwargs):
            return "ok"
        # *args 不是 POSITIONAL_OR_KEYWORD，保守判为非 args-dict
        assert _detect_args_dict_handler(handler) is False

    def test_builtin_no_signature(self):
        # 内置函数取不到签名 → 保守返回 False
        assert _detect_args_dict_handler(len) is False


# ---------------------------------------------------------------------------
# dispatch 适配
# ---------------------------------------------------------------------------

class TestDispatchAdapter:
    def test_dispatch_args_dict_handler_receives_dict(self):
        reg = ToolRegistry()
        received = {}

        def handler(args, **kwargs):
            received.update(args)
            return json.dumps({"got": args.get("url")})

        reg.register(
            name="t_args", toolset="test",
            schema={"type": "function", "function": {"name": "t_args"}},
            handler=handler,
        )
        result = reg.dispatch("t_args", {"url": "http://x.com"})
        assert received == {"url": "http://x.com"}
        assert json.loads(result)["got"] == "http://x.com"

    def test_dispatch_explicit_kwargs_handler(self):
        reg = ToolRegistry()

        def handler(path: str, offset: int = 0):
            return json.dumps({"path": path, "offset": offset})

        reg.register(
            name="t_kw", toolset="test",
            schema={"type": "function", "function": {"name": "t_kw"}},
            handler=handler,
        )
        result = reg.dispatch("t_kw", {"path": "/a", "offset": 5})
        assert json.loads(result) == {"path": "/a", "offset": 5}

    def test_dispatch_explicit_kwargs_default_applies(self):
        reg = ToolRegistry()

        def handler(path: str, offset: int = 7):
            return json.dumps({"offset": offset})

        reg.register(name="t_def", toolset="test", schema={}, handler=handler)
        # 不传 offset → 用默认值
        assert json.loads(reg.dispatch("t_def", {"path": "/a"}))["offset"] == 7

    def test_entry_flag_set_at_register(self):
        reg = ToolRegistry()

        def h_dict(args, **kw):
            return ""

        def h_kw(path):
            return ""

        reg.register(name="a", toolset="t", schema={}, handler=h_dict)
        reg.register(name="b", toolset="t", schema={}, handler=h_kw)
        assert reg.get_entry("a").takes_args_dict is True
        assert reg.get_entry("b").takes_args_dict is False

    def test_dispatch_handler_exception_caught(self):
        reg = ToolRegistry()

        def handler(args, **kw):
            raise ValueError("boom")

        reg.register(name="t_err", toolset="t", schema={}, handler=handler)
        result = reg.dispatch("t_err", {"x": 1})
        assert "error" in json.loads(result)

    def test_dispatch_unknown_tool(self):
        reg = ToolRegistry()
        result = reg.dispatch("nope_xyz", {})
        assert "error" in json.loads(result)


# ---------------------------------------------------------------------------
# 参数签名 TypeError 自愈（模型输出畸形导致空参数/多余参数时引导重试）
# ---------------------------------------------------------------------------

class TestDispatchSignatureHint:
    def test_missing_required_arg_returns_hint(self):
        reg = ToolRegistry()

        def handler(command: str, timeout: int = 30):
            return "ok"

        reg.register(name="t_hint", toolset="t", schema={}, handler=handler)
        result = json.loads(reg.dispatch("t_hint", {}))
        assert "error" in result
        assert "command（必填）" in result["hint"]
        assert "timeout（可选" in result["hint"]
        assert "重新调用" in result["hint"]

    def test_unexpected_kwarg_returns_hint(self):
        reg = ToolRegistry()

        def handler(path: str):
            return "ok"

        reg.register(name="t_extra", toolset="t", schema={}, handler=handler)
        result = json.loads(reg.dispatch("t_extra", {"pathh": "/a"}))
        assert "error" in result
        assert "path（必填）" in result["hint"]

    def test_handler_internal_typeerror_also_hinted(self):
        # handler 内部抛 TypeError 也会命中提示分支 —— 提示无害（仍返回 error）
        reg = ToolRegistry()

        def handler(x: int):
            return int(None)  # 内部 TypeError

        reg.register(name="t_inner", toolset="t", schema={}, handler=handler)
        result = json.loads(reg.dispatch("t_inner", {"x": 1}))
        assert "error" in result

    def test_non_typeerror_exception_unchanged(self):
        reg = ToolRegistry()

        def handler(x: int):
            raise ValueError("boom")

        reg.register(name="t_val", toolset="t", schema={}, handler=handler)
        result = json.loads(reg.dispatch("t_val", {"x": 1}))
        assert "error" in result
        assert "hint" not in result


# ---------------------------------------------------------------------------
# 真实工具回归（曾因签名不匹配全部损坏）
# ---------------------------------------------------------------------------

class TestRealToolsDispatch:
    @pytest.fixture(autouse=True)
    def _load_tools(self):
        import spirit.tools  # noqa: F401  触发自注册
        from spirit.tools.registry import discover_tools
        discover_tools()
        yield

    def test_tts_no_typeerror(self, monkeypatch):
        # 清除可能触发真实 HTTP 调用的密钥
        monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        from spirit.tools.registry import registry
        result = registry.dispatch("tts", {"text": "hello"})
        assert "missing 1 required positional argument" not in result
        data = json.loads(result)
        assert "error" in data or "success" in data

    def test_transcribe_no_typeerror(self):
        from spirit.tools.registry import registry
        # 不存在的文件 → 早返回，不触发任何 API 调用
        result = registry.dispatch("transcribe", {"file_path": "nonexistent_xyz.mp3"})
        assert "missing 1 required positional argument" not in result

    def test_browser_navigate_no_typeerror(self):
        from spirit.tools.registry import registry
        result = registry.dispatch("browser_navigate", {"url": "http://example.com"})
        assert "missing 1 required positional argument" not in result

    def test_skills_list_no_typeerror(self):
        from spirit.tools.registry import registry
        result = registry.dispatch("skills_list", {"tag": ""})
        assert "missing 1 required positional argument" not in result
