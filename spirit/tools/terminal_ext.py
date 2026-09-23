"""终端扩展工具 — Spirit Agent。

合并自 Hermes:
- close_terminal_tool.py: 关闭后台终端标签
- read_terminal_tool.py: 读取终端输出
- daemon_pool.py: 守护线程池
- env_probe.py: 环境探测
- env_passthrough.py: 环境变量透传
- process_registry.py: 进程注册表
- hook_output_spill.py: 钩子输出溢出
- tool_output_limits.py: 工具输出限制
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.thread import _worker
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)

# ============================================================================
# DaemonThreadPoolExecutor（来自 daemon_pool.py）
# ============================================================================


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """守护线程池 — 工作线程不阻塞进程退出。"""

    def _adjust_thread_count(self) -> None:
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = "%s_%d" % (self._thread_name_prefix or self, num_threads)
            t = threading.Thread(
                name=thread_name,
                target=_worker,
                args=(
                    weakref.ref(self, weakref_cb),
                    self._work_queue,
                    self._initializer,
                    self._initargs,
                ),
                daemon=True,
            )
            t.start()
            self._threads.add(t)


# ============================================================================
# 进程注册表
# ============================================================================
#
# 旧版玩具级 ProcessEntry/ProcessRegistry 已在 Phase 4.7 移除，统一改用
# ``spirit.process.process_registry``（对标 Hermes tools/process_registry.py）：
# 后台派生走 terminal_tool(background=true)，本文件的 close_terminal /
# read_terminal 工具直接委托给新注册表的 request_close_terminal / read_log。



# ============================================================================
# 环境变量透传（来自 env_passthrough.py）
# ============================================================================

_allowed_env_vars: ContextVar[Set[str]] = ContextVar("_allowed_env_vars")


def _get_allowed() -> Set[str]:
    try:
        return _allowed_env_vars.get()
    except LookupError:
        val: Set[str] = set()
        _allowed_env_vars.set(val)
        return val


def register_env_passthrough(var_names: Iterable[str]) -> None:
    """注册允许透传到沙箱的环境变量。"""
    for name in var_names:
        name = name.strip()
        if name:
            _get_allowed().add(name)


def is_env_passthrough(var_name: str) -> bool:
    return var_name in _get_allowed()


def clear_env_passthrough() -> None:
    _get_allowed().clear()


# ============================================================================
# 环境探测（来自 env_probe.py）
# ============================================================================

_CACHE_LOCK = threading.Lock()
_CACHED_LINE: Optional[str] = None


def _run_probe_cmd(cmd: list, timeout: float = 3.0) -> Tuple[int, str, str]:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, check=False, stdin=subprocess.DEVNULL,
        )
        return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return -1, "", "error"


def _build_probe_line() -> str:
    """构建环境探测行。"""
    py3_ver = None
    if shutil.which("python3"):
        rc, out, _ = _run_probe_cmd(["python3", "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"])
        if rc == 0:
            py3_ver = out

    has_pip = False
    if shutil.which("python3"):
        rc, _, _ = _run_probe_cmd(["python3", "-m", "pip", "--version"])
        has_pip = rc == 0

    has_uv = shutil.which("uv") is not None

    if py3_ver and has_pip:
        return ""

    bits = []
    bits.append(f"python3={py3_ver or 'missing'}")
    if not has_pip:
        bits.append("pip=missing")
    if has_uv:
        bits.append("uv=installed")
    return "Python: " + ", ".join(bits)


def get_environment_probe_line() -> str:
    global _CACHED_LINE
    if _CACHED_LINE is not None:
        return _CACHED_LINE
    with _CACHE_LOCK:
        if _CACHED_LINE is not None:
            return _CACHED_LINE
        try:
            _CACHED_LINE = _build_probe_line()
        except Exception:
            _CACHED_LINE = ""
        return _CACHED_LINE


# ============================================================================
# 工具输出限制（来自 tool_output_limits.py）
# ============================================================================

from spirit.config import get_config_value

MAX_TOOL_OUTPUT_CHARS = get_config_value("limits.tool_output_max_chars", 100_000)
MAX_TOOL_OUTPUT_LINES = get_config_value("limits.tool_output_max_lines", 2000)


def truncate_tool_output(text: str, max_chars: int = MAX_TOOL_OUTPUT_CHARS,
                         max_lines: int = MAX_TOOL_OUTPUT_LINES) -> str:
    """截断工具输出，防止上下文溢出。"""
    if len(text) <= max_chars and text.count('\n') <= max_lines:
        return text
    lines = text.split('\n')
    if len(lines) > max_lines:
        half = max_lines // 2
        truncated = lines[:half] + [f"\n... (省略 {len(lines) - max_lines} 行) ...\n"] + lines[-half:]
        text = '\n'.join(truncated)
    if len(text) > max_chars:
        half = max_chars // 2
        text = text[:half] + f"\n... (省略 {len(text) - max_chars} 字符) ...\n" + text[-half:]
    return text


# ============================================================================
# close_terminal 工具
# ============================================================================

CLOSE_TERMINAL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "close_terminal",
        "description": "关闭后台进程的终端标签（不杀死进程）。",
        "parameters": {
            "type": "object",
            "properties": {
                "process_id": {
                    "type": "string",
                    "description": "后台进程 ID",
                },
            },
            "required": ["process_id"],
        },
    },
}


def _handle_close_terminal(args: Dict[str, Any], **kwargs) -> str:
    pid = (args.get("process_id") or "").strip()
    if not pid:
        return json.dumps({"error": "process_id 不能为空"})
    from spirit.process import process_registry

    result = process_registry.request_close_terminal(pid)
    if result.get("status") == "error":
        return json.dumps({"error": result.get("error", "关闭失败")}, ensure_ascii=False)
    return json.dumps({
        "success": True,
        "message": result.get("note", f"已关闭终端标签: {pid}"),
        "closed": result.get("closed", pid),
    }, ensure_ascii=False)


registry.register(
    name="close_terminal",
    toolset="terminal",
    schema=CLOSE_TERMINAL_SCHEMA,
    handler=_handle_close_terminal,
    emoji="🖥️",
)


# ============================================================================
# read_terminal 工具
# ============================================================================

READ_TERMINAL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_terminal",
        "description": "读取后台进程的输出。",
        "parameters": {
            "type": "object",
            "properties": {
                "process_id": {
                    "type": "string",
                    "description": "后台进程 ID",
                },
                "start_line": {
                    "type": "integer",
                    "description": "起始行号（0 = 最早）",
                },
                "count": {
                    "type": "integer",
                    "description": "读取行数",
                },
            },
            "required": ["process_id"],
        },
    },
}


def _handle_read_terminal(args: Dict[str, Any], **kwargs) -> str:
    pid = (args.get("process_id") or "").strip()
    if not pid:
        return json.dumps({"error": "process_id 不能为空"})
    from spirit.process import process_registry

    offset = int(args.get("start_line", 0) or 0)
    raw_count = args.get("count")
    # count 未给时用大 limit 保留旧“从 offset 读到尾”语义（read_log 的
    # offset=0 分支取末尾 N 行，offset>0 分支取 [offset, offset+limit)）。
    limit = int(raw_count) if raw_count else 100_000
    result = process_registry.read_log(pid, offset=offset, limit=limit)
    if result.get("status") == "not_found":
        return json.dumps({"error": f"未找到进程: {pid}"}, ensure_ascii=False)
    return json.dumps({
        "process_id": pid,
        "total_lines": result.get("total_lines", 0),
        "running": result.get("status") == "running",
        "text": result.get("output", ""),
    }, ensure_ascii=False)


registry.register(
    name="read_terminal",
    toolset="terminal",
    schema=READ_TERMINAL_SCHEMA,
    handler=_handle_read_terminal,
    emoji="📄",
)
