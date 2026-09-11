"""LSP 服务编排层 — 同步/异步桥接。

LSPService 是同步的 file_operations 层与异步 LSPClient 之间的桥梁。

设计要点：
- 单个 asyncio 事件循环运行在后台线程，所有客户端工作在该循环上
- 按 (server_id, workspace_root) 键懒创建客户端
- 损坏集合记录启动失败的 (server_id, workspace_root) 对，不再重试
- 诊断基线机制：snapshot_baseline() 在写前调用，
  get_diagnostics_sync() 只返回新增的诊断

服务默认关闭 — 调用 is_active() 检查是否实际运行。
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from spirit.lsp.client import LSPClient, _path_to_uri
from spirit.lsp.install import get_resolved_binary, try_install
from spirit.lsp.reporter import compute_delta, format_diagnostics
from spirit.lsp.servers import ServerDef, find_server_for_file, get_server
from spirit.lsp.workspace import find_git_worktree

logger = logging.getLogger("spirit.lsp.manager")

DEFAULT_IDLE_TIMEOUT = 600  # 空闲 >10 分钟回收服务器


class _BackgroundLoop:
    """后台守护线程 + asyncio 事件循环。"""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run_forever,
            daemon=True,
            name="spirit-lsp-loop",
        )
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def _run_forever(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        except Exception:
            pass

    def run(self, coro: Any, timeout: float = 30.0) -> Any:
        """在后台循环上运行协程并阻塞等待结果。"""
        if self._loop is None:
            raise RuntimeError("后台循环未启动")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None


class LSPService:
    """LSP 服务 — 管理所有语言服务器客户端。"""

    def __init__(self, auto_install: str = "auto") -> None:
        self._auto_install = auto_install
        self._loop = _BackgroundLoop()
        self._clients: Dict[Tuple[str, str], LSPClient] = {}
        self._broken: Set[Tuple[str, str]] = set()
        self._baselines: Dict[str, List[dict]] = {}  # uri → 基线诊断
        self._last_access: Dict[Tuple[str, str], float] = {}
        self._active = False

    @classmethod
    def create_from_config(cls) -> Optional["LSPService"]:
        """从配置创建服务实例。

        如果不在 git 仓库内，返回 None。
        """
        from spirit.config import get_config_value
        auto_install = get_config_value("lsp.auto_install", "auto")

        # 检查当前工作目录是否在 git 仓库内
        cwd = os.getcwd()
        git_root = find_git_worktree(cwd)
        if not git_root:
            logger.debug("LSP 禁用: 不在 git 仓库内 (cwd=%s)", cwd)
            return None

        svc = cls(auto_install=auto_install)
        svc._loop.start()
        svc._active = True
        return svc

    @property
    def is_active(self) -> bool:
        return self._active

    # ------------------------------------------------------------------
    # 客户端管理
    # ------------------------------------------------------------------

    def _get_or_create_client(self, file_path: str) -> Optional[LSPClient]:
        """为文件获取或创建 LSP 客户端。"""
        server_def = find_server_for_file(file_path)
        if not server_def:
            return None

        workspace_root = server_def.resolve_root(file_path)
        if not workspace_root:
            workspace_root = find_git_worktree(file_path)
        if not workspace_root:
            return None

        key = (server_def.server_id, workspace_root)

        # 已损坏？
        if key in self._broken:
            return None

        # 已存在？
        client = self._clients.get(key)
        if client and client.is_alive:
            self._last_access[key] = time.time()
            return client

        # 需要新建
        if client and not client.is_alive:
            del self._clients[key]

        binary = get_resolved_binary(server_def.server_id)
        if not binary:
            # 尝试安装
            if self._auto_install == "auto":
                installed = try_install(server_def.server_id)
                if installed:
                    binary = installed

            if not binary:
                logger.debug("LSP 二进制不可用: %s", server_def.server_id)
                self._broken.add(key)
                return None

        command = [binary] + server_def.args
        env = server_def.build_env()

        client = LSPClient(
            server_id=server_def.server_id,
            workspace_root=workspace_root,
            command=command,
            env=env,
            init_options=server_def.init_options,
        )

        # 在后台循环上初始化
        try:
            success = self._loop.run(client.initialize(), timeout=60.0)
            if not success:
                self._broken.add(key)
                return None
        except Exception as e:
            logger.warning("LSP 客户端创建失败 [%s]: %s", server_def.server_id, e)
            self._broken.add(key)
            return None

        self._clients[key] = client
        self._last_access[key] = time.time()
        return client

    # ------------------------------------------------------------------
    # 公共同步 API（供工具层调用）
    # ------------------------------------------------------------------

    def enabled_for(self, file_path: str) -> bool:
        """检查 LSP 是否对该文件可用。"""
        if not self._active:
            return False
        client = self._get_or_create_client(file_path)
        return client is not None

    def touch_file(self, file_path: str) -> None:
        """通知文件变更（写后调用）。"""
        if not self._active:
            return
        client = self._get_or_create_client(file_path)
        if client:
            try:
                self._loop.run(client.change_file(file_path), timeout=15.0)
            except Exception as e:
                logger.debug("touch_file 失败: %s", e)

    def open_file(self, file_path: str) -> None:
        """打开文件（首次访问时调用）。"""
        if not self._active:
            return
        client = self._get_or_create_client(file_path)
        if client:
            try:
                self._loop.run(client.open_file(file_path), timeout=15.0)
            except Exception as e:
                logger.debug("open_file 失败: %s", e)

    def snapshot_baseline(self, file_path: str) -> None:
        """保存写前诊断基线。"""
        if not self._active:
            return
        client = self._get_or_create_client(file_path)
        if client:
            uri = _path_to_uri(file_path)
            try:
                diags = self._loop.run(
                    client.wait_for_diagnostics(file_path, timeout=3.0),
                    timeout=5.0,
                )
                self._baselines[uri] = list(diags)
            except Exception:
                pass

    def get_diagnostics_sync(self, file_path: str,
                              timeout: float = 10.0) -> List[dict]:
        """获取文件的诊断（写后调用，返回新增诊断）。"""
        if not self._active:
            return []

        client = self._get_or_create_client(file_path)
        if not client:
            return []

        try:
            diags = self._loop.run(
                client.wait_for_diagnostics(file_path, timeout=timeout),
                timeout=timeout + 2.0,
            )
        except Exception:
            return []

        # 计算增量
        uri = _path_to_uri(file_path)
        baseline = self._baselines.get(uri, [])
        delta = compute_delta(baseline, diags)
        return delta

    def get_diagnostics_formatted(self, file_path: str,
                                   timeout: float = 10.0) -> str:
        """获取格式化的诊断字符串。"""
        delta = self.get_diagnostics_sync(file_path, timeout)
        if not delta:
            return ""
        return format_diagnostics(file_path, delta)

    # ------------------------------------------------------------------
    # 代码导航（同步包装）
    # ------------------------------------------------------------------

    def go_to_definition(self, file_path: str, line: int, character: int) -> Any:
        """跳转到定义（同步）。"""
        if not self._active:
            return None
        client = self._get_or_create_client(file_path)
        if not client:
            return None
        try:
            return self._loop.run(
                client.go_to_definition(file_path, line, character),
                timeout=15.0,
            )
        except Exception as e:
            logger.debug("go_to_definition 失败: %s", e)
            return None

    def find_references(self, file_path: str, line: int, character: int) -> Any:
        """查找引用（同步）。"""
        if not self._active:
            return None
        client = self._get_or_create_client(file_path)
        if not client:
            return None
        try:
            return self._loop.run(
                client.find_references(file_path, line, character),
                timeout=15.0,
            )
        except Exception as e:
            logger.debug("find_references 失败: %s", e)
            return None

    def hover(self, file_path: str, line: int, character: int) -> Any:
        """获取悬停信息（同步）。"""
        if not self._active:
            return None
        client = self._get_or_create_client(file_path)
        if not client:
            return None
        try:
            return self._loop.run(
                client.hover(file_path, line, character),
                timeout=15.0,
            )
        except Exception as e:
            logger.debug("hover 失败: %s", e)
            return None

    def document_symbols(self, file_path: str) -> List[dict]:
        """获取文件符号列表（同步）。"""
        if not self._active:
            return []
        client = self._get_or_create_client(file_path)
        if not client:
            return []
        try:
            return self._loop.run(
                client.document_symbols(file_path),
                timeout=15.0,
            )
        except Exception:
            return []

    def workspace_symbols(self, query: str, file_path: str = "") -> List[dict]:
        """工作区符号搜索（同步）。"""
        if not self._active:
            return []
        # 用给定文件路径的客户端，或回退到任意活跃客户端
        client = None
        if file_path:
            client = self._get_or_create_client(file_path)
        if not client and self._clients:
            client = next(iter(self._clients.values()), None)
        if not client:
            return []
        try:
            return self._loop.run(
                client.workspace_symbols(query),
                timeout=15.0,
            )
        except Exception:
            return []

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------

    def reap_idle(self) -> int:
        """回收空闲超时的客户端。"""
        now = time.time()
        reaped = 0
        to_remove = []

        for key, last in self._last_access.items():
            if now - last > DEFAULT_IDLE_TIMEOUT:
                to_remove.append(key)

        for key in to_remove:
            client = self._clients.pop(key, None)
            self._last_access.pop(key, None)
            if client:
                try:
                    self._loop.run(client.shutdown(), timeout=10.0)
                except Exception:
                    pass
                reaped += 1

        return reaped

    def shutdown(self) -> None:
        """关闭所有客户端和后台循环。"""
        self._active = False

        for key, client in list(self._clients.items()):
            try:
                self._loop.run(client.shutdown(), timeout=10.0)
            except Exception:
                pass

        self._clients.clear()
        self._baselines.clear()
        self._last_access.clear()
        self._loop.stop()
        logger.debug("LSP 服务已关闭")


__all__ = ["LSPService"]
