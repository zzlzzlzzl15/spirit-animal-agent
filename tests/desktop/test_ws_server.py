"""WebSocket 服务器测试。"""

import pytest
from spirit.desktop.pet_engine import PetEngine
from spirit.desktop.ws_server import WSServer, CommandRegistry


class TestCommandRegistry:
    def test_register_and_get(self):
        reg = CommandRegistry()
        async def handler(client, data):
            return {"ok": True}
        reg.register("test", handler)
        assert reg.get("test") is handler

    def test_get_missing(self):
        reg = CommandRegistry()
        assert reg.get("nonexistent") is None

    def test_list_actions(self):
        reg = CommandRegistry()
        async def h1(c, d): return {}
        async def h2(c, d): return {}
        reg.register("a", h1)
        reg.register("b", h2)
        assert sorted(reg.list_actions()) == ["a", "b"]


class TestWSServer:
    def test_init(self):
        engine = PetEngine()
        server = WSServer(engine, host="127.0.0.1", port=0)
        assert server.engine is engine
        assert server.client_count == 0

    def test_builtin_commands_registered(self):
        engine = PetEngine()
        server = WSServer(engine)
        actions = server.commands.list_actions()
        assert "get_status" in actions
        assert "get_pet_info" in actions
        assert "list_pets" in actions
        assert "switch_pet" in actions
        assert "get_system_status" in actions
        assert "ping" in actions
        assert "set_state" in actions
        assert "idle" in actions
        assert "chat" in actions
        assert "search_knowledge" in actions
        assert "execute_command" in actions

    @pytest.mark.asyncio
    async def test_ping_command(self):
        engine = PetEngine()
        server = WSServer(engine)
        handler = server.commands.get("ping")
        result = await handler(None, {})
        assert result["pong"] is True
        assert "timestamp" in result

    @pytest.mark.asyncio
    async def test_get_status_command(self):
        engine = PetEngine()
        server = WSServer(engine)
        handler = server.commands.get("get_status")
        result = await handler(None, {})
        assert "pet" in result
        assert "state" in result
        assert result["state"]["current"] == "idle"

    @pytest.mark.asyncio
    async def test_idle_command(self):
        engine = PetEngine()
        engine.update_signals(busy=True)
        assert engine.current_state.value == "run"
        server = WSServer(engine)
        handler = server.commands.get("idle")
        result = await handler(None, {})
        assert result["state"] == "idle"
        assert engine.current_state.value == "idle"
