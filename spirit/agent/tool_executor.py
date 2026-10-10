"""工具执行引擎 — 顺序 + 并发工具调度。

参考 Hermes 的 tool_executor.py (1802行)。
提供：
- execute_tool_calls_sequential(): 顺序执行工具调用
- execute_tool_calls_concurrent(): 并发执行工具调用（线程池）
- 工具调用前/后护栏检查
- 中断检测与取消
- 结果格式化与消息构建
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from spirit.agent.tool_guardrails import (
    ToolCallGuardrailController,
    ToolGuardrailDecision,
    toolguard_synthetic_result,
    append_toolguard_guidance,
)
from spirit.agent.tool_result_classification import (
    classify_tool_result,
    format_classified_for_llm,
    ClassifiedToolResult,
    ToolResultCategory,
)
from spirit.agent.tool_liveness import (
    LivenessConfig,
    LivenessDetector,
    OutputLivenessProbe,
    build_timeout_result_json,
    create_default_liveness_detector,
)

# v0.4.x：工具进度事件总线（C-P1.1 接线）
# spirit/agent/conversation_loop.py 在 execute 前发 TOOL_GEN_START；
# 本模块在每个工具启动 / 完成时发 TOOL_STARTED / TOOL_COMPLETED
# （对齐 Hermes tool.started / tool.completed）。
from spirit.agent.progress_notifier import ProgressPhase, notify_progress

logger = logging.getLogger(__name__)

# v0.4.x：工具参数脱敏 + 预览拼接（CLI / 桌宠显示用）
# 现有 spirit 还没有专门的 redact 模块；这里 inline 简单实现，避免暴露 API key
# / 密码 / token 等敏感字段。完整脱敏矩阵可后续独立抽模块。
_SENSITIVE_ARG_KEYS = {
    "api_key", "apikey", "api-key",
    "token", "access_token", "refresh_token", "auth_token", "bearer_token",
    "password", "passwd", "secret",
    "private_key", "client_secret",
}


def _redact_tool_args_for_display(args: Any) -> Any:
    """对工具参数做键名级脱敏，供桌宠 / CLI 安全显示。

    规则：
    - 键名命中敏感集合 → 替换值为 ``"***"``（保留长度提示）
    - 嵌套 dict 递归；list 逐项
    - 顶层不是 dict（如 str）→ 原样返回
    """
    if isinstance(args, dict):
        out: Dict[str, Any] = {}
        for k, v in args.items():
            if k.lower() in _SENSITIVE_ARG_KEYS:
                out[k] = "***"
            elif isinstance(v, (dict, list)):
                out[k] = _redact_tool_args_for_display(v)
            else:
                out[k] = v
        return out
    if isinstance(args, list):
        return [_redact_tool_args_for_display(item) for item in args]
    return args


def _format_tool_args_preview(args: Any, max_len: int = 80) -> str:
    """把工具参数格式化为一行预览字符串（供 CLI ``┊ name(preview)`` 显示）。

    - 优先用 ``key=value`` 拼接
    - 过长按 ``max_len`` 截断 + ``...``
    - 空参数 → ``""``
    - 含敏感字段已先经 ``_redact_tool_args_for_display`` 处理
    """
    if not args:
        return ""
    if isinstance(args, dict):
        parts: List[str] = []
        for k, v in args.items():
            v_str = "***" if v == "***" else str(v)
            if len(v_str) > 24:
                v_str = v_str[:23] + "…"
            parts.append(f"{k}={v_str}")
        s = ", ".join(parts)
        if len(s) > max_len:
            s = s[: max_len - 1] + "…"
        return s
    s = str(args)
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s

# 最大并发工作线程
_MAX_TOOL_WORKERS = 8

# 默认并发超时（秒）—— R-P1.3 从 420s 缩短到 300s（整体硬超时）
_DEFAULT_CONCURRENT_TOOL_TIMEOUT_S = 300.0

# 单工具硬超时（秒）—— R-P1.3 新增（默认 60s）
_DEFAULT_SINGLE_TOOL_TIMEOUT_S = 60.0

# 单工具软超时（秒）—— R-P1.3 新增（默认 30s warning）
_DEFAULT_SINGLE_TOOL_SOFT_WARN_S = 30.0

# 整体软超时（秒）—— R-P1.3 新增（默认 90s warning）
_DEFAULT_OVERALL_SOFT_WARN_S = 90.0


# =========================================================================
# 工具结果消息构建
# =========================================================================

def make_tool_result_message(
    tool_name: str,
    result: str,
    tool_call_id: str,
    *,
    guardrail_decision: Optional[ToolGuardrailDecision] = None,
    classified: Optional[ClassifiedToolResult] = None,
) -> Dict[str, Any]:
    """构建 tool 角色消息。

    Args:
        tool_name: 工具名
        result: 工具返回的字符串
        tool_call_id: OpenAI 工具调用 ID
        guardrail_decision: 护栏后检结果（可选）
        classified: 工具结果分类（来自 :func:`classify_tool_result`，可选）。
                     失败时会自动在 content 前注入 ``_classification`` / ``_retryable`` / ``_guidance``，
                     让 LLM 区分"参数错误（不可重试）"vs"网络超时（可重试）"。
                     成功时不会污染 content。
    """
    # 失败 / 部分成功 / 可重试 / 不可重试时注入分类前缀
    if classified is not None and classified.category != ToolResultCategory.SUCCESS:
        content = format_classified_for_llm(classified, include_original=True)
    else:
        content = result
    if guardrail_decision:
        content = append_toolguard_guidance(content, guardrail_decision)

    return {
        "role": "tool",
        "content": content,
        "tool_call_id": tool_call_id,
        "name": tool_name,
    }


# =========================================================================
# 参数解析
# =========================================================================

def parse_tool_arguments(raw_arguments: Any) -> Tuple[dict, Optional[str]]:
    """解析模型生成的工具参数。

    Returns:
        (parsed_args, error_message)
        如果 error_message 不为 None，说明解析失败，不应执行工具。
    """
    try:
        arguments = json.loads(raw_arguments)
    except (json.JSONDecodeError, TypeError):
        return {}, json.dumps({
            "error": "无效的工具参数",
            "message": "工具参数必须是有效的 JSON 对象；工具未执行。",
        }, ensure_ascii=False)

    if isinstance(arguments, dict):
        return arguments, None

    return {}, json.dumps({
        "error": "无效的工具参数",
        "message": "工具参数必须是 JSON 对象；工具未执行。",
    }, ensure_ascii=False)


# =========================================================================
# 单个工具执行
# =========================================================================

def _emit_hook(agent: Any, event: str, **kwargs) -> None:
    """触发钩子事件（钩子系统未启用时静默跳过）。

    HookManager.emit 内部已对每个 handler 做异常隔离；
    本地不能 import conversation_loop._emit_hook（会循环依赖）。
    """
    hook_manager = getattr(agent, "hook_manager", None)
    if hook_manager is not None:
        hook_manager.emit(event, **kwargs)


def execute_single_tool(
    agent: Any,
    *,
    tool_call: Dict[str, Any],
    guardrails: Optional[ToolCallGuardrailController] = None,
    detector: Optional[LivenessDetector] = None,
) -> Tuple[str, str, Optional[ToolGuardrailDecision]]:
    """执行单个工具调用。

    Args:
        agent: SpiritAgent 实例
        tool_call: 工具调用 dict（含 id, function.name, function.arguments）
        guardrails: 护栏控制器
        detector: 可选的 liveness detector（R-P1.3 新增）。如提供，工具执行
                  期间会监控 elapsed；超时后用分类的 timeout JSON 替换 result
                  （而不是默默失败）。

    Returns:
        (result, tool_name, guardrail_decision)
    """
    function = tool_call.get("function", {})
    tool_name = function.get("name", "")
    tool_call_id = tool_call.get("id", "")

    # 解析参数
    args, parse_error = parse_tool_arguments(function.get("arguments", "{}"))
    if parse_error is not None:
        return parse_error, tool_name, None

    # 护栏：调用前检查
    if guardrails:
        decision = guardrails.before_call(tool_name, args)
        if not decision.allows_execution:
            return toolguard_synthetic_result(decision), tool_name, decision

    # 中断检查
    if getattr(agent, "_interrupt_requested", False):
        return json.dumps({
            "error": f"工具执行被用户中断取消",
            "status": "cancelled",
        }, ensure_ascii=False), tool_name, None

    # 执行（on_tool_start/on_complete 回调统一在 invoke_tool 内触发，避免重复推送）
    _emit_hook(agent, "before_tool_execute", tool_name=tool_name, tool_args=args)
    start_time = time.time()

    # R-P1.3：liveness 检测器启动（如提供）
    if detector is not None:
        detector.start()

    try:
        result = agent.invoke_tool(tool_name, args)
        # R-P1.3：记录进度（重置 inactivity 计时）
        if detector is not None:
            detector.record_progress(output=result)
    except Exception as exc:
        logger.error("工具 %s 执行异常: %s", tool_name, exc, exc_info=True)
        _emit_hook(agent, "on_tool_error", tool_name=tool_name, error=exc, tool_args=args)
        result = json.dumps({
            "error": f"工具执行异常: {type(exc).__name__}: {str(exc)[:500]}",
            "status": "error",
        }, ensure_ascii=False)

    # R-P1.3：liveness 检测器停止 + 检查是否超时
    if detector is not None:
        detector.stop()
        if detector.is_cancelled():
            elapsed = detector.elapsed()
            logger.warning(
                "[tool_executor] 工具 %s 被 liveness 标记假死（%.1fs），替换结果为超时分类",
                tool_name, elapsed,
            )
            result = build_timeout_result_json(
                tool_name=tool_name,
                elapsed_s=elapsed,
                timeout_kind="single_tool",
                retryable=True,
            )

    duration_ms = int((time.time() - start_time) * 1000)
    logger.info("工具 %s 执行完成 (%d ms)", tool_name, duration_ms)
    # args/result 别名供 memora_auto_save 钩子消费（其签名为 tool_name/args/result）
    _emit_hook(
        agent, "after_tool_execute",
        tool_name=tool_name, tool_args=args, tool_result=result,
        duration_ms=duration_ms, args=args, result=result,
    )

    # 护栏：调用后检查
    guardrail_decision = None
    if guardrails:
        # 分类工具结果（新分类器：区分成功 / 部分成功 / 失败 / 可重试 / 不可重试）
        classified = classify_tool_result(tool_name, result)
        guardrail_decision = guardrails.after_call(tool_name, args, result, failed=classified.is_failure)

    return result, tool_name, guardrail_decision


def _detect_tool_failure(tool_name: str, result: str) -> bool:
    """简单的工具失败检测。"""
    if not result:
        return False
    if tool_name == "terminal":
        try:
            data = json.loads(result)
            if isinstance(data, dict):
                exit_code = data.get("exit_code")
                if exit_code is not None and exit_code != 0:
                    return True
        except (json.JSONDecodeError, TypeError):
            pass
        return False
    lower = result[:500].lower()
    return '"error"' in lower or '"failed"' in lower or result.startswith("Error")


# =========================================================================
# 顺序执行
# =========================================================================

def execute_tool_calls_sequential(
    agent: Any,
    tool_calls: List[Dict[str, Any]],
    messages: List[Dict[str, Any]],
    *,
    guardrails: Optional[ToolCallGuardrailController] = None,
    single_tool_timeout_s: float = _DEFAULT_SINGLE_TOOL_TIMEOUT_S,
    single_tool_soft_warn_s: float = _DEFAULT_SINGLE_TOOL_SOFT_WARN_S,
) -> None:
    """顺序执行多个工具调用。

    结果按原始顺序追加到 messages。

    R-P1.3 新增：每个工具启动一个 LivenessDetector，监控 elapsed；超过
    ``single_tool_timeout_s`` 无进展 → 标记取消并用分类的 timeout JSON
    替换 result（不是默默失败）。
    """
    for tool_call in tool_calls:
        if getattr(agent, "_interrupt_requested", False):
            messages.append(make_tool_result_message(
                tool_call.get("function", {}).get("name", ""),
                json.dumps({"error": "工具被用户中断取消", "status": "cancelled"}, ensure_ascii=False),
                tool_call.get("id", ""),
            ))
            continue

        # R-P1.3：为每个工具创建 liveness detector
        tool_name_for_detector = tool_call.get("function", {}).get("name", "")
        detector = create_default_liveness_detector(
            single_tool_timeout_s=single_tool_timeout_s,
            overall_timeout_s=single_tool_timeout_s * 10,  # sequential 下整体 = 10x 单工具
        )
        # 自定义软超时阈值
        detector.config.single_tool_soft_warn_s = single_tool_soft_warn_s

        # v0.4.x：进度事件 — 工具启动（TOOL_STARTED）
        # 解析参数、脱敏、构造预览；异常隔离，失败也不阻断执行。
        _tcall_args, _tcall_parse_err = parse_tool_arguments(
            tool_call.get("function", {}).get("arguments", "{}"),
        )
        _tcall_display_args = _redact_tool_args_for_display(_tcall_args)
        _tcall_preview = _format_tool_args_preview(_tcall_display_args)
        try:
            notify_progress(
                ProgressPhase.TOOL_STARTED,
                tool_name=tool_name_for_detector,
                summary=_tcall_preview or tool_name_for_detector,
                extra={
                    "tool_name": tool_name_for_detector,
                    "function_args": _tcall_args,
                    "display_args": _tcall_display_args,
                    "preview": _tcall_preview,
                },
            )
        except Exception:
            logger.debug("TOOL_STARTED 推送失败（非致命）", exc_info=True)

        _tcall_started = time.monotonic()
        result, tool_name, guardrail_decision = execute_single_tool(
            agent,
            tool_call=tool_call,
            guardrails=guardrails,
            detector=detector,
        )
        _tcall_duration_ms = (time.monotonic() - _tcall_started) * 1000.0

        # v0.4.x：进度事件 — 工具完成（TOOL_COMPLETED）
        # is_error 不分类；与 Hermes _on_tool_progress 字段对齐。
        _classified_for_progress = classify_tool_result(tool_name, result)
        _is_error = bool(_classified_for_progress.is_failure)
        try:
            notify_progress(
                ProgressPhase.TOOL_COMPLETED,
                tool_name=tool_name,
                summary=tool_name,
                extra={
                    "tool_name": tool_name,
                    "function_args": _tcall_args,
                    "result": result,
                    "is_error": _is_error,
                    "status": "error" if _is_error else "success",
                    "duration_ms": _tcall_duration_ms,
                    "error_type": (
                        _classified_for_progress.category.value
                        if _is_error and _classified_for_progress is not None else None
                    ),
                },
            )
        except Exception:
            logger.debug("TOOL_COMPLETED 推送失败（非致命）", exc_info=True)

        # 分类工具结果（新分类器：让 LLM 看到分类指引）
        classified = classify_tool_result(tool_name, result)

        messages.append(make_tool_result_message(
            tool_name,
            result,
            tool_call.get("id", ""),
            guardrail_decision=guardrail_decision,
            classified=classified,
        ))


# =========================================================================
# 并发执行
# =========================================================================

def execute_tool_calls_concurrent(
    agent: Any,
    tool_calls: List[Dict[str, Any]],
    messages: List[Dict[str, Any]],
    *,
    guardrails: Optional[ToolCallGuardrailController] = None,
    max_workers: int = _MAX_TOOL_WORKERS,
    timeout_s: float = _DEFAULT_CONCURRENT_TOOL_TIMEOUT_S,
    single_tool_timeout_s: float = _DEFAULT_SINGLE_TOOL_TIMEOUT_S,
    overall_soft_warn_s: float = _DEFAULT_OVERALL_SOFT_WARN_S,
) -> None:
    """并发执行多个工具调用（线程池）。

    结果按原始 tool_call 顺序追加到 messages。
    超时后取消未完成的 future。

    R-P1.3 新增 liveness 检测：
    - 启动一个 LivenessDetector 监控整批
    - 超过 ``overall_timeout_s``（默认 300s）→ 全局取消
    - 超过 ``overall_soft_warn_s``（默认 90s）→ warning log
    - 取消后的工具 result 被替换为 :func:`build_timeout_result_json`
      （含分类信息，让 LLM 区分原因），不是默默失败
    """
    num_tools = len(tool_calls)

    # 中断预检
    if getattr(agent, "_interrupt_requested", False):
        for tc in tool_calls:
            messages.append(make_tool_result_message(
                tc.get("function", {}).get("name", ""),
                json.dumps({"error": "工具被用户中断取消", "status": "cancelled"}, ensure_ascii=False),
                tc.get("id", ""),
            ))
        return

    # 解析参数 + 护栏预检
    parsed_calls = []  # (tool_call, function_name, args, pre_result, blocked)
    for tc in tool_calls:
        function = tc.get("function", {})
        name = function.get("name", "")
        args, parse_error = parse_tool_arguments(function.get("arguments", "{}"))

        if parse_error is not None:
            parsed_calls.append((tc, name, {}, parse_error, True))
            continue

        # 护栏预检
        if guardrails:
            decision = guardrails.before_call(name, args)
            if not decision.allows_execution:
                parsed_calls.append((tc, name, args, toolguard_synthetic_result(decision), True))
                continue

        parsed_calls.append((tc, name, args, None, False))

    # 需要实际执行的工具
    to_execute = [(tc, name, args) for tc, name, args, pre, blocked in parsed_calls if not blocked]

    # 先追加已有预检结果
    for tc, name, args, pre_result, blocked in parsed_calls:
        if blocked:
            messages.append(make_tool_result_message(name, pre_result, tc.get("id", "")))

    if not to_execute:
        return

    # 解析超时
    timeout = _resolve_timeout(timeout_s)

    # ── R-P1.3：启动整批 liveness detector ──
    detector = LivenessDetector(
        probe=OutputLivenessProbe(),
        config=LivenessConfig(
            single_tool_timeout_s=single_tool_timeout_s,
            single_tool_soft_warn_s=_DEFAULT_SINGLE_TOOL_SOFT_WARN_S,
            overall_timeout_s=timeout if (timeout and timeout > 0) else _DEFAULT_CONCURRENT_TOOL_TIMEOUT_S,
            overall_soft_warn_s=overall_soft_warn_s,
            check_interval_s=2.0,
        ),
    )
    detector.start()

    # 线程池执行
    results: Dict[int, str] = {}
    start_times: Dict[int, float] = {}
    deadline = time.time() + timeout if timeout and timeout > 0 else None
    batch_cancelled_by = None  # 记录是谁触发的取消（"liveness_single" / "liveness_overall" / "interrupt" / "deadline"）

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(max_workers, len(to_execute)),
        thread_name_prefix="spirit-tool",
    ) as executor:
        future_to_idx = {}
        for idx, (tc, name, args) in enumerate(to_execute):
            _emit_hook(agent, "before_tool_execute", tool_name=name, tool_args=args)
            start_times[idx] = time.time()

            # v0.4.x：进度事件 — 工具启动（TOOL_STARTED，并发路径）
            # 在 submit 前发；args 已是 parse 后的 dict
            _tcall_display_args = _redact_tool_args_for_display(args)
            _tcall_preview = _format_tool_args_preview(_tcall_display_args)
            try:
                notify_progress(
                    ProgressPhase.TOOL_STARTED,
                    tool_name=name,
                    summary=_tcall_preview or name,
                    extra={
                        "tool_name": name,
                        "function_args": args,
                        "display_args": _tcall_display_args,
                        "preview": _tcall_preview,
                    },
                )
            except Exception:
                logger.debug("TOOL_STARTED 推送失败（非致命）", exc_info=True)

            future = executor.submit(_run_tool_safe, agent, name, args)
            future_to_idx[future] = idx

        # 收集结果
        done = set()
        while len(done) < len(future_to_idx):
            # 检查中断
            if getattr(agent, "_interrupt_requested", False):
                for f in future_to_idx:
                    if f not in done:
                        f.cancel()
                batch_cancelled_by = "interrupt"
                break

            # 检查 liveness 触发的取消（单工具假死 / 整体超时）
            if detector.is_cancelled():
                for f in future_to_idx:
                    if f not in done:
                        f.cancel()
                # 区分触发原因
                elapsed = detector.elapsed()
                if elapsed >= detector.config.overall_timeout_s:
                    batch_cancelled_by = "liveness_overall"
                else:
                    batch_cancelled_by = "liveness_single"
                break

            # 检查 deadline（保留旧逻辑作为 fallback）
            if deadline and time.time() > deadline:
                for f in future_to_idx:
                    if f not in done:
                        f.cancel()
                logger.warning("并发工具执行超时 (%.0fs)，取消剩余", timeout)
                batch_cancelled_by = "deadline"
                break

            try:
                batch_done, _ = concurrent.futures.wait(
                    future_to_idx,
                    timeout=2.0,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for f in batch_done:
                    if f not in done:
                        done.add(f)
                        idx = future_to_idx[f]
                        try:
                            results[idx] = f.result(timeout=1.0)
                            # R-P1.3：记录进度
                            detector.record_progress(output=results[idx])
                        except concurrent.futures.CancelledError:
                            results[idx] = json.dumps({
                                "error": "工具执行被取消",
                                "status": "cancelled",
                            }, ensure_ascii=False)
                        except Exception as exc:
                            results[idx] = json.dumps({
                                "error": f"工具执行异常: {exc}",
                                "status": "error",
                            }, ensure_ascii=False)
            except concurrent.futures.TimeoutError:
                continue

    # R-P1.3：停止 detector
    detector.stop()

    # 按原始顺序追加结果
    for idx, (tc, name, args) in enumerate(to_execute):
        result = results.get(idx, json.dumps({
            "error": "工具执行超时",
            "status": "timeout",
        }, ensure_ascii=False))

        # R-P1.3：如果被取消且 result 不是 timeout JSON，用 build_timeout_result_json 替换
        if batch_cancelled_by and '"status": "timeout"' not in result and '"status": "cancelled"' not in result:
            elapsed = detector.elapsed() if detector else 0.0
            timeout_kind = (
                "overall" if batch_cancelled_by == "liveness_overall"
                else "single_tool" if batch_cancelled_by == "liveness_single"
                else "cancelled" if batch_cancelled_by == "interrupt"
                else "overall"
            )
            result = build_timeout_result_json(
                tool_name=name,
                elapsed_s=elapsed,
                timeout_kind=timeout_kind,
                retryable=(batch_cancelled_by != "interrupt"),
            )

        # 钩子：并发路径的执行后事件（异常已被 _run_tool_safe 封装进 result，
        # 不单独触发 on_tool_error；args/result 别名供 memora_auto_save 消费）
        duration_ms = int((time.time() - start_times.get(idx, time.time())) * 1000)
        _emit_hook(
            agent, "after_tool_execute",
            tool_name=name, tool_args=args, tool_result=result,
            duration_ms=duration_ms, args=args, result=result,
        )

        # v0.4.x：进度事件 — 工具完成（TOOL_COMPLETED，并发路径）
        # 分类放在 hook 之后（沿用下方 ``classified``），但要先算好避免重分类。
        _concurrent_classified = classify_tool_result(name, result)
        _concurrent_is_error = bool(_concurrent_classified.is_failure)
        try:
            notify_progress(
                ProgressPhase.TOOL_COMPLETED,
                tool_name=name,
                summary=name,
                extra={
                    "tool_name": name,
                    "function_args": args,
                    "result": result,
                    "is_error": _concurrent_is_error,
                    "status": "error" if _concurrent_is_error else "success",
                    "duration_ms": duration_ms,
                    "error_type": (
                        _concurrent_classified.category.value
                        if _concurrent_is_error and _concurrent_classified is not None else None
                    ),
                },
            )
        except Exception:
            logger.debug("TOOL_COMPLETED 推送失败（非致命）", exc_info=True)

        # 护栏后检
        guardrail_decision = None
        if guardrails:
            classified = classify_tool_result(name, result)
            guardrail_decision = guardrails.after_call(name, args, result, failed=classified.is_failure)
        else:
            classified = classify_tool_result(name, result)

        messages.append(make_tool_result_message(
            name, result, tc.get("id", ""),
            guardrail_decision=guardrail_decision,
            classified=classified,
        ))


def _run_tool_safe(agent: Any, tool_name: str, args: dict) -> str:
    """线程安全的工具执行包装。"""
    try:
        return agent.invoke_tool(tool_name, args)
    except Exception as exc:
        return json.dumps({
            "error": f"工具执行异常: {type(exc).__name__}: {str(exc)[:500]}",
            "status": "error",
        }, ensure_ascii=False)


def _resolve_timeout(timeout_s: float) -> Optional[float]:
    """解析超时配置（环境变量优先）。"""
    env_val = os.environ.get("SPIRIT_CONCURRENT_TOOL_TIMEOUT_S", "").strip()
    if env_val:
        try:
            value = float(env_val)
            if value <= 0:
                return None
            return value
        except ValueError:
            pass
    return timeout_s


# =========================================================================
# 消息格式辅助
# =========================================================================

def prepare_api_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """将内部消息格式转为 OpenAI API 格式。"""
    api_messages = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        api_msg: Dict[str, Any] = {"role": role}

        if "tool_calls" in msg and msg["tool_calls"]:
            tool_calls = msg["tool_calls"]
            if tool_calls and isinstance(tool_calls[0], dict) and "id" in tool_calls[0]:
                api_msg["tool_calls"] = tool_calls
            api_msg["content"] = content or None
        elif role == "tool":
            api_msg["content"] = content
            api_msg["tool_call_id"] = msg.get("tool_call_id", "")
        else:
            api_msg["content"] = content

        api_messages.append(api_msg)

    return api_messages


def serialize_tool_call(tool_call) -> Dict[str, Any]:
    """将 OpenAI SDK 的 tool_call 对象序列化为 dict。"""
    return {
        "id": tool_call.id,
        "type": "function",
        "function": {
            "name": tool_call.function.name,
            "arguments": tool_call.function.arguments,
        },
    }


__all__ = [
    "execute_tool_calls_sequential",
    "execute_tool_calls_concurrent",
    "execute_single_tool",
    "make_tool_result_message",
    "parse_tool_arguments",
    "prepare_api_messages",
    "serialize_tool_call",
]
