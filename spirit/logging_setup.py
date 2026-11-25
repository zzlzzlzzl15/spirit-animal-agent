"""统一日志系统 — 终端 / 滚动文件 / 结构化事件三通道。

v0.3.5 新增。在此之前 Spirit 只有 ``logging.basicConfig`` 打到终端，且：

- CLI 默认级别 ERROR，思考过程里的 WARNING/INFO 全部丢失；
- 思考期间 root logger 还会被临时压到 CRITICAL（``cli/main.py``），出错时
  连最后一条错误都可能看不到；
- 没有任何文件日志，进程退出后无从排查（"上一轮为什么 API 调用失败"无据可查）。

本模块提供：

- :func:`setup_logging` —— 幂等的统一初始化，挂 3 个 handler：
    1. 终端 RichHandler（挂在 ``spirit`` logger，默认 ERROR，``-v`` 时 DEBUG）
    2. ``~/.spirit/logs/agent.log``   全量滚动文件（默认 INFO，10MB×5）
    3. ``~/.spirit/logs/errors.log``  仅 ERROR 及以上（便于 tail 错误）
  外加结构化事件流：
    4. ``~/.spirit/logs/events.jsonl`` 每行一个 JSON（:func:`log_event`）

- :func:`log_event` —— 业务事件结构化落盘（llm_request / api_error /
  compression / provider_switch / tool_error / session_* 等），文本字段
  统一经 ``spirit.agent.redact`` 脱敏，正文截断。

关键设计：

- 文件 handler 挂在 ``spirit`` logger 上并 ``propagate=False`` —— CLI 在 turn
  内会把 *root* logger 压到 CRITICAL；挂 root 会连文件日志一起被压掉，挂
  ``spirit`` 才能保证运行过程始终落盘。
- 零硬失败：日志目录不可写 / redact 异常都不能影响主流程，最坏退化为只打终端。
- 测试安全：``SPIRIT_TEST_LOGGING`` 或 pytest 进程默认不落盘用户目录，
  除非显式传 ``log_dir``。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import logging.handlers
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

from spirit.config import SPIRIT_HOME

__all__ = [
    "setup_logging",
    "log_event",
    "get_log_dir",
    "get_events_path",
    "install_excepthook",
    "reset_logging",
    "DEFAULT_EVENT_PREVIEW_CHARS",
]

logger = logging.getLogger("spirit")

# ── 常量 ────────────────────────────────────────────────────────────────────

DEFAULT_MAX_BYTES = 10 * 1024 * 1024   # 10MB
DEFAULT_BACKUP_COUNT = 5
DEFAULT_EVENT_PREVIEW_CHARS = 500      # 事件正文最多保留字符

_SPIRIT_LOGGER_NAME = "spirit"
_FILE_HANDLER_TAG = "_spirit_file_handler"
_ERROR_HANDLER_TAG = "_spirit_error_handler"
_EVENT_HANDLER_TAG = "_spirit_event_handler"
_CONSOLE_HANDLER_TAG = "_spirit_console_handler"

_INIT_LOCK = threading.RLock()
_EVENT_LOCK = threading.Lock()
_CONFIGURED = {"done": False}


# ── 路径 ────────────────────────────────────────────────────────────────────

def _running_under_pytest() -> bool:
    try:
        return "pytest" in sys.argv[0] or any("pytest" in a for a in sys.argv)
    except Exception:  # noqa: BLE001
        return False


def get_log_dir(log_dir: Optional[os.PathLike] = None) -> Path:
    """返回日志目录（默认 ``$SPIRIT_HOME/logs``）。"""
    if log_dir is not None:
        return Path(log_dir).expanduser()
    return Path(SPIRIT_HOME) / "logs"


def get_events_path(log_dir: Optional[os.PathLike] = None) -> Path:
    """结构化事件 JSONL 路径。"""
    return get_log_dir(log_dir) / "events.jsonl"


# ── 脱敏 / 截断 ─────────────────────────────────────────────────────────────

def _redact(text: Any) -> str:
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    try:
        # 延迟导入避免 import 期循环（redact 是零依赖模块，实际很安全）
        from spirit.agent.redact import redact_sensitive_text
        return redact_sensitive_text(s)
    except Exception:  # noqa: BLE001 - 脱敏绝不能影响日志
        return s


def _preview(value: Any, limit: int = DEFAULT_EVENT_PREVIEW_CHARS) -> Any:
    """把任意字段规范成可 JSON 序列化的安全值（截断正文）。"""
    if isinstance(value, str):
        if len(value) > limit:
            return value[:limit] + f"...(+{len(value) - limit} chars)"
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k)[:80]: _preview(v, limit) for k, v in list(value.items())[:30]}
    if isinstance(value, (list, tuple)):
        return [_preview(v, limit) for v in list(value)[:30]]
    return str(value)[:limit]


# ── 事件 JSONL handler ──────────────────────────────────────────────────────

class _EventJsonlHandler(logging.Handler):
    """把携带 ``event=dict`` 属性的 LogRecord 追加写入 events.jsonl。

    普通日志记录不会写 JSONL（避免重复）；只有 :func:`log_event` 注入的
    结构化记录才落盘。
    """

    def __init__(self, path: Path):
        super().__init__(level=logging.DEBUG)
        self.path = Path(path)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        event = getattr(record, "event", None)
        if not isinstance(event, dict):
            return
        try:
            line = json.dumps(event, ensure_ascii=False, default=str)
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:  # noqa: BLE001 - 日志失败静默
            self.handleError(record)


# ── 初始化 ──────────────────────────────────────────────────────────────────

def _remove_tagged_handlers(target: logging.Logger) -> None:
    keep = [h for h in target.handlers if not getattr(h, "_spirit_tag", False)]
    target.handlers = keep


def setup_logging(
    verbose: bool = False,
    *,
    log_dir: Optional[os.PathLike] = None,
    console: bool = True,
    console_stream: Any = None,
    rich_console: Any = None,
    file_level: Optional[int] = None,
    force: bool = False,
) -> Path:
    """初始化统一日志系统（幂等）。

    Args:
        verbose: 终端是否输出 DEBUG（否则终端默认 ERROR；文件始终更详细）
        log_dir: 日志目录覆盖（默认 ``$SPIRIT_HOME/logs``）
        console: 是否安装终端 RichHandler（服务/桌宠场景可关）
        console_stream: RichHandler/StreamHandler 使用的流（默认 stderr）
        rich_console: 已构造好的 rich.Console（CLI 复用同一个，避免双 console）
        file_level: 文件日志级别覆盖（默认 INFO）
        force: 重新安装（测试用）

    Returns:
        日志目录 Path（即使落盘失败也返回期望路径）
    """
    with _INIT_LOCK:
        directory = get_log_dir(log_dir)

        # 配置参数（YAML 可覆盖）
        try:
            from spirit.config import get_config_value
            enabled = get_config_value("logging.enabled", True)
            cfg_level = get_config_value("logging.level", "INFO")
            max_bytes = int(get_config_value("logging.max_bytes", DEFAULT_MAX_BYTES))
            backup_count = int(get_config_value("logging.backup_count", DEFAULT_BACKUP_COUNT))
        except Exception:  # noqa: BLE001
            enabled, cfg_level = True, "INFO"
            max_bytes, backup_count = DEFAULT_MAX_BYTES, DEFAULT_BACKUP_COUNT

        spirit_logger = logging.getLogger(_SPIRIT_LOGGER_NAME)

        if _CONFIGURED["done"] and not force:
            return directory
        _remove_tagged_handlers(spirit_logger)
        # 不动 root 的既有 handler（uvicorn / basicConfig），只确保 spirit 自身：
        spirit_logger.setLevel(logging.DEBUG)   # 细粒度交给各 handler 的 level
        spirit_logger.propagate = False         # 关键：turn 内 root 被压 CRITICAL 时文件仍写

        fmt = logging.Formatter(
            "[%(asctime)s] %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # 1) 终端
        if console:
            handler: logging.Handler
            if rich_console is not None:
                try:
                    from rich.logging import RichHandler
                    handler = RichHandler(
                        console=rich_console,
                        rich_tracebacks=True,
                        show_path=False,
                    )
                except Exception:  # noqa: BLE001
                    handler = logging.StreamHandler(console_stream or sys.stderr)
                    handler.setFormatter(fmt)
            else:
                try:
                    from rich.console import Console
                    from rich.logging import RichHandler
                    handler = RichHandler(
                        console=Console(file=console_stream or sys.stderr),
                        rich_tracebacks=True,
                        show_path=False,
                    )
                except Exception:  # noqa: BLE001
                    handler = logging.StreamHandler(console_stream or sys.stderr)
                    handler.setFormatter(fmt)
            handler.setLevel(logging.DEBUG if verbose else logging.ERROR)
            handler._spirit_tag = _CONSOLE_HANDLER_TAG  # type: ignore[attr-defined]
            spirit_logger.addHandler(handler)

        # 2)+3)+4) 文件（不可用时优雅降级）
        file_writable = False
        if enabled:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                # 探测可写
                probe = directory / ".write_probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink(missing_ok=True)
                file_writable = True
            except Exception as exc:  # noqa: BLE001
                # 不 raise —— 日志系统不能把主程序带崩
                logging.getLogger("spirit").debug("日志目录不可写，仅终端输出: %s", exc)

        if file_writable:
            level = file_level
            if level is None:
                level = getattr(logging, str(cfg_level).upper(), logging.INFO)

            agent_handler = logging.handlers.RotatingFileHandler(
                directory / "agent.log",
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            agent_handler.setLevel(level)
            agent_handler.setFormatter(fmt)
            agent_handler._spirit_tag = _FILE_HANDLER_TAG  # type: ignore[attr-defined]
            spirit_logger.addHandler(agent_handler)

            error_handler = logging.handlers.RotatingFileHandler(
                directory / "errors.log",
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            error_handler.setLevel(logging.ERROR)
            error_handler.setFormatter(
                logging.Formatter(
                    "[%(asctime)s] %(levelname)-7s %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            error_handler._spirit_tag = _ERROR_HANDLER_TAG  # type: ignore[attr-defined]
            spirit_logger.addHandler(error_handler)

            event_handler = _EventJsonlHandler(directory / "events.jsonl")
            event_handler._spirit_tag = _EVENT_HANDLER_TAG  # type: ignore[attr-defined]
            spirit_logger.addHandler(event_handler)

        _CONFIGURED["done"] = True
        return directory


def reset_logging() -> None:
    """测试辅助：移除本模块装的 handler，重置幂等标记。"""
    with _INIT_LOCK:
        spirit_logger = logging.getLogger(_SPIRIT_LOGGER_NAME)
        _remove_tagged_handlers(spirit_logger)
        spirit_logger.propagate = True
        _CONFIGURED["done"] = False


# ── 结构化事件 ──────────────────────────────────────────────────────────────

def log_event(kind: str, level: int = logging.INFO, **fields: Any) -> Dict[str, Any]:
    """记录一条结构化业务事件到 ``events.jsonl``。

    同时向普通日志通道打一条摘要（进 agent.log），但 JSONL 是查询面的数据源。
    敏感正文经 redact 脱敏，所有正文类字段截断到 500 字符。

    Args:
        kind: 事件类型（llm_request / llm_response / api_error / compression /
            payload_invalid / provider_switch / tool_error / session_start /
            session_end / storage_error ...）
        level: 同步到普通日志的级别
        **fields: 事件字段（dict / list / 长文本会被安全化）

    Returns:
        实际写入的事件 dict（便于测试断言）
    """
    event: Dict[str, Any] = {
        "ts": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "kind": kind,
        "level": logging.getLevelName(level),
        "pid": os.getpid(),
    }
    for key, value in fields.items():
        event[key] = _preview(value)

    # 异常对象 → traceback 字符串
    if "exc" in fields and isinstance(fields["exc"], BaseException):
        event["exc_type"] = type(fields["exc"]).__name__
        event["exc"] = _redact(str(fields["exc"]))
        event["traceback"] = _preview(
            "".join(
                traceback.format_exception(
                    type(fields["exc"]), fields["exc"], fields["exc"].__traceback__
                )
            ),
            4000,
        )

    # 已知文本字段统一脱敏
    for text_key in ("message", "error", "detail", "summary", "preview", "notice"):
        if text_key in event and isinstance(event[text_key], str):
            event[text_key] = _redact(event[text_key])

    record = logging.LogRecord(
        name="spirit.events",
        level=level,
        pathname=__file__,
        lineno=0,
        msg=f"event:{kind}",
        args=(),
        exc_info=None,
    )
    record.event = event
    logging.getLogger(_SPIRIT_LOGGER_NAME).handle(record)
    return event


# ── 未捕获异常 ───────────────────────────────────────────────────────────────

def install_excepthook() -> None:
    """把未捕获异常 / 线程异常写入 events.jsonl。幂等。"""
    def _excepthook(exc_type, exc, tb):
        try:
            log_event(
                "uncaught_exception",
                level=logging.ERROR,
                exc_type=getattr(exc_type, "__name__", str(exc_type)),
                traceback="".join(traceback.format_exception(exc_type, exc, tb))[:4000],
            )
        except Exception:  # noqa: BLE001
            pass
        sys.__excepthook__(exc_type, exc, tb)

    def _thread_excepthook(args):
        try:
            log_event(
                "thread_exception",
                level=logging.ERROR,
                thread=getattr(args.thread, "name", str(args.thread)),
                exc_type=getattr(args.exc_type, "__name__", str(args.exc_type)),
                traceback="".join(
                    traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
                )[:4000],
            )
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = _excepthook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_excepthook
