"""对话循环 — Spirit Agent 的核心引擎（重写版）。

参考 Hermes 的 conversation_loop.py (5737行)，实现完整的 tool-calling 循环：
  LLM → 解析 tool_calls → 执行工具 → 结果返回 LLM → 循环

核心能力：
- 错误分类 + 自适应重试（error_handler）
- 工具调用护栏（tool_guardrails）
- 上下文自动压缩（context_compressor）
- 流式输出支持（streaming）
- 动态系统提示词（prompt_builder）
- 并发工具执行（tool_executor）
- 迭代预算控制（IterationBudget）
- Prompt caching（系统提示词缓存，减少 ~75% 输入 token 成本）
- 工具懒加载（首次不发送工具定义，检测到工具调用时才加载）
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from spirit.agent.error_handler import (
    classify_api_error,
    ErrorRecoveryPlan,
    FailoverReason,
    IterationBudget,
)
from spirit.agent.tool_guardrails import ToolCallGuardrailController
from spirit.agent.tool_executor import (
    execute_tool_calls_concurrent,
    execute_tool_calls_sequential,
    prepare_api_messages,
    serialize_tool_call,
)
from spirit.agent.context_compressor import estimate_messages_tokens
from spirit.agent.prompt_caching import apply_cache_control, should_use_prompt_caching
from spirit.agent.text_tool_parser import parse_text_tool_calls

logger = logging.getLogger(__name__)


# =========================================================================
# 主入口
# =========================================================================

def run_conversation(agent, user_message: str, **kwargs) -> "ConversationResult":
    """执行完整对话循环。

    核心流程：
    1. TurnContext per-turn 初始化（消息清理、会话恢复）
    2. 系统提示词构建/缓存
    3. 追加用户消息
    4. 主循环：
       a. 检查中断 + 迭代预算
       b. 构建 API 消息 + 工具定义
       c. 调用 LLM（带重试 + 错误分类）
       d. 解析响应（文本 → 返回；工具调用 → 执行 → 继续）
       e. 上下文压缩检查

    Args:
        agent: SpiritAgent 实例
        user_message: 用户消息
        **kwargs: 额外参数

    Returns:
        ConversationResult
    """
    from spirit.agent.agent import ConversationResult

    # ── Per-Turn 初始化 ───────────────────────────────────────
    turn_ctx = agent.prepare_turn_context(user_message)
    
    # 使用 TurnContext 中的 messages 列表
    agent._messages = turn_ctx.messages
    logger.info(
        "Turn context initialized: session=%s, msg_count=%d",
        agent.session_id[:8], len(turn_ctx.messages),
    )

    # ── 系统提示词构建/缓存 ───────────────────────────────────

    # 系统提示词（首次对话时构建，后续轮次复用缓存 — 保护 prompt cache）
    if not agent.messages:
        system_prompt = agent.get_system_prompt()
        agent.add_message("system", system_prompt)
        logger.info("系统提示词已构建并缓存 (%d 字符)", len(system_prompt))

    # ── 工具懒加载 ─────────────────────────────────────────────
    # 策略: 首轮不发送工具定义（加速首次响应），检测到工具调用时才加载。
    # 这与 Hermes 的 lazy tool discovery 模式一致:
    # - 减少首轮 API 请求的 payload 大小
    # - 对于纯文本对话场景，完全避免工具定义的传输开销
    tools = None
    tools_loaded = False

    # ── Prompt caching 初始化 ──────────────────────────────────
    # 检测当前 provider/model 是否支持 Anthropic 风格的 cache_control
    use_cache, native_layout = should_use_prompt_caching(
        provider=agent.provider,
        model=agent.model,
        base_url=agent.base_url,
    )
    if use_cache:
        logger.info(
            "Prompt caching 已启用: layout=%s",
            "native_anthropic" if native_layout else "openai_wire",
        )

    # 迭代预算
    budget = IterationBudget(agent.max_iterations)

    # 工具护栏
    guardrails = ToolCallGuardrailController()

    # 上下文压缩器
    compressor = getattr(agent, "_context_compressor", None)

    # 计数器
    api_call_count = 0
    retry_count = 0
    from spirit.config import get_config_value
    max_retries = get_config_value("agent.max_retries", 3)
    compression_attempts = 0
    
    # 本轮工具调用列表（用于返回给 CLI 显示）
    tool_calls_in_turn = []

    # ── 主循环 ──────────────────────────────────────────────────

    while budget.remaining > 0:
        # 中断检查
        if agent._interrupt_requested:
            logger.info("对话被用户中断")
            return ConversationResult(
                response="[已中断]",
                messages=agent.messages,
                iterations=api_call_count,
                tool_calls=tool_calls_in_turn,
            )

        # 消耗迭代
        if not budget.consume():
            logger.info("迭代预算耗尽 (%d/%d)", budget.used, budget.max_total)
            return ConversationResult(
                response=f"[达到最大迭代次数 {agent.max_iterations}]",
                messages=agent.messages,
                iterations=api_call_count,
                tool_calls=tool_calls_in_turn,
            )

        api_call_count += 1
        agent._api_call_count = api_call_count

        # ── 上下文压缩预检 ──────────────────────────────────────
        if compressor and len(agent.messages) > 5:
            approx_tokens = estimate_messages_tokens(agent.messages)
            if compressor.should_compress(approx_tokens) and compression_attempts < 3:
                compression_attempts += 1
                logger.info(
                    "预压缩 #%d: ~%d tokens 超过阈值",
                    compression_attempts, approx_tokens,
                )
                agent._messages = compressor.compress(agent.messages, approx_tokens)
                budget.refund()  # 压缩不消耗迭代
                continue

        # ── 构建 API 消息 ───────────────────────────────────────
        api_messages = prepare_api_messages(agent.messages)

        # ── 应用 prompt caching ─────────────────────────────────
        if use_cache:
            api_messages = apply_cache_control(
                api_messages,
                cache_ttl=getattr(agent, "_cache_ttl", "5m"),
                native_anthropic=native_layout,
            )

        # ── 调用 LLM（带重试） ──────────────────────────────────
        response = None
        retry_count = 0

        while retry_count < max_retries:
            try:
                response = _call_llm(agent, api_messages, tools)
                break  # 成功
            except Exception as exc:
                retry_count += 1
                classified = classify_api_error(
                    exc,
                    provider=agent.provider,
                    model=agent.model,
                )
                plan = ErrorRecoveryPlan.from_classification(classified, retry_count)

                logger.warning(
                    "API 调用失败 (#%d/%d): %s — %s",
                    retry_count, max_retries,
                    classified.reason.value, plan.message,
                )

                if plan.should_abort or not plan.should_retry:
                    return ConversationResult(
                        response=f"[API 错误: {classified.message}]",
                        messages=agent.messages,
                        iterations=api_call_count,
                        tool_calls=tool_calls_in_turn,
                    )

                if plan.should_compress and compressor:
                    approx_tokens = estimate_messages_tokens(agent.messages)
                    agent._messages = compressor.compress(agent.messages, approx_tokens)
                    api_messages = prepare_api_messages(agent.messages)
                    budget.refund()
                    break  # 跳出重试，回到主循环

                if plan.wait_seconds > 0:
                    logger.info("等待 %.1f 秒后重试...", plan.wait_seconds)
                    time.sleep(plan.wait_seconds)

        if response is None:
            return ConversationResult(
                response=f"[API 调用失败，已重试 {max_retries} 次]",
                messages=agent.messages,
                iterations=api_call_count,
                tool_calls=tool_calls_in_turn,
            )

        # ── 更新压缩器 token 统计 ───────────────────────────────
        if compressor and hasattr(response, "usage") and response.usage:
            compressor.update_from_response({
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
            })
        
        # ── 更新用量追踪器（辅助任务，失败不中断主流程） ─────────
        try:
            from spirit.agent.usage_tracker import get_usage_tracker
            tracker = get_usage_tracker()
            if hasattr(response, "usage") and response.usage:
                tracker.update({
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                })
        except Exception as e:
            logger.debug("用量追踪失败 (非致命): %s", e)

        # ── 解析响应 ───────────────────────────────────────────
        choice = response.choices[0]
        message = choice.message

        # 检查工具调用：优先使用 OpenAI 结构化 tool_calls，
        # 回退到文本 XML 解析（MiniMax 等不支持原生 tool_calls 的 provider）
        tool_calls = list(message.tool_calls) if message.tool_calls else []
        text_content = message.content or ""

        if not tool_calls and text_content:
            # 尝试从文本中解析 XML 格式的工具调用（照搬 Hermes 逻辑）
            parsed_calls, cleaned_text = parse_text_tool_calls(text_content)
            if parsed_calls:
                tool_calls = parsed_calls
                text_content = cleaned_text  # 使用移除工具调用块后的干净文本
                logger.info(
                    "文本工具调用解析: 从响应文本中提取 %d 个工具调用",
                    len(tool_calls),
                )

        # 有工具调用？
        if tool_calls:
            # 统一 tool_calls 格式：如果是文本解析结果，转换为 dict 列表
            if hasattr(tool_calls[0], 'function'):
                # OpenAI 原生 tool_calls
                tool_calls_dicts = [serialize_tool_call(tc) for tc in tool_calls]
            else:
                # 文本解析的 tool_calls（已经是 dict 列表）
                tool_calls_dicts = tool_calls
            
            # 记录本轮工具调用
            tool_calls_in_turn.extend(tool_calls_dicts)
            logger.info(
                "本轮检测到 %d 个工具调用 (累计: %d)",
                len(tool_calls_dicts), len(tool_calls_in_turn),
            )
            
            # 工具懒加载: 检测到工具调用时才加载工具定义
            if not tools_loaded:
                tools = agent.get_tool_definitions()
                tools_loaded = True
                logger.info(
                    "工具懒加载触发: 检测到 %d 个工具调用，已加载 %d 个工具定义",
                    len(tool_calls), len(tools),
                )

            agent.add_message(
                "assistant",
                text_content,
                tool_calls=tool_calls_dicts,
            )
            # 重置护栏（新一轮工具调用）
            guardrails.reset_for_turn()

            # 执行工具

            if len(tool_calls_dicts) > 1:
                # 并发执行
                execute_tool_calls_concurrent(
                    agent, tool_calls_dicts, agent._messages,
                    guardrails=guardrails,
                )
            else:
                # 单个工具顺序执行
                execute_tool_calls_sequential(
                    agent, tool_calls_dicts, agent._messages,
                    guardrails=guardrails,
                )

            # 检查护栏是否要求停止
            if guardrails.halt_decision and guardrails.halt_decision.should_halt:
                logger.warning("工具护栏触发停止: %s", guardrails.halt_decision.message)
                agent.add_message("assistant", f"[工具循环停止: {guardrails.halt_decision.message}]")
                return ConversationResult(
                    response=f"[工具循环停止: {guardrails.halt_decision.message}]",
                    messages=agent.messages,
                    usage=_extract_usage(response),
                    iterations=api_call_count,
                    tool_calls=tool_calls_in_turn,  # 传递工具调用列表
                )

            # 继续循环（让 LLM 看到工具结果）
            continue

        else:
            # 文本回复，结束循环
            # 工具懒加载: 即使没有工具调用，也加载工具供后续轮次使用
            if not tools_loaded:
                tools = agent.get_tool_definitions()
                tools_loaded = True
                logger.debug("文本回复轮次: 已加载 %d 个工具供后续使用", len(tools))
            content = message.content or ""
            agent.add_message("assistant", content)

            return ConversationResult(
                response=content,
                messages=agent.messages,
                usage=_extract_usage(response),
                iterations=api_call_count,
                tool_calls=tool_calls_in_turn,  # 传递工具调用列表
            )

    # 达到最大迭代
    return ConversationResult(
        response=f"[达到最大迭代次数 {agent.max_iterations}]",
        messages=agent.messages,
        iterations=api_call_count,
        tool_calls=tool_calls_in_turn,  # 传递工具调用列表
    )


# =========================================================================
# LLM 调用
# =========================================================================

def _call_llm(agent, api_messages: List[dict], tools: List[dict]):
    """调用 LLM API。

    集成 prompt caching: 当启用 cache_control 时，消息列表已经
    在上层被注入了 cache_control 断点。这里只负责发送请求。
    """
    request_kwargs = {
        "model": agent.model,
        "messages": api_messages,
    }

    if tools:
        request_kwargs["tools"] = tools

    # 透传 extra_params（如 provider 特定参数）
    extra = getattr(agent, "_extra_api_params", None)
    if extra:
        request_kwargs.update(extra)

    return agent.client.chat.completions.create(**request_kwargs)


# =========================================================================
# 辅助函数
# =========================================================================

def _extract_usage(response) -> Dict[str, int]:
    """从响应中提取 usage。"""
    try:
        if hasattr(response, "usage") and response.usage:
            return {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
    except Exception:
        pass
    return {}


__all__ = ["run_conversation"]
