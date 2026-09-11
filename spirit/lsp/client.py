"""异步 LSP 客户端 — 管理语言服务器子进程。

一个 LSPClient 对应一个 (server_id, workspace_root) 对。
客户端拥有子进程，驱动 JSON-RPC 交换，暴露：

- initialize / shutdown — 生命周期管理
- open_file / change_file — 文档同步
- go_to_definition / find_references / hover — 代码导航
- wait_for_diagnostics / diagnostics_for — 诊断获取
- shutdown — 优雅关闭 + SIGTERM/SIGKILL 回退

设计参考 Hermes agent/lsp/client.py，适配 Spirit 架构。
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote, unquote

from spirit.lsp.protocol import (
    ERROR_CONTENT_MODIFIED,
    ERROR_METHOD_NOT_FOUND,
    LSPProtocolError,
    LSPRequestError,
    classify_message,
    encode_message,
    make_notification,
    make_request,
    read_message,
)

logger = logging.getLogger("spirit.lsp.client")

# 超时配置（秒）
INITIALIZE_TIMEOUT = 45.0
DIAGNOSTICS_DOCUMENT_WAIT = 5.0
DIAGNOSTICS_FULL_WAIT = 10.0
DIAGNOSTICS_REQUEST_TIMEOUT = 3.0
PUSH_DEBOUNCE = 0.15
SHUTDOWN_GRACE = 1.0

# ContentModified 重试策略
MAX_CONTENT_MODIFIED_RETRIES = 3
RETRY_BASE_DELAY = 0.5


def _path_to_uri(path: str) -> str:
    """将文件路径转为 file:// URI。"""
    path = os.path.abspath(path)
    if sys.platform == "win32":
        path = path.replace("\\", "/")
        if not path.startswith("/"):
            path = "/" + path
    return "file://" + quote(path, safe="/:")


def _uri_to_path(uri: str) -> str:
    """将 file:// URI 转回文件路径。"""
    if not uri.startswith("file://"):
        return uri
    raw = unquote(uri[7:])
    if sys.platform == "win32":
        if raw.startswith("/"):
            raw = raw[1:]
        raw = raw.replace("/", "\\")
    return raw


class LSPClient:
    """异步 LSP 客户端。

    管理一个语言服务器子进程的生命周期和 JSON-RPC 通信。
    """

    def __init__(self, server_id: str, workspace_root: str,
                 command: List[str], env: Optional[Dict[str, str]] = None,
                 init_options: Optional[Dict[str, Any]] = None):
        self.server_id = server_id
        self.workspace_root = workspace_root
        self._command = command
        self._env = env
        self._init_options = init_options or {}

        self._process: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._next_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._push_diagnostics: Dict[str, List[dict]] = {}
        self._open_files: Set[str] = set()
        self._initialized = False
        self._shutting_down = False

    @property
    def is_alive(self) -> bool:
        """子进程是否仍在运行。"""
        return self._process is not None and self._process.returncode is None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> bool:
        """启动子进程并发送 initialize 请求。

        Returns:
            True 如果初始化成功。
        """
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace_root,
                env=self._env,
            )
        except (FileNotFoundError, PermissionError, OSError) as e:
            logger.warning("LSP 服务器启动失败 [%s]: %s", self.server_id, e)
            return False

        # 启动消息读取循环
        self._reader_task = asyncio.create_task(self._read_loop())

        # 发送 initialize
        try:
            result = await self._send_request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "rootUri": _path_to_uri(self.workspace_root),
                    "rootPath": self.workspace_root,
                    "capabilities": {
                        "textDocument": {
                            "synchronization": {
                                "dynamicRegistration": False,
                                "didSave": True,
                                "willSave": False,
                            },
                            "definition": {"dynamicRegistration": False},
                            "references": {"dynamicRegistration": False},
                            "hover": {"dynamicRegistration": False},
                            "publishDiagnostics": {
                                "relatedInformation": True,
                            },
                        },
                        "workspace": {
                            "didChangeWatchedFiles": {"dynamicRegistration": False},
                        },
                    },
                    "initializationOptions": self._init_options,
                },
                timeout=INITIALIZE_TIMEOUT,
            )
        except (asyncio.TimeoutError, LSPRequestError, LSPProtocolError) as e:
            logger.warning("LSP initialize 失败 [%s]: %s", self.server_id, e)
            await self._kill_process()
            return False

        # 发送 initialized 通知
        await self._send_notification("initialized", {})
        self._initialized = True
        logger.info("LSP 服务器已初始化: %s @ %s", self.server_id, self.workspace_root)
        return True

    async def shutdown(self) -> None:
        """优雅关闭语言服务器。"""
        if self._shutting_down:
            return
        self._shutting_down = True

        if self._initialized and self.is_alive:
            try:
                await self._send_request("shutdown", None, timeout=5.0)
                await self._send_notification("exit", None)
            except Exception:
                pass

        await self._kill_process()

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        self._initialized = False
        logger.debug("LSP 服务器已关闭: %s", self.server_id)

    async def _kill_process(self) -> None:
        """强制终止子进程。"""
        if self._process is None:
            return
        if self._process.returncode is not None:
            return

        try:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=SHUTDOWN_GRACE)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
        except ProcessLookupError:
            pass
        finally:
            self._process = None

    # ------------------------------------------------------------------
    # 文档同步
    # ------------------------------------------------------------------

    async def open_file(self, file_path: str) -> None:
        """打开文件（发送 textDocument/didOpen）。"""
        if not self.is_alive:
            return

        uri = _path_to_uri(file_path)
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            return

        from spirit.lsp.servers import language_id_for
        params = {
            "textDocument": {
                "uri": uri,
                "languageId": language_id_for(file_path),
                "version": 1,
                "text": content,
            }
        }
        await self._send_notification("textDocument/didOpen", params)
        self._open_files.add(uri)

        # 触发 didChangeWatchedFiles（某些服务器需要）
        await self._send_notification(
            "workspace/didChangeWatchedFiles",
            {"changes": [{"uri": uri, "type": 1}]},  # 1 = Created
        )

    async def change_file(self, file_path: str) -> None:
        """通知文件变更（发送 textDocument/didChange）。"""
        if not self.is_alive:
            return

        uri = _path_to_uri(file_path)
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            return

        params = {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [{"text": content}],
        }
        await self._send_notification("textDocument/didChange", params)

        # 也发送 didChangeWatchedFiles
        await self._send_notification(
            "workspace/didChangeWatchedFiles",
            {"changes": [{"uri": uri, "type": 2}]},  # 2 = Changed
        )

    # ------------------------------------------------------------------
    # 代码导航
    # ------------------------------------------------------------------

    async def go_to_definition(self, file_path: str, line: int, character: int) -> List[dict]:
        """跳转到定义。"""
        return await self._position_request("textDocument/definition", file_path, line, character)

    async def find_references(self, file_path: str, line: int, character: int) -> List[dict]:
        """查找引用。"""
        return await self._position_request(
            "textDocument/references", file_path, line, character,
            extra_params={"context": {"includeDeclaration": True}},
        )

    async def hover(self, file_path: str, line: int, character: int) -> Optional[dict]:
        """获取悬停信息。"""
        try:
            result = await self._position_request(
                "textDocument/hover", file_path, line, character,
            )
            return result
        except (LSPRequestError, LSPProtocolError, asyncio.TimeoutError):
            return None

    async def document_symbols(self, file_path: str) -> List[dict]:
        """获取文件内符号列表。"""
        uri = _path_to_uri(file_path)
        try:
            result = await self._send_request(
                "textDocument/documentSymbol",
                {"textDocument": {"uri": uri}},
                timeout=10.0,
            )
            return result if isinstance(result, list) else []
        except (LSPRequestError, LSPProtocolError, asyncio.TimeoutError):
            return []

    async def workspace_symbols(self, query: str) -> List[dict]:
        """工作区符号搜索。"""
        try:
            result = await self._send_request(
                "workspace/symbol",
                {"query": query},
                timeout=10.0,
            )
            return result if isinstance(result, list) else []
        except (LSPRequestError, LSPProtocolError, asyncio.TimeoutError):
            return []

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------

    def diagnostics_for(self, file_path: str) -> List[dict]:
        """获取文件的当前诊断（从推送缓存中读取）。"""
        uri = _path_to_uri(file_path)
        return list(self._push_diagnostics.get(uri, []))

    async def wait_for_diagnostics(self, file_path: str,
                                    timeout: float = DIAGNOSTICS_FULL_WAIT) -> List[dict]:
        """等待服务器推送诊断。"""
        uri = _path_to_uri(file_path)
        deadline = asyncio.get_event_loop().time() + timeout

        while asyncio.get_event_loop().time() < deadline:
            if uri in self._push_diagnostics:
                return self._push_diagnostics[uri]
            await asyncio.sleep(PUSH_DEBOUNCE)

        return self._push_diagnostics.get(uri, [])

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    async def _position_request(self, method: str, file_path: str,
                                 line: int, character: int,
                                 extra_params: Optional[dict] = None) -> Any:
        """发送需要位置参数的请求。"""
        uri = _path_to_uri(file_path)
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        }
        if extra_params:
            params.update(extra_params)

        try:
            return await self._send_request(method, params, timeout=10.0)
        except LSPRequestError as e:
            if e.code == ERROR_METHOD_NOT_FOUND:
                return [] if method != "textDocument/hover" else None
            raise

    async def _send_request(self, method: str, params: Any,
                            timeout: float = 10.0) -> Any:
        """发送 JSON-RPC 请求并等待响应。"""
        if not self.is_alive:
            raise LSPProtocolError(f"LSP 服务器未运行: {self.server_id}")

        request_id = self._next_id
        self._next_id += 1

        msg = make_request(method, params, request_id)
        frame = encode_message(msg)

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future

        try:
            self._process.stdin.write(frame)
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            self._pending.pop(request_id, None)
            raise LSPProtocolError(f"写入失败: {e}")

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise

    async def _send_notification(self, method: str, params: Any) -> None:
        """发送 JSON-RPC 通知（无响应）。"""
        if not self.is_alive:
            return

        msg = make_notification(method, params)
        frame = encode_message(msg)
        try:
            self._process.stdin.write(frame)
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def _read_loop(self) -> None:
        """后台消息读取循环。"""
        try:
            while self._process and self._process.stdout:
                msg = await read_message(self._process.stdout)
                if msg is None:
                    break  # 服务器关闭

                msg_type = classify_message(msg)

                if msg_type == "response":
                    rid = msg.get("id")
                    future = self._pending.pop(rid, None)
                    if future and not future.done():
                        if "error" in msg:
                            err = msg["error"]
                            future.set_exception(
                                LSPRequestError(err.get("code", 0), err.get("message", ""))
                            )
                        else:
                            future.set_result(msg.get("result"))

                elif msg_type == "notification":
                    method = msg.get("method", "")
                    if method == "textDocument/publishDiagnostics":
                        params = msg.get("params", {})
                        uri = params.get("uri", "")
                        diags = params.get("diagnostics", [])
                        self._push_diagnostics[uri] = diags
                        logger.debug("诊断更新: %s (%d 条)", uri, len(diags))

                elif msg_type == "request":
                    # 服务器反向请求（如 window/workDoneProgress/create）
                    rid = msg.get("id")
                    method = msg.get("method", "")
                    # 简单回复空结果
                    from spirit.lsp.protocol import make_response
                    resp = make_response(rid, None)
                    frame = encode_message(resp)
                    try:
                        self._process.stdin.write(frame)
                        await self._process.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        pass

        except LSPProtocolError as e:
            logger.warning("LSP 协议错误 [%s]: %s", self.server_id, e)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("LSP 读取循环异常 [%s]: %s", self.server_id, e)
        finally:
            # 取消所有挂起的请求
            for future in self._pending.values():
                if not future.done():
                    future.cancel()
            self._pending.clear()


__all__ = ["LSPClient", "_path_to_uri", "_uri_to_path"]
