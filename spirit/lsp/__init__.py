"""Spirit Agent LSP 集成 — 独立语言服务器代码智能。

Spirit 运行完整的语言服务器（pyright、gopls、rust-analyzer、
typescript-language-server 等）作为子进程，提供：

- 写后诊断（post-write diagnostics）
- 跳转到定义（go-to-definition）
- 查找引用（find-references）
- 悬停信息（hover）
- 符号搜索（workspace/document symbols）

LSP 需要 git 工作区门控 — 如果 Agent 的 cwd 在 git 仓库内，
LSP 对该工作区运行；否则回退到内置语法检查。

公共 API::

    from spirit.lsp import get_service

    svc = get_service()
    if svc and svc.enabled_for(path):
        svc.touch_file(path)
        diags = svc.get_diagnostics_sync(path)

架构参考 Hermes agent/lsp/ 模块，适配 Spirit 本地部署架构。
"""
from __future__ import annotations

import atexit
import logging
import threading
from typing import Optional

from spirit.lsp.manager import LSPService
from spirit.lsp.eventlog import (
    event_log,
    log_clean,
    log_disabled,
    log_active,
    log_diagnostics,
    log_no_project_root,
    log_server_unavailable,
    log_no_server_configured,
    log_timeout,
    log_server_error,
    log_spawn_failed,
    reset_announce_caches,
)
from spirit.lsp.range_shift import (
    build_line_shift,
    shift_diagnostic_range,
    shift_baseline,
)

logger = logging.getLogger("spirit.lsp")

_service: Optional[LSPService] = None
_atexit_registered = False
_service_lock = threading.Lock()


def get_service() -> Optional[LSPService]:
    """获取进程级 LSP 服务单例。

    首次调用时懒创建。不在 git 仓库内或 LSP 被禁用时返回 None。
    注册 atexit 处理器在退出时清理语言服务器进程。
    """
    global _service, _atexit_registered
    if _service is not None:
        return _service if _service.is_active else None
    with _service_lock:
        if _service is not None:
            return _service if _service.is_active else None
        _service = LSPService.create_from_config()
        if _service and not _atexit_registered:
            atexit.register(_atexit_shutdown)
            _atexit_registered = True
    return _service if (_service is not None and _service.is_active) else None


def shutdown_service() -> None:
    """关闭 LSP 服务。可安全重复调用。"""
    global _service
    with _service_lock:
        svc = _service
        _service = None
    if svc is not None:
        try:
            svc.shutdown()
        except Exception as e:
            logger.debug("LSP shutdown error: %s", e)


def _atexit_shutdown() -> None:
    """atexit 注册的清理函数。"""
    try:
        shutdown_service()
    except Exception as e:
        logger.debug("atexit LSP shutdown failed: %s", e)


__all__ = [
    "get_service",
    "shutdown_service",
    "LSPService",
    # Event logging
    "event_log",
    "log_clean",
    "log_disabled",
    "log_active",
    "log_diagnostics",
    "log_no_project_root",
    "log_server_unavailable",
    "log_no_server_configured",
    "log_timeout",
    "log_server_error",
    "log_spawn_failed",
    "reset_announce_caches",
    # Range shift utilities
    "build_line_shift",
    "shift_diagnostic_range",
    "shift_baseline",
]
