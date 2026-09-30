"""Memora 知识库 API 客户端。

封装 Memora REST API，供 Spirit Agent 内部调用。
Memora 是一个自托管的个人 AI 知识库系统，提供：
- 文档上传/管理（PDF/DOCX/TXT/MD）
- 语义搜索（向量检索 + BM42 稀疏向量）
- AI 问答（LLM 驱动的知识整理）
- 知识图谱可视化

依赖：Memora 服务运行在 http://127.0.0.1:8000

Usage:
    client = MemoraClient()
    results = await client.search("Python 装饰器")
    doc = await client.create_text("笔记", "今天学到了...")
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from spirit.config import get_config_value

# 默认 Memora API 地址（来自集中式配置）
DEFAULT_MEMORA_BASE = os.getenv("MEMORA_API_BASE", get_config_value("memora.base_url", "http://127.0.0.1:8000"))


@dataclass
class MemoraSearchResult:
    """搜索结果条目。"""
    title: str
    content: str
    score: float
    document_id: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "content": self.content,
            "score": self.score,
            "document_id": self.document_id,
        }


@dataclass
class MemoraDocument:
    """知识库文档。"""
    document_id: str
    title: str
    file_type: str
    created_at: str
    tags: List[str] = field(default_factory=list)
    content: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "document_id": self.document_id,
            "title": self.title,
            "file_type": self.file_type,
            "created_at": self.created_at,
            "tags": self.tags,
        }


class MemoraClient:
    """Memora 知识库 API 客户端。

    使用 httpx 异步 HTTP 客户端与 Memora 后端通信。
    所有方法都是异步的，连接失败时优雅降级。

    线程/事件循环安全性：
        全局单例（`get_memora_client()`）可能被多个事件循环使用——例如
        `memora_auto_save` 钩子对每个文件都新开一个线程并调用
        `asyncio.run()`。httpx 的连接池绑定在创建它的那个循环上，
        跨循环复用会抛 `RuntimeError: Event loop is closed`（且因坏连接
        被丢弃后下次又能新建，表现为**成功/失败严格交替**）。
        因此 `_get_client()` 会检测循环归属，失配时自动重建客户端。
    """

    def __init__(self, base_url: str = None):
        """初始化客户端。

        Args:
            base_url: Memora API 地址，默认 http://127.0.0.1:8000
        """
        self.base_url = (base_url or DEFAULT_MEMORA_BASE).rstrip("/")
        self.api_prefix = f"{self.base_url}/api/v1"
        self._client = None
        self._client_loop: Optional[asyncio.AbstractEventLoop] = None
        self._available: Optional[bool] = None

    async def _get_client(self):
        """获取或创建 httpx 客户端（延迟初始化，绑定当前事件循环）。

        注意：必须显式 `trust_env=False`，否则在 Windows 上 httpx 会从
        系统注册表读取 Clash/V2Ray 写入的代理 (例如 127.0.0.1:7890)，
        把请求劫持到代理端口，导致本地 Memora (8000) 无法连通。
        """
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if self._client is not None and running_loop is not None \
                and self._client_loop is not running_loop:
            # 旧客户端的连接池属于另一个（通常已关闭的）事件循环。
            # 无法 await aclose()——那个循环已经不在了，直接丢弃交给 GC。
            logger.debug(
                "检测到事件循环切换，重建 Memora HTTP 客户端（旧 loop=%s 新 loop=%s）",
                id(self._client_loop),
                id(running_loop),
            )
            self._client = None
            self._client_loop = None

        if self._client is None:
            try:
                import httpx
                self._client = httpx.AsyncClient(
                    timeout=httpx.Timeout(30.0, connect=5.0),
                    follow_redirects=True,
                    trust_env=False,  # 关键：忽略系统代理环境与 Windows 注册表代理
                )
                self._client_loop = running_loop
            except ImportError:
                # httpx 不可用时回退到 urllib
                logger.warning("httpx 未安装，使用 urllib 回退（功能受限）")
                self._client = None
                self._client_loop = None
        return self._client

    async def close(self):
        """关闭 HTTP 客户端。"""
        if self._client:
            try:
                await self._client.aclose()
            except Exception as e:  # 循环已关闭等场景下 aclose 可能失败
                logger.debug("关闭 Memora HTTP 客户端时忽略异常: %s", e)
            self._client = None
            self._client_loop = None

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """检查 Memora 服务是否可用。

        Returns:
            True 如果服务可用
        """
        try:
            client = await self._get_client()
            if client:
                resp = await client.get(f"{self.base_url}/health")
                self._available = resp.status_code == 200
            else:
                self._available = self._health_check_urllib()
            return self._available
        except Exception as e:
            logger.debug("Memora 健康检查失败: %s", e)
            self._available = False
            return False

    def _health_check_urllib(self) -> bool:
        """urllib 回退的健康检查。"""
        import urllib.request
        try:
            req = urllib.request.Request(f"{self.base_url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=get_config_value("timeouts.memora_health", 5)) as resp:
                return resp.status == 200
        except Exception:
            return False

    @property
    def is_available(self) -> bool:
        """Memora 是否可用（缓存结果）。"""
        if self._available is None:
            return False
        return self._available

    # ------------------------------------------------------------------
    # 文档搜索
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        limit: int = 5,
        score_threshold: float = 0.7,
    ) -> List[MemoraSearchResult]:
        """语义搜索知识库文档。

        Args:
            query: 搜索查询
            limit: 最大返回数
            score_threshold: 最低相关性分数

        Returns:
            搜索结果列表
        """
        try:
            data = await self._post_json(
                f"{self.api_prefix}/documents/search",
                {
                    "query": query,
                    "limit": limit,
                    "score_threshold": score_threshold,
                },
            )
            results = []
            for r in data.get("results", []):
                results.append(MemoraSearchResult(
                    title=r.get("title", ""),
                    content=r.get("content", "")[:500],
                    score=r.get("score", 0.0),
                    document_id=r.get("document_id", ""),
                ))
            return results
        except Exception as e:
            logger.warning("Memora 搜索失败: %s", e)
            return []

    async def search_with_answer(
        self,
        query: str,
        limit: int = 5,
        score_threshold: float = 0.7,
    ) -> Dict[str, Any]:
        """搜索知识库并获取 AI 整理答案。

        Args:
            query: 搜索查询
            limit: 最大返回数
            score_threshold: 最低相关性分数

        Returns:
            {"answer": str, "sources": list, "query": str}
        """
        try:
            data = await self._post_json(
                f"{self.api_prefix}/documents/search/answer",
                {
                    "query": query,
                    "limit": limit,
                    "score_threshold": score_threshold,
                },
                timeout=get_config_value("timeouts.memora_client_api", 60),
            )
            return {
                "query": data.get("query", query),
                "answer": data.get("answer", ""),
                "sources": [
                    {
                        "title": r.get("title", ""),
                        "score": r.get("score"),
                        "content_preview": r.get("content", "")[:200],
                    }
                    for r in data.get("results", [])
                ],
            }
        except Exception as e:
            logger.warning("Memora 搜索问答失败: %s", e)
            return {"query": query, "answer": f"知识库查询失败: {e}", "sources": []}

    # ------------------------------------------------------------------
    # 文档管理
    # ------------------------------------------------------------------

    async def list_documents(self, limit: int = 50) -> List[MemoraDocument]:
        """列出知识库中的文档。

        Args:
            limit: 最大返回数

        Returns:
            文档列表
        """
        try:
            data = await self._get_json(f"{self.api_prefix}/documents/?limit={limit}")
            documents = data.get("documents", data) if isinstance(data, dict) else data
            results = []
            for d in documents:
                results.append(MemoraDocument(
                    document_id=d.get("document_id", ""),
                    title=d.get("title", ""),
                    file_type=d.get("file_type", ""),
                    created_at=d.get("created_at", ""),
                    tags=d.get("tags", []),
                ))
            return results
        except Exception as e:
            logger.warning("Memora 列出文档失败: %s", e)
            return []

    async def get_document(self, document_id: str) -> Optional[MemoraDocument]:
        """获取文档详情。

        Args:
            document_id: 文档 ID

        Returns:
            文档对象，未找到返回 None
        """
        try:
            data = await self._get_json(f"{self.api_prefix}/documents/{document_id}")
            return MemoraDocument(
                document_id=data.get("document_id", ""),
                title=data.get("title", ""),
                file_type=data.get("file_type", ""),
                created_at=data.get("created_at", ""),
                tags=data.get("tags", []),
                content=data.get("content", ""),
            )
        except Exception as e:
            logger.warning("Memora 获取文档失败: %s", e)
            return None

    async def create_text(
        self,
        title: str,
        content: str,
        tags: List[str] = None,
        metadata: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """创建纯文本文档。

        Args:
            title: 文档标题
            content: 文档内容
            tags: 标签列表
            metadata: 额外元数据

        Returns:
            {"status": "success", "document_id": str, "title": str}

        Note:
            success 仅代表文档已入库，**向量索引是异步的**。若需确认
            可被语义搜索命中，请随后调用 `get_document_status()`。
        """
        try:
            data = await self._post_json(
                f"{self.api_prefix}/documents/create",
                {
                    "title": title,
                    "content": content,
                    "file_type": "text",
                    "tags": tags or [],
                    "metadata": metadata or {},
                },
            )
            return {
                "status": "success",
                "document_id": data.get("document_id", ""),
                "title": data.get("title", title),
                "message": f"文档 '{title}' 已保存到知识库",
            }
        except Exception as e:
            logger.warning("Memora 创建文档失败: %s", e)
            return {"status": "error", "error": str(e)}

    async def upload_file(
        self,
        file_path: str,
        title: str = None,
        tags: List[str] = None,
    ) -> Dict[str, Any]:
        """上传文件到知识库。

        支持格式：PDF, DOCX, TXT, MD

        Args:
            file_path: 文件路径
            title: 文档标题（默认使用文件名）
            tags: 标签列表

        Returns:
            {"status": "success", "document_id": str, "title": str}
        """
        file_path = Path(file_path)
        if not file_path.exists():
            return {"status": "error", "error": f"文件不存在: {file_path}"}

        if title is None:
            title = file_path.stem

        try:
            client = await self._get_client()
            if client:
                with open(file_path, "rb") as f:
                    files = {"file": (file_path.name, f)}
                    data = {"title": title}
                    if tags:
                        import json
                        data["tags"] = json.dumps(tags)
                    resp = await client.post(
                        f"{self.api_prefix}/documents/upload",
                        files=files,
                        data=data,
                    )
                    result = resp.json()
            else:
                result = self._upload_file_urllib(file_path, title, tags)

            return {
                "status": "success",
                "document_id": result.get("document_id", ""),
                "title": result.get("title", title),
                "message": f"文件 '{file_path.name}' 已上传到知识库",
            }
        except Exception as e:
            logger.warning("Memora 上传文件失败: %s", e)
            return {"status": "error", "error": str(e)}

    def _upload_file_urllib(
        self,
        file_path: Path,
        title: str,
        tags: List[str] = None,
    ) -> Dict:
        """urllib 回退的文件上传。"""
        import urllib.request
        import uuid
        import mimetypes
        import json

        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        with open(file_path, "rb") as f:
            file_data = f.read()

        boundary = uuid.uuid4().hex
        parts = []

        # file
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'.encode())
        parts.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        parts.append(file_data)
        parts.append(b"\r\n")

        # title
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(b'Content-Disposition: form-data; name="title"\r\n\r\n')
        parts.append(title.encode("utf-8"))
        parts.append(b"\r\n")

        # tags
        if tags:
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(b'Content-Disposition: form-data; name="tags"\r\n\r\n')
            parts.append(json.dumps(tags).encode("utf-8"))
            parts.append(b"\r\n")

        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)

        req = urllib.request.Request(
            f"{self.api_prefix}/documents/upload",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=get_config_value("timeouts.memora_client_api", 60)) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ------------------------------------------------------------------
    # 统计信息
    # ------------------------------------------------------------------

    async def get_document_status(self, document_id: str) -> Dict[str, Any]:
        """查询文档的处理（embedding）状态。

        重要：`create_text` / `upload_file` 返回 success 仅代表
        **文档行已入库**，向量索引（embedding）是服务端异步处理的。
        若 embedding 服务不可用（如 LLM 供应商欠费/限流），文档会停在
        `status=failed, progress=60` ——内容存在但**无法被语义搜索命中**。

        Returns:
            {"document_id": str, "status": str, "progress": int,
             "error_message": str|None}
            status 取值：pending / chunking / embedding / completed / failed
        """
        try:
            return await self._get_json(f"{self.api_prefix}/documents/{document_id}/status")
        except Exception as e:
            logger.debug("Memora 查询文档状态失败: %s", e)
            return {"document_id": document_id, "status": "unknown", "error_message": str(e)}

    async def reprocess_document(self, document_id: str) -> Dict[str, Any]:
        """触发文档重新处理（重建向量索引）。

        用于修复 embedding 失败（status=failed）的文档。
        注意：服务端是**同步**执行的，大文档可能耗时数分钟，
        因此这里用较长的超时，并在超时后回查状态而不直接当失败。
        """
        try:
            await self._post_json(
                f"{self.api_prefix}/documents/{document_id}/reprocess",
                {},
                timeout=get_config_value("timeouts.memora_reprocess", 600),
            )
            status = await self.get_document_status(document_id)
            return {"status": "success", "document_status": status}
        except Exception as e:
            # 请求超时不等于处理失败（服务端可能仍在跑）——回查真实状态
            status = await self.get_document_status(document_id)
            if status.get("status") in ("embedding", "chunking", "pending", "completed"):
                return {"status": "success", "document_status": status,
                        "note": f"请求未同步返回但服务端在处理中: {e}"}
            logger.warning("Memora 重建索引失败: %s — %s", document_id, e)
            return {"status": "error", "error": str(e), "document_status": status}

    async def get_stats(self) -> Dict[str, Any]:
        """获取知识库统计信息。"""
        try:
            data = await self._get_json(f"{self.api_prefix}/stats/")
            return data
        except Exception as e:
            logger.debug("Memora 获取统计失败: %s", e)
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # HTTP 辅助方法
    # ------------------------------------------------------------------

    async def _get_json(self, url: str, timeout: int = 15) -> Dict:
        """发送 GET 请求并返回 JSON。"""
        client = await self._get_client()
        if client:
            resp = await client.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        else:
            return self._get_json_urllib(url, timeout)

    async def _post_json(
        self,
        url: str,
        payload: Dict,
        timeout: int = 30,
    ) -> Dict:
        """发送 POST JSON 请求并返回 JSON。"""
        client = await self._get_client()
        if client:
            resp = await client.post(url, json=payload, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        else:
            return self._post_json_urllib(url, payload, timeout)

    def _get_json_urllib(self, url: str, timeout: int = 15) -> Dict:
        """urllib GET 回退。"""
        import urllib.request
        import json
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post_json_urllib(
        self,
        url: str,
        payload: Dict,
        timeout: int = 30,
    ) -> Dict:
        """urllib POST 回退。"""
        import urllib.request
        import json
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

_memora_client: Optional[MemoraClient] = None


def get_memora_client() -> MemoraClient:
    """获取全局 Memora 客户端单例。"""
    global _memora_client
    if _memora_client is None:
        _memora_client = MemoraClient()
    return _memora_client
