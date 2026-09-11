"""Memora 知识库工具测试。

测试工具注册、Schema 正确性、handler 逻辑。
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestMemoraToolRegistration:
    """测试工具是否正确注册到 registry。"""

    def test_knowledge_search_registered(self):
        from spirit.tools.registry import registry
        entry = registry.get_entry("knowledge_search")
        assert entry is not None
        assert entry.toolset == "knowledge"
        assert entry.emoji == "📚"

    def test_knowledge_save_registered(self):
        from spirit.tools.registry import registry
        entry = registry.get_entry("knowledge_save")
        assert entry is not None
        assert entry.toolset == "knowledge"
        assert entry.emoji == "💾"

    def test_knowledge_list_registered(self):
        from spirit.tools.registry import registry
        entry = registry.get_entry("knowledge_list")
        assert entry is not None
        assert entry.toolset == "knowledge"
        assert entry.emoji == "📋"

    def test_knowledge_query_registered(self):
        from spirit.tools.registry import registry
        entry = registry.get_entry("knowledge_query")
        assert entry is not None
        assert entry.toolset == "knowledge"
        assert entry.emoji == "🔍"


class TestToolSchemas:
    """测试 Schema 格式正确性。"""

    def test_knowledge_search_schema(self):
        from spirit.tools.memora_tool import KNOWLEDGE_SEARCH_SCHEMA
        assert KNOWLEDGE_SEARCH_SCHEMA["type"] == "function"
        func = KNOWLEDGE_SEARCH_SCHEMA["function"]
        assert func["name"] == "knowledge_search"
        assert "query" in func["parameters"]["required"]

    def test_knowledge_save_schema(self):
        from spirit.tools.memora_tool import KNOWLEDGE_SAVE_SCHEMA
        func = KNOWLEDGE_SAVE_SCHEMA["function"]
        assert func["name"] == "knowledge_save"
        assert "title" in func["parameters"]["required"]
        assert "content" in func["parameters"]["required"]

    def test_knowledge_list_schema(self):
        from spirit.tools.memora_tool import KNOWLEDGE_LIST_SCHEMA
        func = KNOWLEDGE_LIST_SCHEMA["function"]
        assert func["name"] == "knowledge_list"
        # limit 是可选的
        assert "required" not in func["parameters"] or \
               len(func["parameters"].get("required", [])) == 0

    def test_knowledge_query_schema(self):
        from spirit.tools.memora_tool import KNOWLEDGE_QUERY_SCHEMA
        func = KNOWLEDGE_QUERY_SCHEMA["function"]
        assert func["name"] == "knowledge_query"
        assert "query" in func["parameters"]["required"]


class TestToolHandlers:
    """测试工具 handler 的降级行为（Memora 不可用时）。"""

    def test_knowledge_search_memora_unavailable(self):
        """Memora 不可用时返回友好错误。"""
        from spirit.tools.memora_tool import _knowledge_search_impl

        with patch("spirit.tools.memora_tool._run_async") as mock_run:
            mock_run.return_value = json.dumps({
                "error": "知识库服务不可用",
                "hint": "请确认 Memora 服务正在运行 (默认端口 8080)",
            }, ensure_ascii=False)

            result = _knowledge_search_impl("测试查询")
            data = json.loads(result)
            assert "error" in data

    def test_knowledge_save_memora_unavailable(self):
        from spirit.tools.memora_tool import _knowledge_save_impl

        with patch("spirit.tools.memora_tool._run_async") as mock_run:
            mock_run.return_value = json.dumps({
                "error": "知识库服务不可用",
            }, ensure_ascii=False)

            result = _knowledge_save_impl("标题", "内容")
            data = json.loads(result)
            assert "error" in data

    def test_knowledge_list_memora_unavailable(self):
        from spirit.tools.memora_tool import _knowledge_list_impl

        with patch("spirit.tools.memora_tool._run_async") as mock_run:
            mock_run.return_value = json.dumps({
                "error": "知识库服务不可用",
            }, ensure_ascii=False)

            result = _knowledge_list_impl()
            data = json.loads(result)
            assert "error" in data

    def test_knowledge_query_memora_unavailable(self):
        from spirit.tools.memora_tool import _knowledge_query_impl

        with patch("spirit.tools.memora_tool._run_async") as mock_run:
            mock_run.return_value = json.dumps({
                "error": "知识库服务不可用",
            }, ensure_ascii=False)

            result = _knowledge_query_impl("测试")
            data = json.loads(result)
            assert "error" in data
