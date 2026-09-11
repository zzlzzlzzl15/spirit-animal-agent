"""MemoraClient 单元测试。

测试 Memora 知识库客户端的所有方法，使用 mock 替代真实 HTTP 请求。
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from spirit.skills.memora_client import (
    MemoraClient,
    MemoraSearchResult,
    MemoraDocument,
    get_memora_client,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    """创建测试用 MemoraClient 实例。"""
    return MemoraClient(base_url="http://test:8080")


@pytest.fixture
def mock_httpx():
    """Mock httpx.AsyncClient。"""
    with patch("spirit.skills.memora_client.httpx") as m:
        yield m


# ---------------------------------------------------------------------------
# MemoraSearchResult / MemoraDocument
# ---------------------------------------------------------------------------

class TestDataClasses:
    def test_search_result_to_dict(self):
        r = MemoraSearchResult(
            title="测试", content="内容", score=0.95, document_id="abc"
        )
        d = r.to_dict()
        assert d["title"] == "测试"
        assert d["score"] == 0.95
        assert d["document_id"] == "abc"

    def test_document_to_dict(self):
        d = MemoraDocument(
            document_id="doc1",
            title="文档",
            file_type="text",
            created_at="2024-01-01",
            tags=["test"],
        )
        result = d.to_dict()
        assert result["title"] == "文档"
        assert result["tags"] == ["test"]
        assert "content" not in result  # to_dict 不包含 content


# ---------------------------------------------------------------------------
# MemoraClient 初始化
# ---------------------------------------------------------------------------

class TestClientInit:
    def test_default_base_url(self):
        c = MemoraClient()
        assert "127.0.0.1:8080" in c.base_url

    def test_custom_base_url(self):
        c = MemoraClient(base_url="http://custom:9090/")
        assert c.base_url == "http://custom:9090"
        assert c.api_prefix == "http://custom:9090/api/v1"

    def test_is_available_default_false(self):
        c = MemoraClient()
        assert c.is_available is False


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------

class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_success(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        client._client = mock_client

        result = await client.health_check()
        assert result is True
        assert client.is_available is True

    @pytest.mark.asyncio
    async def test_health_check_failure(self, client):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("连接拒绝"))
        client._client = mock_client

        result = await client.health_check()
        assert result is False
        assert client.is_available is False


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------

class TestSearch:
    @pytest.mark.asyncio
    async def test_search_success(self, client):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "results": [
                {"title": "文档1", "content": "内容1" * 100, "score": 0.9, "document_id": "d1"},
                {"title": "文档2", "content": "内容2", "score": 0.8, "document_id": "d2"},
            ]
        }
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        client._client = mock_client

        results = await client.search("测试查询", limit=5)
        assert len(results) == 2
        assert results[0].title == "文档1"
        assert results[0].score == 0.9
        # 内容截断到 500
        assert len(results[0].content) <= 500

    @pytest.mark.asyncio
    async def test_search_failure_returns_empty(self, client):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("网络错误"))
        client._client = mock_client

        results = await client.search("测试")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_with_answer(self, client):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "query": "什么是 Python",
            "answer": "Python 是一种编程语言...",
            "results": [
                {"title": "Python 简介", "score": 0.95, "content": "Python 是..."},
            ],
        }
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        client._client = mock_client

        result = await client.search_with_answer("什么是 Python")
        assert result["answer"] == "Python 是一种编程语言..."
        assert len(result["sources"]) == 1


# ---------------------------------------------------------------------------
# 文档管理
# ---------------------------------------------------------------------------

class TestDocumentManagement:
    @pytest.mark.asyncio
    async def test_list_documents(self, client):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "documents": [
                {"document_id": "d1", "title": "文档1", "file_type": "text", "created_at": "2024-01-01", "tags": []},
            ]
        }
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        client._client = mock_client

        docs = await client.list_documents(limit=10)
        assert len(docs) == 1
        assert docs[0].title == "文档1"

    @pytest.mark.asyncio
    async def test_get_document(self, client):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "document_id": "d1",
            "title": "详情文档",
            "file_type": "markdown",
            "created_at": "2024-01-01",
            "tags": ["test"],
            "content": "# 标题\n内容",
        }
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        client._client = mock_client

        doc = await client.get_document("d1")
        assert doc is not None
        assert doc.title == "详情文档"
        assert doc.content == "# 标题\n内容"

    @pytest.mark.asyncio
    async def test_get_document_not_found(self, client):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("404"))
        client._client = mock_client

        doc = await client.get_document("nonexistent")
        assert doc is None

    @pytest.mark.asyncio
    async def test_create_text(self, client):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "document_id": "new1",
            "title": "新文档",
        }
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        client._client = mock_client

        result = await client.create_text("新文档", "内容", tags=["test"])
        assert result["status"] == "success"
        assert result["document_id"] == "new1"

    @pytest.mark.asyncio
    async def test_create_text_failure(self, client):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("服务不可用"))
        client._client = mock_client

        result = await client.create_text("标题", "内容")
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# 文件上传
# ---------------------------------------------------------------------------

class TestFileUpload:
    @pytest.mark.asyncio
    async def test_upload_nonexistent_file(self, client):
        result = await client.upload_file("/nonexistent/file.txt")
        assert result["status"] == "error"
        assert "不存在" in result["error"]

    @pytest.mark.asyncio
    async def test_upload_file_success(self, client, tmp_path):
        # 创建临时文件
        test_file = tmp_path / "test.md"
        test_file.write_text("# 测试文档\n内容", encoding="utf-8")

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"document_id": "up1", "title": "test"}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        client._client = mock_client

        result = await client.upload_file(str(test_file), title="测试", tags=["md"])
        assert result["status"] == "success"
        assert result["document_id"] == "up1"


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_get_memora_client_returns_same_instance(self):
        # 重置全局变量
        import spirit.skills.memora_client as mod
        mod._memora_client = None

        c1 = get_memora_client()
        c2 = get_memora_client()
        assert c1 is c2

        # 清理
        mod._memora_client = None
