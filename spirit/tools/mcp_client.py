"""MCP 协议客户端工具 — Spirit Agent。

合并自 Hermes:
- mcp_tool.py: MCP 客户端（stdio/HTTP/SSE 传输）
- mcp_oauth.py: OAuth 2.1 认证
- mcp_dashboard_oauth.py: Dashboard OAuth
- mcp_oauth_manager.py: OAuth 管理器
- mcp_stdio_watchdog.py: stdio 看门狗
"""

import asyncio
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)

# ============================================================================
# MCP 配置
# ============================================================================

SPIRIT_HOME = Path.home() / ".spirit"
MCP_CONFIG_PATH = SPIRIT_HOME / "config.yaml"


@dataclass
class MCPServerConfig:
    """MCP 服务器配置。"""
    name: str
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    url: Optional[str] = None
    transport: str = "stdio"  # stdio | http | sse
    timeout: int = 300
    connect_timeout: int = 60
    keepalive_interval: int = 180
    headers: Dict[str, str] = field(default_factory=dict)
    supports_parallel: bool = False
    sampling_enabled: bool = True
    auth: Optional[str] = None  # "oauth" | None


def _load_mcp_servers() -> List[MCPServerConfig]:
    """从配置文件加载 MCP 服务器列表。"""
    config_path = MCP_CONFIG_PATH
    if not config_path.exists():
        return []
    try:
        import yaml
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not config or not isinstance(config, dict):
            return []
        servers_raw = config.get("mcp_servers", {})
        if not servers_raw:
            return []

        servers = []
        for name, cfg in servers_raw.items():
            if not isinstance(cfg, dict):
                continue
            servers.append(MCPServerConfig(
                name=name,
                command=cfg.get("command"),
                args=cfg.get("args", []),
                env=cfg.get("env", {}),
                url=cfg.get("url"),
                transport=cfg.get("transport", "stdio"),
                timeout=cfg.get("timeout", 300),
                connect_timeout=cfg.get("connect_timeout", 60),
                keepalive_interval=cfg.get("keepalive_interval", 180),
                headers=cfg.get("headers", {}),
                supports_parallel=cfg.get("supports_parallel_tool_calls", False),
                sampling_enabled=cfg.get("sampling", {}).get("enabled", True)
                    if isinstance(cfg.get("sampling"), dict) else True,
                auth=cfg.get("auth"),
            ))
        return servers
    except Exception as exc:
        logger.warning("加载 MCP 配置失败: %s", exc)
        return []


# ============================================================================
# MCP 服务器管理
# ============================================================================

@dataclass
class MCPToolInfo:
    """MCP 服务器提供的工具信息。"""
    name: str
    description: str
    input_schema: Dict[str, Any]
    server_name: str


class MCPServerManager:
    """管理 MCP 服务器连接和工具发现。"""

    def __init__(self):
        self._servers: Dict[str, Any] = {}
        self._tools: Dict[str, MCPToolInfo] = {}
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._initialized = False

    def _ensure_loop(self):
        """确保后台事件循环运行。"""
        if self._loop is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        except Exception:
            pass

    def initialize(self) -> List[MCPToolInfo]:
        """初始化所有 MCP 服务器连接，返回发现的工具列表。"""
        if self._initialized:
            return list(self._tools.values())

        self._ensure_loop()
        servers = _load_mcp_servers()
        discovered: List[MCPToolInfo] = []

        for srv_cfg in servers:
            try:
                tools = self._connect_server(srv_cfg)
                discovered.extend(tools)
            except Exception as exc:
                logger.warning("MCP 服务器 %s 连接失败: %s", srv_cfg.name, exc)

        self._initialized = True
        return discovered

    def _connect_server(self, cfg: MCPServerConfig) -> List[MCPToolInfo]:
        """连接单个 MCP 服务器并发现工具。"""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            logger.debug("mcp 包未安装，MCP 功能不可用")
            return []

        tools: List[MCPToolInfo] = []

        if cfg.transport == "stdio" and cfg.command:
            env = {**os.environ, **cfg.env}
            server_params = StdioServerParameters(
                command=cfg.command, args=cfg.args, env=env,
            )
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._stdio_connect(server_params, cfg), self._loop
                )
                tools = future.result(timeout=cfg.connect_timeout + 10)
            except Exception as exc:
                logger.warning("MCP stdio 连接 %s 失败: %s", cfg.name, exc)

        elif cfg.transport in ("http", "sse") and cfg.url:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._http_connect(cfg), self._loop
                )
                tools = future.result(timeout=cfg.connect_timeout + 10)
            except Exception as exc:
                logger.warning("MCP HTTP 连接 %s 失败: %s", cfg.name, exc)

        with self._lock:
            self._servers[cfg.name] = {"config": cfg, "status": "connected" if tools else "failed"}
            for t in tools:
                self._tools[t.name] = t

        return tools

    async def _stdio_connect(self, server_params, cfg: MCPServerConfig) -> List[MCPToolInfo]:
        """stdio 传输连接。"""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        tools: List[MCPToolInfo] = []
        try:
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    for tool in result.tools:
                        tools.append(MCPToolInfo(
                            name=tool.name,
                            description=tool.description or "",
                            input_schema=tool.inputSchema if hasattr(tool, "inputSchema") else {},
                            server_name=cfg.name,
                        ))
        except Exception as exc:
            logger.debug("MCP stdio session %s: %s", cfg.name, exc)
        return tools

    async def _http_connect(self, cfg: MCPServerConfig) -> List[MCPToolInfo]:
        """HTTP/SSE 传输连接。"""
        tools: List[MCPToolInfo] = []
        try:
            if cfg.transport == "sse":
                from mcp.client.sse import sse_client
                async with sse_client(cfg.url, headers=cfg.headers) as (read, write):
                    from mcp import ClientSession
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.list_tools()
                        for tool in result.tools:
                            tools.append(MCPToolInfo(
                                name=tool.name,
                                description=tool.description or "",
                                input_schema=tool.inputSchema if hasattr(tool, "inputSchema") else {},
                                server_name=cfg.name,
                            ))
            else:
                from mcp.client.streamable_http import streamablehttp_client
                async with streamablehttp_client(cfg.url, headers=cfg.headers) as (read, write, _):
                    from mcp import ClientSession
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.list_tools()
                        for tool in result.tools:
                            tools.append(MCPToolInfo(
                                name=tool.name,
                                description=tool.description or "",
                                input_schema=tool.inputSchema if hasattr(tool, "inputSchema") else {},
                                server_name=cfg.name,
                            ))
        except Exception as exc:
            logger.debug("MCP HTTP session %s: %s", cfg.name, exc)
        return tools

    def call_tool(self, tool_name: str, arguments: Dict[str, Any],
                  timeout: int = 300) -> Dict[str, Any]:
        """调用 MCP 工具。"""
        with self._lock:
            tool_info = self._tools.get(tool_name)
        if not tool_info:
            return {"error": f"MCP 工具 '{tool_name}' 未找到"}

        server_name = tool_info.server_name
        with self._lock:
            srv = self._servers.get(server_name)
        if not srv:
            return {"error": f"MCP 服务器 '{server_name}' 未连接"}

        cfg: MCPServerConfig = srv["config"]

        try:
            if self._loop is None:
                return {"error": "MCP 事件循环未初始化"}

            future = asyncio.run_coroutine_threadsafe(
                self._call_tool_async(cfg, tool_name, arguments), self._loop
            )
            return future.result(timeout=timeout)
        except Exception as exc:
            return {"error": str(exc)}

    async def _call_tool_async(self, cfg: MCPServerConfig, tool_name: str,
                                arguments: Dict[str, Any]) -> Dict[str, Any]:
        """异步调用 MCP 工具。"""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            if cfg.transport == "stdio" and cfg.command:
                env = {**os.environ, **cfg.env}
                server_params = StdioServerParameters(
                    command=cfg.command, args=cfg.args, env=env,
                )
                async with stdio_client(server_params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(tool_name, arguments=arguments)
                        content = []
                        for item in result.content:
                            if hasattr(item, "text"):
                                content.append(item.text)
                            else:
                                content.append(str(item))
                        return {"success": True, "result": "\n".join(content)}

            elif cfg.url:
                if cfg.transport == "sse":
                    from mcp.client.sse import sse_client
                    async with sse_client(cfg.url, headers=cfg.headers) as (read, write):
                        from mcp import ClientSession
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            result = await session.call_tool(tool_name, arguments=arguments)
                            content = [getattr(item, "text", str(item)) for item in result.content]
                            return {"success": True, "result": "\n".join(content)}
                else:
                    from mcp.client.streamable_http import streamablehttp_client
                    async with streamablehttp_client(cfg.url, headers=cfg.headers) as (read, write, _):
                        from mcp import ClientSession
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            result = await session.call_tool(tool_name, arguments=arguments)
                            content = [getattr(item, "text", str(item)) for item in result.content]
                            return {"success": True, "result": "\n".join(content)}

            return {"error": "不支持的传输类型"}
        except Exception as exc:
            return {"error": str(exc)}

    def get_tools(self) -> List[MCPToolInfo]:
        with self._lock:
            return list(self._tools.values())

    def get_servers(self) -> Dict[str, Dict]:
        with self._lock:
            return dict(self._servers)


# 全局管理器
_mcp_manager = MCPServerManager()


# ============================================================================
# MCP 工具注册
# ============================================================================

MCP_LIST_SERVERS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "mcp_list_servers",
        "description": "列出所有已配置的 MCP 服务器及其状态。",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _handle_mcp_list_servers(args: Dict[str, Any], **kwargs) -> str:
    servers = _mcp_manager.get_servers()
    result = []
    for name, info in servers.items():
        cfg = info.get("config")
        result.append({
            "name": name,
            "status": info.get("status", "unknown"),
            "transport": cfg.transport if cfg else "unknown",
        })
    return json.dumps({"servers": result, "count": len(result)}, ensure_ascii=False)


MCP_CALL_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "mcp_call_tool",
        "description": "调用 MCP 服务器提供的工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string", "description": "工具名称"},
                "arguments": {"type": "object", "description": "工具参数"},
            },
            "required": ["tool_name"],
        },
    },
}


def _handle_mcp_call_tool(args: Dict[str, Any], **kwargs) -> str:
    tool_name = args.get("tool_name", "")
    arguments = args.get("arguments", {})
    if not tool_name:
        return json.dumps({"error": "tool_name 不能为空"})
    result = _mcp_manager.call_tool(tool_name, arguments)
    return json.dumps(result, ensure_ascii=False)


def check_mcp() -> bool:
    """检查 MCP 是否可用（有配置且 mcp 包已安装）。"""
    try:
        import mcp  # noqa: F401
        servers = _load_mcp_servers()
        return len(servers) > 0
    except ImportError:
        return False


registry.register(name="mcp_list_servers", toolset="mcp", schema=MCP_LIST_SERVERS_SCHEMA,
                  handler=_handle_mcp_list_servers, check_fn=check_mcp, emoji="🔌")
registry.register(name="mcp_call_tool", toolset="mcp", schema=MCP_CALL_TOOL_SCHEMA,
                  handler=_handle_mcp_call_tool, check_fn=check_mcp, emoji="🔌")
