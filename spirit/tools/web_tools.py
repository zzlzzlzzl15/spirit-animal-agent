"""Web 工具 — web_search / web_fetch。

参考 Hermes 的 tools/web_tools.py 设计，简化为使用 httpx 直接调用。
支持多种搜索引擎后端（可配置）。
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx

from spirit.tools.registry import registry
from spirit.config import get_config_value

logger = logging.getLogger(__name__)

# 超时
DEFAULT_TIMEOUT = get_config_value("timeouts.web_default", 30.0)


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------

WEB_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "搜索互联网获取信息。\n\n"
            "返回搜索结果列表（标题、URL、摘要）。\n"
            "适合查找文档、API 参考、解决方案等。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回结果数量（默认 5，最大 10）",
                },
            },
            "required": ["query"],
        },
    },
}


def _web_search_impl(query: str, limit: int = 5) -> str:
    """搜索互联网。

    支持的后端（按优先级）：
    1. Tavily（需要 TAVILY_API_KEY）
    2. DuckDuckGo HTML（无需 API Key）
    """
    limit = min(max(limit, 1), 10)

    # 尝试 Tavily
    tavily_key = os.getenv("TAVILY_API_KEY", "")
    if tavily_key:
        return _search_tavily(query, limit, tavily_key)

    # 回退到 DuckDuckGo HTML
    return _search_duckduckgo(query, limit)


def _search_tavily(query: str, limit: int, api_key: str) -> str:
    """使用 Tavily API 搜索。"""
    try:
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "max_results": limit,
                "search_depth": "basic",
            },
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("results", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", "")[:300],
            })

        return json.dumps({"results": results, "count": len(results)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"Tavily 搜索失败: {e}"})


def _search_duckduckgo(query: str, limit: int) -> str:
    """使用 DuckDuckGo HTML 搜索（无需 API Key）。"""
    try:
        resp = httpx.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (Spirit Agent)"},
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()
        html = resp.text

        # 简单解析搜索结果
        results = []
        # 提取结果块
        result_blocks = re.findall(
            r'<a rel="nofollow" class="result__a" href="([^"]+)"[^>]*>(.*?)</a>.*?'
            r'<a class="result__snippet"[^>]*>(.*?)</a>',
            html,
            re.DOTALL,
        )

        for url, title, snippet in result_blocks[:limit]:
            # 清理 HTML 标签
            clean_title = re.sub(r"<[^>]+>", "", title).strip()
            clean_snippet = re.sub(r"<[^>]+>", "", snippet).strip()
            # DuckDuckGo 的 URL 是重定向链接
            if "uddg=" in url:
                actual_url = url.split("uddg=")[-1].split("&")[0]
                from urllib.parse import unquote
                url = unquote(actual_url)

            results.append({
                "title": clean_title,
                "url": url,
                "snippet": clean_snippet[:300],
            })

        if not results:
            return json.dumps({
                "results": [],
                "message": "未找到结果，请尝试更具体的查询",
            })

        return json.dumps({"results": results, "count": len(results)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"搜索失败: {e}"})


registry.register(
    name="web_search",
    toolset="web",
    schema=WEB_SEARCH_SCHEMA,
    handler=_web_search_impl,
    description="搜索互联网",
    emoji="🔍",
)


# ---------------------------------------------------------------------------
# web_fetch
# ---------------------------------------------------------------------------

WEB_FETCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": (
            "获取网页内容并提取为纯文本/Markdown。\n\n"
            "适合阅读文档、API 参考、博客文章等。\n"
            "自动提取主要内容，忽略导航/广告等无关内容。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "网页 URL",
                },
                "max_length": {
                    "type": "integer",
                    "description": "最大返回字符数（默认 8000）",
                },
            },
            "required": ["url"],
        },
    },
}


def _web_fetch_impl(url: str, max_length: int = 8000) -> str:
    """获取网页内容。"""
    max_length = min(max(max_length, 1000), 50000)

    try:
        resp = httpx.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Spirit Agent; +https://spirit.agent)",
                "Accept": "text/html,application/json,text/plain,*/*",
            },
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
        )
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")

        # JSON 直接返回
        if "application/json" in content_type:
            text = resp.text[:max_length]
            return json.dumps({"url": url, "content": text, "format": "json"})

        # HTML 提取正文
        if "text/html" in content_type:
            text = _extract_html_content(resp.text)
        else:
            text = resp.text

        # 截断
        if len(text) > max_length:
            text = text[:max_length] + f"\n\n... [内容已截断，共 {len(text)} 字符]"

        return json.dumps({
            "url": url,
            "content": text,
            "format": "text",
            "length": len(text),
        }, ensure_ascii=False)

    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"HTTP {e.response.status_code}: {url}"})
    except Exception as e:
        return json.dumps({"error": f"获取失败: {e}"})


def _extract_html_content(html: str) -> str:
    """从 HTML 提取主要内容（简化版 readability）。"""
    # 移除 script/style
    html = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", html)
    html = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", html)
    html = re.sub(r"<!--[\s\S]*?-->", "", html)

    # 尝试提取 <article> 或 <main> 或 <body>
    for tag in ("article", "main"):
        match = re.search(rf"<{tag}[^>]*>([\s\S]*?)</{tag}>", html)
        if match:
            html = match.group(1)
            break
    else:
        match = re.search(r"<body[^>]*>([\s\S]*?)</body>", html)
        if match:
            html = match.group(1)

    # 转换基本 HTML 到文本
    # 标题
    for i in range(6, 0, -1):
        html = re.sub(
            rf"<h{i}[^>]*>([\s\S]*?)</h{i}>",
            lambda m: "\n" + "#" * i + " " + m.group(1).strip() + "\n",
            html,
        )

    # 段落/换行
    html = re.sub(r"<br\s*/?>", "\n", html)
    html = re.sub(r"<p[^>]*>", "\n", html)
    html = re.sub(r"</p>", "\n", html)
    html = re.sub(r"<li[^>]*>", "\n- ", html)

    # 代码块
    html = re.sub(r"<code[^>]*>([\s\S]*?)</code>", r"`\1`", html)
    html = re.sub(r"<pre[^>]*>([\s\S]*?)</pre>", r"\n```\n\1\n```\n", html)

    # 链接
    html = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>([\s\S]*?)</a>', r"[\2](\1)", html)

    # 移除剩余标签
    text = re.sub(r"<[^>]+>", "", html)

    # 清理空白
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.strip() for line in text.splitlines())

    # 解码 HTML 实体
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    text = text.replace("&#x27;", "'")
    text = text.replace("&nbsp;", " ")

    return text.strip()


registry.register(
    name="web_fetch",
    toolset="web",
    schema=WEB_FETCH_SCHEMA,
    handler=_web_fetch_impl,
    description="获取网页内容",
    emoji="🌐",
)
