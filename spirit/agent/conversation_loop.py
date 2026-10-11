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
from spirit.agent.tool_guardrails import (
    ToolCallGuardrailController,
    ToolGuardrailDecision,
)
from spirit.agent.hitl_channel import DecisionKind, has_channel, request_decision
from spirit.agent.tool_executor import (
    execute_tool_calls_concurrent,
    execute_tool_calls_sequential,
    prepare_api_messages,
    serialize_tool_call,
)
from spirit.agent.context_compressor import estimate_messages_tokens
from spirit.agent.prompt_caching import apply_cache_control, should_use_prompt_caching
from spirit.agent.provider_switch_protection import (
    ProviderRoute,
    apply_provider_labels,
    protect_provider_switch,
    snapshot_agent_route,
)
from spirit.agent.accuracy_attribution import (
    should_activate_moa,
    should_self_correct,
    build_self_correction_prompt,
)
from spirit.agent.text_tool_parser import parse_text_tool_calls
from spirit.agent.turn_truncation import (
    build_text_continuation_prompt,
    detect_truncation,
)
from spirit.config import get_config_value
from spirit.agent.progress_notifier import (
    ProgressPhase,
    notify_progress,
    set_current_session_id,
)

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

    # ── C-P1.1：进度推送（任务开始） ─────────────────
    # 钉住当前 session_id（所有后续进度事件会带上），推送 task_start。
    # 此处必须在主循环前调用，否则后续 thinking/tool_call 会因 session 为空被丢失。
    set_current_session_id(agent.session_id)
    _task_summary = (
        user_message[:60] + "..." if len(user_message) > 60 else user_message
    )
    notify_progress(
        ProgressPhase.TASK_START,
        summary=_task_summary,
    )

    # ── v0.4.0 turn_facade wiring ──────────────────────────
    # 拿到 agent 已装配的 turn_facade（如已接 storage_event_bridge），
    # 通知它一个 turn 已经开始。这一跳是 async 的，失败不主路径中断。
    _turn_facade = getattr(agent, "turn_facade", None)
    if _turn_facade is not None and hasattr(_turn_facade, "persist_turn_start"):
        try:
            _turn_facade.persist_turn_start(
                session_id=agent.session_id,
                user_message=user_message,
            )
        except Exception:  # noqa: BLE001
            logger.debug("turn_facade.persist_turn_start 失败 (非致命)", exc_info=True)

    # ── v0.4.0 storage_lifecycle wiring ─────────────────────
    # turn 层面 touch_session（每轮刷新 last_touched_at）。
    _storage_lifecycle = getattr(agent, "storage_lifecycle", None)
    if _storage_lifecycle is not None and hasattr(_storage_lifecycle, "on_turn_start"):
        try:
            _storage_lifecycle.on_turn_start(session_id=agent.session_id)
        except Exception:  # noqa: BLE001
            logger.debug("storage_lifecycle.on_turn_start 失败 (非致命)", exc_info=True)

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

    # C-P1.2：本 turn 内 provider 切换 HITL 只问一次（连锁 failover 不反复弹窗）
    agent._provider_switch_hitl_asked = False

    # 上下文压缩器
    compressor = getattr(agent, "_context_compressor", None)

    # 计数器
    api_call_count = 0
    retry_count = 0
    from spirit.config import get_config_value
    max_retries = get_config_value("agent.max_retries", 3)
    compression_attempts = 0

    # ── R-P2.2：启动 turn 活性 watchdog ───────────────────────
    # 在主循环启动前安装； turn_active=True 让 watchdog 采样，LLM/工具触摸后
    # 推进 generation；超 600s 无活动 → _turn_liveness_aborted=True →
    # 下轮主循环 break。try/finally 保证 turn 退出/异常时 stop() 总会调用。
    from spirit.agent.turn_liveness import resolve_turn_liveness_settings, TurnLivenessWatchdog
    _tl_timeout, _tl_poll = resolve_turn_liveness_settings(agent.config if hasattr(agent, "config") else None)
    _tl_watchdog = TurnLivenessWatchdog(agent, timeout_s=_tl_timeout, poll_s=_tl_poll)
    _tl_watchdog_active = False
    if _tl_watchdog.enabled:
        agent._turn_active = True
        agent._turn_liveness_aborted = False
        # 防御式：tests 中的 FakeAgent 可能没 _activity_lock
        _act_lock = getattr(agent, "_activity_lock", None)
        if _act_lock is not None:
            with _act_lock:
                agent._last_activity_ts = None
                agent._turn_liveness_activity_generation = 0
        else:
            agent._last_activity_ts = None
            agent._turn_liveness_activity_generation = 0
        _tl_watchdog.start()
        _tl_watchdog_active = True

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
            # C-P1.1：进度推送（任务中断）
            notify_progress(
                ProgressPhase.TASK_ABORTED,
                iterations=api_call_count,
                summary="用户中断",
                extra={"reason": "user_interrupt"},
            )
            if _tl_watchdog_active:
                _tl_watchdog.stop()
                agent._turn_active = False
            return ConversationResult(
                response="[已中断]",
                messages=agent.messages,
                iterations=api_call_count,
                tool_calls=tool_calls_in_turn,
            )

        # R-P2.2: watchdog abort 检查
        if _tl_watchdog_active and getattr(agent, "_turn_liveness_aborted", False):
            logger.warning("turn 被 liveness watchdog 强制结束 (idle 超时)")
            notify_progress(
                ProgressPhase.TASK_ABORTED,
                iterations=api_call_count,
                summary="liveness watchdog timeout",
                extra={"reason": "turn_liveness_abort"},
            )
            _tl_watchdog.stop()
            agent._turn_active = False
            return ConversationResult(
                response="[turn 被活性 watchdog 终止]",
                messages=agent.messages,
                iterations=api_call_count,
                tool_calls=tool_calls_in_turn,
            )

        # 消耗迭代
        if not budget.consume():
            logger.info("迭代预算耗尽 (%d/%d)", budget.used, budget.max_total)
            # C-P1.1：进度推送（迭代预算耗尽）
            notify_progress(
                ProgressPhase.TASK_COMPLETE,
                iterations=api_call_count,
                summary=f"达到最大迭代次数 {agent.max_iterations}",
                extra={"reason": "max_iterations"},
            )
            return ConversationResult(
                response=f"[达到最大迭代次数 {agent.max_iterations}]",
                messages=agent.messages,
                iterations=api_call_count,
                tool_calls=tool_calls_in_turn,
            )

        api_call_count += 1
        agent._api_call_count = api_call_count

        # ── M-P1.0：自我纠错检查 ───────────────────────────
        # 检测上一轮 assistant 回复 / 当前 user message 是否触发自我纠错
        # （上一轮含 <error> / failed / 用户追问“哪里不对” / 工具 status=error）。
        # 触发时注入一条审视提示，让 LLM 重新检查上一轮答案，不是默默重复。
        if len(agent.messages) >= 2:
            previous_assistant = ""
            for _m in reversed(agent.messages):
                if _m.get("role") == "assistant":
                    previous_assistant = _m.get("content", "") or ""
                    break
            current_user = ""
            for _m in reversed(agent.messages):
                if _m.get("role") == "user":
                    current_user = _m.get("content", "") or ""
                    break
            if previous_assistant and current_user:
                try:
                    if should_self_correct(
                        previous_response=previous_assistant,
                        user_message=current_user,
                    ):
                        correction_prompt = build_self_correction_prompt(
                            reason="上一轮结果包含错误标识 或 用户追问'哪里不对'"
                        )
                        agent.add_message("user", correction_prompt)
                        logger.info(
                            "[M-P1.0] 自我纠错触发，注入审视提示（上一轮长度 %d 字符）",
                            len(previous_assistant),
                        )
                except Exception as exc:  # noqa: BLE001 - 纠错检测失败不阻断主循环
                    logger.debug("[M-P1.0] 自我纠错检测异常（非致命）: %s", exc)

        # ── 上下文压缩预检 ──────────────────────────────────────
        if compressor and len(agent.messages) > 5:
            approx_tokens = estimate_messages_tokens(agent.messages)
            if compressor.should_compress(approx_tokens) and compression_attempts < 3:
                compression_attempts += 1
                logger.info(
                    "预压缩 #%d: ~%d tokens 超过阈值",
                    compression_attempts, approx_tokens,
                )
                # ── v0.4.0 compression lock ───────────────────────────
                # 同一 session 同一时间只能有一个 compressor 在跑。
                # claim 失败跳过本次压缩（与 Hermes 语义一致）。
                _db = getattr(agent, "_session_db", None)
                _lock_acquired = False
                if _db is not None and hasattr(_db, "claim_compression_lock"):
                    try:
                        _lock_acquired = bool(_db.claim_compression_lock(
                            session_id=agent.session_id,
                            holder=f"cl:{id(agent)}",
                            ttl=60.0,
                        ))
                    except Exception:  # noqa: BLE001
                        _lock_acquired = False
                if _db is None or _lock_acquired:
                    agent._messages = compressor.compress(agent.messages, approx_tokens)
                    _emit_hook(
                        agent, "on_context_compress",
                        before_tokens=approx_tokens,
                        after_tokens=estimate_messages_tokens(agent._messages),
                        trigger="preemptive",
                    )
                    # ── v0.4.0 compaction_observer hook ──────────────────
                    try:
                        _observer = getattr(agent, "compaction_observer", None)
                        if _observer is not None:
                            _observer.observe_compaction(
                                session_id=agent.session_id,
                                message_ids=[],
                                compressed_summary=compressor.last_summary or "",
                                strategy="preemptive",
                            )
                    except Exception:  # noqa: BLE001
                        logger.debug("compaction_observer.observe_compaction 失败 (非致命)",
                                     exc_info=True)
                    if _db is not None and hasattr(_db, "release_compression_lock"):
                        try:
                            _db.release_compression_lock(agent.session_id)
                        except Exception:  # noqa: BLE001
                            pass
                    budget.refund()  # 压缩不消耗迭代
                    continue
                else:
                    logger.info("压缩锁被占用；跳过本轮 (session=%s)",
                                agent.session_id[:8])

        # ── 构建 API 消息 ───────────────────────────────────────
        api_messages = _prepare_api_messages_safe(agent)

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
                # C-P1.1：进度推送（思考开始）
                notify_progress(
                    ProgressPhase.THINKING,
                    iterations=api_call_count,
                    summary=f"第 {api_call_count} 轮推理",
                )
                _llm_started = time.monotonic()
                # R-P2.2: 触摸活性时钟 (防御式: FakeAgent 可能没装)
                _touch = getattr(agent, "_touch_activity", None)
                if callable(_touch):
                    _touch("llm_call_start")
                response = _call_llm(agent, api_messages, tools, stream_callback)
                if callable(_touch):
                    _touch("llm_call_done")
                # C-P1.1：进度推送（思考结束 + 耗时）
                _llm_duration_ms = (time.monotonic() - _llm_started) * 1000.0
                notify_progress(
                    ProgressPhase.THINKING_END,
                    iterations=api_call_count,
                    extra={"duration_ms": _llm_duration_ms},
                )
                _emit_hook(
                    agent, "after_llm_call",
                    response=response,
                    duration_ms=_llm_duration_ms,
                )
                # ── 累计会话级 token 用量（状态栏 / 计费 / debug）──
                # 每次成功 API 调用都累加到 agent.session_* 字段；状态栏会取这些
                # 字段显示「用量是多少」。response.usage 缺失 / 字段异常不主路径中断
                # （只一条 debug 错误）。与 Hermes agent.session_*_tokens 语义对齐。
                try:
                    _usage = getattr(response, "usage", None)
                    if _usage:
                        _in_tok = int(getattr(_usage, "prompt_tokens", 0) or 0)
                        _out_tok = int(getattr(_usage, "completion_tokens", 0) or 0)
                        _total_tok = int(
                            getattr(_usage, "total_tokens", 0)
                            or (_in_tok + _out_tok)
                        )
                        agent.session_input_tokens += _in_tok
                        agent.session_output_tokens += _out_tok
                        agent.session_total_tokens += _total_tok
                        agent.session_api_calls += 1
                except Exception as _usage_exc:  # noqa: BLE001
                    logger.debug(
                        "[token-accumulate] 累加 usage 失败（非致命）: %s", _usage_exc
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
                        api_messages = _prepare_api_messages_safe(agent)
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
                    # ── v0.4.0 compaction_observer hook ────────────
                    try:
                        _observer = getattr(agent, "compaction_observer", None)
                        if _observer is not None:
                            _observer.observe_compaction(
                                session_id=agent.session_id,
                                message_ids=[],
                                compressed_summary=compressor.last_summary or "",
                                strategy="error_recovery",
                            )
                    except Exception:  # noqa: BLE001
                        logger.debug("compaction_observer.observe_compaction 失败 (非致命)",
                                     exc_info=True)
                    api_messages = _prepare_api_messages_safe(agent)
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

            # ── C-P1.1：进度推送（工具调用开始） ─────────────────
            # 工具名拼成 summary，让桌宠能看到"正在跑哪几个工具"。
            _tool_names = [
                tc.get("function", {}).get("name", "?")
                for tc in tool_calls_dicts
            ]
            _tool_summary = (
                "、".join(_tool_names[:3])
                + (f" 等 {len(_tool_names)} 个" if len(_tool_names) > 3 else "")
            )
            _tool_started = time.monotonic()
            notify_progress(
                ProgressPhase.TOOL_CALL,
                iterations=api_call_count,
                summary=_tool_summary,
                extra={"count": len(_tool_names)},
            )

            # v0.4.x：进度事件 — 工具参数生成阶段（TOOL_GEN_START）
            # 对齐 Hermes _fire_tool_gen_started；多工具时透传 summary，单工具
            # 时透传 _tool_names[0] 让 sink 可以区分多 vs 单。
            try:
                notify_progress(
                    ProgressPhase.TOOL_GEN_START,
                    iterations=api_call_count,
                    tool_name=(
                        _tool_names[0] if len(_tool_names) == 1 else None
                    ),
                    summary=_tool_summary,
                    extra={
                        "tool_name": (
                            _tool_names[0] if len(_tool_names) == 1 else None
                        ),
                        "summary": _tool_summary,
                        "count": len(_tool_names),
                    },
                )
            except Exception:
                logger.debug("TOOL_GEN_START 推送失败（非致命）", exc_info=True)

            # 执行工具（同步阻塞 — 按记忆 5d01f921：钩子 + execute 都是同步路径）
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

            # ── C-P1.1：进度推送（工具调用完成 + 耗时） ─────────
            _tool_duration_ms = (time.monotonic() - _tool_started) * 1000.0
            notify_progress(
                ProgressPhase.TOOL_RESULT,
                iterations=api_call_count,
                summary=_tool_summary,
                extra={
                    "duration_ms": _tool_duration_ms,
                    "count": len(_tool_names),
                },
            )

            # ── C-P1.2：工具连续失败 → HITL 决策面板（桌宠拍板）──────
            # 在 halt 检查之前消费 hitl 信号；用户选放弃会 force_halt，
            # 下方既有护栏停止分支随即接管。任何异常 fail-open 不阻断循环。
            try:
                _maybe_tool_failure_hitl(agent, guardrails)
            except Exception:  # noqa: BLE001
                logger.debug("[C-P1.2] tool_failure HITL 接线异常 → fail-open", exc_info=True)

            # 检查护栏是否要求停止
            if guardrails.halt_decision and guardrails.halt_decision.should_halt:
                logger.warning("工具护栏触发停止: %s", guardrails.halt_decision.message)
                # C-P1.1：进度推送（护栏触发）
                notify_progress(
                    ProgressPhase.TASK_ABORTED,
                    iterations=api_call_count,
                    summary=f"工具护栏触发: {guardrails.halt_decision.message}",
                    extra={"reason": "guardrail_halt"},
                )
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
            # R-P2.1 截断多策略检测（优先于 length 续传）
            # - abort：思考耗尽 / repetition / 窗口填满 → 直接收尾
            # - fallback：内容过滤 / refusal → 切换 fallback provider
            # - continue：走原 length 续传路径（双层防御）
            _truncation_verdict = detect_truncation(
                agent, choice, list(getattr(agent, "messages", []) or []),
                finish_reason=finish,
                continuation_attempts=truncation_continuations,
            )
            if _truncation_verdict.action == "abort" and _truncation_verdict.final_response:
                _tv_resp = _truncation_verdict.final_response
                agent.add_message("assistant", _clean_for_display(_tv_resp) or _tv_resp)
                _emit_hook(
                    agent, "after_conversation",
                    response=_tv_resp, iterations=api_call_count,
                    tool_calls=tool_calls_in_turn,
                )
                return _tv_resp
            if _truncation_verdict.action == "fallback":
                logger.warning(
                    "[R-P2.1] 内容过滤触发 fallback: reason=%s",
                    _truncation_verdict.reason,
                )
                # 交由 protect_provider_switch 接管（与现有 provider 切换
                # 路径一致；下一轮 API 调用会自动跳 fallback provider）。
                _reason_txt = _truncation_verdict.reason or "content_filter"
                try:
                    _from_route = ProviderRoute(
                        provider=getattr(agent, "provider", "primary"),
                        model=getattr(agent, "model", ""),
                    )
                    _fb_chain = getattr(agent, "_fallback_chain", None) or []
                    _to_route = (
                        ProviderRoute(
                            provider=str(_fb_chain[0].get("provider", "")),
                            model=str(_fb_chain[0].get("model", "")),
                        )
                        if _fb_chain else _from_route
                    )
                    _protected, _notice = protect_provider_switch(
                        list(getattr(agent, "_messages", []) or []),
                        route_from=_from_route, route_to=_to_route,
                        reason=_reason_txt,
                    )
                    if hasattr(agent, "_messages"):
                        agent._messages = _protected
                    if _notice is not None:
                        agent.add_message("user", _notice)
                except Exception as _fb_exc:  # noqa: BLE001 - 退化为纯续推
                    logger.warning(
                        "[R-P2.1] protect_provider_switch 失败，降级续推: %s",
                        _fb_exc,
                    )
                continue

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
                    build_text_continuation_prompt(),
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

            # ── M-P1.0：MoA 兜底检测（启发式信心判断）────────
            # Spirit 暂无模型内置 logprobs，这里用启发式指纹判断主 LLM 信心低：
            # 回复极短 / 含"我不确定"等低信心短语。MoA 在 Spirit 是 provider
            # 配置（all-or-nothing），无法动态开关，所以这里只记 log 推荐用户
            # 启用 MoA（provider="moa" 切换）。LLM 端仍能感知到原始回复。
            try:
                if should_activate_moa(
                    response_text=content,
                    response_length=len(content),
                ):
                    logger.info(
                        "[M-P1.0] 主 LLM 信心低（回复长度 %d），建议下一轮启用 MoA "
                        "(provider='moa') 或检查任务复杂度（当前 provider=%s）",
                        len(content), getattr(agent, "provider", ""),
                    )
                    _emit_hook(
                        agent, "on_moa_recommended",
                        response_length=len(content),
                        current_provider=getattr(agent, "provider", ""),
                    )
            except Exception as exc:  # noqa: BLE001 - MoA 检测不阻断主流程
                logger.debug("[M-P1.0] MoA 兑底检测异常（非致命）: %s", exc)

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

            # C-P1.1：进度推送（任务完成）
            notify_progress(
                ProgressPhase.TASK_COMPLETE,
                iterations=api_call_count,
                summary="完成",
                extra={
                    "iterations": api_call_count,
                    "tool_calls": len(tool_calls_in_turn),
                },
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
    # C-P1.1：进度推送（主循环自然结束于最大迭代）
    notify_progress(
        ProgressPhase.TASK_COMPLETE,
        iterations=api_call_count,
        summary=f"达到最大迭代次数 {agent.max_iterations}",
        extra={"reason": "max_iterations"},
    )
    _emit_hook(
        agent, "after_conversation",
        response="[max_iterations]", iterations=api_call_count,
        tool_calls=tool_calls_in_turn,
    )
    # R-P2.2: turn 出口关闭 watchdog (仅主函数体最后 return 前)
    if _tl_watchdog_active:
        _tl_watchdog.stop()
        agent._turn_active = False
    return ConversationResult(
        response=f"[达到最大迭代次数 {agent.max_iterations}]",
        messages=agent.messages,
        iterations=api_call_count,
        tool_calls=tool_calls_in_turn,  # 传递工具调用列表
    )


# =========================================================================
# LLM 调用
# =========================================================================
def _maybe_tool_failure_hitl(agent, guardrails) -> None:
    """C-P1.2：同一工具本轮连续失败达阈值 → 经全局 HITL 通道请用户拍板。

    消费（并清除）``guardrails.hitl_decision`` 一次性信号：

    - 无全局通道（CLI / 桌宠未连）→ fail-open，直接放行让主循环自主继续；
    - 超时 / 通道异常 / 用户选“重试 / 换方法” → 把用户指示回灌为一条
      user 消息，LLM 下一轮据此调整，循环继续；
    - 用户选“放弃” → ``force_halt`` 置位一个 halt 决策，交由主循环既有的
      护栏停止分支优雅收尾（不另造一套中断路径）。
    任何异常都不得阻断主循环（fail-open 铁律）。
    """
    decision = guardrails.hitl_decision
    if decision is None:
        return
    guardrails.clear_hitl_decision()

    if not has_channel():
        return  # 桌宠未连 / CLI 场景 → 自主继续

    recent_error = ""
    try:
        for msg in reversed(getattr(agent, "_messages", []) or []):
            if isinstance(msg, dict) and msg.get("role") == "tool":
                recent_error = str(msg.get("content") or "")[-400:]
                break
    except Exception:  # noqa: BLE001 - 背景提取失败不影响询问
        recent_error = ""

    try:
        reply = request_decision(
            DecisionKind.TOOL_FAILURE,
            f"{decision.tool_name} 已连续失败 {decision.count} 次，要怎么处理？",
            options=["继续重试（我已调整思路）", "换一种方法", "放弃这个任务"],
            context=(
                decision.message
                + (f"\n最近工具返回：\n{recent_error}" if recent_error else "")
            ),
            is_fork=True,
            task_id=str(getattr(agent, "session_id", "") or ""),
        )
    except Exception:  # noqa: BLE001 - 通道异常 fail-open
        logger.debug("[C-P1.2] tool_failure 询问异常 → fail-open", exc_info=True)
        return

    if reply is None:
        return  # 超时 / 无响应 → 自主继续

    if "放弃" in reply:
        guardrails.force_halt(
            ToolGuardrailDecision(
                action="halt",
                code="user_abandon_after_hitl",
                message=(
                    f"用户在 HITL 面板选择放弃：{decision.tool_name} "
                    f"本轮连续失败 {decision.count} 次。"
                ),
                tool_name=decision.tool_name,
                count=decision.count,
            )
        )
        return

    # 重试 / 换方法：把用户拍板指示回灌，LLM 下一轮据此调整策略
    try:
        agent.add_message("user", f"[用户介入指示] {reply}")
    except Exception:  # noqa: BLE001
        logger.debug("[C-P1.2] 回灌用户指示失败（非致命）", exc_info=True)


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


def _prepare_api_messages_safe(agent):
    """prepare_api_messages 包装版：R-P1.4 provider 标签剥离。

    R-P1.4 在切换 provider 后会给 messages 打 ``_provider / _model`` 等
    元数据。这些元数据是 Spirit 内部追踪用的，**不应**进 LLM API payload。
    本函数在 prepare_api_messages 前先 ``apply_provider_labels`` 剥离
    （同时补齐缺失的标签——保证每条消息都明确归属），再交给 prepare_api_messages
    转为 OpenAI 格式。

    切换提示（system 消息）的 ``role + content`` 不被剥离，LLM 能看到。
    """
    current_route = snapshot_agent_route(agent)
    labeled = apply_provider_labels(agent.messages, current_route)
    return prepare_api_messages(labeled)


def _maybe_provider_switch_hitl(agent, fb: Dict[str, Any], reason_txt: str) -> bool:
    """C-P1.2：跨 provider 自动 failover 前，请用户拍板。

    返回 True = 继续切换到 *fb*；False = 用户要求留在当前模型重试。

    约束（fail-open 铁律 + 不破坏自动 failover）：
    - 无全局通道（CLI / 桌宠未连）→ True（照原逻辑自动切）；
    - 超时 / 异常 → True（自动切，绝不因问不到人而卡死在坏 provider 上）；
    - 每轮最多问一次（``agent._provider_switch_hitl_asked``），避免连锁
      failover 时反复弹窗；
    - 询问超时较短（30s），provider 已故障时不让用户久等。
    """
    if getattr(agent, "_provider_switch_hitl_asked", False):
        return True
    agent._provider_switch_hitl_asked = True
    if not has_channel():
        return True
    try:
        cur = f"{getattr(agent, 'provider', '')}/{getattr(agent, 'model', '')}"
        nxt = f"{fb.get('name') or fb.get('provider')}/{fb.get('model')}"
        reply = request_decision(
            DecisionKind.PROVIDER_SWITCH,
            f"当前模型 {cur} 调用失败（{reason_txt}），要切换到备用模型 {nxt} 吗？",
            options=[f"切到备用模型 {nxt}", "留在当前模型重试"],
            context="自动故障转移会在多个备用模型间依次尝试；选“留在当前”则不切换。",
            is_fork=True,
            timeout=30.0,
            task_id=str(getattr(agent, "session_id", "") or ""),
        )
    except Exception:  # noqa: BLE001 - 任何异常 fail-open 自动切
        logger.debug("[C-P1.2] provider_switch 询问异常 → 自动切换", exc_info=True)
        return True
    if reply is None:
        return True  # 超时 → 自动切
    # 仅当用户明确选择“留在当前”才中止切换；其余（含切备用）一律继续
    return "留在当前" not in reply


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

        # ── C-P1.2：切换前经 HITL 面板请用户拍板（无通道/超时则自动切）──
        _reason_txt_pre = getattr(reason, "value", None) or str(reason or "") or "调用失败"
        if not _maybe_provider_switch_hitl(agent, fb, _reason_txt_pre):
            # 用户选择留在当前模型重试：不推进切换，回退已自增的链索引
            agent._fallback_index = max(0, idx)
            logger.info("[fallback] 用户在 HITL 面板选择留在当前 provider，中止切换")
            return False

        # 首次切换前快照主运行时（turn 作用域：下一轮开头恢复，Hermes 语义）
        if getattr(agent, "_primary_runtime", None) is None:
            agent._primary_runtime = {
                "provider": agent.provider,
                "model": agent.model,
                "api_key": agent.api_key,
                "base_url": agent.base_url,
            }
        # 快照“即将离开”的路由（用于切换提示文案 + provider 标签）
        route_from = ProviderRoute(
            provider=agent.provider,
            model=agent.model,
            base_url=agent.base_url,
            api_key=agent.api_key,
        )
        agent.provider = fb.get("provider")
        agent.model = fb_model
        agent.api_key = fb.get("api_key")
        agent.base_url = fb.get("base_url")
        agent._client = None  # 惰性 client 下次访问时按新参数重建
        agent._fallback_activated = True
        route_to = ProviderRoute(
            provider=agent.provider,
            model=agent.model,
            base_url=agent.base_url,
            api_key=agent.api_key,
        )
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

        # ── R-P1.4：provider 切换保护 ──
        # 给 agent._messages 里的消息打 route_to 标签 + 注入一条 system
        # “刚切换了 provider，请基于现有工具结果继续”提示。下一轮
        # prepare_api_messages 前会用 apply_provider_labels 剥掉元数据
        # （不污染 API payload），但切换提示作为 system 消息被保留。
        if hasattr(agent, "_messages") and agent._messages is not None:
            try:
                protected_messages, switch_notice = protect_provider_switch(
                    list(agent._messages),
                    route_from=route_from,
                    route_to=route_to,
                    reason=reason_txt,
                )
                agent._messages = protected_messages
                logger.info(
                    "[fallback] 已给 %d 条消息打 provider 标签 + 注入切换提示（from %s → to %s）",
                    len(protected_messages),
                    route_from.label(),
                    route_to.label(),
                )
            except Exception as exc:  # noqa: BLE001 - 保护失败不影响切换主路径
                logger.warning("[fallback] provider 切换保护注入失败（非致命）: %s", exc)

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
    reasoning_parts: List[str] = []
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

        # ── reasoning_content 增量（Hermes-style 实时思考）────────────
        # 上游 LLM 走原生 reasoning_content 字段时（DeepSeek R1 / Qwen3 thinking /
        # GLM-5 / MiniMax-M3 / 火山方舟 ark-code-latest 等），每个 delta 会带一段推理 token。
        # 推到 progress_notifier 全局 sink 总线：CLI / 桌宠各自注册 sink 渲染（不写入 SQLite，
        # 不混入 messages.content，遵循 spirit 不持久化中间思考过程的策略）。
        reasoning_piece = getattr(delta, "reasoning_content", None)
        if not reasoning_piece and hasattr(delta, "model_extra"):
            _de = getattr(delta, "model_extra", None) or {}
            if isinstance(_de, dict):
                reasoning_piece = _de.get("reasoning_content")
        if reasoning_piece:
            reasoning_parts.append(reasoning_piece)
            try:
                notify_progress(
                    ProgressPhase.REASONING_DELTA,
                    extra={"text": reasoning_piece},
                )
            except Exception:
                logger.debug("REASONING_DELTA 推送失败（非致命）", exc_info=True)

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
                    # v0.4.x：进度事件 — 最终回答流式（STREAM_TEXT_DELTA）
                    # 与 stream_callback 同一位置推送，确保 sink 看到的与终端一致
                    # （gate 关闭 / think / tool_call 块开始时不推，避免泄露给桌宠）
                    try:
                        notify_progress(
                            ProgressPhase.STREAM_TEXT_DELTA,
                            extra={"text": piece},
                        )
                    except Exception:
                        logger.debug("STREAM_TEXT_DELTA 推送失败（非致命）", exc_info=True)

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

    # ── 推理流结束信号（让 CLI / 桌宠 flush 推理尾巴）────────────────
    # 只在本轮有 reasoning 时 emit；普通模型不会触发。
    if reasoning_parts:
        try:
            notify_progress(ProgressPhase.REASONING_END)
        except Exception:
            logger.debug("REASONING_END 推送失败（非致命）", exc_info=True)

    # v0.4.x：流式结束信号 — 最终回答（STREAM_TEXT_END）
    # 与 REASONING_END 平行；有内容 delta 才发（避免空响应时开空 box）。
    if content_parts:
        try:
            notify_progress(
                ProgressPhase.STREAM_TEXT_END,
                extra={"text": "".join(content_parts)},
            )
        except Exception:
            logger.debug("STREAM_TEXT_END 推送失败（非致命）", exc_info=True)

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
