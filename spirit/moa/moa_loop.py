"""Mixture-of-Agents 运行时 — ``/moa`` 一次性轮次与 ``provider==moa`` 持久模式。

对标 Hermes ``agent/moa_loop.py``。``/moa`` 不是一个模型工具：它把某个用户轮标记为
MoA 启用；正常的 Spirit agent 循环仍然拥有工具调用与轮次终止权，而本模块在每次模型
迭代前收集参考模型（advisory）的上下文。

核心机制：:class:`MoAClient` 是一个 OpenAI-chat 兼容的 facade，替换 ``agent.client``。
它的 ``.chat.completions.create(**kwargs)`` 拦截调用 → 并行跑参考模型 → 把合成的
guidance 追加到消息尾部 → 调聚合器（acting model）→ 把 ``TransportResponse`` 适配回
conversation_loop 期望的 OpenAI 形状响应。

与 Hermes 的关键差异（Spirit 适配）：
- Hermes 用 ``call_llm(task=..., **runtime)``；Spirit 用
  ``create_transport(TransportConfig(**resolve_slot_runtime(slot))).chat(...)``。
  可 monkeypatch 的 seam 是 :func:`_call_slot` / :func:`_call_slot_stream`。
- Hermes 返回 OpenAI 形状响应；Spirit transport 返回 ``TransportResponse``，故需
  :func:`_adapt_to_chat_response` / :func:`_adapt_stream_to_chunks` 适配回
  conversation_loop 期望的形状。
- Hermes 用 ``CanonicalUsage`` + 定价；Spirit 无定价表，usage 以 dict 累加，
  ``consume_reference_usage`` 返回 ``(usage_dict, None)``。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 并发参考模型调用的上限。参考是独立的 advisory 调用（无工具、无相互依赖），故像
# delegate_task 跑批那样一次性全部 fan-out，全部完成后再收集结果。预设很少列出超过
# 几个参考；此上限只防止病态的大预设一次打开几十个 socket。
_MAX_REFERENCE_WORKERS = 8

# advisory 参考视图里每个工具结果的字符预算。工具结果可能巨大（完整 diff、5000 行
# 文件转储）；每个参考每个工具循环步都逐字重放会撑爆参考模型的上下文窗口和成本。我们
# 完整保留 agent 的**动作**（工具调用）——它们廉价、高信号，告诉参考 agent 做了什么——
# 但对每个工具**结果**做 head+tail 预览，让参考仍看到返回了什么而不重放兆字节。acting
# 聚合器总是拿到完整未裁剪的转录；此预算只塑造 advisory 副本。
_REFERENCE_TOOL_RESULT_BUDGET = 4000

# 前置到每个参考模型调用的系统提示。参考是 advisory——它们不行动、不调工具、不拥有
# 任务。没有这个框架，参考会拿到裸的裁剪对话并假设自己是 acting agent：于是拒绝
# （"我无法从这里访问仓库/URL"）或尝试调用它没有的工具。此提示把模型重新定位为分析
# 师，其工作是对呈现的状态推理，并把最佳思考交给真正行动的聚合器/编排器。
_REFERENCE_SYSTEM_PROMPT = (
    "You are a reference advisor in a Mixture of Agents (MoA) process. You are "
    "NOT the acting agent and you do NOT execute anything: you cannot call "
    "tools, run commands, browse, or access files, repositories, or URLs, and "
    "you should not try to or apologize for being unable to. A separate "
    "aggregator/orchestrator model holds those capabilities and will take the "
    "actual actions.\n\n"
    "The conversation below is the current state of a task handled by that "
    "acting agent. Your job is to give your most intelligent analysis of that "
    "state: understand the goal, reason about the problem, and advise on what "
    "to do next. Surface the best approach, concrete next steps and tool-use "
    "strategy, likely pitfalls and risks, and anything the acting agent may "
    "have missed or gotten wrong. Assume any referenced files, URLs, or "
    "systems exist and reason about them from the context given rather than "
    "asking for access.\n\n"
    "Respond with your advice directly — no preamble, no disclaimers about "
    "tools or access. Your response is private guidance handed to the "
    "aggregator, not an answer shown to the user."
)

# 当 advisory 视图以 assistant 轮结尾时追加的合成 user 轮（满足 Anthropic 无 prefill
# 规则，同时不删除 agent 的最新上下文）。
_ADVISORY_INSTRUCTION = (
    "[The conversation above is the current state of the task. Give your "
    "most intelligent judgement: what is going on, what should happen next, "
    "what risks or mistakes you see, and how the acting agent should "
    "proceed.]"
)


# ---------------------------------------------------------------------------
# 参考记账
# ---------------------------------------------------------------------------

class _RefAccounting:
    """单个参考的 token 用量 + 完整追踪，作为参考输出三元组的第三个槽携带。

    ``usage`` 是归一化的 dict（``prompt_tokens``/``completion_tokens``/
    ``total_tokens``）。``messages``/``output``/``model``/``provider``/
    ``temperature`` 携带完整的参考输入与输出用于追踪持久化（展示用的 ``text`` 是截断
    预览，不足以审计一个 advisor 实际看到了什么）。Spirit 无定价表，故不含 cost 字段。
    """

    __slots__ = ("usage", "messages", "output", "model", "provider", "temperature")

    def __init__(
        self,
        usage: Optional[Dict[str, int]] = None,
        *,
        messages: Any = None,
        output: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        temperature: Any = None,
    ):
        self.usage = usage if isinstance(usage, dict) else _empty_usage()
        self.messages = messages
        self.output = output
        self.model = model
        self.provider = provider
        self.temperature = temperature


def _empty_usage() -> Dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _add_usage(a: Dict[str, int], b: Dict[str, int]) -> Dict[str, int]:
    """逐桶累加两个 usage dict。"""
    a = a or {}
    b = b or {}
    return {
        "prompt_tokens": (a.get("prompt_tokens", 0) or 0) + (b.get("prompt_tokens", 0) or 0),
        "completion_tokens": (a.get("completion_tokens", 0) or 0) + (b.get("completion_tokens", 0) or 0),
        "total_tokens": (a.get("total_tokens", 0) or 0) + (b.get("total_tokens", 0) or 0),
    }


def _usage_from_response(response: Any) -> Dict[str, int]:
    """从 ``TransportResponse``（usage 为 dict）或 OpenAI 形状（usage 为对象）提取用量。"""
    usage = getattr(response, "usage", None)
    if isinstance(usage, dict):
        return {
            "prompt_tokens": usage.get("prompt_tokens", 0) or 0,
            "completion_tokens": usage.get("completion_tokens", 0) or 0,
            "total_tokens": usage.get("total_tokens", 0) or 0,
        }
    if usage is not None:
        return {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "total_tokens": getattr(usage, "total_tokens", 0) or 0,
        }
    return _empty_usage()


# ---------------------------------------------------------------------------
# 内容扁平化（Spirit 版 flatten_message_text）
# ---------------------------------------------------------------------------

_NON_TEXT_PART_TYPES = {"image", "image_url", "input_image", "audio", "input_audio"}
_TEXT_KEYS = ("text", "content", "input_text", "output_text", "summary_text")


def _field(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _text_from_part(part: Any) -> str:
    if part is None:
        return ""
    if isinstance(part, str):
        return part
    part_type = str(_field(part, "type") or "").strip().lower()
    if part_type in _NON_TEXT_PART_TYPES:
        return ""
    for key in _TEXT_KEYS:
        text = _field(part, key)
        if isinstance(text, str):
            return text
    return ""


def _flatten_content(content: Any, *, sep: str = "\n") -> str:
    """把消息 content（str 或 content-part 列表）扁平化为可见文本。

    对标 Hermes ``flatten_message_text``：str 原样返回；list 提取所有文本部分（跳过
    image/audio 等非文本部分）用 ``sep`` 拼接；其它类型尽力 str() 兜底。这保证被
    prompt-cache 装饰过的转录（content 变成 ``[{"type":"text",...,"cache_control":...}]``）
    与未装饰的转录产出字节一致的 advisory 视图。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = [_text_from_part(part) for part in content]
        return sep.join(chunk for chunk in chunks if chunk)
    text = _text_from_part(content)
    if text:
        return text
    try:
        return str(content)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# slot 标签 / 推理配置
# ---------------------------------------------------------------------------

def _slot_label(slot: Dict[str, Any]) -> str:
    label = f"{(slot.get('provider') or '').strip()}:{(slot.get('model') or '').strip()}"
    effort = str(slot.get("reasoning_effort") or "").strip()
    return f"{label}[reasoning={effort}]" if effort else label


def _slot_reasoning_config(slot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把可选的 per-slot ``reasoning_effort`` 翻译成 transport 的 reasoning 配置。

    对齐 ``OpenAITransport._apply_reasoning``：``{"enabled": False}`` 关闭推理，
    ``{"enabled": True, "effort": ...}`` 设定档位，None 表示不覆盖（用 provider 默认）。
    """
    effort = str(slot.get("reasoning_effort") or "").strip().lower()
    if not effort:
        return None
    if effort == "none":
        return {"enabled": False}
    return {"enabled": True, "effort": effort}


# ---------------------------------------------------------------------------
# Transport 调用 seam（测试 monkeypatch 点）
# ---------------------------------------------------------------------------

def _build_transport(slot: Dict[str, Any]):
    """把一个 slot 解析成真实运行时并构建 transport。

    一个 MoA slot 只是模型选择，必须像其它地方调用模型那样被调用——通过
    ``resolve_slot_runtime`` 拿到 provider 的真实 base_url/api_key，而不是留空让
    transport 的 auto 检测去猜。
    """
    from spirit.agent.transports.base import TransportConfig
    from spirit.agent.transports.factory import create_transport
    from spirit.moa.config import resolve_slot_runtime

    runtime = resolve_slot_runtime(slot)
    cfg = TransportConfig(
        provider=runtime.get("provider") or "openai",
        model=runtime.get("model") or "",
        api_key=runtime.get("api_key") or "",
        base_url=runtime.get("base_url") or "",
        reasoning=_slot_reasoning_config(slot),
    )
    return create_transport(cfg)


def _slot_chat_kwargs(temperature: Optional[float], max_tokens: Optional[int]) -> Dict[str, Any]:
    """构造下发给 ``transport.chat`` 的可选参数。

    None（默认）= 不下发该参数，用 provider 默认——对齐单模型 Agent 行为，也对齐
    ``max_tokens=None`` 不封顶（避免截断长聚合，并绕开拒绝 max_tokens 的 provider）。
    """
    kwargs: Dict[str, Any] = {}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    return kwargs


def _call_slot(
    slot: Dict[str, Any],
    messages: List[Dict[str, Any]],
    *,
    task: str = "moa_reference",
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
):
    """非流式调用一个 slot → ``TransportResponse``。测试的主要 monkeypatch seam。

    ``task`` 仅用于路由/可观测性（区分参考与聚合器调用），对标 Hermes ``call_llm``
    的 ``task`` 参数。
    """
    transport = _build_transport(slot)
    return transport.chat(
        messages, tools=tools, **_slot_chat_kwargs(temperature, max_tokens),
    )


def _call_slot_stream(
    slot: Dict[str, Any],
    messages: List[Dict[str, Any]],
    *,
    task: str = "moa_aggregator",
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> Iterator[Dict[str, Any]]:
    """流式调用一个 slot → transport 事件迭代器。测试的流式 monkeypatch seam。"""
    transport = _build_transport(slot)
    return transport.chat_stream(
        messages, tools=tools, **_slot_chat_kwargs(temperature, max_tokens),
    )


# ---------------------------------------------------------------------------
# 参考 fan-out
# ---------------------------------------------------------------------------

def _run_reference(
    slot: Dict[str, Any],
    ref_messages: List[Dict[str, Any]],
    *,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> Tuple[str, str, Any]:
    """调用一个参考模型，返回 ``(label, text, acct)``。

    前置 advisory-role 系统提示，让参考明白它在为聚合器分析状态、而非行动于任务。
    永不抛异常：失败的参考变成带标签的备注，让聚合器仍能用部分上下文行动。设计为在
    线程池里运行——``transport.chat`` 是同步阻塞的，故线程（而非 asyncio）是正确的
    并发原语，镜像 ``delegate_task`` 的批量 fan-out。
    """
    label = _slot_label(slot)
    messages = [{"role": "system", "content": _REFERENCE_SYSTEM_PROMPT}, *ref_messages]
    try:
        response = _call_slot(
            slot, messages, task="moa_reference",
            temperature=temperature, max_tokens=max_tokens,
        )
        usage = _usage_from_response(response)
        output_text = _extract_text(response) or "(empty response)"
        acct = _RefAccounting(
            usage,
            messages=messages,
            output=output_text,
            model=slot.get("model"),
            provider=slot.get("provider"),
            temperature=temperature,
        )
        return label, output_text, acct
    except Exception as exc:
        logger.warning("MoA 参考模型 %s 失败: %s", label, exc)
        return label, f"[failed: {exc}]", _RefAccounting(
            _empty_usage(),
            messages=messages,
            output=f"[failed: {exc}]",
            model=slot.get("model"),
            provider=slot.get("provider"),
            temperature=temperature,
        )


def _run_references_parallel(
    reference_models: List[Dict[str, Any]],
    ref_messages: List[Dict[str, Any]],
    *,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> List[Tuple[str, str, Any]]:
    """并行 fan-out 所有参考模型，按序返回输出。

    像 ``delegate_task`` 的批量模式，每个参考一次性派发，阻塞直到全部完成再把 joined
    结果交给聚合器。输出顺序匹配 ``reference_models``，使 ``Reference {idx}`` 标签稳定。
    引用另一个 MoA 预设的 slot 在此跳过（递归守卫）并带标签备注。
    """
    if not reference_models:
        return []

    results: List[Optional[Tuple[str, str, Any]]] = [None] * len(reference_models)
    futures: Dict[Any, int] = {}
    workers = min(_MAX_REFERENCE_WORKERS, len(reference_models))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        for idx, slot in enumerate(reference_models):
            if str(slot.get("provider") or "").strip().lower() == "moa":
                results[idx] = (
                    _slot_label(slot),
                    "[skipped: MoA presets cannot recursively reference MoA]",
                    _RefAccounting(_empty_usage()),
                )
                continue
            futures[
                executor.submit(
                    _run_reference, slot, ref_messages,
                    temperature=temperature, max_tokens=max_tokens,
                )
            ] = idx
        # 收集每个参考后再返回——聚合器需要完整集合，故此处无提前退出/首个完成路径。
        for future, idx in futures.items():
            results[idx] = future.result()

    return [r for r in results if r is not None]


# ---------------------------------------------------------------------------
# advisory 视图构造
# ---------------------------------------------------------------------------

def _truncate_tool_result(text: str, budget: int = _REFERENCE_TOOL_RESULT_BUDGET) -> str:
    """工具结果在 advisory 视图里的 head+tail 预览。

    保留预算的前半与后半，中间用 ``[... N chars omitted ...]`` 标记，使参考既看到结果
    如何开始也看到如何结束，而不重放整个 payload。
    """
    if not text or len(text) <= budget:
        return text
    half = budget // 2
    omitted = len(text) - 2 * half
    return f"{text[:half]}\n[... {omitted} chars omitted ...]\n{text[-half:]}"


def _render_tool_calls(tool_calls: Any) -> str:
    """把一个 assistant 轮的 tool_calls 渲染成可读文本行。

    advisory 视图不能携带真实的 ``tool_calls`` payload（严格的 provider 会拒绝参考从未
    产生的 tool_calls），故 agent 的动作被扁平化成参考能阅读并推理的文本。
    """
    lines: List[str] = []
    for tc in tool_calls or []:
        fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
        name = fn.get("name") or (tc.get("name") if isinstance(tc, dict) else "") or "tool"
        args = fn.get("arguments") if fn else (tc.get("args") if isinstance(tc, dict) else None)
        if isinstance(args, str):
            args_text = args
        elif args is not None:
            try:
                args_text = json.dumps(args, ensure_ascii=False)
            except Exception:
                args_text = str(args)
        else:
            args_text = ""
        lines.append(
            f"[called tool: {name}({args_text})]" if args_text else f"[called tool: {name}]"
        )
    return "\n".join(lines)


def _reference_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """为参考模型构造对话的 advisory 视图。

    参考要对当前状态给出**知情**判断，故它必须看到 agent 实际做了什么——它的工具调用
    **和**返回的工具结果——而不只是 agent 的叙述。因此我们保留整个对话流，但把它扁平化
    成干净的 user/assistant **文本**轮：

      - system 提示：丢弃（大量样板，非 advisory 信号）。
      - assistant 轮：保留；任何 ``tool_calls`` 内联渲染成 ``[called tool: name(args)]``
        文本行追加到该轮文本。
      - ``tool``-role 结果：**不**丢弃。每个被折叠（head+tail 预览）进**前一个**
        assistant 轮，作为 ``[tool result: ...]`` 块，使参考看到返回了什么。

    这产出零个 ``tool``-role 消息和零个 ``tool_calls`` 数组——只有纯 user/assistant
    文本——故拒绝孤立 tool 消息/未产生 tool_calls 的严格 provider（Mistral、Fireworks）
    不会 400，而参考仍有完整图景。

    视图**必须**以 ``user`` 轮结尾。Anthropic（及 OpenRouter→Anthropic）把结尾的
    assistant 轮解释为要续写的 assistant *prefill*，而无 prefill 的模型会用
    ``400 ... must end with a user message`` 拒绝它。我们**追加**一个合成 user 轮请参考
    判断上面的状态，而非**删除** agent 的最新上下文——既满足 end-on-user 又不丢上下文。
    """
    rendered: List[Dict[str, Any]] = []
    last_user_content: Optional[str] = None
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        # 把结构化 content（parts 列表）扁平化为可见文本。content 以列表（而非字符串）
        # 到达有两种常见情况：(1) prompt-cache 装饰把字符串 content 转成
        # [{"type":"text","text":...,"cache_control":...}]；(2) 多模态轮（文本+图片）。
        # _flatten_content 提取文本部分并跳过图片部分，且原样返回字符串——故装饰过的与
        # 未装饰的转录产出字节一致的 advisory 视图（保持 advisory 前缀稳定供 advisor
        # prompt caching）。
        text = _flatten_content(content)

        if role == "system":
            continue
        if role == "user":
            if not text.strip() and isinstance(content, list) and content:
                # 无可提取文本的结构化 content（如纯图片轮）。发空 user 消息会被严格
                # provider 丢弃/拒绝（Anthropic 对空文本块 400），而静默跳过会破坏
                # advisory 视图里的 user/assistant 交替。用占位符替代，让参考知道发生了
                # 一个非文本轮。只有结构化 content 符合——空或纯空白的**字符串**轮不携带
                # 任何内容，在下面被丢弃。
                text = "[user sent non-text content (e.g. an image attachment)]"
            if not text.strip():
                # 真正空的 user 轮（content=""/None）。它不携带 advisory 内容，严格
                # provider（Kimi/Moonshot、ZAI 等强制非空 user content 的）会用 400
                # "message ... with role 'user' must not be empty" 拒绝它。advisory 视图
                # 本就不严格交替（每个工具循环都出现相邻 assistant 轮），故丢弃无内容轮
                # 是安全的。
                continue
            last_user_content = text
            rendered.append({"role": "user", "content": text})
        elif role == "assistant":
            parts: List[str] = []
            if text.strip():
                parts.append(text.strip())
            calls_text = _render_tool_calls(msg.get("tool_calls"))
            if calls_text:
                parts.append(calls_text)
            # 空 assistant 轮（无文本、无调用）不携带 advisory 内容。
            if parts:
                rendered.append({"role": "assistant", "content": "\n".join(parts)})
        elif role == "tool":
            # 把工具结果折叠进前一个 assistant 轮作为文本，使参考看到返回了什么，而不
            # 发出一个参考从未产生的 tool-role 消息。
            result_text = _truncate_tool_result(text)
            block = f"[tool result: {result_text}]"
            if rendered and rendered[-1].get("role") == "assistant":
                rendered[-1]["content"] = rendered[-1]["content"] + "\n" + block
            else:
                # 没有可附着的 assistant 轮（如前导的工具结果）；单独保留为 advisory
                # 上下文的 assistant-role 行。
                rendered.append({"role": "assistant", "content": block})
        # 任何其它 role 被忽略。

    # 以 user 轮结尾：追加合成 advisory 请求，而非删除 agent 的最新 assistant 上下文。
    if rendered and rendered[-1].get("role") == "assistant":
        rendered.append({"role": "user", "content": _ADVISORY_INSTRUCTION})
    elif rendered and rendered[-1].get("role") == "user":
        # 已经以 user 轮结尾（新用户提示，尚无 agent 动作）。保留——参考直接回答该提示。
        pass

    if not rendered:
        # 退化情况：什么都没渲染。回退到最近的 user 轮。
        if last_user_content is not None:
            return [{"role": "user", "content": last_user_content}]
        for msg in reversed(messages):
            if msg.get("role") == "user":
                fallback_text = _flatten_content(msg.get("content"))
                if fallback_text.strip():
                    return [{"role": "user", "content": fallback_text}]
    return rendered


def _extract_text(response: Any) -> str:
    """从响应提取文本，兼容 ``TransportResponse``（``.content``）与 OpenAI 形状。"""
    content = getattr(response, "content", None)
    if isinstance(content, str) and content.strip():
        return content.strip()
    try:
        message = response.choices[0].message
        if isinstance(message, dict):
            c = message.get("content")
        else:
            c = getattr(message, "content", message)
        if not isinstance(c, str):
            c = str(c) if c else ""
        return c.strip()
    except Exception:
        return ""


def _preset_temperature(preset: Dict[str, Any], key: str) -> Optional[float]:
    """从预设读取可选温度。

    当 key 缺失、为空或显式 null 时返回 None——意为"不下发 temperature；用 provider
    默认"，恰如单模型 Agent（除非配置否则从不下发 temperature）。
    """
    value = preset.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("忽略 MoA 预设里非数值的 %s=%r", key, value)
        return None


# ---------------------------------------------------------------------------
# 响应适配（TransportResponse → conversation_loop 期望的 OpenAI 形状）
# ---------------------------------------------------------------------------

def _tool_call_args_text(args: Any) -> str:
    if isinstance(args, str):
        return args or "{}"
    if args is None:
        return "{}"
    try:
        return json.dumps(args, ensure_ascii=False)
    except Exception:
        return str(args)


def _adapt_to_chat_response(tr: Any, *, model: str = "") -> Any:
    """把 ``TransportResponse`` 适配成 conversation_loop 期望的 OpenAI 形状响应。

    conversation_loop 读 ``response.choices[0].message.{content, tool_calls}`` 与
    ``response.usage.{prompt_tokens, completion_tokens, total_tokens}``；tool_calls 走
    ``hasattr(tc, 'function')`` → ``serialize_tool_call(tc)``（读 ``tc.id``、
    ``tc.function.name``、``tc.function.arguments``）。TransportResponse.tool_calls 是
    ``[{"id","name","args"}]`` dict，故在此转成带 ``.function`` 的对象。
    """
    tool_calls_objs = []
    for idx, tc in enumerate(getattr(tr, "tool_calls", None) or []):
        if isinstance(tc, dict):
            tc_id = tc.get("id")
            name = tc.get("name")
            args = tc.get("args")
        else:
            tc_id = getattr(tc, "id", None)
            name = getattr(tc, "name", "")
            args = getattr(tc, "args", None)
        tool_calls_objs.append(
            SimpleNamespace(
                id=tc_id or f"call_{idx}",
                function=SimpleNamespace(
                    name=name or "", arguments=_tool_call_args_text(args),
                ),
            )
        )

    message = SimpleNamespace(
        content=getattr(tr, "content", "") or "",
        tool_calls=tool_calls_objs or None,
    )
    usage_dict = getattr(tr, "usage", None) or {}
    if isinstance(usage_dict, dict):
        usage = SimpleNamespace(
            prompt_tokens=usage_dict.get("prompt_tokens", 0) or 0,
            completion_tokens=usage_dict.get("completion_tokens", 0) or 0,
            total_tokens=usage_dict.get("total_tokens", 0) or 0,
        )
    else:  # pragma: no cover - 兼容对象形态 usage
        usage = SimpleNamespace(
            prompt_tokens=getattr(usage_dict, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage_dict, "completion_tokens", 0) or 0,
            total_tokens=getattr(usage_dict, "total_tokens", 0) or 0,
        )
    choice = SimpleNamespace(
        message=message, finish_reason=getattr(tr, "finish_reason", "") or "stop",
    )
    return SimpleNamespace(
        choices=[choice], usage=usage, model=getattr(tr, "model", "") or model,
    )


def _adapt_stream_to_chunks(events: Iterator[Dict[str, Any]]) -> Iterator[Any]:
    """把 transport 的流式事件转成 conversation_loop 期望的 OpenAI chunk 形状。

    transport ``chat_stream`` 产出 ``{"type":"content","text":...}`` /
    ``{"type":"tool_call","id","name","args"}`` / ``{"type":"done","usage":...}``；
    conversation_loop 的流式路径读 ``chunk.usage``、
    ``chunk.choices[0].{finish_reason, delta.{content, tool_calls[].{index, id,
    function.name, function.arguments}}}``。此适配器桥接两种形状。
    """
    tc_index = 0
    for ev in events or []:
        etype = ev.get("type")
        if etype == "content":
            delta = SimpleNamespace(content=ev.get("text") or "", tool_calls=None)
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=delta, finish_reason=None)], usage=None,
            )
        elif etype == "tool_call":
            tc = SimpleNamespace(
                index=tc_index,
                id=ev.get("id") or f"call_{tc_index}",
                function=SimpleNamespace(
                    name=ev.get("name") or "",
                    arguments=_tool_call_args_text(ev.get("args")),
                ),
            )
            tc_index += 1
            delta = SimpleNamespace(content=None, tool_calls=[tc])
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=delta, finish_reason=None)], usage=None,
            )
        elif etype == "done":
            usage_dict = ev.get("usage") or {}
            usage = SimpleNamespace(
                prompt_tokens=usage_dict.get("prompt_tokens", 0) or 0,
                completion_tokens=usage_dict.get("completion_tokens", 0) or 0,
                total_tokens=usage_dict.get("total_tokens", 0) or 0,
            )
            delta = SimpleNamespace(content=None, tool_calls=None)
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=delta, finish_reason="stop")], usage=usage,
            )


# ---------------------------------------------------------------------------
# 一次性 /moa 合成
# ---------------------------------------------------------------------------

def aggregate_moa_context(
    *,
    user_prompt: str,
    api_messages: List[Dict[str, Any]],
    reference_models: List[Dict[str, Any]],
    aggregator: Dict[str, Any],
    temperature: Optional[float] = None,
    aggregator_temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """跑配置的参考模型并合成它们的建议（一次性 ``/moa <prompt>`` 命令用）。

    失败作为模型特定备注返回，而非中断正常 agent 循环；主模型仍能用部分上下文行动。
    ``max_tokens`` 默认 None：MoA 不封顶参考或聚合器输出，各模型用自己的最大值。
    """
    ref_messages = _reference_messages(api_messages)
    reference_outputs = _run_references_parallel(
        reference_models, ref_messages, temperature=temperature, max_tokens=max_tokens,
    )

    joined = "\n\n".join(
        f"Reference {idx} — {label}:\n{text}"
        for idx, (label, text, _acct) in enumerate(reference_outputs, start=1)
    )
    synth_prompt = (
        "You are the aggregator in a Mixture of Agents process. Synthesize the "
        "reference responses into concise, actionable guidance for the main "
        "agent. Focus on next steps, tool-use strategy, risks, and any "
        "disagreements. Do not answer the user directly unless that is all that "
        "is needed; produce context the main agent should use in its normal loop.\n\n"
        f"Original user prompt:\n{user_prompt}\n\n"
        f"Reference responses:\n{joined}"
    )

    agg_label = _slot_label(aggregator)
    try:
        response = _call_slot(
            aggregator, [{"role": "user", "content": synth_prompt}],
            task="moa_aggregator",
            temperature=aggregator_temperature, max_tokens=max_tokens,
        )
        synthesis = _extract_text(response)
    except Exception as exc:
        logger.warning("MoA 聚合器模型 %s 失败: %s", agg_label, exc)
        synthesis = ""

    if not synthesis:
        synthesis = joined

    return (
        "[Mixture of Agents context — use this as private guidance for the "
        "normal agent loop. You may call tools, continue reasoning, or "
        "finish normally.]\n"
        f"Aggregator: {agg_label}\n"
        f"References: {', '.join(_slot_label(slot) for slot in reference_models)}\n\n"
        f"{synthesis.strip()}"
    )


def _attach_reference_guidance(agg_messages: List[Dict[str, Any]], guidance: str) -> None:
    """把 per-turn 参考块附加到聚合器提示的**末尾**。

    参考文本在每个工具循环迭代都不同。在 agentic 循环里，最近的 ``user`` 消息是坐在
    上下文**顶部**附近的*原始任务*（其后全是 assistant/tool 轮），故把 turn-varying 的
    参考块合并进它会在早期就分叉提示前缀——服务器的 KV 缓存无法复用，整个对话在每一步
    重新 prefill（每个工具调用都完整 prefill，在长上下文上主导延迟）。

    附加到最末尾保持 ``[system][task][tool-history]`` 前缀稳定且可缓存复用（只有新块
    重新 prefill），并给聚合器带 recency 的参考。仅当最后一条已经是结尾的 ``user`` 轮
    （纯聊天——仍在末尾）时才合并进它。结尾 user 轮的 content 可能是字符串或 content-part
    列表（prompt-cache 装饰/多模态），两种形状都就地合并：在 cache_control 标记部分
    **之后**追加新文本部分，保持缓存前缀字节稳定，同时 turn-varying 的 guidance 骑在
    缓存跨度之外。在此追加**单独**的 user 消息会产生两个连续 user 轮——严格 provider 拒绝。
    """
    last = agg_messages[-1] if agg_messages else None
    if last is not None and last.get("role") == "user":
        last_content = last.get("content")
        if isinstance(last_content, str):
            last["content"] = last_content + "\n\n" + guidance
            return
        if isinstance(last_content, list):
            last["content"] = [*last_content, {"type": "text", "text": "\n\n" + guidance}]
            return
    agg_messages.append({"role": "user", "content": guidance})


# ---------------------------------------------------------------------------
# MoA facade（替换 agent.client）
# ---------------------------------------------------------------------------

def _load_moa_config() -> Dict[str, Any]:
    """读取合并后的 ``moa`` 配置节（含用户 YAML 预设）。测试 monkeypatch 点。

    用 ``load_config()``（DEFAULT_CONFIG + YAML + env 合并）而非 ``get_config_value``
    （只读 DEFAULT_CONFIG），因为 MoA 预设是重度用户配置的。
    """
    from spirit.config import load_config

    return load_config().get("moa") or {}


class MoAChatCompletions:
    """OpenAI-chat 兼容 facade，其中聚合器是 acting model。"""

    def __init__(self, preset_name: str, reference_callback: Any = None):
        self.preset_name = preset_name or "default"
        # 可选展示钩子。在参考输出变得可用时调用，使前端能在聚合器行动前把每个参考模型的
        # 答案显示为带标签的块。签名：reference_callback(event, **kwargs)，event 为：
        #   "moa.reference"   kwargs: index, count, label, text
        #   "moa.aggregating" kwargs: aggregator (label), ref_count
        # 永不抛进模型调用——展示是尽力而为。
        self.reference_callback = reference_callback
        # 状态域参考缓存。agent 循环每个工具循环迭代调一次 create()；参考应在任务**状态**
        # 推进时重跑——即每个新 user 消息**和**每个新工具结果——使每个参考判断最新状态。
        # advisory 视图（_reference_messages）把工具调用+结果渲染成文本，故其签名在每个
        # 新工具响应时变化；缓存键就是那个签名，于是新工具结果是缓存 MISS（参考重跑），
        # 而状态相同的冗余 create() 调用是 HIT（不重跑、不重复 emit）。
        self._ref_cache_key: Optional[tuple] = None
        self._ref_cache_outputs: List[Tuple[str, str, Any]] = []
        # 最近一次缓存 MISS 的 create() 的参考 fan-out token 用量，等待会话记账消费。
        # 每次 create() 都设置（缓存 HIT 时归零，使 per-turn advisor 花费只计一次）。
        self._pending_reference_usage: Dict[str, int] = _empty_usage()
        # 最近一次 create() 解析出的聚合器 slot（{provider, model, ...}）；供会话成本记账
        # 把聚合器的 acting 轮按真实模型定价，而非虚拟预设名。
        self.last_aggregator_slot: Any = None
        # 缓存 MISS 的 create() 上暂存的完整轮次追踪部件，等待调用方拼入实时 session_id +
        # 解析出的聚合器输出并刷到追踪文件（仅当 moa.save_traces 开启）。
        self._pending_trace: Any = None

    def consume_reference_usage(self) -> Tuple[Any, Any]:
        """弹出待处理的参考 fan-out 用量，重置为空。

        返回最近一次 ``create()`` 的 ``(usage_dict, None)`` 并清空待处理值，使后续读取
        （如流式重试重新进入记账）不会重复计数。Spirit 无定价表，故 cost 恒为 None。
        """
        usage = self._pending_reference_usage or _empty_usage()
        self._pending_reference_usage = _empty_usage()
        return usage, None

    def consume_and_save_trace(
        self, session_id: Any = None, aggregator_output_fallback: Any = None,
    ) -> None:
        """把待处理的完整轮次追踪刷到磁盘（若有）。

        追踪关闭（``save_moa_turn`` 检查配置）、无待处理追踪（缓存 HIT 迭代没跑参考）、
        或聚合器输入从未记录时为 no-op。清空待处理追踪使重复 consume 不会双写。尽力
        而为——永不抛异常。``aggregator_output_fallback`` 是聚合器解析出的 acting 文本
        （流式路径在 create() 时无法内联捕获，由此折入使追踪自包含）。
        """
        pending = self._pending_trace
        self._pending_trace = None
        if not pending or "aggregator_input_messages" not in pending:
            return
        try:
            from spirit.moa.moa_trace import save_moa_turn

            agg_slot = pending.get("aggregator_slot") or {}
            agg_output = pending.get("aggregator_output")
            if agg_output is None and aggregator_output_fallback:
                agg_output = aggregator_output_fallback
            save_moa_turn(
                session_id=session_id,
                preset_name=pending.get("preset", ""),
                reference_outputs=pending.get("reference_outputs", []),
                aggregator_label=pending.get("aggregator_label", ""),
                aggregator_model=agg_slot.get("model"),
                aggregator_provider=agg_slot.get("provider"),
                aggregator_temperature=pending.get("aggregator_temperature"),
                aggregator_input_messages=pending.get("aggregator_input_messages"),
                aggregator_output=agg_output,
                aggregator_streamed=bool(pending.get("aggregator_streamed")),
            )
        except Exception as exc:  # pragma: no cover - 追踪绝不能中断轮次
            logger.debug("MoA 追踪刷写失败: %s", exc)

    def _emit(self, event: str, **kwargs: Any) -> None:
        cb = self.reference_callback
        if cb is None:
            return
        try:
            cb(event, **kwargs)
        except Exception as exc:  # pragma: no cover - 展示绝不能中断轮次
            logger.debug("MoA reference_callback 在 %s 失败: %s", event, exc)

    def create(self, **api_kwargs: Any) -> Any:
        from spirit.moa.config import resolve_moa_preset

        preset = resolve_moa_preset(_load_moa_config(), self.preset_name)
        messages = list(api_kwargs.get("messages") or [])
        reference_models = preset.get("reference_models") or []
        aggregator = preset.get("aggregator") or {}
        # 暴露解析出的聚合器 slot，使会话成本记账能按聚合器 acting 轮的真实模型/provider
        # 定价。MoA 路径上 agent 的 model/provider 是虚拟预设名与 "moa"，无定价条目。
        self.last_aggregator_slot = dict(aggregator) if aggregator else None
        # 默认 MoA 不封顶参考或聚合器输出：各模型用自己的最大值（max_tokens=None →
        # 不下发参数，长聚合成永不被截断，拒绝 max_tokens 的 provider 也不 400）。预设
        # **可**设 reference_max_tokens 只封顶 ADVISOR 输出——advisor 生成是 MoA 的主要
        # 延迟，聚合器只需每条建议的要旨，故封顶可显著降低每轮墙钟时间。acting 聚合器
        # 在此从不封顶（其输出是用户可见答案）。
        reference_max_tokens = preset.get("reference_max_tokens")
        # None（默认）= 不下发 temperature；provider 默认生效，匹配单模型 agent 行为。
        temperature = _preset_temperature(preset, "reference_temperature")
        aggregator_temperature = _preset_temperature(preset, "aggregator_temperature")
        if aggregator_temperature is None and api_kwargs.get("temperature") is not None:
            # acting agent 自己配置的温度（若有）仍适用于聚合器，因为它就是 acting model。
            aggregator_temperature = api_kwargs.get("temperature")

        # 预设禁用时，跳过参考 fan-out，让配置的聚合器独自行动——它是预设的 acting model，
        # 故禁用的 MoA 预设就是"直接用聚合器"。
        if not preset.get("enabled", True):
            reference_models = []

        reference_outputs: List[Tuple[str, str, Any]] = []
        ref_messages = _reference_messages(messages)

        # fan-out 节奏。"per_iteration"（默认）：advisory 视图变化时 advisor 重跑——即每个
        # 工具迭代，因为视图随每个工具结果增长。"user_turn"：advisor 每用户轮只跑一次；
        # 后续工具迭代复用该轮的建议，聚合器独自行动（原始 MoA 形态：开头合成，然后让
        # acting model 工作）。通过只哈希到**最后一条 user 消息**的前缀实现，使轮中增长
        # 不改变签名——迭代 2+ 变成缓存 HIT。
        fanout_mode = str(preset.get("fanout") or "per_iteration").strip().lower()
        sig_messages = ref_messages
        if fanout_mode == "user_turn":
            # 找到最后一条**真实** user 消息。advisory 视图在以 assistant 轮结尾时追加合成
            # user 标记（_ADVISORY_INSTRUCTION）——即第一个之后的每个工具迭代——故该标记
            # 不能算作用户轮，否则前缀会包含轮中增长的上下文，签名每迭代都变（彻底破坏
            # 每轮一次的节奏）。
            last_user_idx = None
            for _i in range(len(ref_messages) - 1, -1, -1):
                _m = ref_messages[_i]
                if _m.get("role") == "user" and _m.get("content") != _ADVISORY_INSTRUCTION:
                    last_user_idx = _i
                    break
            if last_user_idx is not None:
                sig_messages = ref_messages[: last_user_idx + 1]

        # turn 域缓存：仅当 advisory 视图变化（即新用户轮）时才跑+展示参考。一个轮内 agent
        # 循环每个工具迭代调一次 create()；user_turn 模式下签名跨这些迭代稳定（上面的前缀
        # 哈希），故 fan-out 每用户轮跑一次，迭代复用建议。
        _sig = hashlib.sha256(
            "\u0000".join(
                f"{m.get('role')}:{m.get('content')}" for m in sig_messages
            ).encode("utf-8", "replace")
        ).hexdigest()
        _cache_key = (self.preset_name, _sig, tuple(_slot_label(s) for s in reference_models))
        _refs_from_cache = _cache_key == self._ref_cache_key and bool(self._ref_cache_outputs)

        if _refs_from_cache:
            reference_outputs = list(self._ref_cache_outputs)
            # 参考已在本轮早先跑过（并记过账）；此次 create() 是复用缓存建议的重复工具
            # 迭代。在此再次计费它们的 token 会把 advisor 花费乘以工具迭代数，故 pending 归零。
            self._pending_reference_usage = _empty_usage()
            # 缓存 HIT 也无追踪——完整轮次已在跑参考的 MISS 上追踪过。重复迭代不是新 MoA 轮。
            self._pending_trace = None
        else:
            reference_outputs = _run_references_parallel(
                reference_models, ref_messages,
                temperature=temperature, max_tokens=reference_max_tokens,
            )
            self._ref_cache_key = _cache_key
            self._ref_cache_outputs = list(reference_outputs)
            # 累加 advisor fan-out 的 token 用量，使调用方能把 advisor 花费折进会话记账，
            # 每轮恰好一次。只有新跑的参考（缓存 MISS）贡献；上面的缓存 HIT 归零。
            _ref_usage = _empty_usage()
            for _lbl, _txt, _acct in reference_outputs:
                if isinstance(_acct, _RefAccounting):
                    _ref_usage = _add_usage(_ref_usage, _acct.usage)
            self._pending_reference_usage = _ref_usage
            # 暂存完整参考 fan-out 用于追踪持久化。聚合器输入/标签在下面 agg_messages 构建
            # 后填入；聚合器**输出**由调用方（consume_and_save_trace）在响应解析后拼入。
            self._pending_trace = {
                "preset": self.preset_name,
                "reference_outputs": list(reference_outputs),
                "aggregator_slot": aggregator,
                "aggregator_temperature": aggregator_temperature,
            }

            # 在聚合器行动**之前**把每个参考模型的答案 surfaced 到展示——每轮一次（只在
            # 真正跑了它们的那次迭代）。用户看到每个参考一个带标签的块，使 MoA 过程可见而非
            # 静默暂停。尽力而为：绝不阻塞轮次。
            _ref_count = len(reference_outputs)
            for _idx, (_label, _text, _u) in enumerate(reference_outputs, start=1):
                self._emit(
                    "moa.reference", index=_idx, count=_ref_count, label=_label, text=_text,
                )
            if _ref_count:
                self._emit(
                    "moa.aggregating",
                    aggregator=_slot_label(aggregator), ref_count=_ref_count,
                )

        agg_messages = [dict(m) for m in messages]
        if reference_outputs:
            joined = "\n\n".join(
                f"Reference {idx} — {label}:\n{text}"
                for idx, (label, text, _u) in enumerate(reference_outputs, start=1)
            )
            guidance = (
                "[Mixture of Agents reference context]\n"
                f"Preset: {self.preset_name}\n"
                f"Aggregator/acting model: {_slot_label(aggregator)}\n"
                f"References: {', '.join(label for label, _, _ in reference_outputs)}\n\n"
                "Use the reference responses below as private context. You are the "
                "aggregator and acting model: answer the user directly or call tools "
                "as needed.\n\n"
                f"{joined}"
            )
            _attach_reference_guidance(agg_messages, guidance)

        if str(aggregator.get("provider") or "").strip().lower() == "moa":
            raise RuntimeError("MoA aggregator cannot be another MoA preset")

        # 把确切的聚合器**输入**（含注入的参考上下文）记入待处理追踪，使追踪捕获聚合器实际
        # 看到的，而非重建。
        if self._pending_trace is not None:
            self._pending_trace["aggregator_input_messages"] = agg_messages
            self._pending_trace["aggregator_label"] = _slot_label(aggregator)

        stream = bool(api_kwargs.get("stream"))
        tools = api_kwargs.get("tools")
        agg_max_tokens = api_kwargs.get("max_tokens")

        # 聚合器是 acting model。流式路径：先跑参考（上面），再返回聚合器的**原始** token
        # 流，使 acting model 的输出实时到达用户。消费方重组 chunks + tool_calls。非流式
        # 路径：聚合器输出内联可得，现在捕获进待处理追踪。
        if stream:
            if self._pending_trace is not None:
                self._pending_trace["aggregator_streamed"] = True
                self._pending_trace["aggregator_output"] = None
            events = _call_slot_stream(
                aggregator, agg_messages, task="moa_aggregator",
                temperature=aggregator_temperature, max_tokens=agg_max_tokens, tools=tools,
            )
            return _adapt_stream_to_chunks(events)

        response = _call_slot(
            aggregator, agg_messages, task="moa_aggregator",
            temperature=aggregator_temperature, max_tokens=agg_max_tokens, tools=tools,
        )
        if self._pending_trace is not None:
            self._pending_trace["aggregator_streamed"] = False
            try:
                self._pending_trace["aggregator_output"] = _extract_text(response)
            except Exception:  # pragma: no cover - 防御性
                self._pending_trace["aggregator_output"] = None
        return _adapt_to_chat_response(response)


class MoAClient:
    """替换 ``agent.client`` 的 facade：``.chat.completions.create`` 走 MoA 流程。"""

    def __init__(self, preset_name: str, reference_callback: Any = None):
        self.chat = type("_MoAChat", (), {})()
        self.chat.completions = MoAChatCompletions(
            preset_name, reference_callback=reference_callback,
        )

    def consume_reference_usage(self) -> Any:
        """从 completions facade 弹出待处理的参考 fan-out 用量。

        让会话记账把 MoA advisor token 折进本轮用量，而无需伸进 ``.chat.completions``
        内部。
        """
        return self.chat.completions.consume_reference_usage()

    @property
    def last_aggregator_slot(self) -> Any:
        """最近一次 create() 解析出的聚合器 slot（{provider, model, ...}），或 None。"""
        return getattr(self.chat.completions, "last_aggregator_slot", None)

    def consume_and_save_trace(
        self, session_id: Any = None, aggregator_output_fallback: Any = None,
    ) -> None:
        """通过 completions facade 刷写待处理的完整轮次 MoA 追踪。

        除非 ``moa.save_traces`` 启用且有一个轮次待处理，否则 no-op。
        """
        return self.chat.completions.consume_and_save_trace(
            session_id, aggregator_output_fallback=aggregator_output_fallback,
        )


__all__ = [
    "MoAClient",
    "MoAChatCompletions",
    "aggregate_moa_context",
    "_reference_messages",
    "_attach_reference_guidance",
    "_run_reference",
    "_run_references_parallel",
    "_slot_label",
    "_extract_text",
    "_call_slot",
    "_call_slot_stream",
    "_adapt_to_chat_response",
    "_REFERENCE_SYSTEM_PROMPT",
    "_REFERENCE_TOOL_RESULT_BUDGET",
    "_ADVISORY_INSTRUCTION",
    "_RefAccounting",
]
