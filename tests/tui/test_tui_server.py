"""``spirit.tui.tui_server`` 网关协议 / 路由 / 会话 / 接线测试（Phase 4.4）。

对标 Hermes ``tests/tui_gateway/test_protocol.py``（信封形状、方法路由、错误折叠）+
``test_projects_rpc.py``（projects.tree 接线）的精简子集。:class:`TuiGateway` 是**传输无关**
纯同步层（``handle_message(msg dict) -> 响应信封 dict``），所有外部依赖（``resolve`` /
``renderer`` / ``slash_runner`` / ``clock`` / ``id_factory``）都是可注入 seam，故协议、路由、
会话生命周期、三模块 RPC 接线全部离线断言，无需真实 WebSocket / 子进程 / 渲染器 / git。
"""

from __future__ import annotations

import json

from spirit.tui.tui_server import (
    EVENT_TYPE,
    RESPONSE_TYPE,
    SessionRegistry,
    TuiGateway,
)

from .conftest import FakeRenderer


# ── 响应 / 事件信封形状 ──

def test_ok_response_shape():
    gw = TuiGateway(clock=lambda: 42.0)
    resp = gw.handle_message({"id": 1, "method": "ping"})
    assert resp == {
        "type": RESPONSE_TYPE, "id": 1, "method": "ping",
        "ok": True, "data": {"pong": True}, "timestamp": 42.0,
    }


def test_unknown_method_error():
    gw = TuiGateway()
    resp = gw.handle_message({"id": 2, "method": "nope"})
    assert resp["ok"] is False
    assert resp["error"] == "unknown method: nope"
    assert resp["id"] == 2
    assert resp["method"] == "nope"
    assert resp["type"] == RESPONSE_TYPE


def test_missing_method_error():
    gw = TuiGateway()
    resp = gw.handle_message({"id": 3})
    assert resp["ok"] is False
    assert resp["error"] == "missing method"
    assert resp["id"] == 3


def test_malformed_message_error():
    gw = TuiGateway()
    resp = gw.handle_message("not a dict")
    assert resp["ok"] is False
    assert resp["error"] == "malformed message"
    assert resp["id"] is None


def test_params_must_be_object_error():
    gw = TuiGateway()
    resp = gw.handle_message({"id": 4, "method": "ping", "params": "bad"})
    assert resp["ok"] is False
    assert resp["error"] == "params must be an object"


def test_handler_exception_folded_into_error():
    gw = TuiGateway()

    def boom(params):
        raise RuntimeError("handler down")

    gw.register("boom", boom)
    resp = gw.handle_message({"id": 5, "method": "boom"})
    assert resp["ok"] is False
    assert "handler down" in resp["error"]


def test_action_alias_for_method():
    gw = TuiGateway(clock=lambda: 1.0)
    resp = gw.handle_message({"id": 6, "action": "ping"})
    assert resp["ok"] is True
    assert resp["data"] == {"pong": True}
    assert resp["method"] == "ping"


def test_missing_params_defaults_to_empty():
    gw = TuiGateway()
    resp = gw.handle_message({"id": 7, "method": "ping"})
    assert resp["ok"] is True


def test_make_event_shape():
    gw = TuiGateway(clock=lambda: 99.0)
    ev = gw.make_event("session.updated", {"id": "s1"})
    assert ev == {"type": EVENT_TYPE, "event": "session.updated", "data": {"id": "s1"}, "timestamp": 99.0}


def test_make_event_default_data_empty():
    gw = TuiGateway(clock=lambda: 5.0)
    ev = gw.make_event("tick")
    assert ev["type"] == EVENT_TYPE
    assert ev["data"] == {}


# ── handle_line：JSON-line 便捷包装 ──

def test_handle_line_valid():
    gw = TuiGateway()
    resp = json.loads(gw.handle_line('{"id":1,"method":"ping"}'))
    assert resp["ok"] is True
    assert resp["data"] == {"pong": True}


def test_handle_line_invalid_json():
    gw = TuiGateway()
    resp = json.loads(gw.handle_line("{not json"))
    assert resp["ok"] is False
    assert resp["error"] == "invalid JSON"
    assert resp["id"] is None


# ── 方法注册表 ──

def test_builtin_methods_registered():
    gw = TuiGateway()
    assert gw.list_methods() == sorted([
        "ping", "session.create", "session.list", "session.close",
        "projects.tree", "render.message", "render.diff", "slash.exec",
    ])


def test_register_custom_method():
    gw = TuiGateway()
    gw.register("custom.echo", lambda p: {"echo": p.get("x")})
    assert "custom.echo" in gw.list_methods()
    resp = gw.handle_message({"id": 1, "method": "custom.echo", "params": {"x": 5}})
    assert resp["data"] == {"echo": 5}


# ── 会话生命周期 ──

def test_session_create_returns_session():
    gw = TuiGateway(id_factory=lambda: "S1", clock=lambda: 10.0)
    resp = gw.handle_message(
        {"id": 1, "method": "session.create", "params": {"model": "gpt", "cwd": "/w"}}
    )
    sess = resp["data"]["session"]
    assert sess["id"] == "S1"
    assert sess["session_key"] == "S1"  # 缺省 = id
    assert sess["model"] == "gpt"
    assert sess["cwd"] == "/w"
    assert sess["created_at"] == 10.0


def test_session_create_explicit_key():
    gw = TuiGateway(id_factory=lambda: "S2")
    resp = gw.handle_message(
        {"id": 1, "method": "session.create", "params": {"session_key": "mykey"}}
    )
    assert resp["data"]["session"]["session_key"] == "mykey"


def test_session_list_and_count():
    gw = TuiGateway()
    gw.handle_message({"id": 1, "method": "session.create"})
    gw.handle_message({"id": 2, "method": "session.create"})
    resp = gw.handle_message({"id": 3, "method": "session.list"})
    assert resp["data"]["count"] == 2
    assert len(resp["data"]["sessions"]) == 2


def test_session_close():
    gw = TuiGateway(id_factory=lambda: "S9")
    gw.handle_message({"id": 1, "method": "session.create"})
    resp = gw.handle_message({"id": 2, "method": "session.close", "params": {"id": "S9"}})
    assert resp["data"]["closed"] is True
    assert gw.sessions.count() == 0


def test_session_close_unknown_returns_false():
    gw = TuiGateway()
    resp = gw.handle_message({"id": 1, "method": "session.close", "params": {"id": "nope"}})
    assert resp["data"]["closed"] is False


def test_session_close_missing_id_returns_false():
    gw = TuiGateway()
    resp = gw.handle_message({"id": 1, "method": "session.close", "params": {}})
    assert resp["data"]["closed"] is False
    assert resp["data"]["id"] is None


def test_session_registry_get():
    reg = SessionRegistry(id_factory=lambda: "R1")
    reg.create()
    assert reg.get("R1")["id"] == "R1"
    assert reg.get("missing") is None
    assert reg.get(None) is None


def test_session_registry_auto_ids_increment():
    reg = SessionRegistry()
    a = reg.create()
    b = reg.create()
    assert a["id"] == "sess-1"
    assert b["id"] == "sess-2"
    assert reg.count() == 2


# ── 三模块 RPC 接线 ──

def test_projects_tree_rpc():
    gw = TuiGateway(resolve=lambda cwd: None)
    sessions = [{
        "id": "s1", "cwd": "/work/notes", "git_branch": "", "git_repo_root": "",
        "started_at": 1000, "last_active": 1000, "title": None, "preview": None, "source": "cli",
    }]
    resp = gw.handle_message({
        "id": 1, "method": "projects.tree",
        "params": {"projects": [], "sessions": sessions, "discovered_repos": [], "hydrate": True},
    })
    tree = resp["data"]
    assert "projects" in tree
    assert [p["id"] for p in tree["projects"]] == ["/work/notes"]


def test_render_message_rpc_with_renderer():
    gw = TuiGateway(renderer=FakeRenderer())
    resp = gw.handle_message(
        {"id": 1, "method": "render.message", "params": {"text": "hi", "cols": 100}}
    )
    assert resp["data"]["rendered"] == "<msg:hi:100>"


def test_render_message_rpc_without_renderer_returns_none():
    gw = TuiGateway()
    resp = gw.handle_message({"id": 1, "method": "render.message", "params": {"text": "hi"}})
    assert resp["data"]["rendered"] is None


def test_render_diff_rpc_with_renderer():
    gw = TuiGateway(renderer=FakeRenderer())
    resp = gw.handle_message(
        {"id": 1, "method": "render.diff", "params": {"text": "+x", "cols": 90}}
    )
    assert resp["data"]["rendered"] == "<diff:+x:90>"


def test_slash_exec_rpc_uses_runner_and_normalizes():
    gw = TuiGateway(slash_runner=lambda cmd: f"ran:{cmd}")
    resp = gw.handle_message({"id": 1, "method": "slash.exec", "params": {"command": "goal"}})
    assert resp["data"]["command"] == "/goal"
    assert resp["data"]["output"] == "ran:/goal"


def test_slash_exec_rpc_default_runner():
    gw = TuiGateway()
    resp = gw.handle_message({"id": 1, "method": "slash.exec", "params": {"command": "goal"}})
    assert resp["data"]["command"] == "/goal"
    assert "no slash runner" in resp["data"]["output"]
