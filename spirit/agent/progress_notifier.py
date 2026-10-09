"""长任务进度推送 — Spirit Agent 的桌宠可视化层。

C-P1.1 落盘
----------
V3 规划 C-P1.1：长任务进度推送（<freq/feature-roadmap.md> §3）。
Spirit 执行长任务时桌宠完全无感；用户不知道 Agent 在干嘛、卡没卡。
v0.2.2 的 delegate 进度 sink 只覆盖"子任务"，不覆盖"主任务"。

功能
----
- 在 ``conversation_loop.py`` 关键节点广播进度：
  - 任务开始 / 思考开始 / 思考结束 / 工具调用 / 工具完成 / 任务完成 / 任务中断
- 进度事件推送到桌宠（沿用 v0.2.2 的 WS broadcast 协议）
- 桌宠端可渲染：进度条 + 当前步骤文字 + 可折叠（避免打扰用户）

事件 schema
-----------
::

    {
        "type": "event",
        "event": "progress",
        "data": {
            "phase": "task_start | thinking | thinking_end
                     | tool_call | tool_result | task_complete | task_aborted",
            "session_id": "xxx",
            "iterations": 3,
            "tool_name": "search_files",     # 可选，仅 tool_call/result 有
            "summary": "调用 search_files ...",  # 人类可读
            "timestamp": 1234567890,
            # 自由扩展字段：
            "duration_ms": 2500,
            "classification": "success",     # tool_result 时附上
            "reason": "max_iterations"       # task_aborted 时附上
        },
        "timestamp": 1234567890
    }

集成方式
--------
``ws_server.py`` 在 ``__init__`` 注册 sink（类似 ``_delegate_progress_sink``），
桥接到 ``self.broadcast("progress", event)``：

::

    from spirit.agent.progress_notifier import register_progress_sink

    def _progress_sink(evt):
        try:
            asyncio.run_coroutine_threadsafe(self.broadcast("progress", evt), loop)
        except Exception:
            pass

    register_progress_sink(_progress_sink)

``conversation_loop.py`` 在关键节点调 :func:`notify_progress`：

::

    from spirit.agent.progress_notifier import ProgressPhase, notify_progress

    notify_progress(ProgressPhase.TASK_START, summary=f"开始任务：{msg[:60]}...")
    notify_progress(ProgressPhase.THINKING, iterations=api_call_count)
    notify_progress(ProgressPhase.TOOL_CALL, tool_name=name, summary=f"调用 {name}")
    notify_progress(ProgressPhase.TASK_COMPLETE, iterations=api_call_count)

设计要点
--------
- **异常隔离**：sink 回调异常不阻断主循环（每个 sink 单独 try/except）
- **无 sink 时零开销**：直接 return，避免无谓的 dict 构造
- **线程安全**：sinks 列表 + session_id 都有锁保护（同步上下文）
- **不依赖 asyncio**：notify_progress 本身是同步的，sink 内部自行桥接到事件循环
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# =========================================================================
# 进度阶段枚举
# =========================================================================

class ProgressPhase(str, enum.Enum):
    """长任务进度的 7 个阶段。"""

    TASK_START = "task_start"
    """任务开始（每轮 run_conversation 入口）。"""

    THINKING = "thinking"
    """LLM 推理开始（每次 _call_llm 前）。"""

    THINKING_END = "thinking_end"
    """LLM 推理结束（含 duration_ms）。"""

    TOOL_CALL = "tool_call"
    """决定调用工具（含 tool_name + summary）。"""

    TOOL_RESULT = "tool_result"
    """工具执行完成（含 tool_name + classification）。"""

    TASK_COMPLETE = "task_complete"
    """任务最终回答已生成（含 summary 摘要）。"""

    TASK_ABORTED = "task_aborted"
    """任务被中断（中断检查 / 迭代预算耗尽 / 护栏停止 / API 错误）。"""


# =========================================================================
# 全局 sink 注册表 + session 上下文
# =========================================================================

_sinks: List[Callable[[Dict[str, Any]], None]] = []
_sinks_lock = threading.Lock()

_current_session_id: Optional[str] = None
_session_lock = threading.Lock()


def register_progress_sink(callback: Callable[[Dict[str, Any]], None]) -> None:
    """注册进度回调 sink（多个并存）。

    sink 接收一个 dict（事件 payload），返回 None。
    sink 内部异常会被隔离（不影响主循环与其他 sink）。
    """
    with _sinks_lock:
        if callback not in _sinks:
            _sinks.append(callback)


def unregister_progress_sink(callback: Callable[[Dict[str, Any]], None]) -> None:
    """反注册进度回调 sink。"""
    with _sinks_lock:
        try:
            _sinks.remove(callback)
        except ValueError:
            pass


def set_current_session_id(session_id: Optional[str]) -> None:
    """设置当前会话 ID（进度事件会带上，便于前端路由到对应会话）。

    通常在 ``run_conversation`` 入口调一次。
    """
    global _current_session_id
    with _session_lock:
        _current_session_id = session_id


# =========================================================================
# 事件构造 + 广播
# =========================================================================

def _build_event(
    phase: ProgressPhase,
    *,
    session_id: Optional[str] = None,
    iterations: Optional[int] = None,
    tool_name: Optional[str] = None,
    summary: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构造一个标准进度事件 payload。"""
    payload: Dict[str, Any] = {
        "phase": phase.value,
        "timestamp": time.time(),
    }
    if session_id is not None:
        payload["session_id"] = session_id
    if iterations is not None:
        payload["iterations"] = iterations
    if tool_name:
        payload["tool_name"] = tool_name
    if summary:
        payload["summary"] = summary
    if extra:
        # 自由扩展字段直接 merge（注意不要让 extra 覆盖上面的基础字段）
        for k, v in extra.items():
            if k not in payload:
                payload[k] = v
    return payload


def notify_progress(
    phase: ProgressPhase,
    *,
    iterations: Optional[int] = None,
    tool_name: Optional[str] = None,
    summary: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """广播一个进度事件给所有 sink。

    设计：
    - **无 sink 时零开销**：直接 return（节省 dict 构造与锁开销）
    - **异常隔离**：每个 sink 单独 try/except，单个 sink 失败不影响其他
    - **同步上下文**：本函数是同步的；sink 内部自行桥接 asyncio 事件循环
      （参考 ws_server.py 的 ``_delegate_progress_sink`` 模式）
    - **session_id 自动取全局值**：调用方未显式传 session_id 时，
      使用 :func:`set_current_session_id` 缓存的当前会话 ID

    参数
    ----
    phase : ProgressPhase
        进度阶段枚举值
    iterations : int, optional
        当前 API 调用计数（用于进度计算）
    tool_name : str, optional
        工具名（仅 tool_call / tool_result 有意义）
    summary : str, optional
        人类可读的进度摘要（不超过 100 字符为佳）
    extra : dict, optional
        自由扩展字段（如 duration_ms / classification / reason 等）
    """
    if not _sinks:
        return  # 无 sink 时直接跳过，零开销

    # 取 session_id：extra 里的优先，否则用全局缓存
    sid: Optional[str] = None
    if extra and "session_id" in extra:
        sid = extra["session_id"]
    if sid is None:
        with _session_lock:
            sid = _current_session_id

    try:
        event = _build_event(
            phase,
            session_id=sid,
            iterations=iterations,
            tool_name=tool_name,
            summary=summary,
            extra=extra,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[C-P1.1] 构造 progress event 失败（非致命）: %s", exc)
        return

    with _sinks_lock:
        sinks = list(_sinks)

    for sink in sinks:
        try:
            sink(event)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[C-P1.1] progress sink 异常（非致命）: %s", exc)


# =========================================================================
# 测试 / 重置工具
# =========================================================================

def clear_sinks() -> None:
    """清空所有 sink + 清空 session_id。仅供测试使用。"""
    global _sinks, _current_session_id
    with _sinks_lock:
        _sinks.clear()
    with _session_lock:
        _current_session_id = None


def get_sink_count() -> int:
    """返回当前已注册的 sink 数量。仅供测试 / 诊断使用。"""
    with _sinks_lock:
        return len(_sinks)