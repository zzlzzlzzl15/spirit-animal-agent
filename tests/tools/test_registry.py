"""tests/tools/test_registry.py — 工具注册表测试。"""

import pytest
from spirit.tools.registry import ToolEntry


class TestToolEntry:
    """ToolEntry 数据模型测试。"""

    def test_create_entry(self):
        entry = ToolEntry(
            name="test_tool",
            toolset="test",
            schema={"type": "function", "function": {"name": "test_tool"}},
            handler=lambda: "result",
            description="A test tool",
        )
        assert entry.name == "test_tool"
        assert entry.toolset == "test"
        assert entry.description == "A test tool"

    def test_entry_defaults(self):
        entry = ToolEntry(
            name="minimal",
            toolset="test",
            schema={},
            handler=lambda: None,
        )
        assert entry.check_fn is None
        assert entry.is_async is False
        assert entry.emoji == ""
        assert entry.max_result_size_chars is None


class TestToolRegistry:
    """工具注册表集成测试。"""

    def test_discover_tools(self):
        """测试工具自动发现。"""
        from spirit.tools.registry import registry, discover_tools
        discover_tools()
        names = registry.get_tool_names()
        assert len(names) > 0
        # 核心工具应存在
        assert "read_file" in names
        assert "write_file" in names
        # terminal 工具可能命名为 execute_command 或 terminal_execute
        assert any("terminal" in n or "command" in n for n in names)

    def test_get_definitions(self):
        """测试获取工具定义。"""
        from spirit.tools.registry import registry, discover_tools
        discover_tools()
        defs = registry.get_definitions()
        assert len(defs) > 0
        # 每个定义应有 function 字段
        for d in defs:
            assert "type" in d
            assert d["type"] == "function"
            assert "function" in d

    def test_dispatch_read_file(self, tmp_path):
        """测试工具调度（read_file）。"""
        from spirit.tools.registry import registry, discover_tools
        discover_tools()

        # 创建临时文件
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world", encoding="utf-8")

        result = registry.dispatch("read_file", {"path": str(test_file)})
        assert "hello world" in result

    def test_dispatch_list_dir(self, tmp_path):
        """测试工具调度（list_dir）。"""
        from spirit.tools.registry import registry, discover_tools
        discover_tools()

        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        (tmp_path / "b.txt").write_text("b", encoding="utf-8")

        result = registry.dispatch("list_dir", {"path": str(tmp_path)})
        assert "a.txt" in result
        assert "b.txt" in result

    def test_dispatch_unknown_tool(self):
        """测试调用不存在的工具。"""
        from spirit.tools.registry import registry, discover_tools
        discover_tools()
        result = registry.dispatch("nonexistent_tool_xyz", {})
        # 未知工具应返回错误信息或空结果
        assert result is not None

    def test_toolset_filtering(self):
        """测试工具集过滤。"""
        from spirit.tools.registry import registry, discover_tools
        discover_tools()
        # 只获取 file 工具集
        defs = registry.get_definitions(enabled_toolsets=["file"])
        assert len(defs) > 0
