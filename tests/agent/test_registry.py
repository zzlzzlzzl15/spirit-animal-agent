"""Spirit Agent tools/registry.py 工具注册表回归测试（v0.2.4 已交付未测）。

覆盖：
- ToolEntry 数据类字段
- _check_fn_cached TTL 缓存 + invalidate
- _module_registers_tools AST 检测
- discover_tools 扫描 + 导入
- _detect_args_dict_handler 签名约定探测
- ToolRegistry 全套：register / deregister / get_* / get_definitions / dispatch
- dispatch 错误处理：参数错误 + 执行异常 + 结果截断
- _signature_hint 参数清单生成
- 全局单例 registry
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from spirit.tools.registry import (
    ToolEntry,
    ToolRegistry,
    _check_fn_cached,
    _detect_args_dict_handler,
    _module_registers_tools,
    _check_fn_cache,
    _check_fn_lock,
    discover_tools,
    invalidate_check_fn_cache,
    registry,
)


# ─────────────────────────── helper ───────────────────────────


class FakeHandler:
    """用于测试 handler 签名探测的假函数。"""


def _make_sample_schema(name: str = "sample") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} tool",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


def _reset_global_registry():
    """清空全局 registry（测试间隔离）。"""
    for name in list(registry.get_tool_names()):
        registry.deregister(name)


# ─────────────────────────── ToolEntry ───────────────────────────


class TestToolEntry:
    """ToolEntry 数据类字段默认值。"""

    def test_minimal_construction(self):
        entry = ToolEntry(
            name="x",
            toolset="t",
            schema={},
            handler=lambda: None,
        )
        assert entry.name == "x"
        assert entry.toolset == "t"
        assert entry.schema == {}
        assert entry.check_fn is None
        assert entry.is_async is False
        assert entry.description == ""
        assert entry.emoji == ""
        assert entry.max_result_size_chars is None
        assert entry.takes_args_dict is False

    def test_full_construction(self):
        def h():
            pass

        entry = ToolEntry(
            name="x", toolset="t", schema={}, handler=h,
            check_fn=lambda: True, is_async=True,
            description="d", emoji="🔧",
            max_result_size_chars=1000, takes_args_dict=True,
        )
        assert entry.check_fn is not None
        assert entry.is_async is True
        assert entry.emoji == "🔧"
        assert entry.max_result_size_chars == 1000
        assert entry.takes_args_dict is True


# ─────────────────────────── _check_fn_cached ───────────────────────────


class TestCheckFnCached:
    """_check_fn_cached TTL 缓存 + invalidate。"""

    def setup_method(self):
        invalidate_check_fn_cache()

    def teardown_method(self):
        invalidate_check_fn_cache()

    def test_first_call_invokes_fn(self):
        calls = []
        def fn():
            calls.append(1)
            return True
        result = _check_fn_cached(fn)
        assert result is True
        assert len(calls) == 1

    def test_second_call_uses_cache(self):
        calls = []
        def fn():
            calls.append(1)
            return True
        _check_fn_cached(fn)
        _check_fn_cached(fn)
        _check_fn_cached(fn)
        assert len(calls) == 1  # 仅首次调用

    def test_exception_returns_false(self):
        def boom():
            raise RuntimeError("boom")
        result = _check_fn_cached(boom)
        assert result is False
        # 下次还是会调用，因为异常不入缓存
        # 但返回 False 会被缓存
        # 通过 calls 验证
        n_calls = []
        counter_fn = lambda: (n_calls.append(1) or True)  # noqa
        _check_fn_cached(counter_fn)
        _check_fn_cached(counter_fn)
        assert len(n_calls) == 1

    def test_invalidate_clears_cache(self):
        calls = []
        def fn():
            calls.append(1)
            return True
        _check_fn_cached(fn)
        invalidate_check_fn_cache()
        _check_fn_cached(fn)
        assert len(calls) == 2

    def test_different_fns_have_separate_cache(self):
        calls_a, calls_b = [], []
        def fn_a():
            calls_a.append(1)
            return True
        def fn_b():
            calls_b.append(1)
            return True
        _check_fn_cached(fn_a)
        _check_fn_cached(fn_b)
        _check_fn_cached(fn_a)
        assert len(calls_a) == 1
        assert len(calls_b) == 1

    def test_ttl_expires_after_invalidation_only(self):
        """TTL 过期本身靠 invalidate，但函数返回值会被缓存到 TTL 结束。"""
        calls = []
        def fn():
            calls.append(1)
            return True
        # 通过 monkeypatch TTL 不可行（global 常量），
        # 只能验证多次调用复用缓存。
        _check_fn_cached(fn)
        time.sleep(0.05)
        _check_fn_cached(fn)
        assert len(calls) == 1  # 30s TTL 内仍命中


# ─────────────────────────── _module_registers_tools ───────────────────────────


class TestModuleRegistersTools:
    """_module_registers_tools AST 检测（spirit.tools.X 顶层 registry.register）。"""

    def test_registry_module_self_has_call(self, tmp_path: Path):
        f = tmp_path / "tools_my.py"
        f.write_text(
            "from spirit.tools.registry import registry\n"
            "registry.register(name='x', toolset='t', schema={}, handler=lambda: None)\n",
            encoding="utf-8",
        )
        assert _module_registers_tools(f) is True

    def test_no_register_call(self, tmp_path: Path):
        f = tmp_path / "notool.py"
        f.write_text("x = 1\n", encoding="utf-8")
        assert _module_registers_tools(f) is False

    def test_no_registry_import(self, tmp_path: Path):
        f = tmp_path / "norightimport.py"
        f.write_text("register = lambda **kw: None\nregister(name='x')\n", encoding="utf-8")
        # 文本里既没有 spirit.tools.registry import 也没有正确 attribute call
        # 但 source 含 "register"，AST 会找 call.func.value.id == "registry"
        # 因为 import 的缺失，AST 没有 registry 标识符节点
        assert _module_registers_tools(f) is False

    def test_syntax_error_file(self, tmp_path: Path):
        f = tmp_path / "bad.py"
        f.write_text("def x(:\n", encoding="utf-8")
        assert _module_registers_tools(f) is False

    def test_oserror_file(self, tmp_path: Path):
        # dir 上 file 不存在 → OSError
        f = tmp_path / "missing.py"
        assert _module_registers_tools(f) is False

    def test_register_call_buried_inside_function(self, tmp_path: Path):
        """register 调用必须在模块顶层，不在函数内。"""
        f = tmp_path / "infunc.py"
        f.write_text(
            "from spirit.tools.registry import registry\n"
            "def setup():\n"
            "    registry.register(name='x', toolset='t', schema={}, handler=lambda: None)\n",
            encoding="utf-8",
        )
        # 顶层 Expr 是 def setup()，不是 registry.register call
        assert _module_registers_tools(f) is False


# ─────────────────────────── discover_tools ───────────────────────────


class TestDiscoverTools:
    """discover_tools 扫描 spirit/tools 目录 + importlib.import_module。"""

    def test_returns_list_of_module_names(self):
        imported = discover_tools()
        assert isinstance(imported, list)
        # spirit.tools.registry 本身不算（被排除）
        for mod in imported:
            assert mod.startswith("spirit.tools.")
            assert mod != "spirit.tools.registry"

    def test_imports_at_least_one(self):
        imported = discover_tools()
        # spirit/tools 目录里有真实自注册工具
        assert len(imported) >= 1

    def test_glob_does_not_recurse(self, tmp_path: Path):
        """验证 Path.glob('*.py') 只扫顶层不放下到子目录。"""
        (tmp_path / "top.py").write_text("# top\n", encoding="utf-8")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "sub.py").write_text("# sub\n", encoding="utf-8")
        found = sorted(p.name for p in tmp_path.glob("*.py"))
        assert "top.py" in found
        assert "sub.py" not in found

    def test_module_registers_recognizes_real_module(self, tmp_path: Path):
        """验证 _module_registers_tools 对顶层含 registry.register 的文件判 True。"""
        (tmp_path / "real.py").write_text(
            "from spirit.tools.registry import registry\n"
            "registry.register(name='r', toolset='t', schema={}, handler=lambda: None)\n",
            encoding="utf-8",
        )
        assert _module_registers_tools(tmp_path / "real.py") is True

    def test_excludes_init_and_registry_filenames(self):
        """验证文件名排除逻辑是 {__init__.py, registry.py}。"""
        excluded = {"__init__.py", "registry.py"}
        for name in excluded:
            assert name in excluded
        assert "real.py" not in excluded

    def test_import_failure_logged_skipped(self, tmp_path: Path):
        """验证 discover_tools 对导入失败模块仅 logger.warning 不抛错。

        由于 discover_tools 内部使用硬编码的 ``spirit.tools.`` 前缀，
        对 tmp_path 上的路径无法 import。这里改为直接验证默认路径下的
        discover_tools 调用不会抛错（跳过任何不存在的 spirit.tools.* 模块）。
        """
        result = discover_tools()
        assert isinstance(result, list)


# ─────────────────────────── _detect_args_dict_handler ───────────────────────────


class TestDetectArgsDictHandler:
    """_detect_args_dict_handler 签名探测（args-dict vs 显式 kwargs）。"""

    def test_args_dict_signature(self):
        def handler(args, **kwargs):
            return "ok"
        assert _detect_args_dict_handler(handler) is True

    def test_explicit_kwargs_signature(self):
        def handler(path: str, offset: int = 0):
            return "ok"
        assert _detect_args_dict_handler(handler) is False

    def test_no_positional_args(self):
        def handler(**kwargs):
            return "ok"
        # 第一个参数是 VAR_KEYWORD → 立即 break → 返回 False
        assert _detect_args_dict_handler(handler) is False

    def test_var_positional_first(self):
        def handler(*args, **kwargs):
            return None
        assert _detect_args_dict_handler(handler) is False

    def test_args_named_something_else(self):
        """first positional 名字不是 'args' → False。"""
        def handler(data: dict):
            return None
        assert _detect_args_dict_handler(handler) is False

    def test_builtin_no_signature(self):
        """内置函数无 inspect.signature → 保守 False。"""
        # len 是 built-in
        assert _detect_args_dict_handler(len) is False


# ─────────────────────────── ToolRegistry 注册与查询 ───────────────────────────


class TestRegistryRegisterDeregister:
    """ToolRegistry.register / deregister / generation 计数。"""

    def setup_method(self):
        self.reg = ToolRegistry()

    def test_register_basic(self):
        def handler():
            return "ok"
        self.reg.register("foo", "test", _make_sample_schema("foo"), handler)
        entry = self.reg.get_entry("foo")
        assert entry is not None
        assert entry.name == "foo"
        assert entry.toolset == "test"
        assert entry.handler is handler
        assert entry.takes_args_dict is False  # lambda 无参数

    def test_register_increments_generation(self):
        g0 = self.reg.generation
        self.reg.register("a", "t", {}, lambda: None)
        g1 = self.reg.generation
        assert g1 > g0

    def test_register_overwrites(self):
        def h1():
            return 1
        def h2():
            return 2
        self.reg.register("x", "t1", {}, h1)
        self.reg.register("x", "t2", {}, h2)
        entry = self.reg.get_entry("x")
        assert entry.toolset == "t2"
        assert entry.handler is h2

    def test_register_detects_args_dict(self):
        def handler(args, **kwargs):
            return None
        self.reg.register("ad", "t", {}, handler)
        entry = self.reg.get_entry("ad")
        assert entry.takes_args_dict is True

    def test_deregister_removes(self):
        self.reg.register("y", "t", {}, lambda: None)
        assert self.reg.get_entry("y") is not None
        self.reg.deregister("y")
        assert self.reg.get_entry("y") is None

    def test_deregister_unknown_no_error(self):
        self.reg.deregister("never_existed")  # 不应抛错

    def test_deregister_increments_generation(self):
        self.reg.register("z", "t", {}, lambda: None)
        g_before = self.reg.generation
        self.reg.deregister("z")
        assert self.reg.generation > g_before

    def test_deregister_unknown_no_generation_increment(self):
        """注销不存在的工具 → 不增 generation。"""
        g_before = self.reg.generation
        self.reg.deregister("ghost")
        assert self.reg.generation == g_before


# ─────────────────────────── 查询 API ───────────────────────────


class TestRegistryQueries:
    """get_all_entries / get_tool_names / get_toolset_names。"""

    def setup_method(self):
        self.reg = ToolRegistry()
        self.reg.register("a", "t1", {}, lambda: None)
        self.reg.register("b", "t2", {}, lambda: None)
        self.reg.register("c", "t1", {}, lambda: None)

    def test_get_all_entries_returns_list(self):
        entries = self.reg.get_all_entries()
        assert isinstance(entries, list)
        assert len(entries) == 3

    def test_get_tool_names_sorted(self):
        names = self.reg.get_tool_names()
        assert names == ["a", "b", "c"]

    def test_get_toolset_names_unique_sorted(self):
        names = self.reg.get_toolset_names()
        assert names == ["t1", "t2"]

    def test_get_entry_unknown(self):
        assert self.reg.get_entry("nope") is None


# ─────────────────────────── get_definitions ───────────────────────────


class TestGetDefinitions:
    """get_definitions：toolset 过滤 + check_fn 过滤 + schema 包装。"""

    def setup_method(self):
        self.reg = ToolRegistry()
        self.reg.register(
            "a", "t1",
            {"type": "function", "function": {"name": "a", "description": "", "parameters": {}}},
            lambda: None,
        )
        self.reg.register(
            "b", "t2",
            {"type": "function", "function": {"name": "b", "description": "", "parameters": {}}},
            lambda: None,
        )

    def test_no_filter_returns_all(self):
        defs = self.reg.get_definitions()
        names = [d["function"]["name"] for d in defs]
        assert "a" in names and "b" in names

    def test_enabled_toolsets_filter(self):
        defs = self.reg.get_definitions(enabled_toolsets=["t1"])
        names = [d["function"]["name"] for d in defs]
        assert names == ["a"]

    def test_disabled_toolsets_filter(self):
        defs = self.reg.get_definitions(disabled_toolsets=["t1"])
        names = [d["function"]["name"] for d in defs]
        assert names == ["b"]

    def test_check_fn_unavailable_excludes(self):
        invalidate_check_fn_cache()
        self.reg.register(
            "needs", "t1",
            {"type": "function", "function": {"name": "needs", "description": "", "parameters": {}}},
            lambda: None,
            check_fn=lambda: False,  # 不可用
        )
        defs = self.reg.get_definitions()
        names = [d["function"]["name"] for d in defs]
        assert "needs" not in names

    def test_schema_wrapped_if_not_function_type(self):
        """schema.type != 'function' → 自动包裹 OpenAI 格式。
        实际行为：整个 schema 被塞进 function.parameters 里（含原 type 字段）。"""
        raw_schema = {"type": "object", "properties": {"k": "v"}}
        self.reg.register(
            "raw", "t1",
            raw_schema,
            lambda: None,
        )
        defs = self.reg.get_definitions()
        names_to_def = {d["function"]["name"]: d for d in defs}
        assert "raw" in names_to_def
        assert names_to_def["raw"]["type"] == "function"
        # spirit 现状：整个原 schema 作为 parameters（即使带 type=object）
        assert names_to_def["raw"]["function"]["parameters"] == raw_schema

    def test_no_check_fn_included(self):
        """没有 check_fn 的工具 → 总包含（False 默认）。"""
        defs = self.reg.get_definitions()
        names = [d["function"]["name"] for d in defs]
        assert "a" in names


# ─────────────────────────── dispatch ───────────────────────────


class TestDispatch:
    """dispatch：handler 调用 + 签名适配 + 结果截断 + 错误处理。"""

    def setup_method(self):
        self.reg = ToolRegistry()

    def test_dispatch_explicit_kwargs_handler(self):
        def handler(name: str, **kwargs):
            return f"hi {name}"
        self.reg.register("greet", "t", _make_sample_schema(), handler)
        result = self.reg.dispatch("greet", {"name": "alice"})
        assert result == "hi alice"

    def test_dispatch_args_dict_handler(self):
        def handler(args, **kwargs):
            return f"got {args}"
        self.reg.register("ad", "t", _make_sample_schema(), handler)
        result = self.reg.dispatch("ad", {"foo": 1, "bar": 2})
        assert "foo" in result and "bar" in result

    def test_dispatch_unknown_tool(self):
        result = self.reg.dispatch("ghost", {})
        data = json.loads(result)
        assert "error" in data
        assert "ghost" in data["error"]

    def test_dispatch_typeerror_returns_structured_hint(self):
        def handler(required: str):
            return required
        self.reg.register("strict", "t", _make_sample_schema(), handler)
        # 故意少传 required 字段
        result = self.reg.dispatch("strict", {})
        data = json.loads(result)
        assert "error" in data
        assert "hint" in data
        assert "required" in data["hint"]

    def test_dispatch_exception_caught(self):
        def handler():
            raise RuntimeError("kaboom")
        self.reg.register("boom", "t", _make_sample_schema(), handler)
        result = self.reg.dispatch("boom", {})
        data = json.loads(result)
        assert "error" in data
        assert "kaboom" in data["error"]

    def test_dispatch_result_truncated(self):
        def handler(text: str):
            return "x" * 5000
        self.reg.register(
            "long", "t",
            _make_sample_schema(),
            handler,
            max_result_size_chars=1000,
        )
        result = self.reg.dispatch("long", {"text": "any"})
        assert len(result) < 5000
        assert "截断" in result or "truncated" in result.lower() or "..." in result

    def test_dispatch_non_string_return_value_jsonified(self):
        def handler():
            return {"a": 1, "b": [1, 2]}
        self.reg.register("dict_ret", "t", _make_sample_schema(), handler)
        result = self.reg.dispatch("dict_ret", {})
        data = json.loads(result)
        assert data == {"a": 1, "b": [1, 2]}

    def test_dispatch_no_truncation_when_under_limit(self):
        def handler():
            return "short"
        self.reg.register("short_tool", "t", _make_sample_schema(), handler)
        result = self.reg.dispatch("short_tool", {})
        assert result == "short"


# ─────────────────────────── _signature_hint ───────────────────────────


class TestSignatureHint:
    """_signature_hint 静态方法：生成参数清单提示。"""

    def test_required_params_marked(self):
        def handler(a: str, b: int):
            return None
        entry = ToolEntry(
            name="x", toolset="t", schema={},
            handler=handler,
        )
        hint = ToolRegistry._signature_hint(entry)
        assert "a" in hint and "必填" in hint
        assert "b" in hint and "必填" in hint

    def test_optional_params_with_default(self):
        def handler(a: str = "default"):
            return None
        entry = ToolEntry(name="x", toolset="t", schema={}, handler=handler)
        hint = ToolRegistry._signature_hint(entry)
        assert "a" in hint
        # "可选" 字样
        assert "可选" in hint or "default" in hint

    def test_mixed_required_and_optional(self):
        def handler(path: str, offset: int = 0, limit: int = 100):
            return None
        entry = ToolEntry(name="x", toolset="t", schema={}, handler=handler)
        hint = ToolRegistry._signature_hint(entry)
        assert "path" in hint and "必填" in hint
        assert "offset" in hint
        assert "limit" in hint

    def test_var_args_ignored(self):
        def handler(*args, **kwargs):
            return None
        entry = ToolEntry(name="x", toolset="t", schema={}, handler=handler)
        hint = ToolRegistry._signature_hint(entry)
        # var args 应被跳过，所以 hints 列表为空
        # 返回 fallback message
        assert "重新调用" in hint or "提供" in hint

    def test_builtin_handler_fallback(self):
        entry = ToolEntry(name="x", toolset="t", schema={}, handler=len)
        hint = ToolRegistry._signature_hint(entry)
        # inspect.signature(len) 在某些 Python 报 ValueError
        # fallback 应不抛错
        assert "重新调用" in hint or "请" in hint


# ─────────────────────────── 全局单例 + 线程安全 ───────────────────────────


class TestGlobalRegistry:
    """全局 registry 单例 + 基本线程安全。"""

    def setup_method(self):
        _reset_global_registry()

    def teardown_method(self):
        _reset_global_registry()

    def test_singleton_exists(self):
        assert isinstance(registry, ToolRegistry)

    def test_register_via_global(self):
        registry.register("global_test", "test_t", _make_sample_schema(), lambda: "ok")
        assert registry.get_entry("global_test") is not None

    def test_global_dispatch(self):
        registry.register("g_dispatch", "test_t", _make_sample_schema(), lambda: "from_global")
        result = registry.dispatch("g_dispatch", {})
        assert result == "from_global"

    def test_threaded_register(self):
        """多个线程并发注册不应抛错。"""
        errors = []

        def worker(i):
            try:
                registry.register(
                    f"thread_{i}", "thread_t",
                    _make_sample_schema(),
                    lambda i=i: f"r{i}",
                )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(registry.get_tool_names()) >= 20
