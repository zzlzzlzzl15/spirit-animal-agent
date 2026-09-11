"""终端执行工具 — 在 shell 中执行命令（双模式）。

参考 Hermes 的 tools/terminal_tool.py 设计，提供安全的终端执行能力。

双模式架构：
- CLI 模式（默认）：使用 subprocess 直接执行
- VSCode 模式：通过 WebSocket 发送到 VSCode 扩展，在集成终端中执行
  当 platform == "vscode" 时自动切换模式
"""

import json
import os
import platform
import subprocess
import signal
import logging
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from spirit.tools.registry import registry
from spirit.config import get_config_value

logger = logging.getLogger(__name__)

# 默认超时（秒，来自集中式配置）
DEFAULT_TIMEOUT = get_config_value("timeouts.terminal_default", 120)
MAX_TIMEOUT = get_config_value("timeouts.terminal_max", 600)


# ---------------------------------------------------------------------------
# VSCode 终端桥接 — 请求/响应匹配
# ---------------------------------------------------------------------------

class _PendingTerminalRequest:
    """等待 VSCode 终端响应的挂起请求。"""

    def __init__(self):
        self.event = threading.Event()
        self.output: Optional[str] = None
        self.exit_code: int = -1
        self.error: Optional[str] = None
        self.timed_out: bool = False

    def wait(self, timeout: float = 120.0) -> dict:
        """阻塞等待结果。"""
        if not self.event.wait(timeout=timeout):
            self.timed_out = True
            raise TimeoutError(f"VSCode 终端请求超时 ({timeout}s)")
        if self.error:
            raise RuntimeError(self.error)
        return {"output": self.output, "exit_code": self.exit_code}


class TerminalBridge:
    """Agent ↔ VSCode 终端桥。

    每个 Agent 持有一个实例。终端工具调用时通过 bridge 发送请求，
    WebSocket 层负责将响应路由回来。
    """

    def __init__(self, send_fn: Optional[Callable] = None):
        """
        Args:
            send_fn: 发送消息到 VSCode 的回调函数。
                     签名: send_fn(data: dict) -> None
        """
        self._send_fn = send_fn
        self._pending: Dict[str, _PendingTerminalRequest] = {}
        self._lock = threading.Lock()

    def execute(self, command: str, cwd: Optional[str] = None,
                timeout: float = 120.0) -> dict:
        """发送终端执行请求并等待结果。"""
        request_id = str(uuid.uuid4())[:8]
        pending = _PendingTerminalRequest()

        with self._lock:
            self._pending[request_id] = pending

        try:
            # 发送请求到 VSCode
            self._send_fn({
                "type": "terminal_execute",
                "request_id": request_id,
                "command": command,
                "cwd": cwd,
                "timeout": int(timeout),
            })

            # 等待响应
            result = pending.wait(timeout)
            return result

        except TimeoutError:
            return {"output": "", "exit_code": -1, "error": f"终端命令超时 ({timeout}s)"}
        except Exception as e:
            return {"output": "", "exit_code": -1, "error": str(e)}
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def handle_response(self, request_id: str, output: Optional[str] = None,
                        exit_code: int = 0, error: Optional[str] = None,
                        timed_out: bool = False):
        """处理来自 VSCode 的终端响应。"""
        with self._lock:
            pending = self._pending.get(request_id)

        if pending:
            pending.output = output
            pending.exit_code = exit_code
            pending.error = error
            pending.timed_out = timed_out
            pending.event.set()
        else:
            logger.warning("收到未知终端请求的响应: %s", request_id)

    @property
    def is_available(self) -> bool:
        return self._send_fn is not None


# ---------------------------------------------------------------------------
# 全局桥注册表
# ---------------------------------------------------------------------------

_terminal_bridges: Dict[str, TerminalBridge] = {}


def get_terminal_bridge(session_id: str) -> Optional[TerminalBridge]:
    """获取指定会话的终端桥。"""
    return _terminal_bridges.get(session_id)


def register_terminal_bridge(session_id: str, send_fn: Callable) -> TerminalBridge:
    """注册新的终端桥。"""
    bridge = TerminalBridge(send_fn)
    _terminal_bridges[session_id] = bridge
    return bridge


def unregister_terminal_bridge(session_id: str):
    """移除终端桥。"""
    _terminal_bridges.pop(session_id, None)


# 当前活跃的平台和会话映射（server.py 负责设置）
_agent_platforms: Dict[str, str] = {}  # session_id → platform


def set_agent_platform(session_id: str, platform_name: str):
    """设置 Agent 的平台标识。"""
    _agent_platforms[session_id] = platform_name


def remove_agent_platform(session_id: str):
    """移除 Agent 平台标识。"""
    _agent_platforms.pop(session_id, None)


def get_active_terminal_bridge(session_id: str,
                                platform_name: str) -> Optional[TerminalBridge]:
    """获取当前活跃的终端桥（仅 VSCode 模式下返回）。"""
    if platform_name in ("vscode", "websocket"):
        return get_terminal_bridge(session_id)
    return None


# ---------------------------------------------------------------------------
# Schema 定义
# ---------------------------------------------------------------------------

TERMINAL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "terminal",
        "description": (
            "在终端中执行 shell 命令。\n\n"
            "返回命令的 stdout 和 stderr 输出。\n"
            "支持设置超时时间。适合运行构建、测试、git 等操作。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 shell 命令",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"超时时间（秒），默认 {DEFAULT_TIMEOUT}，最大 {MAX_TIMEOUT}",
                    "default": DEFAULT_TIMEOUT,
                },
                "cwd": {
                    "type": "string",
                    "description": "工作目录（默认当前目录）",
                },
            },
            "required": ["command"],
        },
    },
}


# ---------------------------------------------------------------------------
# 处理函数
# ---------------------------------------------------------------------------

def _handle_terminal(
    command: str,
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str = None,
) -> str:
    """执行 shell 命令并返回输出（双模式）。

    当 VSCode 终端桥可用时，发送到 VSCode 集成终端执行。
    否则使用 subprocess 直接执行（CLI 模式）。
    """
    # 超时限制
    timeout = min(max(timeout, 1), MAX_TIMEOUT)

    # 工作目录
    work_dir = None
    if cwd:
        work_dir = Path(cwd).expanduser()
        if not work_dir.exists():
            return json.dumps({"error": f"目录不存在: {cwd}"})

    # ── 尝试 VSCode 模式 ──
    vscode_result = _try_vscode_terminal(command, timeout, str(work_dir) if work_dir else None)
    if vscode_result is not None:
        return vscode_result

    # ── CLI 模式：subprocess 执行 ──
    return _execute_subprocess(command, timeout, work_dir)


def _try_vscode_terminal(command: str, timeout: int, cwd: Optional[str]) -> Optional[str]:
    """尝试通过 VSCode 终端执行命令。

    Returns:
        执行结果字符串（如果 VSCode 终端可用），否则返回 None。
    """
    # 遍历所有已注册的终端桥，找到可用的
    for session_id, bridge in _terminal_bridges.items():
        if bridge.is_available:
            try:
                result = bridge.execute(command, cwd=cwd, timeout=float(timeout))

                output_parts = [f"$ {command}"]
                if cwd:
                    output_parts.append(f"[工作目录: {cwd} (VSCode 终端)]")
                output_parts.append("")

                if result.get("output"):
                    stdout = result["output"].rstrip()
                    max_chars = get_config_value("limits.terminal_output_max_chars", 50_000)
                    if len(stdout) > max_chars:
                        stdout = stdout[:max_chars] + f"\n\n... [输出已截断，共 {len(result['output'])} 字符]"
                    output_parts.append(stdout)

                exit_code = result.get("exit_code", 0)
                if exit_code != 0:
                    output_parts.append(f"\n[退出码: {exit_code}]")

                return "\n".join(output_parts)

            except Exception as e:
                logger.warning("VSCode 终端执行失败，回退到 subprocess: %s", e)
                return None

    return None


def _execute_subprocess(command: str, timeout: int, work_dir: Optional[Path]) -> str:
    """使用 subprocess 执行命令（CLI 模式）。"""
    # 选择 shell
    is_windows = platform.system() == "Windows"
    shell_cmd = ["cmd", "/c", command] if is_windows else ["bash", "-c", command]

    try:
        result = subprocess.run(
            shell_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(work_dir) if work_dir else None,
            encoding="utf-8",
            errors="replace",
        )

        output_parts = []

        # 命令信息
        output_parts.append(f"$ {command}")
        if work_dir:
            output_parts.append(f"[工作目录: {work_dir}]")
        output_parts.append("")

        # stdout
        if result.stdout:
            stdout = result.stdout.rstrip()
            # 截断过长输出
            max_chars = get_config_value("limits.terminal_output_max_chars", 50_000)
            if len(stdout) > max_chars:
                stdout = stdout[:max_chars] + f"\n\n... [输出已截断，共 {len(result.stdout)} 字符]"
            output_parts.append(stdout)

        # stderr
        if result.stderr:
            stderr = result.stderr.rstrip()
            output_parts.append(f"\n[stderr]\n{stderr}")

        # 退出码
        if result.returncode != 0:
            output_parts.append(f"\n[退出码: {result.returncode}]")

        return "\n".join(output_parts)

    except subprocess.TimeoutExpired:
        return json.dumps({
            "error": f"命令超时（{timeout}秒）",
            "command": command,
        })
    except FileNotFoundError:
        return json.dumps({
            "error": "Shell 不可用",
            "hint": "请确保 bash (Linux/Mac) 或 cmd (Windows) 可用",
        })
    except Exception as e:
        return json.dumps({"error": f"执行失败: {e}"})


# ---------------------------------------------------------------------------
# 可用性检查
# ---------------------------------------------------------------------------

def _check_terminal_available() -> bool:
    """检查终端是否可用。"""
    if platform.system() == "Windows":
        return True  # Windows 总有 cmd
    # Linux/Mac 检查 bash
    try:
        subprocess.run(["bash", "--version"], capture_output=True, timeout=5)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 自注册
# ---------------------------------------------------------------------------

registry.register(
    name="terminal",
    toolset="terminal",
    schema=TERMINAL_SCHEMA,
    handler=_handle_terminal,
    check_fn=_check_terminal_available,
    emoji="💻",
    max_result_size_chars=get_config_value("limits.terminal_output_max_chars", 50_000),
)


__all__ = [
    "TerminalBridge",
    "get_terminal_bridge",
    "register_terminal_bridge",
    "unregister_terminal_bridge",
    "set_agent_platform",
    "remove_agent_platform",
    "get_active_terminal_bridge",
]
