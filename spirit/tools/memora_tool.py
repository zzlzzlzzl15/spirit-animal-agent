"""Memora 知识库工具 — 让 Agent 主动调用个人知识库。

注册 4 个工具到 ToolRegistry：
- knowledge_search: 语义搜索知识库文档
- knowledge_save: 保存内容到知识库
- knowledge_list: 列出知识库文档
- knowledge_query: 搜索并获取 AI 整理答案

依赖：spirit/skills/memora_client.py (MemoraClient)
"""

import asyncio
import json
import logging
from typing import Optional

from spirit.tools.registry import registry
from spirit.config import get_config_value

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 辅助：获取运行中的事件循环或创建新的
# ---------------------------------------------------------------------------

def _run_async(coro):
    """在同步上下文中运行异步协程。

    如果有运行中的事件循环则调度 task，否则创建新循环。
    """
    try:
        loop = asyncio.get_running_loop()
        # 在已有循环中创建 task 并同步等待 — 使用 nest_asyncio 模式
        # 但 uvicorn 环境下可能不支持，回退到线程池
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result(timeout=get_config_value("timeouts.memora_tool", 60))
    except RuntimeError:
        # 没有运行中的事件循环
        return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. knowledge_search — 语义搜索
# ---------------------------------------------------------------------------

KNOWLEDGE_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "knowledge_search",
        "description": (
            "在个人知识库中进行语义搜索。\n"
            "搜索范围包括已上传的文档、笔记、保存的知识片段。\n"
            "返回最相关的文档片段及其相关性分数。\n\n"
            "使用场景：\n"
            "- 查找之前保存的笔记或资料\n"
            "- 检索项目相关的背景知识\n"
            "- 搜索特定主题的文档"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询（自然语言描述）",
                },
                "limit": {
                    "type": "integer",
                    "description": "最大返回结果数（默认 5）",
                },
            },
            "required": ["query"],
        },
    },
}


def _knowledge_search_impl(query: str, limit: int = 5) -> str:
    """knowledge_search 工具实现。"""
    from spirit.skills.memora_client import get_memora_client

    async def _search():
        client = get_memora_client()
        if not await client.health_check():
            return json.dumps({
                "error": "知识库服务不可用",
                "hint": "请确认 Memora 服务正在运行 (默认端口 8080)",
            }, ensure_ascii=False)
        results = await client.search(query, limit=limit)
        return json.dumps({
            "query": query,
            "count": len(results),
            "results": [r.to_dict() for r in results],
        }, ensure_ascii=False, indent=2)

    try:
        return _run_async(_search())
    except Exception as e:
        logger.warning("knowledge_search 失败: %s", e)
        return json.dumps({"error": f"搜索失败: {e}"}, ensure_ascii=False)


registry.register(
    name="knowledge_search",
    toolset="knowledge",
    schema=KNOWLEDGE_SEARCH_SCHEMA,
    handler=_knowledge_search_impl,
    is_async=False,
    description="语义搜索知识库",
    emoji="📚",
)


# ---------------------------------------------------------------------------
# 2. knowledge_save — 保存到知识库
# ---------------------------------------------------------------------------

KNOWLEDGE_SAVE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "knowledge_save",
        "description": (
            "将内容保存到个人知识库。\n\n"
            "适用场景：\n"
            "- 用户要求记住某些信息\n"
            "- 生成了重要的笔记或总结\n"
            "- 需要长期保存的知识片段\n\n"
            "保存后可通过 knowledge_search 检索。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "文档标题",
                },
                "content": {
                    "type": "string",
                    "description": "要保存的内容（纯文本或 Markdown）",
                },
                "tags": {
                    "type": "string",
                    "description": "标签（逗号分隔，如 'python,笔记,学习'）",
                },
            },
            "required": ["title", "content"],
        },
    },
}


def _knowledge_save_impl(
    title: str,
    content: str,
    tags: str = "",
) -> str:
    """knowledge_save 工具实现。"""
    from spirit.skills.memora_client import get_memora_client

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    # 自动添加 spirit 标签标识来源
    if "spirit-agent" not in tag_list:
        tag_list.append("spirit-agent")

    async def _save():
        client = get_memora_client()
        if not await client.health_check():
            return json.dumps({
                "error": "知识库服务不可用",
                "hint": "请确认 Memora 服务正在运行",
            }, ensure_ascii=False)
        result = await client.create_text(title, content, tags=tag_list)
        return json.dumps(result, ensure_ascii=False, indent=2)

    try:
        return _run_async(_save())
    except Exception as e:
        logger.warning("knowledge_save 失败: %s", e)
        return json.dumps({"error": f"保存失败: {e}"}, ensure_ascii=False)


registry.register(
    name="knowledge_save",
    toolset="knowledge",
    schema=KNOWLEDGE_SAVE_SCHEMA,
    handler=_knowledge_save_impl,
    is_async=False,
    description="保存内容到知识库",
    emoji="💾",
)


# ---------------------------------------------------------------------------
# 3. knowledge_list — 列出文档
# ---------------------------------------------------------------------------

KNOWLEDGE_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "knowledge_list",
        "description": (
            "列出知识库中最近的文档。\n"
            "返回文档标题、类型、创建时间和标签。\n"
            "用于浏览知识库内容概览。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "最大返回数量（默认 20）",
                },
            },
        },
    },
}


def _knowledge_list_impl(limit: int = 20) -> str:
    """knowledge_list 工具实现。"""
    from spirit.skills.memora_client import get_memora_client

    async def _list():
        client = get_memora_client()
        if not await client.health_check():
            return json.dumps({
                "error": "知识库服务不可用",
                "hint": "请确认 Memora 服务正在运行",
            }, ensure_ascii=False)
        docs = await client.list_documents(limit=limit)
        return json.dumps({
            "count": len(docs),
            "documents": [d.to_dict() for d in docs],
        }, ensure_ascii=False, indent=2)

    try:
        return _run_async(_list())
    except Exception as e:
        logger.warning("knowledge_list 失败: %s", e)
        return json.dumps({"error": f"列出文档失败: {e}"}, ensure_ascii=False)


registry.register(
    name="knowledge_list",
    toolset="knowledge",
    schema=KNOWLEDGE_LIST_SCHEMA,
    handler=_knowledge_list_impl,
    is_async=False,
    description="列出知识库文档",
    emoji="📋",
)


# ---------------------------------------------------------------------------
# 4. knowledge_query — 搜索 + AI 整理答案
# ---------------------------------------------------------------------------

KNOWLEDGE_QUERY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "knowledge_query",
        "description": (
            "在知识库中搜索并由 AI 整理答案。\n"
            "不同于 knowledge_search 返回原始文档片段，\n"
            "此工具会综合多个文档内容，由 AI 生成一个整理好的回答。\n\n"
            "适合需要综合多个文档信息的复杂问题。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "问题描述",
                },
            },
            "required": ["query"],
        },
    },
}


def _knowledge_query_impl(query: str) -> str:
    """knowledge_query 工具实现。"""
    from spirit.skills.memora_client import get_memora_client

    async def _query():
        client = get_memora_client()
        if not await client.health_check():
            return json.dumps({
                "error": "知识库服务不可用",
                "hint": "请确认 Memora 服务正在运行",
            }, ensure_ascii=False)
        result = await client.search_with_answer(query)
        return json.dumps(result, ensure_ascii=False, indent=2)

    try:
        return _run_async(_query())
    except Exception as e:
        logger.warning("knowledge_query 失败: %s", e)
        return json.dumps({"error": f"查询失败: {e}"}, ensure_ascii=False)


registry.register(
    name="knowledge_query",
    toolset="knowledge",
    schema=KNOWLEDGE_QUERY_SCHEMA,
    handler=_knowledge_query_impl,
    is_async=False,
    description="搜索并获取 AI 整理答案",
    emoji="🔍",
)
