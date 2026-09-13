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

logger = logging.getLogger(__name__)

# 最大并发工作线程
_MAX_TOOL_WORKERS = 8

# 默认并发超时（秒）
_DEFAULT_CONCURRENT_TOOL_TIMEOUT_S = 420.0


# =========================================================================
# 工具结果消息构建
# =========================================================================

def make_tool_result_message(
    tool_name: str,
    result: str,
    tool_call_id: str,
    *,
    guardrail_decision: Optional[ToolGuardrailDecision] = None,
) -> Dict[str, Any]:
    """构建 tool 角色消息。"""
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

def execute_single_tool(
    agent: Any,
    *,
    tool_call: Dict[str, Any],
    guardrails: Optional[ToolCallGuardrailController] = None,
) -> Tuple[str, str, Optional[ToolGuardrailDecision]]:
    """执行单个工具调用。

    Args:
        agent: SpiritAgent 实例
        tool_call: 工具调用 dict（含 id, function.name, function.arguments）
        guardrails: 护栏控制器

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
    start_time = time.time()
    try:
        result = agent.invoke_tool(tool_name, args)
    except Exception as exc:
        logger.error("工具 %s 执行异常: %s", tool_name, exc, exc_info=True)
        result = json.dumps({
            "error": f"工具执行异常: {type(exc).__name__}: {str(exc)[:500]}",
            "status": "error",
        }, ensure_ascii=False)

    duration_ms = int((time.time() - start_time) * 1000)
    logger.info("工具 %s 执行完成 (%d ms)", tool_name, duration_ms)

    # 护栏：调用后检查
    guardrail_decision = None
    if guardrails:
        # 简单失败检测
        failed = _detect_tool_failure(tool_name, result)
        guardrail_decision = guardrails.after_call(tool_name, args, result, failed=failed)

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
) -> None:
    """顺序执行多个工具调用。

    结果按原始顺序追加到 messages。
    """
    for tool_call in tool_calls:
        if getattr(agent, "_interrupt_requested", False):
            messages.append(make_tool_result_message(
                tool_call.get("function", {}).get("name", ""),
                json.dumps({"error": "工具被用户中断取消", "status": "cancelled"}, ensure_ascii=False),
                tool_call.get("id", ""),
            ))
            continue

        result, tool_name, guardrail_decision = execute_single_tool(
            agent,
            tool_call=tool_call,
            guardrails=guardrails,
        )

        messages.append(make_tool_result_message(
            tool_name,
            result,
            tool_call.get("id", ""),
            guardrail_decision=guardrail_decision,
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
) -> None:
    """并发执行多个工具调用（线程池）。

    结果按原始 tool_call 顺序追加到 messages。
    超时后取消未完成的 future。
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

    # 线程池执行
    results: Dict[int, str] = {}
    deadline = time.time() + timeout if timeout and timeout > 0 else None

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(max_workers, len(to_execute)),
        thread_name_prefix="spirit-tool",
    ) as executor:
        future_to_idx = {}
        for idx, (tc, name, args) in enumerate(to_execute):
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
                break

            # 检查超时
            if deadline and time.time() > deadline:
                for f in future_to_idx:
                    if f not in done:
                        f.cancel()
                logger.warning("并发工具执行超时 (%.0fs)，取消剩余", timeout)
                break

            try:
                batch_done, _ = concurrent.futures.wait(
                    future_to_idx,
                    timeout=5.0,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for f in batch_done:
                    if f not in done:
                        done.add(f)
                        idx = future_to_idx[f]
                        try:
                            results[idx] = f.result(timeout=1.0)
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

    # 按原始顺序追加结果
    for idx, (tc, name, args) in enumerate(to_execute):
        result = results.get(idx, json.dumps({
            "error": "工具执行超时",
            "status": "timeout",
        }, ensure_ascii=False))

        # 护栏后检
        guardrail_decision = None
        if guardrails:
            failed = _detect_tool_failure(name, result)
            guardrail_decision = guardrails.after_call(name, args, result, failed=failed)

        messages.append(make_tool_result_message(
            name, result, tc.get("id", ""),
            guardrail_decision=guardrail_decision,
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
