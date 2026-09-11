"""Memora API 反向代理 — 统一 API 入口。

将 /memora/api/v1/* 请求代理到 Memora 后端 (http://localhost:8080/api/v1/*)。
好处：
- 前端无需处理跨域
- 统一入口，浏览器只需知道 Spirit 地址
- 可添加认证/日志中间件

Usage:
    # 在 server.py 中注册
    from spirit.api.memora_proxy import create_memora_router
    app.include_router(create_memora_router(), prefix="/memora")
"""

import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

from spirit.config import get_config_value

# 默认 Memora 后端地址（来自集中式配置）
DEFAULT_MEMORA_URL = get_config_value("memora.base_url", "http://127.0.0.1:8080")


def create_memora_router(memora_base_url: str = None) -> APIRouter:
    """创建 Memora 反向代理路由。

    Args:
        memora_base_url: Memora 后端地址，默认 http://127.0.0.1:8080

    Returns:
        FastAPI APIRouter 实例
    """
    router = APIRouter(tags=["memora-proxy"])
    target_base = (memora_base_url or DEFAULT_MEMORA_URL).rstrip("/")

    # 共享的 HTTP 客户端
    _client: Optional[httpx.AsyncClient] = None

    async def _get_client() -> httpx.AsyncClient:
        nonlocal _client
        if _client is None:
            _client = httpx.AsyncClient(
                base_url=target_base,
                timeout=httpx.Timeout(30.0, connect=5.0),
                follow_redirects=True,
            )
        return _client

    # ------------------------------------------------------------------
    # 通用代理 — 转发所有 /api/v1/* 请求
    # ------------------------------------------------------------------

    @router.api_route(
        "/api/v1/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    )
    async def proxy_memora_api(request: Request, path: str):
        """代理 Memora API v1 请求。"""
        client = await _get_client()
        target_url = f"/api/v1/{path}"

        try:
            # 转发请求
            body = await request.body()
            headers = dict(request.headers)
            # 移除 hop-by-hop 头
            for h in ("host", "connection", "keep-alive", "transfer-encoding"):
                headers.pop(h, None)

            resp = await client.request(
                method=request.method,
                url=target_url,
                content=body if body else None,
                headers=headers,
                params=dict(request.query_params),
            )

            # 构建响应
            response_headers = dict(resp.headers)
            for h in ("content-encoding", "transfer-encoding", "connection"):
                response_headers.pop(h, None)

            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=response_headers,
            )

        except httpx.ConnectError:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "Memora 服务不可用",
                    "hint": f"无法连接到 {target_base}，请确认 Memora 正在运行",
                },
            )
        except httpx.TimeoutException:
            raise HTTPException(
                status_code=504,
                detail={"error": "Memora 请求超时"},
            )
        except Exception as e:
            logger.warning("Memora 代理错误: %s", e)
            raise HTTPException(
                status_code=502,
                detail={"error": f"代理请求失败: {e}"},
            )

    # ------------------------------------------------------------------
    # 健康检查 — 聚合 Spirit + Memora 状态
    # ------------------------------------------------------------------

    @router.get("/health")
    async def memora_health():
        """检查 Memora 后端健康状态。"""
        client = await _get_client()
        try:
            resp = await client.get("/health", timeout=get_config_value("timeouts.memora_health", 5.0))
            data = resp.json() if resp.status_code == 200 else {}
            return {
                "status": "connected" if resp.status_code == 200 else "degraded",
                "memora_url": target_base,
                "memora_response": data,
            }
        except Exception as e:
            return {
                "status": "disconnected",
                "memora_url": target_base,
                "error": str(e),
            }

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------

    @router.on_event("shutdown")
    async def cleanup():
        nonlocal _client
        if _client:
            await _client.aclose()
            _client = None

    return router
