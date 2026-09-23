"""最小 TUI 网关核心 — 协议信封 + RPC 路由 + 会话注册（Phase 4.4）。

对标 Hermes ``tui_gateway/server.py``（15619 行）的**精简子集**：只保留协议信封、方法路由、
会话注册，以及把 :mod:`spirit.tui.render` / :mod:`spirit.tui.project_tree` /
:mod:`spirit.tui.slash_worker` 三模块接成 RPC 的核心逻辑。Hermes 完整服务器里的子进程编排、
MCP 发现、计费、审批脱敏、compute host 等不在本子集内。

RPC 方法（对齐 Hermes 命名）：``ping`` · ``session.create`` · ``session.list`` ·
``session.close`` · ``projects.tree`` · ``render.message`` · ``render.diff`` · ``slash.exec``。

**可测试抽象层**：网关是**传输无关**的——:meth:`TuiGateway.handle_message` 是纯同步函数
（``msg dict -> 响应信封 dict``），所有外部依赖（git ``resolve``、``renderer``、``slash_runner``、
``clock``、session id 工厂）都是可注入 seam，故协议 / 路由 / 会话生命周期全部离线单测，无需真实
WebSocket 或子进程。部署时把 ``websockets`` 的 send/recv 适配到 :meth:`handle_message` 即可
（信封与 :class:`spirit.desktop.ws_server.WSServer` 的 ``type: response/event`` 约定一致）。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Optional

# 请求信封键：``{"id", "method"|"action", "params"}``；响应：``{"type":"response", ...}``；
# 事件：``{"type":"event", "event", "data", "timestamp"}``。
RESPONSE_TYPE = "response"
EVENT_TYPE = "event"


class SessionRegistry:
    """内存会话注册表（TUI 会话生命周期）。可注入 ``clock`` / ``id_factory`` 供确定性测试。"""

    def __init__(self, *, clock: Optional[Callable[[], float]] = None,
                 id_factory: Optional[Callable[[], str]] = None) -> None:
        self._clock = clock or time.time
        self._id_factory = id_factory
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._counter = 0

    def _next_id(self) -> str:
        if self._id_factory is not None:
            return self._id_factory()
        self._counter += 1
        return f"sess-{self._counter}"

    def create(self, *, session_key: Optional[str] = None, model: str = "", cwd: str = "") -> Dict[str, Any]:
        sid = self._next_id()
        session = {
            "id": sid,
            "session_key": session_key or sid,
            "model": model,
            "cwd": cwd,
            "created_at": self._clock(),
        }
        self._sessions[sid] = session
        return dict(session)

    def get(self, session_id: Optional[str]) -> Optional[Dict[str, Any]]:
        session = self._sessions.get(session_id) if session_id else None
        return dict(session) if session else None

    def list(self) -> list:
        return [dict(s) for s in self._sessions.values()]

    def close(self, session_id: Optional[str]) -> bool:
        if not session_id:
            return False
        return self._sessions.pop(session_id, None) is not None

    def count(self) -> int:
        return len(self._sessions)


class TuiGateway:
    """传输无关的 TUI 网关：方法注册表 + 信封成形 + 会话注册 + 三模块 RPC 接线。"""

    def __init__(
        self,
        *,
        resolve: Optional[Callable[[str], Optional[dict]]] = None,
        renderer: Optional[Any] = None,
        slash_runner: Optional[Callable[[str], str]] = None,
        clock: Optional[Callable[[], float]] = None,
        id_factory: Optional[Callable[[], str]] = None,
        session_store: Optional[SessionRegistry] = None,
    ) -> None:
        self._resolve = resolve
        self._renderer = renderer
        self._slash_runner = slash_runner or (lambda command: f"(no slash runner for {command})")
        self._clock = clock or time.time
        self.sessions = session_store or SessionRegistry(clock=self._clock, id_factory=id_factory)
        self._methods: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
        self._register_builtins()

    # ------------------------------------------------------------------
    # 方法注册
    # ------------------------------------------------------------------
    def register(self, method: str, handler: Callable[[Dict[str, Any]], Any]) -> None:
        """注册一个 RPC 方法（handler 签名：``(params: dict) -> data``；抛异常折叠成错误响应）。"""
        self._methods[method] = handler

    def list_methods(self) -> list:
        return sorted(self._methods)

    def _register_builtins(self) -> None:
        self.register("ping", self._rpc_ping)
        self.register("session.create", self._rpc_session_create)
        self.register("session.list", self._rpc_session_list)
        self.register("session.close", self._rpc_session_close)
        self.register("projects.tree", self._rpc_projects_tree)
        self.register("render.message", self._rpc_render_message)
        self.register("render.diff", self._rpc_render_diff)
        self.register("slash.exec", self._rpc_slash_exec)

    # ------------------------------------------------------------------
    # 信封成形
    # ------------------------------------------------------------------
    def _ok_response(self, req_id: Any, method: str, data: Any) -> Dict[str, Any]:
        return {
            "type": RESPONSE_TYPE, "id": req_id, "method": method,
            "ok": True, "data": data, "timestamp": self._clock(),
        }

    def _error_response(self, req_id: Any, method: str, error: str) -> Dict[str, Any]:
        return {
            "type": RESPONSE_TYPE, "id": req_id, "method": method,
            "ok": False, "error": error, "timestamp": self._clock(),
        }

    def make_event(self, event: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """构造一个推送事件信封（供网关主动向 TUI 广播状态）。"""
        return {"type": EVENT_TYPE, "event": event, "data": data or {}, "timestamp": self._clock()}

    # ------------------------------------------------------------------
    # 派发（纯函数，无 I/O）
    # ------------------------------------------------------------------
    def handle_message(self, msg: Any) -> Dict[str, Any]:
        """把一个请求信封 dict 派发成响应信封 dict（同步、纯、绝不抛）。"""
        if not isinstance(msg, dict):
            return self._error_response(None, "", "malformed message")

        req_id = msg.get("id")
        method = msg.get("method") or msg.get("action")
        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(method, str) or not method:
            return self._error_response(req_id, method if isinstance(method, str) else "", "missing method")
        if not isinstance(params, dict):
            return self._error_response(req_id, method, "params must be an object")

        handler = self._methods.get(method)
        if handler is None:
            return self._error_response(req_id, method, f"unknown method: {method}")
        try:
            data = handler(params)
        except Exception as exc:  # 任何 RPC 崩溃折叠成错误响应，绝不掀翻网关
            return self._error_response(req_id, method, str(exc))
        return self._ok_response(req_id, method, data)

    def handle_line(self, line: str) -> str:
        """JSON-line 便捷包装：``str -> str``（非法 JSON → 错误响应帧）。"""
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return json.dumps(self._error_response(None, "", "invalid JSON"), ensure_ascii=False)
        return json.dumps(self.handle_message(msg), ensure_ascii=False)

    # ------------------------------------------------------------------
    # 内置 RPC handlers
    # ------------------------------------------------------------------
    def _rpc_ping(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"pong": True}

    def _rpc_session_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        session = self.sessions.create(
            session_key=params.get("session_key"),
            model=params.get("model", ""),
            cwd=params.get("cwd", ""),
        )
        return {"session": session}

    def _rpc_session_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"sessions": self.sessions.list(), "count": self.sessions.count()}

    def _rpc_session_close(self, params: Dict[str, Any]) -> Dict[str, Any]:
        sid = params.get("id") or params.get("session_id")
        return {"closed": self.sessions.close(sid), "id": sid}

    def _rpc_projects_tree(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from spirit.tui.project_tree import build_tree

        return build_tree(
            params.get("projects") or [],
            params.get("sessions") or [],
            params.get("discovered_repos") or [],
            self._resolve,
            preview_limit=int(params.get("preview_limit", 3)),
            hydrate=bool(params.get("hydrate", False)),
        )

    def _rpc_render_message(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from spirit.tui.render import render_message

        rendered = render_message(
            params.get("text", ""), int(params.get("cols", 80)), renderer=self._renderer,
        )
        return {"rendered": rendered}

    def _rpc_render_diff(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from spirit.tui.render import render_diff

        rendered = render_diff(
            params.get("text", ""), int(params.get("cols", 80)), renderer=self._renderer,
        )
        return {"rendered": rendered}

    def _rpc_slash_exec(self, params: Dict[str, Any]) -> Dict[str, Any]:
        from spirit.tui.slash_worker import normalize_command

        command = normalize_command(params.get("command", ""))
        return {"command": command, "output": self._slash_runner(command)}


__all__ = ["RESPONSE_TYPE", "EVENT_TYPE", "SessionRegistry", "TuiGateway"]
