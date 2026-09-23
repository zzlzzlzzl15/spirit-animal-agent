"""``process`` 工具 — 管理 ``terminal(background=true)`` 派生的后台进程。

对标 Hermes ``tools/process_registry.py`` 尾部的 ``PROCESS_SCHEMA`` /
``_handle_process`` / ``_redact_process_result``（Spirit 把数据层拆到了
``spirit/process/``，本文件只保留工具层）。

八个动作：

``list``
    列出当前任务 + 当前会话下的所有后台进程（含被遗忘的跨任务会话级进程）。
``poll``
    只读状态查询 + 输出预览。**不**标记输出已消费（见注册表注释）。
``log``
    完整输出分页读取（默认最后 200 行）。读到收尾即标记消费。
``wait``
    阻塞直到退出/超时/中断；超时被钳制到 ``process.wait_default_seconds``。
``kill``
    进程树终止（Windows ``taskkill /T /F``，POSIX psutil 遍历 + SIGKILL 升级）。
``write`` / ``submit`` / ``close``
    stdin 交互：原始写入 / 写入并回车 / 关闭 stdin 发 EOF。

安全：所有回给模型的输出字段都过 ``_redact_process_result``，与前台
``terminal`` 的脱敏保持一致，避免后台输出成为凭据泄漏的旁路。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict

from spirit.process import process_registry
from spirit.tools.registry import registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

PROCESS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "process",
        "description": (
            "管理用 terminal(background=true) 启动的后台进程。\n"
            "动作：'list'（列出全部）、'poll'（查状态 + 新输出）、"
            "'log'（完整输出，支持分页）、'wait'（阻塞到结束或超时）、"
            "'kill'（终止进程树）、'write'（向 stdin 写原始数据，不加换行）、"
            "'submit'（写数据 + 回车，用于回答交互式提示）、"
            "'close'（关闭 stdin / 发送 EOF）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "poll", "log", "wait", "kill", "write", "submit", "close"],
                    "description": "要对后台进程执行的动作",
                },
                "session_id": {
                    "type": "string",
                    "description": (
                        "进程会话 ID（来自 terminal 后台执行的返回）。"
                        "除 'list' 外的所有动作都必填。"
                    ),
                },
                "data": {
                    "type": "string",
                    "description": "要写入进程 stdin 的文本（'write' / 'submit' 动作）",
                },
                "timeout": {
                    "type": "integer",
                    "description": "'wait' 动作的最大阻塞秒数。超时返回部分输出。",
                    "minimum": 1,
                },
                "offset": {
                    "type": "integer",
                    "description": "'log' 动作的行偏移（默认取最后 200 行）",
                },
                "limit": {
                    "type": "integer",
                    "description": "'log' 动作返回的最大行数",
                    "minimum": 1,
                },
            },
            "required": ["action"],
        },
    },
}

_VALID_ACTIONS = ("list", "poll", "log", "wait", "kill", "write", "submit", "close")


def _tool_error(message: str) -> str:
    """统一的工具错误 JSON（Spirit 注册表没有 tool_error 辅助，这里就地实现）。"""
    return json.dumps({"error": message}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 输出脱敏
# ---------------------------------------------------------------------------

# 常见凭据形态。这是 ``spirit/security/`` 独立脱敏模块落地前的就地兜底：
# 后台进程输出会直接进模型上下文与 session.db，必须与前台 terminal 同等对待。
#
# 整体匹配型：命中即整段替换为 [REDACTED]
_SECRET_WHOLE_PATTERNS = (
    re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),                       # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),                    # GitHub token
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),                     # GitLab PAT
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),                 # Slack token
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),                        # OpenAI 风格 key
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}\b"),             # Authorization 头
)

# 键值对型：保留键名与分隔符，只抹掉值（否则模型看不出哪个字段被脱敏了）
_SECRET_KV_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd)"
    r"(\s*[:=]\s*)(['\"]?)([^\s'\",;]{6,})(\3)"
)


def _redact_text(text: str) -> str:
    """把文本里的凭据形态替换为 ``[REDACTED]``。"""
    if not text:
        return text
    out = text
    for pat in _SECRET_WHOLE_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    out = _SECRET_KV_PATTERN.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED]{m.group(5)}", out,
    )
    return out


def _redact_process_result(result: Any) -> Any:
    """在后台进程输出抵达模型 / session.db / CLI 显示之前脱敏。

    镜像前台 ``terminal`` 的脱敏，避免两个面出现分叉（后台输出曾被逐字返回）。
    命令字符串本身也脱敏，以防它携带内联凭据。
    """
    if not isinstance(result, dict):
        return result
    command = result.get("command") or ""
    for field_name in ("output", "output_preview"):
        value = result.get(field_name)
        if isinstance(value, str) and value:
            result[field_name] = _redact_text(value)
    if isinstance(command, str) and command:
        result["command"] = _redact_text(command)
    return result


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def _current_session_key() -> str:
    """取当前会话键（用于 list 的跨任务发现 + 通知路由）。"""
    try:
        from spirit.tools.approval import get_current_session_key

        return get_current_session_key(default="") or ""
    except Exception:
        return ""


def _handle_process(args: Dict[str, Any], **kwargs) -> str:
    """``process`` 工具入口（args-dict 约定）。"""
    task_id = kwargs.get("task_id") or ""
    action = str(args.get("action", "") or "")
    # 强制转字符串 —— 有些模型会把 session_id 发成整数
    raw_sid = args.get("session_id")
    session_id = str(raw_sid) if raw_sid is not None else ""

    if action == "list":
        # 除了本任务自己的进程，也浮出会话级的后台进程（例如被遗忘的预览
        # 服务器）—— 它们共享会话键，且会阻塞会话重置。
        return json.dumps(
            {
                "processes": process_registry.list_sessions(
                    task_id=task_id or None,
                    session_key=_current_session_key() or None,
                ),
                "running": process_registry.count_running(),
            },
            ensure_ascii=False,
        )

    if action in _VALID_ACTIONS:
        if not session_id:
            return _tool_error(f"{action} 动作需要 session_id")
        if action == "poll":
            return json.dumps(
                _redact_process_result(process_registry.poll(session_id)),
                ensure_ascii=False,
            )
        if action == "log":
            return json.dumps(
                _redact_process_result(process_registry.read_log(
                    session_id,
                    offset=int(args.get("offset") or 0),
                    limit=int(args.get("limit") or 200),
                )),
                ensure_ascii=False,
            )
        if action == "wait":
            timeout = args.get("timeout")
            return json.dumps(
                _redact_process_result(process_registry.wait(
                    session_id, timeout=int(timeout) if timeout else None,
                )),
                ensure_ascii=False,
            )
        if action == "kill":
            return json.dumps(
                _redact_process_result(process_registry.kill_process(session_id)),
                ensure_ascii=False,
            )
        if action == "write":
            return json.dumps(
                process_registry.write_stdin(session_id, str(args.get("data", ""))),
                ensure_ascii=False,
            )
        if action == "submit":
            return json.dumps(
                process_registry.submit_stdin(session_id, str(args.get("data", ""))),
                ensure_ascii=False,
            )
        # close
        return json.dumps(
            process_registry.close_stdin(session_id), ensure_ascii=False,
        )

    return _tool_error(
        f"未知的 process 动作: {action}。可用: {', '.join(_VALID_ACTIONS)}"
    )


# ---------------------------------------------------------------------------
# 自注册
# ---------------------------------------------------------------------------

registry.register(
    name="process",
    toolset="terminal",
    schema=PROCESS_SCHEMA,
    handler=_handle_process,
    emoji="⚙️",
)


__all__ = [
    "PROCESS_SCHEMA",
    "process_registry",
]
