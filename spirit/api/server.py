"""FastAPI 服务 — REST API + WebSocket 实时通信。

提供：
- REST API：会话管理、消息查询、工具列表
- WebSocket：实时对话（流式事件推送）
- 静态文件：Web Dashboard（未来）

参考 Hermes 的 gateway 架构，但简化为单进程 FastAPI 服务。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from spirit.api.events import (
    Commentary,
    ErrorEvent,
    MessageChunk,
    MessageStop,
    StatusUpdate,
    ToolCallResult,
    ToolCallStart,
    event_to_dict,
)
from spirit.api.context import WorkspaceContext
from spirit.api.memora_proxy import create_memora_router

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic 模型（REST API 请求/响应）
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    """对话请求。"""
    message: str
    session_id: Optional[str] = None
    model: Optional[str] = None
    context: Optional[Dict[str, Any]] = None


class ChatResponse(BaseModel):
    """对话响应。"""
    response: str
    session_id: str
    usage: Dict[str, int] = {}
    iterations: int = 0


class SessionInfo(BaseModel):
    """会话信息。"""
    id: str
    source: str
    model: str
    started_at: str
    ended_at: Optional[str] = None
    message_count: int = 0


class ToolInfo(BaseModel):
    """工具信息。"""
    name: str
    toolset: str
    description: str


class StatusResponse(BaseModel):
    """服务状态。"""
    status: str = "ok"
    version: str = "0.1.0"
    active_sessions: int = 0
    tool_count: int = 0


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    """创建 FastAPI 应用。"""
    app = FastAPI(
        title="Spirit Agent",
        description="统一后端 AI Agent 服务",
        version="0.1.0",
    )

    # CORS（允许 VSCode 扩展和 Web Dashboard 跨域访问）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Agent 管理器（简化版：单进程内管理多个 Agent 实例）
    agent_manager = AgentManager()

    # 注册路由
    _register_rest_routes(app, agent_manager)
    _register_websocket_routes(app, agent_manager)

    # Memora 反向代理
    app.include_router(create_memora_router(), prefix="/memora")

    # Web Dashboard 静态文件
    web_dir = Path(__file__).resolve().parent.parent.parent / "web"
    if web_dir.exists():
        from fastapi.staticfiles import StaticFiles
        app.mount("/static", StaticFiles(directory=str(web_dir)), name="web")

    # 启动时检查 Memora 连接
    @app.on_event("startup")
    async def check_memora():
        try:
            from spirit.skills.memora_client import get_memora_client
            client = get_memora_client()
            if await client.health_check():
                logger.info("Memora 知识库已连接: %s", client.base_url)
            else:
                logger.warning("Memora 未运行，知识库功能不可用")
        except Exception as e:
            logger.debug("Memora 启动检查跳过: %s", e)

    return app


# ---------------------------------------------------------------------------
# Agent 管理器
# ---------------------------------------------------------------------------

class AgentManager:
    """管理多个 Agent 会话实例。

    每个 WebSocket 连接或 REST 请求可以创建/复用 Agent 实例。
    简化版：同一 session_id 复用同一个 Agent。
    """

    def __init__(self):
        self._agents: Dict[str, Any] = {}  # session_id → SpiritAgent
        self._lock = asyncio.Lock()

    def get_or_create(
        self,
        session_id: str = None,
        model: str = None,
        platform: str = "api",
    ) -> tuple:
        """获取或创建 Agent。返回 (agent, session_id)。"""
        session_id = session_id or str(uuid.uuid4())

        if session_id not in self._agents:
            from spirit.agent.agent import AgentConfig, SpiritAgent
            from spirit.config import load_config

            raw = load_config()
            llm_cfg = raw.get("llm", {})

            config = AgentConfig(
                model=model or llm_cfg.get("model", ""),
                api_key=llm_cfg.get("api_key", ""),
                base_url=llm_cfg.get("base_url", ""),
                session_id=session_id,
                platform=platform,
            )
            agent = SpiritAgent(config)
            self._agents[session_id] = agent
            logger.info("创建 Agent: session=%s, model=%s", session_id[:8], agent.model)

        return self._agents[session_id], session_id

    def get_agent(self, session_id: str) -> Optional[Any]:
        return self._agents.get(session_id)

    def remove_agent(self, session_id: str):
        self._agents.pop(session_id, None)

    @property
    def active_count(self) -> int:
        return len(self._agents)

    def list_sessions(self) -> List[Dict]:
        """列出所有活跃会话。"""
        result = []
        for sid, agent in self._agents.items():
            result.append({
                "id": sid,
                "model": agent.model,
                "platform": agent.platform,
                "message_count": len(agent.messages),
                "api_calls": agent._api_call_count,
            })
        return result


# ---------------------------------------------------------------------------
# REST 路由
# ---------------------------------------------------------------------------

def _register_rest_routes(app: FastAPI, manager: AgentManager):

    @app.get("/api/status", response_model=StatusResponse)
    async def get_status():
        """服务状态。"""
        from spirit.tools.registry import registry, discover_tools
        discover_tools()
        return StatusResponse(
            status="ok",
            active_sessions=manager.active_count,
            tool_count=len(registry.get_tool_names()),
        )

    @app.post("/api/chat", response_model=ChatResponse)
    async def chat_rest(request: ChatRequest):
        """REST 对话接口（非流式，一次性返回）。"""
        agent, session_id = manager.get_or_create(
            session_id=request.session_id,
            model=request.model,
        )

        # 注入工作区上下文
        if request.context:
            agent.workspace_context = WorkspaceContext.from_dict(request.context)

        try:
            # 在线程池中运行同步的对话循环
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, agent.run_conversation, request.message)

            return ChatResponse(
                response=result.response,
                session_id=session_id,
                usage=result.usage,
                iterations=result.iterations,
            )
        except Exception as e:
            logger.error("对话失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/sessions")
    async def list_sessions():
        """列出活跃会话。"""
        return {"sessions": manager.list_sessions()}

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str):
        """获取会话详情。"""
        agent = manager.get_agent(session_id)
        if not agent:
            raise HTTPException(status_code=404, detail="会话不存在")
        return {
            "id": session_id,
            "model": agent.model,
            "platform": agent.platform,
            "status": agent.get_status(),
            "messages": agent.messages[-50:],  # 最近 50 条
        }

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        """结束并移除会话。"""
        manager.remove_agent(session_id)
        return {"ok": True}

    @app.get("/api/tools")
    async def list_tools():
        """列出所有可用工具。"""
        from spirit.tools.registry import registry, discover_tools
        discover_tools()
        entries = registry.get_all_entries()
        return {
            "tools": [
                {
                    "name": e.name,
                    "toolset": e.toolset,
                    "description": e.description,
                    "schema": e.schema,
                }
                for e in entries
            ]
        }

    @app.post("/api/sessions/{session_id}/interrupt")
    async def interrupt_session(session_id: str):
        """中断当前操作。"""
        agent = manager.get_agent(session_id)
        if not agent:
            raise HTTPException(status_code=404, detail="会话不存在")
        agent.interrupt()
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/reset")
    async def reset_session(session_id: str):
        """重置会话。"""
        agent = manager.get_agent(session_id)
        if not agent:
            raise HTTPException(status_code=404, detail="会话不存在")
        agent.reset_session()
        return {"ok": True, "new_session_id": agent.session_id}


# ---------------------------------------------------------------------------
# WebSocket 路由
# ---------------------------------------------------------------------------

def _register_websocket_routes(app: FastAPI, manager: AgentManager):

    @app.websocket("/ws/chat")
    async def ws_chat(websocket: WebSocket):
        """WebSocket 对话端点 — 流式事件推送。

        协议：
        客户端发送 JSON：
            {"type": "chat", "message": "...", "session_id": "...", "model": "..."}
            {"type": "interrupt"}
            {"type": "reset"}
            {"type": "ping"}

        服务端推送事件（见 events.py）：
            {"type": "message_chunk", "text": "..."}
            {"type": "message_stop", "final": true}
            {"type": "tool_call_start", "tool_name": "...", ...}
            {"type": "tool_call_result", ...}
            {"type": "status", "status": "thinking"}
            {"type": "error", "code": "...", "message": "..."}
            {"type": "pong"}
        """
        await websocket.accept()
        logger.info("WebSocket 连接已建立")

        agent = None
        session_id = None

        try:
            while True:
                # 接收客户端消息
                data = await websocket.receive_text()
                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    await _ws_send(websocket, ErrorEvent(
                        code="invalid_json",
                        message="无效的 JSON 格式",
                    ))
                    continue

                msg_type = msg.get("type", "")

                # ── 对话 ──
                if msg_type == "chat":
                    user_message = msg.get("message", "").strip()
                    if not user_message:
                        continue

                    # 获取或创建 Agent
                    model = msg.get("model")
                    sid = msg.get("session_id")
                    agent, session_id = manager.get_or_create(
                        session_id=sid,
                        model=model,
                        platform="websocket",
                    )

                    # 注入工作区上下文
                    ctx_data = msg.get("context")
                    if ctx_data:
                        agent.workspace_context = WorkspaceContext.from_dict(ctx_data)

                    # 设置事件回调（通过 WebSocket 推送）
                    _setup_agent_callbacks(agent, websocket)

                    # 设置代码智能桥（Agent → VSCode 代码定位请求）
                    _setup_code_intelligence_bridge(agent, websocket)

                    # 设置编辑提案管理器（Agent → VSCode 编辑提案）
                    _setup_edit_proposal_manager(agent, websocket)

                    # 设置终端桥（Agent → VSCode 终端执行）
                    _setup_terminal_bridge(agent, session_id, websocket)

                    # 发送 session_id 给客户端
                    await websocket.send_json({
                        "type": "session",
                        "session_id": session_id,
                    })

                    # 发送状态
                    await _ws_send(websocket, StatusUpdate("thinking"))

                    # 在线程池中运行对话循环
                    loop = asyncio.get_event_loop()
                    try:
                        result = await loop.run_in_executor(
                            None, agent.run_conversation, user_message
                        )

                        # 发送最终响应
                        await _ws_send(websocket, MessageChunk(result.response))
                        await _ws_send(websocket, MessageStop(final=True))
                        await _ws_send(websocket, StatusUpdate("idle"))

                    except Exception as e:
                        logger.error("WebSocket 对话错误: %s", e)
                        await _ws_send(websocket, ErrorEvent(
                            code="conversation_error",
                            message=str(e),
                        ))

                # ── 上下文更新（不触发对话）──
                elif msg_type == "context_update":
                    if agent:
                        ctx_data = msg.get("context")
                        if ctx_data:
                            agent.workspace_context = WorkspaceContext.from_dict(ctx_data)
                            logger.debug("上下文已更新: session=%s", session_id)

                # ── 代码智能响应（来自 VSCode）──
                elif msg_type == "code_intelligence_response":
                    if agent:
                        bridge = getattr(agent, "_code_intelligence_bridge", None)
                        if bridge:
                            request_id = msg.get("request_id", "")
                            result = msg.get("result")
                            error = msg.get("error")
                            bridge.handle_response(request_id, result, error)

                # ── 编辑响应（用户接受/拒绝，来自 VSCode）──
                elif msg_type == "edit_response":
                    if agent:
                        mgr = getattr(agent, "_edit_proposal_manager", None)
                        if mgr:
                            edit_id = msg.get("edit_id", "")
                            accepted = msg.get("accepted", False)
                            message = msg.get("message", "")
                            mgr.handle_response(edit_id, accepted, message)

                # ── 终端响应（来自 VSCode 终端执行结果）──
                elif msg_type == "terminal_response":
                    if agent:
                        from spirit.tools.terminal_tool import get_terminal_bridge
                        bridge = get_terminal_bridge(session_id)
                        if bridge:
                            request_id = msg.get("request_id", "")
                            output = msg.get("output")
                            exit_code = msg.get("exit_code", 0)
                            error = msg.get("error")
                            timed_out = msg.get("timed_out", False)
                            bridge.handle_response(
                                request_id, output, exit_code, error, timed_out
                            )

                # ── 中断 ──
                elif msg_type == "interrupt":
                    if agent:
                        agent.interrupt()
                        await _ws_send(websocket, StatusUpdate("idle", "已中断"))

                # ── 重置 ──
                elif msg_type == "reset":
                    if agent:
                        agent.reset_session()
                        session_id = agent.session_id
                        await websocket.send_json({
                            "type": "session",
                            "session_id": session_id,
                        })
                        await _ws_send(websocket, StatusUpdate("idle", "会话已重置"))

                # ── 心跳 ──
                elif msg_type == "ping":
                    await websocket.send_json({"type": "pong"})

                else:
                    await _ws_send(websocket, ErrorEvent(
                        code="unknown_type",
                        message=f"未知消息类型: {msg_type}",
                    ))

        except WebSocketDisconnect:
            logger.info("WebSocket 连接已断开: session=%s", session_id)
        except Exception as e:
            logger.error("WebSocket 异常: %s", e)


async def _ws_send(websocket: WebSocket, event) -> None:
    """发送事件到 WebSocket。"""
    try:
        await websocket.send_json(event_to_dict(event))
    except Exception:
        pass  # 连接可能已断开


def _setup_agent_callbacks(agent, websocket: WebSocket) -> None:
    """为 Agent 设置通过 WebSocket 推送事件的回调。

    注意：这些回调在 Agent 的工作线程中同步调用，
    需要通过 asyncio.run_coroutine_threadsafe 投递到事件循环。
    """
    loop = asyncio.get_event_loop()

    def on_tool_start(name: str, args: dict):
        event = ToolCallStart(
            tool_name=name,
            tool_call_id=str(uuid.uuid4())[:8],
            args=args,
        )
        asyncio.run_coroutine_threadsafe(
            _ws_send(websocket, event), loop
        )

    def on_tool_complete(name: str, result: str):
        event = ToolCallResult(
            tool_name=name,
            tool_call_id="",
            result=result[:300],
        )
        asyncio.run_coroutine_threadsafe(
            _ws_send(websocket, event), loop
        )

    agent.on_tool_start = on_tool_start
    agent.on_tool_complete = on_tool_complete


def _setup_code_intelligence_bridge(agent, websocket: WebSocket) -> None:
    """为 Agent 设置代码智能桥（Agent → VSCode 请求通道）。

    工具调用 bridge.request() 时，通过此回调发送请求到 VSCode。
    VSCode 执行代码智能命令后，通过 code_intelligence_response 返回。
    """
    # 避免重复设置
    if getattr(agent, "_code_intelligence_bridge", None):
        return

    from spirit.tools.code_intelligence import CodeIntelligenceBridge

    loop = asyncio.get_event_loop()

    def send_to_vscode(event_type: str, data: dict):
        """发送代码智能请求到 VSCode（线程安全）。"""
        payload = {"type": event_type, **data}
        asyncio.run_coroutine_threadsafe(
            websocket.send_json(payload), loop
        )

    bridge = CodeIntelligenceBridge(send_fn=send_to_vscode)
    agent._code_intelligence_bridge = bridge


def _setup_edit_proposal_manager(agent, websocket: WebSocket) -> None:
    """为 Agent 设置编辑提案管理器（Agent → VSCode 编辑提案通道）。

    文件编辑工具调用 mgr.propose_edit() 时，通过回调发送提案到 VSCode。
    用户接受/拒绝后，通过 edit_response 返回。
    """
    # 避免重复设置
    if getattr(agent, "_edit_proposal_manager", None):
        return

    from spirit.tools.edit_proposal import EditProposalManager

    loop = asyncio.get_event_loop()

    def send_proposal_to_vscode(proposal: dict):
        """发送编辑提案到 VSCode（线程安全）。"""
        asyncio.run_coroutine_threadsafe(
            websocket.send_json(proposal), loop
        )

    mgr = EditProposalManager(send_fn=send_proposal_to_vscode)
    agent._edit_proposal_manager = mgr


def _setup_terminal_bridge(agent, session_id: str, websocket: WebSocket) -> None:
    """为 Agent 设置终端桥（Agent → VSCode 终端执行通道）。

    终端工具调用 bridge.execute() 时，通过回调发送请求到 VSCode。
    VSCode 在集成终端中执行命令后，通过 terminal_response 返回。
    """
    from spirit.tools.terminal_tool import register_terminal_bridge, set_agent_platform

    loop = asyncio.get_event_loop()

    def send_to_vscode(data: dict):
        """发送终端请求到 VSCode（线程安全）。"""
        asyncio.run_coroutine_threadsafe(
            websocket.send_json(data), loop
        )

    bridge = register_terminal_bridge(session_id, send_to_vscode)
    set_agent_platform(session_id, "vscode")
    agent._terminal_bridge = bridge


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------

app = create_app()


def run_server(host: str = "127.0.0.1", port: int = 8765, reload: bool = False):
    """启动 API 服务。"""
    import uvicorn
    logger.info("Spirit Agent 服务启动: http://%s:%d", host, port)
    uvicorn.run(
        "spirit.api.server:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


if __name__ == "__main__":
    run_server()
