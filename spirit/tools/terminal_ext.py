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
# 进程注册表（来自 process_registry.py，简化版）
# ============================================================================


class ProcessEntry:
    """一个后台进程条目。"""

    def __init__(self, process_id: str, process, command: str, cwd: str = ""):
        self.process_id = process_id
        self.process = process
        self.command = command
        self.cwd = cwd
        self.output_lines: List[str] = []
        self.exit_code: Optional[int] = None
        self.started_at = __import__("time").time()

    def read_output(self, start: int = 0, count: Optional[int] = None) -> str:
        """读取进程输出。"""
        end = start + count if count else len(self.output_lines)
        return "\n".join(self.output_lines[start:end])

    @property
    def is_running(self) -> bool:
        return self.process.poll() is None


class ProcessRegistry:
    """管理所有后台进程。"""

    def __init__(self):
        self._entries: Dict[str, ProcessEntry] = {}
        self._lock = threading.Lock()

    def register(self, process_id: str, process, command: str, cwd: str = "") -> ProcessEntry:
        entry = ProcessEntry(process_id, process, command, cwd)
        with self._lock:
            self._entries[process_id] = entry
        return entry

    def get(self, process_id: str) -> Optional[ProcessEntry]:
        with self._lock:
            return self._entries.get(process_id)

    def list_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            result = []
            for pid, entry in self._entries.items():
                result.append({
                    "process_id": pid,
                    "command": entry.command,
                    "cwd": entry.cwd,
                    "running": entry.is_running,
                    "output_lines": len(entry.output_lines),
                    "exit_code": entry.exit_code,
                })
            return result

    def kill(self, process_id: str) -> bool:
        entry = self.get(process_id)
        if not entry or not entry.is_running:
            return False
        try:
            entry.process.kill()
            return True
        except Exception:
            return False

    def remove(self, process_id: str):
        with self._lock:
            self._entries.pop(process_id, None)


# 全局进程注册表
process_registry = ProcessRegistry()


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
    entry = process_registry.get(pid)
    if not entry:
        return json.dumps({"error": f"未找到进程: {pid}"})
    process_registry.remove(pid)
    return json.dumps({"success": True, "message": f"已关闭终端标签: {pid}"})


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
    entry = process_registry.get(pid)
    if not entry:
        return json.dumps({"error": f"未找到进程: {pid}"})
    start = args.get("start_line", 0) or 0
    count = args.get("count")
    output = entry.read_output(start, count)
    return json.dumps({
        "process_id": pid,
        "total_lines": len(entry.output_lines),
        "running": entry.is_running,
        "text": output,
    })
