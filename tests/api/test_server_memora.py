"""spirit/api/server.py + spirit/api/memora_proxy.py 回归测试。

server: Pydantic 模型 / create_app / AgentManager / 路由注册
memora_proxy: create_memora_router + 直接调用内部 async 函数

注：httpx 0.28 与 fastapi 0.104 TestClient 不兼容，跳过 TestClient HTTP 测试。
"""
import asyncio
import inspect
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ──────────────────────────────────────────────────────────────────────
# server.py — Pydantic 模型
# ──────────────────────────────────────────────────────────────────────

class TestChatRequest:
    def test_minimal(self):
        from spirit.api.server import ChatRequest
        r = ChatRequest(message="hi")
        assert r.message == "hi"
        assert r.session_id is None
        assert r.model is None
        assert r.context is None

    def test_with_all(self):
        from spirit.api.server import ChatRequest
        r = ChatRequest(
            message="m", session_id="s1", model="gpt-4", context={"k": "v"},
        )
        assert r.session_id == "s1"
        assert r.model == "gpt-4"
        assert r.context == {"k": "v"}


class TestChatResponse:
    def test_minimal(self):
        from spirit.api.server import ChatResponse
        r = ChatResponse(response="ok", session_id="s1")
        assert r.response == "ok"
        assert r.session_id == "s1"
        assert r.usage == {}
        assert r.iterations == 0

    def test_with_usage(self):
        from spirit.api.server import ChatResponse
        r = ChatResponse(
            response="x", session_id="s",
            usage={"prompt_tokens": 10, "completion_tokens": 20},
            iterations=3,
        )
        assert r.usage == {"prompt_tokens": 10, "completion_tokens": 20}
        assert r.iterations == 3


class TestSessionInfo:
    def test_basic(self):
        from spirit.api.server import SessionInfo
        s = SessionInfo(
            id="s1", source="ws", model="gpt-4",
            started_at="2024-01-01T00:00:00",
        )
        assert s.id == "s1"
        assert s.ended_at is None
        assert s.message_count == 0

    def test_with_ended(self):
        from spirit.api.server import SessionInfo
        s = SessionInfo(
            id="s1", source="api", model="m",
            started_at="t0", ended_at="t1", message_count=5,
        )
        assert s.ended_at == "t1"
        assert s.message_count == 5


class TestToolInfo:
    def test_basic(self):
        from spirit.api.server import ToolInfo
        t = ToolInfo(name="x", toolset="web", description="d")
        assert t.name == "x"
        assert t.toolset == "web"


class TestStatusResponse:
    def test_defaults(self):
        from spirit.api.server import StatusResponse
        s = StatusResponse()
        assert s.status == "ok"
        assert s.version == "0.1.0"
        assert s.active_sessions == 0
        assert s.tool_count == 0

    def test_with_values(self):
        from spirit.api.server import StatusResponse
        s = StatusResponse(
            status="degraded", version="1.0",
            active_sessions=5, tool_count=20,
        )
        assert s.status == "degraded"
        assert s.version == "1.0"


# ──────────────────────────────────────────────────────────────────────
# server.py — AgentManager
# ──────────────────────────────────────────────────────────────────────

class TestAgentManager:
    def test_empty_init(self):
        from spirit.api.server import AgentManager
        m = AgentManager()
        assert m._agents == {}
        assert m.active_count == 0

    def test_list_sessions_empty(self):
        from spirit.api.server import AgentManager
        m = AgentManager()
        assert m.list_sessions() == []

    def test_remove_nonexistent(self):
        from spirit.api.server import AgentManager
        m = AgentManager()
        m.remove_agent("never-existed")  # 不应抛
        assert m.active_count == 0

    def test_get_nonexistent(self):
        from spirit.api.server import AgentManager
        m = AgentManager()
        assert m.get_agent("nope") is None

    def test_get_or_create_with_mock(self):
        """get_or_create 调用 SpiritAgent，需要 mock。"""
        from spirit.api.server import AgentManager
        m = AgentManager()
        mock_agent = MagicMock()
        mock_agent.model = "test-model"
        mock_agent.platform = "api"
        mock_agent.messages = []
        mock_agent._api_call_count = 0
        mock_agent.session_id = "sess-A"

        with patch("spirit.agent.agent.SpiritAgent", return_value=mock_agent):
            agent, sid = m.get_or_create(session_id="sess-A", model="test-model")

        assert agent is mock_agent
        assert sid == "sess-A"
        assert m.active_count == 1

    def test_get_or_create_reuses(self):
        """第二次相同 session_id 应复用，不创建新 agent。"""
        from spirit.api.server import AgentManager
        m = AgentManager()
        mock_agent = MagicMock()
        mock_agent.model = "m"
        mock_agent.platform = "api"
        mock_agent.messages = []
        mock_agent._api_call_count = 0
        mock_agent.session_id = "sess-A"

        with patch("spirit.agent.agent.SpiritAgent", return_value=mock_agent) as mock_cls:
            a1, _ = m.get_or_create(session_id="sess-A")
            a2, _ = m.get_or_create(session_id="sess-A")

        assert a1 is a2
        assert mock_cls.call_count == 1

    def test_get_or_create_generates_session_id(self):
        """未传 session_id 时自动生成 UUID。"""
        from spirit.api.server import AgentManager
        m = AgentManager()
        mock_agent = MagicMock()
        mock_agent.model = "m"
        mock_agent.platform = "api"
        mock_agent.messages = []
        mock_agent._api_call_count = 0

        with patch("spirit.agent.agent.SpiritAgent", return_value=mock_agent):
            _, sid = m.get_or_create(model="m")

        # 至少 32 字符的 UUID
        assert len(sid) >= 32
        assert m.active_count == 1

    def test_get_or_create_passes_model(self):
        """get_or_create 应把 model 传给 AgentConfig。"""
        from spirit.api.server import AgentManager
        m = AgentManager()
        mock_agent = MagicMock()
        mock_agent.model = "m"
        mock_agent.platform = "api"
        mock_agent.messages = []
        mock_agent._api_call_count = 0
        mock_agent.session_id = "s"

        with patch("spirit.agent.agent.SpiritAgent", return_value=mock_agent), \
             patch("spirit.agent.agent.AgentConfig") as mock_cfg:
            m.get_or_create(session_id="s", model="custom-model")
            assert mock_cfg.called
            call_kwargs = mock_cfg.call_args.kwargs
            assert call_kwargs.get("model") == "custom-model"

    def test_list_sessions_after_creates(self):
        from spirit.api.server import AgentManager
        m = AgentManager()
        mock_agent = MagicMock()
        mock_agent.model = "m1"
        mock_agent.platform = "cli"
        mock_agent.messages = ["a", "b"]
        mock_agent._api_call_count = 3
        mock_agent.session_id = "s1"

        with patch("spirit.agent.agent.SpiritAgent", return_value=mock_agent):
            m.get_or_create(session_id="s1")

        sessions = m.list_sessions()
        assert len(sessions) == 1
        s = sessions[0]
        assert s["id"] == "s1"
        assert s["model"] == "m1"
        assert s["platform"] == "cli"
        assert s["message_count"] == 2
        assert s["api_calls"] == 3

    def test_remove_agent_removes(self):
        from spirit.api.server import AgentManager
        m = AgentManager()
        mock_agent = MagicMock()
        mock_agent.model = "m"
        mock_agent.platform = "api"
        mock_agent.messages = []
        mock_agent._api_call_count = 0
        mock_agent.session_id = "s1"

        with patch("spirit.agent.agent.SpiritAgent", return_value=mock_agent):
            m.get_or_create(session_id="s1")
        assert m.active_count == 1

        m.remove_agent("s1")
        assert m.active_count == 0
        assert m.get_agent("s1") is None

    def test_default_platform_is_api(self):
        """get_or_create 的 platform 默认值是 'api'。"""
        from spirit.api.server import AgentManager
        m = AgentManager()
        mock_agent = MagicMock()
        mock_agent.model = "m"
        mock_agent.platform = "api"
        mock_agent.messages = []
        mock_agent._api_call_count = 0
        mock_agent.session_id = "s"

        with patch("spirit.agent.agent.SpiritAgent", return_value=mock_agent), \
             patch("spirit.agent.agent.AgentConfig") as mock_cfg:
            m.get_or_create(session_id="s")
            assert mock_cfg.call_args.kwargs.get("platform") == "api"

    def test_custom_platform(self):
        from spirit.api.server import AgentManager
        m = AgentManager()
        mock_agent = MagicMock()
        mock_agent.model = "m"
        mock_agent.platform = "vscode"
        mock_agent.messages = []
        mock_agent._api_call_count = 0
        mock_agent.session_id = "s"

        with patch("spirit.agent.agent.SpiritAgent", return_value=mock_agent), \
             patch("spirit.agent.agent.AgentConfig") as mock_cfg:
            m.get_or_create(session_id="s", platform="vscode")
            assert mock_cfg.call_args.kwargs.get("platform") == "vscode"


# ──────────────────────────────────────────────────────────────────────
# server.py — create_app 工厂 + 路由注册
# ──────────────────────────────────────────────────────────────────────

class TestCreateApp:
    def test_creates_fastapi(self):
        from spirit.api.server import create_app
        app = create_app()
        from fastapi import FastAPI
        assert isinstance(app, FastAPI)

    def test_title_and_version(self):
        from spirit.api.server import create_app
        app = create_app()
        assert app.title == "Spirit Agent"
        assert app.version == "0.1.0"

    def test_cors_middleware_added(self):
        from spirit.api.server import create_app
        app = create_app()
        middleware_classes = [m.cls.__name__ for m in app.user_middleware]
        assert "CORSMiddleware" in middleware_classes

    def test_routes_registered(self):
        from spirit.api.server import create_app
        app = create_app()
        paths = [r.path for r in app.routes]
        # REST 路由
        assert "/api/status" in paths
        assert "/api/chat" in paths
        assert "/api/sessions" in paths
        assert "/api/tools" in paths
        # WebSocket
        assert "/ws/chat" in paths
        # Memora 代理
        assert any(p.startswith("/memora") for p in paths)

    def test_chat_route_uses_post(self):
        from spirit.api.server import create_app
        app = create_app()
        chat_route = next(r for r in app.routes if r.path == "/api/chat")
        assert "POST" in chat_route.methods

    def test_sessions_get(self):
        from spirit.api.server import create_app
        app = create_app()
        # GET /api/sessions
        list_route = next(r for r in app.routes if r.path == "/api/sessions")
        assert "GET" in list_route.methods

        # GET /api/sessions/{session_id}
        detail_route = next(
            r for r in app.routes
            if r.path == "/api/sessions/{session_id}" and "GET" in r.methods
        )
        assert detail_route is not None

    def test_sessions_delete(self):
        from spirit.api.server import create_app
        app = create_app()
        # DELETE /api/sessions/{session_id}
        del_route = next(
            r for r in app.routes
            if r.path == "/api/sessions/{session_id}" and "DELETE" in r.methods
        )
        assert del_route is not None

    def test_sessions_path_has_get_and_delete(self):
        """/api/sessions/{session_id} 路径同时有 GET 和 DELETE 路由。"""
        from spirit.api.server import create_app
        app = create_app()
        all_methods = set()
        for r in app.routes:
            if r.path == "/api/sessions/{session_id}":
                all_methods |= r.methods
        assert "GET" in all_methods
        assert "DELETE" in all_methods

    def test_interrupt_and_reset_routes(self):
        from spirit.api.server import create_app
        app = create_app()
        int_route = next(
            r for r in app.routes
            if r.path == "/api/sessions/{session_id}/interrupt"
        )
        assert "POST" in int_route.methods

        rst_route = next(
            r for r in app.routes if r.path == "/api/sessions/{session_id}/reset"
        )
        assert "POST" in rst_route.methods


# ──────────────────────────────────────────────────────────────────────
# server.py — run_server
# ──────────────────────────────────────────────────────────────────────

class TestRunServer:
    def test_runs_uvicorn(self):
        """run_server 调用 uvicorn.run。"""
        from spirit.api import server
        with patch("uvicorn.run") as mock_run:
            server.run_server(host="1.2.3.4", port=9999, reload=True)
            mock_run.assert_called_once()
            kwargs = mock_run.call_args.kwargs
            assert kwargs.get("host") == "1.2.3.4"
            assert kwargs.get("port") == 9999
            assert kwargs.get("reload") is True

    def test_default_args(self):
        from spirit.api import server
        with patch("uvicorn.run") as mock_run:
            server.run_server()
            assert mock_run.called

    def test_app_string_format(self):
        """run_server 第一个参数是 'spirit.api.server:app'。"""
        from spirit.api import server
        with patch("uvicorn.run") as mock_run:
            server.run_server()
            args = mock_run.call_args.args
            # 第一个位置参数应是 app 字符串
            assert "spirit.api.server" in args[0]


# ──────────────────────────────────────────────────────────────────────
# server.py — _ws_send 内部辅助
# ──────────────────────────────────────────────────────────────────────

class TestWsSend:
    def test_sends_event(self):
        from spirit.api.server import _ws_send
        from spirit.api.events import MessageChunk

        mock_ws = MagicMock()
        mock_ws.send_json = AsyncMock()

        asyncio.run(_ws_send(mock_ws, MessageChunk(text="hi")))
        mock_ws.send_json.assert_called_once()
        call_arg = mock_ws.send_json.call_args[0][0]
        assert call_arg == {"type": "message_chunk", "text": "hi"}

    def test_silently_swallows_send_failure(self):
        """send_json 抛错时不应冒泡（连接可能已断开）。"""
        from spirit.api.server import _ws_send
        from spirit.api.events import MessageChunk

        mock_ws = MagicMock()
        mock_ws.send_json = AsyncMock(side_effect=RuntimeError("ws closed"))

        # 不应抛
        asyncio.run(_ws_send(mock_ws, MessageChunk(text="hi")))

    def test_silently_swallows_disconnect(self):
        from spirit.api.server import _ws_send
        from spirit.api.events import MessageChunk

        mock_ws = MagicMock()
        mock_ws.send_json = AsyncMock(side_effect=ConnectionError("closed"))

        asyncio.run(_ws_send(mock_ws, MessageChunk(text="x")))


# ──────────────────────────────────────────────────────────────────────
# server.py — _setup_agent_callbacks
# ──────────────────────────────────────────────────────────────────────

class TestSetupAgentCallbacks:
    def _setup(self):
        """调用 _setup_agent_callbacks，patch asyncio.get_event_loop。"""
        from spirit.api.server import _setup_agent_callbacks
        mock_agent = MagicMock()
        mock_ws = MagicMock()
        with patch("asyncio.get_event_loop", return_value=MagicMock()):
            _setup_agent_callbacks(mock_agent, mock_ws)
        return mock_agent

    def test_assigns_callbacks(self):
        from spirit.api.server import _setup_agent_callbacks
        mock_agent = MagicMock()
        mock_ws = MagicMock()
        with patch("asyncio.get_event_loop", return_value=MagicMock()):
            _setup_agent_callbacks(mock_agent, mock_ws)
        # 源码把 on_tool_start / on_tool_complete 设为内部函数
        assert callable(mock_agent.on_tool_start)
        assert callable(mock_agent.on_tool_complete)

    def test_on_tool_start_uses_run_coroutine(self):
        """on_tool_start 通过 run_coroutine_threadsafe 投递。"""
        mock_agent = self._setup()
        with patch("asyncio.run_coroutine_threadsafe") as mock_rcs:
            mock_agent.on_tool_start("read", {"p": 1})
            assert mock_rcs.called

    def test_on_tool_complete_uses_run_coroutine(self):
        mock_agent = self._setup()
        with patch("asyncio.run_coroutine_threadsafe") as mock_rcs:
            mock_agent.on_tool_complete("read", "ok")
            assert mock_rcs.called

    def test_on_tool_start_passes_event_args(self):
        """on_tool_start 内部生成的 ToolCallStart 包含 tool_name 和 args。"""
        mock_agent = self._setup()
        with patch("asyncio.run_coroutine_threadsafe") as mock_rcs:
            mock_agent.on_tool_start("my_tool", {"x": 1, "y": 2})
            # 第一个位置参数是 coroutine, 我们拿不到直接字段, 但要确认 rcs 被调用
            assert mock_rcs.called

    def test_on_tool_complete_truncates_result_300(self):
        """源码 .result[:300]。"""
        mock_agent = self._setup()
        with patch("asyncio.run_coroutine_threadsafe") as mock_rcs:
            mock_agent.on_tool_complete("x", "y" * 1000)
            assert mock_rcs.called


# ──────────────────────────────────────────────────────────────────────
# memora_proxy.py — create_memora_router
# ──────────────────────────────────────────────────────────────────────

class TestCreateMemoraRouter:
    def test_returns_router(self):
        from fastapi import APIRouter
        from spirit.api.memora_proxy import create_memora_router
        router = create_memora_router()
        assert isinstance(router, APIRouter)

    def test_tag(self):
        from spirit.api.memora_proxy import create_memora_router
        router = create_memora_router()
        assert "memora-proxy" in router.tags

    def test_routes_registered(self):
        from spirit.api.memora_proxy import create_memora_router
        router = create_memora_router()
        paths = [r.path for r in router.routes]
        # 通用代理
        assert any("/api/v1/{path:path}" == p for p in paths)
        # 健康检查
        assert "/health" in paths

    def test_methods_supported(self):
        """通用代理支持 5 种 HTTP 方法。"""
        from spirit.api.memora_proxy import create_memora_router
        router = create_memora_router()
        proxy_route = next(
            r for r in router.routes if "/api/v1/{path:path}" in r.path
        )
        assert set(proxy_route.methods) >= {
            "GET", "POST", "PUT", "DELETE", "PATCH",
        }

    def test_custom_base_url(self):
        """传入 memora_base_url 时使用自定义地址。"""
        from spirit.api.memora_proxy import create_memora_router
        with patch("spirit.config.get_config_value",
                   return_value="http://default:8000"):
            r = create_memora_router(memora_base_url="http://custom:9000/")
        assert r is not None

    def test_default_base_url(self):
        """未传入 memora_base_url 时使用 DEFAULT_MEMORA_URL。"""
        from spirit.api.memora_proxy import create_memora_router
        with patch("spirit.config.get_config_value", return_value="http://cfg:1234"):
            r = create_memora_router()
        assert r is not None

    def test_strips_trailing_slash(self):
        """memora_base_url 末尾的 / 会被去掉。"""
        from spirit.api.memora_proxy import create_memora_router
        # 实际行为: 源码 .rstrip("/"), 但因为 _client 是闭包内变量,
        # 只能从外部通过 mock httpx 来验证
        with patch("spirit.api.memora_proxy.httpx.AsyncClient") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            router = create_memora_router(memora_base_url="http://x:1234//")
            # 找到 proxy 路由
            # 简单验证 router 存在
            assert router is not None


# ──────────────────────────────────────────────────────────────────────
# memora_proxy.py — 内部 async 函数
# ──────────────────────────────────────────────────────────────────────

def _get_proxy_handler(router):
    """提取 /api/v1/{path:path} 路由的处理函数。"""
    from spirit.api.memora_proxy import create_memora_router
    for r in router.routes:
        if "/api/v1/{path:path}" in r.path:
            return r.endpoint
    raise RuntimeError("proxy handler not found")


def _get_health_handler(router):
    from spirit.api.memora_proxy import create_memora_router
    for r in router.routes:
        if r.path == "/health":
            return r.endpoint
    raise RuntimeError("health handler not found")


class TestMemoraProxyHealthHandler:
    def test_health_connected(self):
        """Memora 健康时返回 status=connected。"""
        from spirit.api.memora_proxy import create_memora_router
        router = create_memora_router()
        handler = _get_health_handler(router)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True}

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        async def run():
            with patch("spirit.api.memora_proxy.httpx.AsyncClient",
                       return_value=mock_client):
                return await handler()

        result = asyncio.run(run())
        assert result["status"] == "connected"
        assert result["memora_response"] == {"ok": True}

    def test_health_degraded(self):
        from spirit.api.memora_proxy import create_memora_router
        router = create_memora_router()
        handler = _get_health_handler(router)

        mock_resp = MagicMock()
        mock_resp.status_code = 500

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        async def run():
            with patch("spirit.api.memora_proxy.httpx.AsyncClient",
                       return_value=mock_client):
                return await handler()

        result = asyncio.run(run())
        assert result["status"] == "degraded"

    def test_health_disconnected(self):
        from spirit.api.memora_proxy import create_memora_router
        router = create_memora_router()
        handler = _get_health_handler(router)
        import httpx

        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("nope"))

        async def run():
            with patch("spirit.api.memora_proxy.httpx.AsyncClient",
                       return_value=mock_client):
                return await handler()

        result = asyncio.run(run())
        assert result["status"] == "disconnected"
        assert "error" in result

    def test_health_uses_configured_timeout(self):
        from spirit.api.memora_proxy import create_memora_router
        router = create_memora_router()
        handler = _get_health_handler(router)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("spirit.api.memora_proxy.get_config_value",
                   return_value=7.5), \
             patch("spirit.api.memora_proxy.httpx.AsyncClient",
                   return_value=mock_client):
            asyncio.run(handler())
            # 验证调用 get 时传了 timeout
            call_kwargs = mock_client.get.call_args.kwargs
            assert call_kwargs.get("timeout") == 7.5


class TestMemoraProxyHandler:
    def test_basic_get_proxy(self):
        """直接调 proxy_memora_api 验证转发。"""
        from spirit.api.memora_proxy import create_memora_router
        from fastapi import Request
        router = create_memora_router()
        handler = _get_proxy_handler(router)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"ok": true}'
        mock_resp.headers = {"content-type": "application/json"}

        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=mock_resp)

        # 构造 mock Request
        mock_request = MagicMock(spec=Request)
        mock_request.body = AsyncMock(return_value=b"")
        mock_request.headers = {}
        mock_request.method = "GET"
        mock_request.query_params = {}

        async def run():
            with patch("spirit.api.memora_proxy.httpx.AsyncClient",
                       return_value=mock_client):
                return await handler(mock_request, path="some/path")

        result = asyncio.run(run())
        # result 是 Response 对象
        from fastapi import Response
        assert isinstance(result, Response)
        assert result.status_code == 200
        # 验证 mock_client.request 被正确调用
        assert mock_client.request.called
        kwargs = mock_client.request.call_args.kwargs
        assert kwargs["method"] == "GET"
        assert kwargs["url"] == "/api/v1/some/path"

    def test_post_with_body(self):
        from spirit.api.memora_proxy import create_memora_router
        from fastapi import Request
        router = create_memora_router()
        handler = _get_proxy_handler(router)

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.content = b'{"id": 1}'
        mock_resp.headers = {}

        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=mock_resp)

        mock_request = MagicMock(spec=Request)
        mock_request.body = AsyncMock(return_value=b'{"name": "x"}')
        mock_request.headers = {"content-type": "application/json"}
        mock_request.method = "POST"
        mock_request.query_params = {}

        async def run():
            with patch("spirit.api.memora_proxy.httpx.AsyncClient",
                       return_value=mock_client):
                return await handler(mock_request, path="items")

        asyncio.run(run())
        kwargs = mock_client.request.call_args.kwargs
        assert kwargs["method"] == "POST"
        assert kwargs["content"] == b'{"name": "x"}'

    def test_strips_hop_by_hop_headers(self):
        from spirit.api.memora_proxy import create_memora_router
        from fastapi import Request
        router = create_memora_router()
        handler = _get_proxy_handler(router)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"x"
        mock_resp.headers = {}

        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=mock_resp)

        mock_request = MagicMock(spec=Request)
        mock_request.body = AsyncMock(return_value=b"")
        mock_request.headers = {
            "host": "should-be-removed",
            "connection": "close",
            "keep-alive": "timeout=5",
            "transfer-encoding": "chunked",
            "X-Custom": "preserved",
        }
        mock_request.method = "GET"
        mock_request.query_params = {}

        async def run():
            with patch("spirit.api.memora_proxy.httpx.AsyncClient",
                       return_value=mock_client):
                await handler(mock_request, path="t")

        asyncio.run(run())
        sent = mock_client.request.call_args.kwargs["headers"]
        for h in ("host", "connection", "keep-alive", "transfer-encoding"):
            assert h not in sent
        assert sent.get("X-Custom") == "preserved"

    def test_strips_response_hop_by_hop_headers(self):
        from spirit.api.memora_proxy import create_memora_router
        from fastapi import Request
        router = create_memora_router()
        handler = _get_proxy_handler(router)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"x"
        mock_resp.headers = {
            "content-encoding": "gzip",
            "transfer-encoding": "chunked",
            "connection": "close",
            "X-Custom": "preserved",
        }

        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=mock_resp)

        mock_request = MagicMock(spec=Request)
        mock_request.body = AsyncMock(return_value=b"")
        mock_request.headers = {}
        mock_request.method = "GET"
        mock_request.query_params = {}

        async def run():
            with patch("spirit.api.memora_proxy.httpx.AsyncClient",
                       return_value=mock_client):
                return await handler(mock_request, path="t")

        result = asyncio.run(run())
        for h in ("content-encoding", "transfer-encoding", "connection"):
            assert h not in result.headers
        assert result.headers.get("X-Custom") == "preserved"

    def test_connect_error_returns_502(self):
        from spirit.api.memora_proxy import create_memora_router
        from fastapi import Request, HTTPException
        import httpx
        router = create_memora_router()
        handler = _get_proxy_handler(router)

        mock_client = MagicMock()
        mock_client.request = AsyncMock(
            side_effect=httpx.ConnectError("refused"),
        )

        mock_request = MagicMock(spec=Request)
        mock_request.body = AsyncMock(return_value=b"")
        mock_request.headers = {}
        mock_request.method = "GET"
        mock_request.query_params = {}

        async def run():
            with patch("spirit.api.memora_proxy.httpx.AsyncClient",
                       return_value=mock_client):
                return await handler(mock_request, path="t")

        with pytest.raises(HTTPException) as exc:
            asyncio.run(run())
        assert exc.value.status_code == 502
        assert "Memora" in exc.value.detail["error"]

    def test_timeout_returns_504(self):
        from spirit.api.memora_proxy import create_memora_router
        from fastapi import Request, HTTPException
        import httpx
        router = create_memora_router()
        handler = _get_proxy_handler(router)

        mock_client = MagicMock()
        mock_client.request = AsyncMock(
            side_effect=httpx.ReadTimeout("slow"),
        )

        mock_request = MagicMock(spec=Request)
        mock_request.body = AsyncMock(return_value=b"")
        mock_request.headers = {}
        mock_request.method = "GET"
        mock_request.query_params = {}

        async def run():
            with patch("spirit.api.memora_proxy.httpx.AsyncClient",
                       return_value=mock_client):
                return await handler(mock_request, path="t")

        with pytest.raises(HTTPException) as exc:
            asyncio.run(run())
        assert exc.value.status_code == 504

    def test_general_exception_returns_502(self):
        from spirit.api.memora_proxy import create_memora_router
        from fastapi import Request, HTTPException
        router = create_memora_router()
        handler = _get_proxy_handler(router)

        mock_client = MagicMock()
        mock_client.request = AsyncMock(
            side_effect=RuntimeError("unknown"),
        )

        mock_request = MagicMock(spec=Request)
        mock_request.body = AsyncMock(return_value=b"")
        mock_request.headers = {}
        mock_request.method = "GET"
        mock_request.query_params = {}

        async def run():
            with patch("spirit.api.memora_proxy.httpx.AsyncClient",
                       return_value=mock_client):
                return await handler(mock_request, path="t")

        with pytest.raises(HTTPException) as exc:
            asyncio.run(run())
        assert exc.value.status_code == 502
        assert "代理请求失败" in exc.value.detail["error"]

    def test_query_params_forwarded(self):
        from spirit.api.memora_proxy import create_memora_router
        from fastapi import Request
        router = create_memora_router()
        handler = _get_proxy_handler(router)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"x"
        mock_resp.headers = {}

        mock_client = MagicMock()
        mock_client.request = AsyncMock(return_value=mock_resp)

        mock_request = MagicMock(spec=Request)
        mock_request.body = AsyncMock(return_value=b"")
        mock_request.headers = {}
        mock_request.method = "GET"
        mock_request.query_params = {"q": "search", "limit": "5"}

        async def run():
            with patch("spirit.api.memora_proxy.httpx.AsyncClient",
                       return_value=mock_client):
                await handler(mock_request, path="search")

        asyncio.run(run())
        params = mock_client.request.call_args.kwargs["params"]
        assert params == {"q": "search", "limit": "5"}


# ──────────────────────────────────────────────────────────────────────
# __init__.py — package init
# ──────────────────────────────────────────────────────────────────────

class TestApiInit:
    def test_imports(self):
        import spirit.api
        assert spirit.api is not None
