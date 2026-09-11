"""代码智能工具 — 双模式代码定位。

支持两种后端：
1. **VSCode 回调桥**（优先）— 通过 IDE 回调请求 VSCode 执行
   内置的代码智能命令，利用 IDE 的语言服务能力。
2. **独立 LSP**（回退）— 直接启动语言服务器子进程（pyright 等），
   无需 VSCode 环境，通过 spirit.lsp 模块管理。

工具列表：
- go_to_definition: 跳转到定义
- find_references: 查找引用
- get_hover_info: 获取悬停信息（类型、文档）
- get_diagnostics: 获取文件诊断（错误/警告）
- workspace_symbols: 工作区符号搜索
- get_document_symbols: 文件内符号列表

选择逻辑：
1. 如果有 VSCode 桥且可用 → 使用 VSCode 桥
2. 否则尝试独立 LSP 服务 → 使用 spirit.lsp
3. 两者都不可用 → 返回错误
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# IDE 回调桥接 — 请求/响应匹配
# ---------------------------------------------------------------------------

class _PendingRequest:
    """等待 VSCode 响应的挂起请求。"""

    def __init__(self):
        self.event = threading.Event()
        self.result: Any = None
        self.error: Optional[str] = None

    def wait(self, timeout: float = 10.0) -> Any:
        """阻塞等待结果。"""
        if not self.event.wait(timeout=timeout):
            raise TimeoutError(f"IDE 代码智能请求超时 ({timeout}s)")
        if self.error:
            raise RuntimeError(self.error)
        return self.result


class CodeIntelligenceBridge:
    """Agent ↔ VSCode 代码智能桥。

    每个 Agent 持有一个实例。工具调用时通过 bridge 发送请求，
    WebSocket 层负责将响应路由回来。
    """

    def __init__(self, send_fn: Optional[Callable] = None):
        """
        Args:
            send_fn: 发送消息到 VSCode 的回调函数。
                     签名: send_fn(event_type: str, data: dict) -> None
        """
        self._send_fn = send_fn
        self._pending: Dict[str, _PendingRequest] = {}
        self._lock = threading.Lock()

    def request(self, command: str, params: Dict[str, Any], timeout: float = 10.0) -> Any:
        """发送代码智能请求并等待结果。

        Args:
            command: VSCode 命令名（如 "go_to_definition"）
            params: 命令参数
            timeout: 超时秒数

        Returns:
            VSCode 返回的结果
        """
        if not self._send_fn:
            return {"error": "代码智能不可用（非 VSCode 模式）"}

        request_id = str(uuid.uuid4())[:12]
        pending = _PendingRequest()

        with self._lock:
            self._pending[request_id] = pending

        try:
            # 发送请求到 VSCode
            self._send_fn("code_intelligence_request", {
                "request_id": request_id,
                "command": command,
                "params": params,
            })

            # 等待响应
            result = pending.wait(timeout)
            return result

        except TimeoutError:
            return {"error": f"请求超时: {command}"}
        except Exception as e:
            return {"error": str(e)}
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def handle_response(self, request_id: str, result: Any = None, error: Optional[str] = None):
        """处理来自 VSCode 的响应。"""
        with self._lock:
            pending = self._pending.get(request_id)

        if pending:
            pending.result = result
            pending.error = error
            pending.event.set()
        else:
            logger.warning("收到未知请求的响应: %s", request_id)

    @property
    def is_available(self) -> bool:
        return self._send_fn is not None


# 全局桥实例（每个 Agent 在初始化时创建）
_bridges: Dict[str, CodeIntelligenceBridge] = {}


def get_bridge(session_id: str) -> Optional[CodeIntelligenceBridge]:
    """获取指定会话的代码智能桥。"""
    return _bridges.get(session_id)


def register_bridge(session_id: str, send_fn: Callable) -> CodeIntelligenceBridge:
    """注册新的代码智能桥。"""
    bridge = CodeIntelligenceBridge(send_fn)
    _bridges[session_id] = bridge
    return bridge


def unregister_bridge(session_id: str):
    """移除代码智能桥。"""
    _bridges.pop(session_id, None)


# ---------------------------------------------------------------------------
# Schema 定义
# ---------------------------------------------------------------------------

GO_TO_DEFINITION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "go_to_definition",
        "description": (
            "跳转到符号定义位置。返回定义所在的文件路径、行号和字符位置。\n"
            "需要 VSCode 环境（语言服务器支持）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "文件路径（不传则使用当前活动文件）",
                },
                "line": {
                    "type": "integer",
                    "description": "行号（0-based）",
                },
                "character": {
                    "type": "integer",
                    "description": "字符位置（0-based）",
                },
            },
            "required": ["line", "character"],
        },
    },
}

FIND_REFERENCES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "find_references",
        "description": (
            "查找符号的所有引用位置。返回引用所在的文件路径和行号列表。\n"
            "需要 VSCode 环境。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "文件路径（不传则使用当前活动文件）",
                },
                "line": {
                    "type": "integer",
                    "description": "行号（0-based）",
                },
                "character": {
                    "type": "integer",
                    "description": "字符位置（0-based）",
                },
            },
            "required": ["line", "character"],
        },
    },
}

GET_HOVER_INFO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_hover_info",
        "description": (
            "获取指定位置的悬停信息（类型签名、文档注释等）。\n"
            "需要 VSCode 环境。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "文件路径（不传则使用当前活动文件）",
                },
                "line": {
                    "type": "integer",
                    "description": "行号（0-based）",
                },
                "character": {
                    "type": "integer",
                    "description": "字符位置（0-based）",
                },
            },
            "required": ["line", "character"],
        },
    },
}

GET_DIAGNOSTICS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_diagnostics",
        "description": (
            "获取文件的诊断信息（编译错误、类型错误、lint 警告等）。\n"
            "不传 file 则获取所有文件的诊断概要。\n"
            "需要 VSCode 环境。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "文件路径（不传则获取所有文件的诊断概要）",
                },
            },
            "required": [],
        },
    },
}

WORKSPACE_SYMBOLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "workspace_symbols",
        "description": (
            "在工作区中搜索符号（函数名、类名、变量名等）。\n"
            "返回匹配的符号列表，包含文件位置和类型信息。\n"
            "需要 VSCode 环境。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词",
                },
            },
            "required": ["query"],
        },
    },
}

GET_DOCUMENT_SYMBOLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_document_symbols",
        "description": (
            "获取文件中的所有符号（函数、类、变量等）的结构化列表。\n"
            "返回符号的层级结构，包含名称、类型、位置。\n"
            "需要 VSCode 环境。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "文件路径（不传则使用当前活动文件）",
                },
            },
            "required": [],
        },
    },
}


# ---------------------------------------------------------------------------
# 辅助函数 — 解析参数中的文件/位置
# ---------------------------------------------------------------------------

def _resolve_file_and_position(args: Dict[str, Any], agent) -> Dict[str, Any]:
    """从参数和工作区上下文中解析文件路径和位置。"""
    result = {}

    # 文件路径：参数优先，否则使用活动文件
    file_path = args.get("file")
    if not file_path and agent.workspace_context:
        file_path = agent.workspace_context.active_file
    if file_path:
        result["file"] = file_path

    # 行号和字符位置
    if "line" in args:
        result["line"] = args["line"]
    elif agent.workspace_context and agent.workspace_context.cursor_position:
        result["line"] = agent.workspace_context.cursor_position.line

    if "character" in args:
        result["character"] = args["character"]
    elif agent.workspace_context and agent.workspace_context.cursor_position:
        result["character"] = agent.workspace_context.cursor_position.character

    return result


def _get_bridge_for_agent(agent) -> Optional[CodeIntelligenceBridge]:
    """获取 Agent 关联的代码智能桥。"""
    # 优先使用 Agent 上直接绑定的桥
    bridge = getattr(agent, "_code_intelligence_bridge", None)
    if bridge:
        return bridge
    # 回退到全局注册表
    return get_bridge(agent.session_id)


# ---------------------------------------------------------------------------
# 独立 LSP 回退
# ---------------------------------------------------------------------------

def _get_lsp_service():
    """获取独立 LSP 服务（懒加载）。"""
    try:
        from spirit.lsp import get_service
        return get_service()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------

def _go_to_definition(args: Dict[str, Any], agent) -> str:
    """跳转到定义。"""
    params = _resolve_file_and_position(args, agent)

    # 优先 VSCode 桥
    bridge = _get_bridge_for_agent(agent)
    if bridge and bridge.is_available:
        if "file" not in params or "line" not in params:
            return json.dumps({"error": "需要指定文件路径和位置（或使用 VSCode 打开文件）"})
        result = bridge.request("go_to_definition", params)
        return json.dumps(result, ensure_ascii=False, indent=2)

    # 回退到独立 LSP
    svc = _get_lsp_service()
    if svc and "file" in params and "line" in params:
        result = svc.go_to_definition(
            params["file"], params["line"], params.get("character", 0)
        )
        return json.dumps(result, ensure_ascii=False, indent=2)

    return json.dumps({"error": "代码智能不可用（需要 VSCode 环境或 git 仓库内的 LSP 服务器）"})


def _find_references(args: Dict[str, Any], agent) -> str:
    """查找引用。"""
    params = _resolve_file_and_position(args, agent)

    bridge = _get_bridge_for_agent(agent)
    if bridge and bridge.is_available:
        if "file" not in params or "line" not in params:
            return json.dumps({"error": "需要指定文件路径和位置"})
        result = bridge.request("find_references", params)
        return json.dumps(result, ensure_ascii=False, indent=2)

    svc = _get_lsp_service()
    if svc and "file" in params and "line" in params:
        result = svc.find_references(
            params["file"], params["line"], params.get("character", 0)
        )
        return json.dumps(result, ensure_ascii=False, indent=2)

    return json.dumps({"error": "代码智能不可用（需要 VSCode 环境或 git 仓库内的 LSP 服务器）"})


def _get_hover_info(args: Dict[str, Any], agent) -> str:
    """获取悬停信息。"""
    params = _resolve_file_and_position(args, agent)

    bridge = _get_bridge_for_agent(agent)
    if bridge and bridge.is_available:
        if "file" not in params or "line" not in params:
            return json.dumps({"error": "需要指定文件路径和位置"})
        result = bridge.request("get_hover_info", params)
        return json.dumps(result, ensure_ascii=False, indent=2)

    svc = _get_lsp_service()
    if svc and "file" in params and "line" in params:
        result = svc.hover(
            params["file"], params["line"], params.get("character", 0)
        )
        return json.dumps(result, ensure_ascii=False, indent=2)

    return json.dumps({"error": "代码智能不可用（需要 VSCode 环境或 git 仓库内的 LSP 服务器）"})


def _get_diagnostics(args: Dict[str, Any], agent) -> str:
    """获取诊断信息。"""
    file_path = args.get("file")
    if not file_path and agent.workspace_context and agent.workspace_context.active_file:
        file_path = agent.workspace_context.active_file

    bridge = _get_bridge_for_agent(agent)
    if bridge and bridge.is_available:
        params = {}
        if file_path:
            params["file"] = file_path
        result = bridge.request("get_diagnostics", params)
        return json.dumps(result, ensure_ascii=False, indent=2)

    svc = _get_lsp_service()
    if svc and file_path:
        formatted = svc.get_diagnostics_formatted(file_path)
        if formatted:
            return formatted
        return json.dumps({"message": "无诊断信息"})

    return json.dumps({"error": "代码智能不可用（需要 VSCode 环境或 git 仓库内的 LSP 服务器）"})


def _workspace_symbols(args: Dict[str, Any], agent) -> str:
    """工作区符号搜索。"""
    query = args.get("query", "")
    if not query:
        return json.dumps({"error": "请提供搜索关键词"})

    bridge = _get_bridge_for_agent(agent)
    if bridge and bridge.is_available:
        result = bridge.request("workspace_symbols", {"query": query})
        return json.dumps(result, ensure_ascii=False, indent=2)

    # 独立 LSP — 需要知道用哪个客户端
    svc = _get_lsp_service()
    if svc:
        file_path = args.get("file", "")
        if not file_path and agent.workspace_context and agent.workspace_context.active_file:
            file_path = agent.workspace_context.active_file
        result = svc.workspace_symbols(query, file_path=file_path)
        return json.dumps(result, ensure_ascii=False, indent=2)

    return json.dumps({"error": "代码智能不可用（需要 VSCode 环境或 git 仓库内的 LSP 服务器）"})


def _get_document_symbols(args: Dict[str, Any], agent) -> str:
    """获取文件符号列表。"""
    file_path = args.get("file")
    if not file_path and agent.workspace_context and agent.workspace_context.active_file:
        file_path = agent.workspace_context.active_file

    bridge = _get_bridge_for_agent(agent)
    if bridge and bridge.is_available:
        params = {}
        if file_path:
            params["file"] = file_path
        if "file" not in params:
            return json.dumps({"error": "需要指定文件路径（或使用 VSCode 打开文件）"})
        result = bridge.request("get_document_symbols", params)
        return json.dumps(result, ensure_ascii=False, indent=2)

    svc = _get_lsp_service()
    if svc and file_path:
        result = svc.document_symbols(file_path)
        return json.dumps(result, ensure_ascii=False, indent=2)

    return json.dumps({"error": "代码智能不可用（需要 VSCode 环境或 git 仓库内的 LSP 服务器）"})


# ---------------------------------------------------------------------------
# 工具注册
# ---------------------------------------------------------------------------

registry.register(
    name="go_to_definition",
    handler=_go_to_definition,
    schema=GO_TO_DEFINITION_SCHEMA,
    toolset="code_intelligence",
    description="跳转到符号定义位置（VSCode 模式或独立 LSP）",
    emoji="📍",
)

registry.register(
    name="find_references",
    handler=_find_references,
    schema=FIND_REFERENCES_SCHEMA,
    toolset="code_intelligence",
    description="查找符号的所有引用（VSCode 模式或独立 LSP）",
    emoji="🔍",
)

registry.register(
    name="get_hover_info",
    handler=_get_hover_info,
    schema=GET_HOVER_INFO_SCHEMA,
    toolset="code_intelligence",
    description="获取符号的类型和文档信息（VSCode 模式或独立 LSP）",
    emoji="💡",
)

registry.register(
    name="get_diagnostics",
    handler=_get_diagnostics,
    schema=GET_DIAGNOSTICS_SCHEMA,
    toolset="code_intelligence",
    description="获取文件诊断信息（VSCode 模式或独立 LSP）",
    emoji="🔴",
)

registry.register(
    name="workspace_symbols",
    handler=_workspace_symbols,
    schema=WORKSPACE_SYMBOLS_SCHEMA,
    toolset="code_intelligence",
    description="在工作区搜索符号（VSCode 模式或独立 LSP）",
    emoji="🔎",
)

registry.register(
    name="get_document_symbols",
    handler=_get_document_symbols,
    schema=GET_DOCUMENT_SYMBOLS_SCHEMA,
    toolset="code_intelligence",
    description="获取文件内的符号结构（VSCode 模式或独立 LSP）",
    emoji="📋",
)


__all__ = [
    "CodeIntelligenceBridge",
    "get_bridge",
    "register_bridge",
    "unregister_bridge",
]
