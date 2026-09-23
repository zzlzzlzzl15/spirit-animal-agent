"""``computer_use`` 工具注册 / 发现 / check_fn 测试（Phase 4.5）。

对标 Hermes ``tests/tools/test_computer_use.py::TestRegistration``：Hermes 经顶层 shim
``tools/computer_use_tool.py`` 注册（子包 ``discover_builtin_tools`` 扁平 glob 发现）。Spirit
同款——shim ``spirit/tools/computer_use_tool.py`` 承担 ``registry.register``，使
``discover_tools()`` 扫描 ``spirit/tools/*.py`` 时发现本子包工具。

check_fn 语义（Spirit 适配）：Hermes 门控在 ``sys.platform`` + cua-driver 二进制；Spirit 门控在
``computer_use.enabled`` 配置 + 后端 ``is_available()``（noop 恒可用 = 安全降级）。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from spirit.computer_use import tool as cu_tool
from spirit.computer_use.noop_backend import NoopBackend
from spirit.tools.registry import registry


SHIM_MODULE = "spirit.tools.computer_use_tool"


class TestShimDiscovery:
    def test_shim_module_is_ast_detectable(self):
        """shim 含顶层 registry.register()，故 _module_registers_tools 能识别它。"""
        from spirit.tools.registry import _module_registers_tools

        shim_path = Path(cu_tool.__file__).parent.parent / "tools" / "computer_use_tool.py"
        assert shim_path.exists()
        assert _module_registers_tools(shim_path) is True

    def test_discover_tools_imports_shim(self):
        from spirit.tools.registry import discover_tools

        imported = discover_tools()
        assert SHIM_MODULE in imported

    def test_import_shim_registers_tool(self):
        importlib.import_module(SHIM_MODULE)
        entry = registry.get_entry("computer_use")
        assert entry is not None
        assert entry.toolset == "computer_use"
        assert entry.schema["function"]["name"] == "computer_use"


class TestRegistrationEntry:
    @pytest.fixture(autouse=True)
    def _ensure_registered(self):
        importlib.import_module(SHIM_MODULE)

    def test_handler_is_handle_computer_use(self):
        entry = registry.get_entry("computer_use")
        assert entry.handler is cu_tool.handle_computer_use

    def test_handler_detected_as_args_dict(self):
        """handler 首位置参数名为 args → dispatch 用 args-dict 约定调用。"""
        entry = registry.get_entry("computer_use")
        assert entry.takes_args_dict is True

    def test_check_fn_wired(self):
        entry = registry.get_entry("computer_use")
        assert entry.check_fn is cu_tool.check_computer_use_requirements

    def test_emoji_and_description_present(self):
        entry = registry.get_entry("computer_use")
        assert entry.emoji  # 🖥️
        assert entry.description.strip()


class TestCheckRequirements:
    def test_true_with_noop_backend_available(self):
        # autouse fixture 强制 noop 后端；noop 恒可用（安全降级）。
        assert cu_tool.check_computer_use_requirements() is True

    def test_false_when_backend_unavailable(self):
        cu_tool.set_backend(NoopBackend(available=False))
        assert cu_tool.check_computer_use_requirements() is False

    def test_false_when_disabled_via_config(self, monkeypatch):
        from spirit.config import DEFAULT_CONFIG

        monkeypatch.setitem(DEFAULT_CONFIG["computer_use"], "enabled", False)
        assert cu_tool.check_computer_use_requirements() is False

    def test_true_when_enabled_via_config(self, monkeypatch):
        from spirit.config import DEFAULT_CONFIG

        monkeypatch.setitem(DEFAULT_CONFIG["computer_use"], "enabled", True)
        assert cu_tool.check_computer_use_requirements() is True

    def test_false_when_backend_start_raises(self):
        class Boom(NoopBackend):
            def is_available(self):
                raise RuntimeError("driver exploded")

        cu_tool.set_backend(Boom())
        # check_fn 吞掉异常 → False（工具面隐藏而非崩溃）
        assert cu_tool.check_computer_use_requirements() is False
