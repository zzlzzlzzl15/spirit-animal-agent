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
import re
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

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
from spirit.config import get_config_value

logger = logging.getLogger(__name__)


class StreamStaleTimeoutError(TimeoutError):
    """流看门狗超时：首字节或 chunk 间隔长期断供，判流死亡并已关闭。"""

# 流式门控：检测到这些标签开头时停止实时推送（避免 think/工具调用 JSON 泄露到终端）
_STREAM_OPENER_RE = re.compile(
    r'<(?:think|thinking|reasoning|tool_call|invoke)\b', re.IGNORECASE,
)

# 各 provider 默认输出上限（llm.max_tokens 配置为 0 时生效）。
# 思考模型（MiniMax-M3 等）的推理过程计入输出配额；不显式下发 max_tokens 时
# 服务端默认上限偏小，长推理会把可见文本与工具调用截断（finish_reason=length），
# 表现为“宣布步骤后就中断”。MiniMax-M3 输出上限 131072，32768 留足余量。
_DEFAULT_MAX_TOKENS_BY_PROVIDER = {
    "minimax": 32768,
}
_DEFAULT_MAX_TOKENS = 16384

# 单轮内截断自动续写的最大次数（防止模型反复截断导致无限循环）
_MAX_TRUNCATION_CONTINUATIONS = 2

# 单轮内"最终回复为空"时注入收尾提示的最大次数（保证任务不以无结论静默结束）
_MAX_EMPTY_FINAL_NUDGES = 2

# 额度耗尽型失败指纹：月配额/总额度耗尽属于"恢复期以天计的必败"，与瞬时限流
# 不同——退避重试无意义，每轮恢复主 provider 也只是白打一次必败调用。
_QUOTA_EXHAUSTED_RE = re.compile(
    r"insufficient_quota|quota\s+(?:has\s+been\s+)?exhausted|quota\s+exceeded"
    r"|insufficient\s+balance|balance\s+insufficient|credits?\s+exhausted",
    re.IGNORECASE,
)

# 主 provider 额度耗尽冷却（秒）：冷却期内新一轮不恢复主 provider，直接沿用
# 上一轮的 fallback 运行时（避免每条消息都先打一次必败 429 + 刷一条切换通知）；
# 冷却到期后主 provider 再获得一次探活机会（保留 Hermes turn 作用域语义，仅节流）。
_PRIMARY_QUOTA_COOLDOWN_SECONDS = 24 * 3600.0

# 值得跨 provider 故障转移的失败原因（provider 侧不可用，换一家可能就能用）。
# 不含自身载荷问题（context_overflow/payload_too_large/image_too_large/
# content_policy_blocked/format_error）——换家解决不了，走压缩/修复路径。
_PROVIDER_FAILOVER_REASONS = {
    FailoverReason.timeout,
    FailoverReason.server_error,
    FailoverReason.overloaded,
    FailoverReason.rate_limit,
    FailoverReason.upstream_rate_limit,
    FailoverReason.auth,
    FailoverReason.auth_permanent,
    FailoverReason.billing,
    FailoverReason.model_not_found,
    FailoverReason.ssl_cert_verification,
    FailoverReason.unknown,
}


# =========================================================================
# 主入口
# =========================================================================

def run_conversation(
    agent,
    user_message: str,
    stream_callback: Optional[Callable[[str], None]] = None,
    **kwargs,
) -> "ConversationResult":
    """执行完整对话循环（唯一主循环，流式作为传输层集成在循环内）。

    核心流程（参考 Hermes run_conversation 阶段划分）：
    1. TurnContext per-turn 初始化（消息清理、会话恢复）
    2. 系统提示词构建/缓存
    3. 追加用户消息
    4. 主循环：
       a. 检查中断 + 迭代预算
       b. 构建 API 消息 + 工具定义
       c. 调用 LLM（带重试 + 错误分类；可选流式增量回调）
       d. 解析响应（原生 tool_calls 或文本 XML/JSON 回退）
       e. 有工具调用 → 执行 → continue；无 → 最终回答 → return
       f. 上下文压缩检查

    Args:
        agent: SpiritAgent 实例
        user_message: 用户消息
        stream_callback: 可选的文本增量回调 (text: str) -> None
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

    # fallback 是 turn 作用域（Hermes 语义）：新一轮恢复主 provider、重置链索引，
    # 主家持续不可用时每轮先给它一次机会，失败再按消息重新激活链
    _restore_primary_runtime(agent)

    # ── 系统提示词构建/缓存 ───────────────────────────────────

    # 系统提示词（首次对话时构建，后续轮次复用缓存 — 保护 prompt cache）
    if not agent.messages:
        system_prompt = agent.get_system_prompt()
        agent.add_message("system", system_prompt)
        logger.info("系统提示词已构建并缓存 (%d 字符)", len(system_prompt))

    # ── 工具定义加载 ─────────────────────────────────────────────
    # 必须在首轮 API 调用前加载工具定义，否则 LLM 不知道有哪些工具可用，
    # 无法生成 tool_calls。这与 Hermes 的做法一致：每次 API 调用都携带工具定义。
    tools = agent.get_tool_definitions()
    logger.info("工具定义已加载: %d 个工具", len(tools))

    # ── 钩子：对话开始 ─────────────────────────────────
    _emit_hook(agent, "before_conversation", user_message=user_message, session_id=agent.session_id)

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

    # fallback 切换等过程通知走独立回调（如有）：前端把该通道当"过程通知"，
    # 不计入流式增量——通知若混进 stream_delta 会让 streamBuffer 非空，
    # 轮末错误结论的兜底渲染会被"已流式送达"判定吞掉，失败轮静默结束。
    notice_callback = kwargs.pop("notice_callback", None) or stream_callback

    # 本轮工具调用列表（用于返回给 CLI 显示）
    tool_calls_in_turn = []

    # 截断自动续写计数（finish_reason=length 时注入续写提示再推一轮）
    truncation_continuations = 0

    # 空结论收尾提示计数（最终回复为空时注入收尾提示再推一轮）
    empty_final_nudges = 0

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
                _emit_hook(
                    agent, "on_context_compress",
                    before_tokens=approx_tokens,
                    after_tokens=estimate_messages_tokens(agent._messages),
                    trigger="preemptive",
                )
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
                _emit_hook(
                    agent, "before_llm_call",
                    messages=api_messages, tools=tools, retry_count=retry_count,
                )
                _llm_started = time.monotonic()
                response = _call_llm(agent, api_messages, tools, stream_callback)
                _emit_hook(
                    agent, "after_llm_call",
                    response=response,
                    duration_ms=(time.monotonic() - _llm_started) * 1000.0,
                )
                break  # 成功
            except Exception as exc:
                retry_count += 1
                _emit_hook(agent, "on_llm_error", error=exc, retry_count=retry_count)
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

                # 额度耗尽型失败（月配额重置以天计）且当前还跑在主 provider 上：
                # 标记冷却，冷却期内新一轮直接走 fallback，不再"每轮给主家一次机会"
                if (
                    classified.reason in _PROVIDER_FAILOVER_REASONS
                    and not getattr(agent, "_fallback_activated", False)
                    and (
                        classified.reason == FailoverReason.billing
                        or _QUOTA_EXHAUSTED_RE.search(str(exc))
                    )
                ):
                    _mark_primary_quota_cooldown(agent)

                # 跨 provider 切换（Hermes try_activate_fallback 语义）：这家不可用
                # 时沿 fallback 链换下一家，重试计数清零、按新 provider 重建请求
                # 上下文，本轮内继续重试。放在 abort 判定之前：billing 类 plan 会
                # 直接 abort，但换一家还能救。
                if classified.reason in _PROVIDER_FAILOVER_REASONS:
                    if _try_activate_fallback(agent, classified.reason, notice_callback):
                        retry_count = 0
                        use_cache, native_layout = should_use_prompt_caching(
                            provider=agent.provider, model=agent.model,
                            base_url=agent.base_url,
                        )
                        api_messages = prepare_api_messages(agent.messages)
                        if use_cache:
                            api_messages = apply_cache_control(
                                api_messages,
                                cache_ttl=getattr(agent, "_cache_ttl", "5m"),
                                native_anthropic=native_layout,
                            )
                        continue

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
                    _emit_hook(
                        agent, "on_context_compress",
                        before_tokens=approx_tokens,
                        after_tokens=estimate_messages_tokens(agent._messages),
                        trigger="error_recovery",
                    )
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
            # ── MoA：把 advisor fan-out 用量折进本轮记账（Phase 4.1）──────
            # 参考模型跑在独立 slot 上，其 token 不在聚合器响应里；不折叠则
            # advisor 花费（常是 MoA 轮的大头）对用量追踪完全不可见。仅折叠进
            # 会话用量追踪器，**不**折叠进压缩器——advisor 是旁路调用，其 token
            # 不进入对话上下文，不应膨胀压缩器对对话规模的估计。
            _moa_client = agent.client
            if hasattr(_moa_client, "consume_reference_usage"):
                _ref_usage, _ = _moa_client.consume_reference_usage()
                if _ref_usage and _ref_usage.get("total_tokens"):
                    tracker.update(_ref_usage)
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
            elif getattr(response, "_stream_gate_closed", False):
                # 门控曾检测到 think/工具标签开头而暂扣推送，但解析不出
                # 任何工具调用 — 记录被暂扣的原始内容，用于定位新格式/畸形输出
                logger.warning(
                    "门控暂扣内容未解析出工具调用 (content_len=%d)，原始内容前 800 字符: %r",
                    len(message.content or ""), (message.content or "")[:800],
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
                    tool_calls=tool_calls_in_turn,
                )

            # 继续循环（让 LLM 看到工具结果后再次推理）
            continue

        else:
            # 文本回复 — 先区分“模型说完了”与“输出被截断”
            content = message.content or ""
            finish = getattr(choice, "finish_reason", "stop") or "stop"
            logger.info(
                "模型回复结束: finish_reason=%s content_len=%d gate_closed=%s",
                finish, len(content), getattr(response, "_stream_gate_closed", False),
            )

            # ── 截断自愈 ─────────────────────────────────────
            # 输出撞 max_tokens 上限（思考模型常见：推理消耗配额后可见文本与
            # 工具调用被截断），不得当作最终回答结束轮次：保存干净文本、
            # 注入续写提示后再推一轮，让模型从断点继续发出完整工具调用。
            if (
                finish == "length"
                and truncation_continuations < _MAX_TRUNCATION_CONTINUATIONS
                and budget.remaining > 0
            ):
                truncation_continuations += 1
                logger.warning(
                    "输出被截断 (finish_reason=length)，自动续写第 %d 轮",
                    truncation_continuations,
                )
                agent.add_message("assistant", _clean_for_display(content) or content)
                agent.add_message(
                    "user",
                    "[系统提示] 你上一轮输出因长度上限被截断，其中的工具调用未执行。"
                    "请直接从中断处继续任务：重新发出完整的工具调用，"
                    "不要重复已写过的说明文字。",
                )
                continue

            # ── 空结论自愈 ─────────────────────────────────────
            # 思考模型可能把输出配额全耗在推理上，最终 message.content 为空：
            # 绝不能当作"最终回答"静默收尾（用户会看到任务跑完却没有任何结论）。
            # 先注入收尾提示再推一轮让模型补结论；提示用尽仍为空则合成兜底总结。
            if not content.strip():
                if empty_final_nudges < _MAX_EMPTY_FINAL_NUDGES and budget.remaining > 0:
                    empty_final_nudges += 1
                    logger.warning(
                        "模型最终回复为空，注入收尾提示第 %d 轮", empty_final_nudges,
                    )
                    agent.add_message(
                        "user",
                        "[系统提示] 你上一轮没有输出任何内容。请基于前面工具调用"
                        "已得到的结果，直接给出本任务的最终结论或总结；"
                        "除非确有必要，不要再调用工具。",
                    )
                    continue
                fallback = _synthesize_fallback_summary(tool_calls_in_turn)
                logger.warning("模型最终回复为空且收尾提示用尽，返回兜底总结")
                agent.add_message("assistant", fallback)
                _emit_hook(
                    agent, "after_conversation",
                    response=fallback, iterations=api_call_count,
                    tool_calls=tool_calls_in_turn,
                )
                return ConversationResult(
                    response=fallback,
                    messages=agent.messages,
                    usage=_extract_usage(response),
                    iterations=api_call_count,
                    tool_calls=tool_calls_in_turn,
                )

            agent.add_message("assistant", content)

            # ── 后台进程通知回灌（Phase 4.7）──────────────────
            # 模型给出了最终文本回答，但返回前先检查是否有后台进程/
            # 异步委托完成事件待处理。有则注入为 user 消息并再推一轮，让
            # Agent 据此决定下一步（例如服务已就绪、后台构建完成）。
            if budget.remaining > 0 and _inject_process_notifications(agent) > 0:
                continue

            # 流式门控回补：若本轮文本因检测到 think/工具标签而被门控暂扣，
            # 现在统一清理后一次性推送（最终回答）。
            # 只推暂扣的后缀：实时已推送的前缀不得重发，否则终端出现重复文本。
            if stream_callback and getattr(response, "_stream_gate_closed", False):
                cleaned = _clean_for_display(content)
                pushed = getattr(response, "_stream_pushed_text", "")
                if pushed and cleaned.startswith(pushed):
                    cleaned = cleaned[len(pushed):]
                if cleaned:
                    try:
                        stream_callback(cleaned)
                    except Exception:
                        logger.debug("流式回补推送失败", exc_info=True)

            # ── MoA：轮次边界刷写完整追踪（opt-in，Phase 4.1）──────────
            # create() 在缓存 MISS 时暂存 pending trace；此处把实时 session_id 与
            # 解析出的聚合器 acting 文本（content）拼入并刷到追踪文件。流式路径
            # 靠 fallback 提供输出，非流式已内联捕获。追踪关闭时是廉价 no-op。
            _moa_client = agent.client
            if hasattr(_moa_client, "consume_and_save_trace"):
                _moa_client.consume_and_save_trace(
                    session_id=agent.session_id,
                    aggregator_output_fallback=content,
                )

            _emit_hook(
                agent, "after_conversation",
                response=content, iterations=api_call_count,
                tool_calls=tool_calls_in_turn,
            )
            return ConversationResult(
                response=content,
                messages=agent.messages,
                usage=_extract_usage(response),
                iterations=api_call_count,
                tool_calls=tool_calls_in_turn,
            )

    # 达到最大迭代
    _emit_hook(
        agent, "after_conversation",
        response="[max_iterations]", iterations=api_call_count,
        tool_calls=tool_calls_in_turn,
    )
    return ConversationResult(
        response=f"[达到最大迭代次数 {agent.max_iterations}]",
        messages=agent.messages,
        iterations=api_call_count,
        tool_calls=tool_calls_in_turn,  # 传递工具调用列表
    )


# =========================================================================
# LLM 调用
# =========================================================================

def _inject_process_notifications(agent) -> int:
    """把后台进程/异步委托的完成通知回灌进对话（Phase 4.7）。

    返回注入的通知条数（0 表示无待处理事件，调用方据此决定是否结束循环）。
    通知按当前会话键路由（``get_current_session_key``），单轮上限读
    ``process.max_notifications_per_turn``，超出的事件重新入队留待下一轮。
    任何异常都吞掉并返回 0 —— 通知回灌是增强，绝不能让它打断主对话。
    """
    try:
        from spirit.process import process_registry
        from spirit.tools.approval import get_current_session_key
        from spirit.config import get_config_value

        session_key = get_current_session_key(default="") or ""
        events = process_registry.drain_notifications(session_key=session_key)
        if not events:
            return 0

        limit = max(1, int(get_config_value("process.max_notifications_per_turn", 5)))
        injected = 0
        for _evt, text in events[:limit]:
            if not text:
                continue
            agent.add_message("user", text)
            injected += 1
        # 超出单轮上限的事件重新入队，留待下一轮（避免静默丢弃）
        for evt, _text in events[limit:]:
            try:
                process_registry.completion_queue.put(evt)
            except Exception:
                pass
        return injected
    except Exception:
        logger.debug("后台进程通知回灌失败（非致命）", exc_info=True)
        return 0


def _get_fallback_chain(agent) -> List[Dict[str, Any]]:
    """惰性构建并缓存 fallback 链（Hermes ``_fallback_chain`` 语义）。

    链 = 配置里 ``llm.failover.providers`` 的优先级列表（复用 llm_pool 的
    声明式解析：inline 值优先，缺省从 ProviderProfile/环境变量补齐）；
    未启用 failover 时为空链（行为与单 provider 完全一致）。
    """
    chain = getattr(agent, "_fallback_chain", None)
    if chain is None:
        try:
            from spirit import llm_pool
            chain = llm_pool.load_providers() if llm_pool.failover_enabled() else []
        except Exception as exc:  # noqa: BLE001 - 链构建失败绝不阻断主循环
            logger.debug("fallback 链构建失败（非致命）: %s", exc)
            chain = []
        agent._fallback_chain = chain
    return chain


def _try_activate_fallback(agent, reason=None, stream_callback: Optional[Callable[[str], None]] = None) -> bool:
    """切换到 fallback 链中下一个可用的 provider（照搬 Hermes try_activate_fallback）。

    沿 ``_fallback_index`` 走链：跳过曾标记不可用的条目、字段不全的条目、
    以及与当前后端重复的条目（回退到刚失败的同一后端会循环失败）。
    激活本身**零网络请求**（Hermes 同款）：只换连接参数 + 重置惰性 client；
    切过去若仍失败，重试循环会再次分类并激活下一家，天然实现"哪个能用
    用哪个"。切换通知经 stream_callback 推给前端保证可见。
    """
    chain = _get_fallback_chain(agent)
    unavailable = getattr(agent, "_unavailable_fallback_keys", None)
    if unavailable is None:
        unavailable = set()
        agent._unavailable_fallback_keys = unavailable
    cooldowns = getattr(agent, "_cooldown_keys", None) or {}
    current_base = (getattr(agent, "base_url", "") or "").rstrip("/")

    while getattr(agent, "_fallback_index", 0) < len(chain):
        idx = getattr(agent, "_fallback_index", 0)
        agent._fallback_index = idx + 1
        fb = chain[idx]
        key = f"{fb.get('provider')}/{fb.get('model')}"
        if key in unavailable:
            logger.debug("fallback 跳过：%s 曾标记不可用", key)
            continue
        cd_until = cooldowns.get(key)
        if cd_until and cd_until > time.time():
            logger.debug("fallback 跳过：%s 额度冷却中", key)
            continue
        fb_provider = (fb.get("provider") or "").strip().lower()
        fb_model = (fb.get("model") or "").strip()
        fb_base = (fb.get("base_url") or "").rstrip("/")
        if not (fb_provider and fb_model and fb_base and fb.get("api_key")):
            logger.debug("fallback 跳过：%s 条目不完整", key)
            unavailable.add(key)
            continue
        if fb_base == current_base or (
            fb_provider == str(getattr(agent, "provider", "")).strip().lower()
            and fb_model == getattr(agent, "model", "")
        ):
            logger.debug("fallback 跳过：%s 与当前后端重复", key)
            continue

        # 首次切换前快照主运行时（turn 作用域：下一轮开头恢复，Hermes 语义）
        if getattr(agent, "_primary_runtime", None) is None:
            agent._primary_runtime = {
                "provider": agent.provider,
                "model": agent.model,
                "api_key": agent.api_key,
                "base_url": agent.base_url,
            }
        agent.provider = fb.get("provider")
        agent.model = fb_model
        agent.api_key = fb.get("api_key")
        agent.base_url = fb.get("base_url")
        agent._client = None  # 惰性 client 下次访问时按新参数重建
        agent._fallback_activated = True
        reason_txt = getattr(reason, "value", None) or str(reason or "") or "调用失败"
        notice = (
            f"\n⚠ {reason_txt} — 已切换 fallback 提供方 {fb.get('name')}"
            f"（模型 {fb_model}）\n"
        )
        logger.warning("[fallback] %s", notice.strip())
        if stream_callback:
            try:
                stream_callback(notice)
            except Exception:  # noqa: BLE001 - 通知失败不影响切换
                logger.debug("fallback 通知推送失败", exc_info=True)
        return True
    return False


def _mark_primary_quota_cooldown(agent) -> None:
    """把当前在跑的主 provider 标记为额度耗尽冷却。

    冷却作用于两处：``_restore_primary_runtime`` 冷却期内新一轮不恢复主
    provider；``_try_activate_fallback`` 跳过冷却期内与主家同名的链条目
    （防止链走回刚失败的必败后端）。
    """
    until = time.time() + _PRIMARY_QUOTA_COOLDOWN_SECONDS
    agent._primary_cooldown_until = until
    key = f"{getattr(agent, 'provider', '')}/{getattr(agent, 'model', '')}"
    cooldowns = getattr(agent, "_cooldown_keys", None)
    if cooldowns is None:
        cooldowns = {}
        agent._cooldown_keys = cooldowns
    cooldowns[key] = until
    logger.warning(
        "[fallback] 主 provider %s 额度耗尽，进入 %.0f 小时冷却（冷却期内直接用 fallback）",
        key, _PRIMARY_QUOTA_COOLDOWN_SECONDS / 3600.0,
    )


def _restore_primary_runtime(agent) -> None:
    """新一轮开始时恢复主 provider（Hermes：fallback 是 turn 作用域）。

    主家持续不可用期间，每轮先给它一次机会，失败则链会按消息重新激活；
    无论上一轮是否切换过都重置索引，防止索引搁浅后永久阻断后续
    fallback（Hermes #20465 语义）。曾标记不可用的条目集合跨轮保留。
    额度耗尽冷却期内例外：主家是"恢复期以天计的必败"，不恢复、沿用
    上一轮 fallback 运行时，避免每条消息白打一次必败调用 + 刷通知。
    """
    if (getattr(agent, "_primary_cooldown_until", 0) or 0) > time.time():
        agent._fallback_activated = True
        agent._fallback_index = 0
        logger.info(
            "[fallback] 主 provider 额度冷却中，本轮沿用 fallback provider=%s model=%s",
            agent.provider, agent.model,
        )
        return
    agent._primary_cooldown_until = 0
    primary = getattr(agent, "_primary_runtime", None)
    if getattr(agent, "_fallback_activated", False) and primary:
        agent.provider = primary["provider"]
        agent.model = primary["model"]
        agent.api_key = primary["api_key"]
        agent.base_url = primary["base_url"]
        agent._client = None
        logger.info(
            "[fallback] 新一轮开始，恢复主 provider=%s model=%s",
            primary["provider"], primary["model"],
        )
    agent._fallback_activated = False
    agent._fallback_index = 0


def _synthesize_fallback_summary(tool_calls) -> str:
    """模型最终回复为空且收尾提示用尽时的兜底总结（保证任务不以静默结束）。"""
    names = []
    for tc in (tool_calls or [])[-6:]:
        if isinstance(tc, dict):
            fn = tc.get("function") or {}
            name = fn.get("name") or tc.get("name") or ""
        else:
            name = getattr(tc, "name", "") or ""
        if name:
            names.append(str(name))
    head = f"[本轮未产出最终结论] 共执行 {len(tool_calls or [])} 次工具调用"
    if names:
        head += f"（最近：{', '.join(names)}）"
    return head + "。模型未给出总结性结论，请查看上述工具结果，或重新提问以获取结论。"


def _clean_for_display(text: str) -> str:
    """清理 think/工具调用标签后的可见文本（延迟导入避免循环依赖）。"""
    try:
        from spirit.cli.main_enhanced import _clean_think_tags
        return _clean_think_tags(text)
    except Exception:
        return text


def _emit_hook(agent, event: str, **kwargs) -> None:
    """触发钩子事件（钩子系统未启用时静默跳过）。

    HookManager.emit 内部已对每个 handler 做异常隔离，
    钩子失败不会影响主循环；这里只兼容 hook_manager 不存在的情况。
    """
    hook_manager = getattr(agent, "hook_manager", None)
    if hook_manager is not None:
        hook_manager.emit(event, **kwargs)


def _call_llm(
    agent,
    api_messages: List[dict],
    tools: List[dict],
    stream_callback: Optional[Callable[[str], None]] = None,
):
    """调用 LLM API。

    集成 prompt caching: 当启用 cache_control 时，消息列表已经
    在上层被注入了 cache_control 断点。这里只负责发送请求。

    流式模式（stream_callback 非空）：
    - 实时推送文本增量，但带门控：一旦累积文本出现 think/工具标签开头，
      停止实时推送（避免原始 JSON 泄露），剩余文本在轮次结束后回补。
    - 累积原生 tool_calls 增量，组装为与非流式一致的响应结构。
    """
    request_kwargs = {
        "model": agent.model,
        "messages": api_messages,
    }

    # 显式下发输出上限：思考模型的推理计入输出配额，依赖服务端默认上限会
    # 导致长推理截断可见文本与工具调用（见 _DEFAULT_MAX_TOKENS_BY_PROVIDER 注释）
    max_tokens = getattr(agent, "max_tokens", 0) or get_config_value("llm.max_tokens", 0) or 0
    if max_tokens <= 0:
        max_tokens = _DEFAULT_MAX_TOKENS_BY_PROVIDER.get(
            getattr(agent, "provider", ""), _DEFAULT_MAX_TOKENS,
        )
    request_kwargs["max_tokens"] = max_tokens

    if tools:
        request_kwargs["tools"] = tools

    # 透传 extra_params（如 provider 特定参数）
    extra = getattr(agent, "_extra_api_params", None)
    if extra:
        request_kwargs.update(extra)

    if not stream_callback:
        return agent.client.chat.completions.create(**request_kwargs)

    # ── 流式调用 + 门控 ───────────────────────────────────
    request_kwargs["stream"] = True
    stream = agent.client.chat.completions.create(**request_kwargs)

    # ── 流看门狗：首字节 TTFB + chunk 间隔停滞检测 ────────────
    # 半开连接/停滞流会让下面的 chunk 循环永久阻塞（socket 读无超时）。
    # 看门狗超限时从另一线程 close 流解锁（Python 打断不了 C 层 socket
    # read，只能关连接），抛 timeout 类异常进外层重试循环重连。
    ttfb_lim = float(get_config_value("timeouts.stream_ttfb_seconds", 90.0))
    stale_lim = float(get_config_value("timeouts.stream_chunk_stale_seconds", 120.0))
    wd: Dict[str, Any] = {
        "last": time.monotonic(), "first": False, "kill": "",
        "stop": threading.Event(),
    }

    def _watchdog() -> None:
        while not wd["stop"].wait(5.0):
            gap = time.monotonic() - wd["last"]
            if not wd["first"]:
                if ttfb_lim > 0 and gap > ttfb_lim:
                    wd["kill"] = (
                        f"流看门狗 timeout：{gap:.0f}s 无首字节"
                        f"（限 {ttfb_lim:.0f}s）"
                    )
                    break
            elif stale_lim > 0 and gap > stale_lim:
                wd["kill"] = (
                    f"流看门狗 timeout：{gap:.0f}s 无新 chunk"
                    f"（限 {stale_lim:.0f}s）"
                )
                break
        if wd["kill"]:
            try:
                stream.close()
            except Exception:
                logger.debug("看门狗关流失败", exc_info=True)

    threading.Thread(target=_watchdog, daemon=True).start()

    def _watched_chunks():
        try:
            for chunk in stream:
                wd["last"] = time.monotonic()
                wd["first"] = True
                yield chunk
        except Exception as exc:
            wd["stop"].set()
            if wd["kill"]:
                raise StreamStaleTimeoutError(wd["kill"]) from exc
            raise
        finally:
            wd["stop"].set()

    content_parts: List[str] = []
    pushed_parts: List[str] = []
    tc_buffers: Dict[int, Dict[str, str]] = {}
    finish_reason = None
    usage = None
    gate_open = True

    for chunk in _watched_chunks():
        u = getattr(chunk, "usage", None)
        if u:
            usage = u
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        if choice.finish_reason:
            finish_reason = choice.finish_reason
        delta = choice.delta
        if delta is None:
            continue

        piece = getattr(delta, "content", None) or ""
        if piece:
            content_parts.append(piece)
            if gate_open:
                if _STREAM_OPENER_RE.search("".join(content_parts)):
                    gate_open = False  # 开始暂扣，不再实时推送
                else:
                    try:
                        stream_callback(piece)
                        pushed_parts.append(piece)
                    except Exception:
                        logger.debug("流式增量推送失败", exc_info=True)

        # 原生 tool_calls 增量累积
        if getattr(delta, "tool_calls", None):
            for tc in delta.tool_calls:
                buf = tc_buffers.setdefault(
                    tc.index, {"id": "", "name": "", "arguments": ""},
                )
                if tc.id:
                    buf["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        buf["name"] += tc.function.name
                    if tc.function.arguments:
                        buf["arguments"] += tc.function.arguments

    if wd["kill"]:
        # 流被 close 静默结束（未抛异常）：同样抛超时进重试循环
        raise StreamStaleTimeoutError(wd["kill"])

    # 组装为与非流式一致的响应结构
    tool_calls_objs = [
        SimpleNamespace(
            id=buf["id"] or f"call_{idx}",
            function=SimpleNamespace(
                name=buf["name"], arguments=buf["arguments"] or "{}",
            ),
        )
        for idx, buf in sorted(tc_buffers.items())
        if buf["name"]
    ]
    message = SimpleNamespace(
        content="".join(content_parts),
        tool_calls=tool_calls_objs or None,
    )
    choice_obj = SimpleNamespace(
        message=message, finish_reason=finish_reason or "stop",
    )
    response = SimpleNamespace(choices=[choice_obj], usage=usage)
    response._stream_gate_closed = not gate_open
    response._stream_pushed_text = "".join(pushed_parts)
    return response


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
