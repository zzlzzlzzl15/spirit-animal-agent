"""后台进程通知格式化 — Spirit Agent。

对标 Hermes ``tools/process_registry.py`` 尾部的 ``format_process_notification``
与 ``_format_async_delegation``。统一通知队列（``ProcessRegistry.completion_queue``）
里流转四类事件，本模块负责把它们渲染成**能直接注入对话**的文本：

``completion``
    进程退出通知（``notify_on_complete=True`` 时入队）。渲染为
    ``[IMPORTANT: Background process ... ]``，包含退出码、命令、输出尾部。
``watch_match``
    输出命中 watch 模式。含命中的模式、匹配行、被限流丢弃的条数。
``watch_disabled`` / ``watch_overflow_*``
    限流熔断的自解释摘要（告诉模型"为什么突然安静了"）。
``async_delegation``
    异步子 Agent 完成。渲染为**自包含**的任务来源 + 结果块——因为回灌时
    模型可能早已在无关上下文里，不记得当初为什么派这个子 Agent。

返回 ``None`` 表示该事件无需注入（例如空事件）。
"""

from __future__ import annotations

import time as _time
from typing import Optional


def _format_age(seconds: float) -> str:
    """人类友好的时长串（``18m`` / ``2h3m`` / ``45s``）。"""
    try:
        s = int(max(0, seconds))
    except (TypeError, ValueError):
        return "?"
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m" if s == 0 else f"{m}m{s}s"
    h, m = divmod(m, 60)
    return f"{h}h" if m == 0 else f"{h}h{m}m"


def _format_async_delegation(evt: dict) -> str:
    """把异步委托完成事件渲染成自包含的再注入块。

    携带**完整的原始任务来源**（goal、父级给的 context、toolsets、role、model）
    加上派发时间、状态与结果摘要。回灌进对话时 Agent 可能正深陷无关上下文，
    因此这一块必须能独立读懂——既够用来消费结果，也够用来判断"世界已经变了，
    重新派发一次"。
    """
    deleg_id = evt.get("delegation_id", "unknown")
    goal = evt.get("goal", "") or ""
    context = evt.get("context")
    toolsets = evt.get("toolsets")
    role = evt.get("role") or "leaf"
    model = evt.get("model") or "?"
    status = evt.get("status") or "completed"
    summary = evt.get("summary")
    error = evt.get("error")
    api_calls = evt.get("api_calls", 0)
    duration = evt.get("duration_seconds", "?")
    dispatched_at = evt.get("dispatched_at")
    completed_at = evt.get("completed_at") or _time.time()

    # ----- 批量（fan-out）完成：合并的多任务块 -----
    # 整个 delegate_task 扇出作为一个后台单元一起结束，携带 per-task 的
    # ``results`` 列表。把所有子 Agent 的摘要渲染进同一块，让模型一次拿到
    # 合并后的结果。
    batch_results = evt.get("results")
    if evt.get("is_batch") or isinstance(batch_results, list):
        results = batch_results or []
        goals = evt.get("goals") or []
        n = len(results) if results else len(goals)
        total_dur = evt.get("total_duration_seconds", duration)
        lines = [
            f"[ASYNC DELEGATION BATCH COMPLETE — {deleg_id}]",
            f"A background fan-out of {n} subagent(s) you dispatched earlier "
            "has finished. All ran in parallel and waited on each other; their "
            "consolidated results are below. You may have moved on since "
            "dispatching — act on these or re-dispatch if things have changed.",
            "",
        ]
        if isinstance(dispatched_at, (int, float)):
            ts = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(dispatched_at))
            lines.append(f"Dispatched: {ts} ({_format_age(completed_at - dispatched_at)} ago)")
        if context:
            lines.append(f"Context you provided: {context}")
        if toolsets:
            lines.append(f"Toolsets: {', '.join(toolsets)}")
        lines.append(f"Role: {role}   Model: {model}   Total duration: {total_dur}s")
        if error and not results:
            lines.append("--- ERROR ---")
            lines.append(f"The batch did not complete successfully: {error}")
            return "\n".join(lines)
        for r in sorted(results, key=lambda x: x.get("task_index", 0)):
            idx = r.get("task_index", 0)
            r_status = r.get("status", "?")
            r_summary = r.get("summary")
            r_error = r.get("error")
            r_goal = goals[idx] if idx < len(goals) else r.get("goal", "")
            icon = "✓" if r_status in ("completed", "success") else "✗"
            lines.append("")
            header = f"--- {icon} TASK {idx + 1}/{n}"
            if r_goal:
                header += f": {r_goal}"
            header += f"  (status={r_status}"
            if r.get("api_calls"):
                header += f", api_calls={r['api_calls']}"
            if r.get("duration_seconds") is not None:
                header += f", {r['duration_seconds']}s"
            header += ") ---"
            lines.append(header)
            if r_status in ("completed", "success") and r_summary:
                lines.append(r_summary)
            elif r_summary:
                if r_error:
                    lines.append(f"({r_status}: {r_error})")
                lines.append("Partial output:")
                lines.append(r_summary)
            else:
                lines.append(
                    f"(no summary — status={r_status}"
                    + (f": {r_error}" if r_error else "")
                    + ")"
                )
        return "\n".join(lines)

    age = ""
    if isinstance(dispatched_at, (int, float)):
        age = f" ({_format_age(completed_at - dispatched_at)} ago)"

    lines = [
        f"[ASYNC DELEGATION COMPLETE — {deleg_id}]",
        "A background subagent you dispatched earlier has finished. You may "
        "have moved on since dispatching it; the full task source is below so "
        "you can act on the result or re-dispatch if things have changed.",
        "",
    ]
    if isinstance(dispatched_at, (int, float)):
        ts = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(dispatched_at))
        lines.append(f"Dispatched: {ts}{age}")
    lines.append(f"Original goal: {goal}")
    if context:
        lines.append(f"Context you provided: {context}")
    if toolsets:
        lines.append(f"Toolsets: {', '.join(toolsets)}")
    lines.append(f"Role: {role}   Model: {model}")
    lines.append(f"Status: {status}   API calls: {api_calls}   Duration: {duration}s")
    lines.append("--- RESULT ---")
    if status in ("completed", "success") and summary:
        lines.append(summary)
    elif status == "interrupted":
        lines.append(
            "The subagent was interrupted before completing"
            + (f": {error}" if error else ".")
        )
        if summary:
            lines.append("Partial output:")
            lines.append(summary)
    else:
        # error / timeout / failed
        lines.append(
            f"The subagent did not complete successfully (status={status})."
            + (f"\n{error}" if error else "")
        )
        if summary:
            lines.append("Partial output:")
            lines.append(summary)
    return "\n".join(lines)


def format_process_notification(evt: dict) -> Optional[str]:
    """把一条进程通知事件格式化为可注入对话的文本（无需注入时返回 ``None``）。

    处理完成事件（notify_on_complete）、watch 模式匹配、限流熔断摘要与
    异步委托完成，全部来自统一的 ``completion_queue``。
    """
    if not isinstance(evt, dict) or not evt:
        return None

    evt_type = evt.get("type", "completion")
    _sid = evt.get("session_id", "unknown")
    _cmd = evt.get("command", "unknown")

    # 限流/熔断类事件：message 已经是自解释文案，直接包裹
    if evt_type in {"watch_disabled", "watch_overflow_tripped", "watch_overflow_released"}:
        message = evt.get("message", "")
        return f"[IMPORTANT: {message}]" if message else None

    if evt_type == "watch_match":
        _pat = evt.get("pattern", "?")
        _out = evt.get("output", "")
        _sup = evt.get("suppressed", 0)
        text = (
            f"[IMPORTANT: Background process {_sid} matched "
            f"watch pattern \"{_pat}\".\n"
            f"Command: {_cmd}\n"
            f"Matched output:\n{_out}"
        )
        if _sup:
            text += f"\n({_sup} earlier matches were suppressed by rate limit)"
        text += "]"
        return text

    if evt_type == "async_delegation":
        return _format_async_delegation(evt)

    _exit = evt.get("exit_code", "?")
    _out = evt.get("output", "")
    _reason = evt.get("completion_reason") or "exited"
    _source = evt.get("termination_source") or ""
    _signal = ""
    if _exit in {-15, 143, "-15", "143"}:
        _signal = ", SIGTERM"
    # 措辞必须区分"我们杀的"与"外部 SIGTERM"：completion_reason 只有被
    # 注册表主动终止时才是 killed，否则即使退出码是 143 也报 exited。
    if _reason == "killed":
        _status = f"terminated by {_source or 'Spirit'}"
    elif _reason == "lost":
        _status = "marked lost because the process backend disappeared"
    elif _reason == "failed_start":
        _status = "failed to start"
    elif _exit == 0:
        _status = "completed normally"
    else:
        _status = "exited"
    return (
        f"[IMPORTANT: Background process {_sid} {_status} "
        f"(exit code {_exit}{_signal}).\n"
        f"Command: {_cmd}\n"
        f"Output:\n{_out}]"
    )


__all__ = [
    "format_process_notification",
    "format_async_delegation",
]

# 公开别名（下划线版本是内部实现，导出稳定名供测试与网关使用）
format_async_delegation = _format_async_delegation
