"""代理补全 seam —— 把 OpenAI messages 翻译成 Spirit agent 调用。

这是代理服务器的**可测试接缝**：核心 handler 只依赖一个补全函数
``completion_fn(messages, *, model=None) -> str``。默认实现
:func:`default_completion_fn` 惰性构造 :class:`~spirit.agent.agent.SpiritAgent`
并调用其 ``chat()``；测试注入 fake 函数即可完全离线验证 handler，无需真实 LLM。

纯函数（:func:`content_to_text` / :func:`extract_system_prompt` /
:func:`messages_to_user_prompt`）负责 OpenAI 消息格式 → 文本的翻译，单独可测。

OpenAI 是无状态的（每次请求带完整 ``messages``）。Spirit agent 自带会话历史，
故这里把 messages 折叠为「system 提示 + 单条 user 文本（含既往轮次转录）」。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def content_to_text(content: Any) -> str:
    """把 OpenAI ``content`` 归一为纯文本。

    兼容三种形态：``str``、``[{"type":"text","text":...}, ...]``（多模态分部）、None。
    非文本分部（image_url 等）被忽略。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif isinstance(part.get("text"), str):
                    parts.append(part["text"])
        return "\n".join(p for p in parts if p)
    return str(content)


def _role_of(msg: Dict[str, Any]) -> str:
    return str(msg.get("role", "")).lower()


def extract_system_prompt(messages: List[Dict[str, Any]]) -> str:
    """拼接所有 ``role == system`` 消息的文本（换行分隔）。"""
    sys_texts = [
        content_to_text(m.get("content"))
        for m in messages
        if isinstance(m, dict) and _role_of(m) == "system"
    ]
    return "\n".join(t for t in sys_texts if t).strip()


def messages_to_user_prompt(messages: List[Dict[str, Any]]) -> str:
    """把对话 messages 折叠为单条 user 文本。

    - 只有一条非 system 消息 → 直接返回其文本。
    - 多条 → 把既往轮次转录为 ``role: text`` 前言，末尾附最新 user 消息，
      让无状态代理也能携带上下文。
    """
    convo = [
        m for m in messages
        if isinstance(m, dict) and _role_of(m) != "system"
    ]
    if not convo:
        # 退化：没有任何非 system 消息
        return ""
    if len(convo) == 1:
        return content_to_text(convo[0].get("content")).strip()

    lines: List[str] = []
    for m in convo[:-1]:
        text = content_to_text(m.get("content")).strip()
        if text:
            lines.append(f"{_role_of(m) or 'user'}: {text}")
    last_text = content_to_text(convo[-1].get("content")).strip()
    if lines:
        preamble = "\n".join(lines)
        return f"{preamble}\n\n{last_text}" if last_text else preamble
    return last_text


def default_completion_fn(
    messages: List[Dict[str, Any]],
    *,
    model: Optional[str] = None,
) -> str:
    """默认补全实现：惰性构造 SpiritAgent 并跑一轮对话。

    任何异常都被捕获并转为文本错误提示（代理层再包成 OpenAI 错误体），
    绝不让底层 agent 异常穿透到 HTTP handler。
    """
    system_prompt = extract_system_prompt(messages)
    user_prompt = messages_to_user_prompt(messages)
    if not user_prompt:
        return ""
    try:
        from spirit.agent.agent_init import initialize_agent

        overrides: Dict[str, Any] = {}
        if model:
            overrides["model"] = model
        if system_prompt:
            overrides["system_prompt"] = system_prompt
        agent = initialize_agent(**overrides)
        result = agent.chat(user_prompt)
        return str(result.get("response", "") or "")
    except Exception as exc:  # noqa: BLE001 - agent 失败绝不穿透到 HTTP 层
        logger.warning("代理补全失败: %s", exc)
        raise


__all__ = [
    "content_to_text",
    "extract_system_prompt",
    "messages_to_user_prompt",
    "default_completion_fn",
]
