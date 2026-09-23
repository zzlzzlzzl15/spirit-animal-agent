"""完整 MoA 轮次追踪持久化（opt-in，配置 ``moa.save_traces``）— Phase 4.1。

对标 Hermes ``agent/moa_trace.py``。启用后，每个真正跑了参考 fan-out 的 MoA 轮
（``MoAChatCompletions.create`` 里的一次缓存 MISS）向
``<spirit_home>/moa-traces/<session_id>.jsonl`` 追加一行 JSON。记录是**真正的完整
轮次**——每个参考模型收到的确切 messages 数组（advisory 系统提示 + advisory 视图，
不是截断的展示预览）、每个参考的完整输出、聚合器收到的确切 messages 数组（含注入的
参考上下文 guidance 块）及其输出——于是一次运行可离线端到端审计：每个模型看到了什么、
说了什么、花了多少。

这是旁路追踪，**不是**对话 messages 表，永不进入消息历史或回放——MoA 参考是带自己
系统提示的 advisory 旁路调用，不是对话轮，持久化成消息行会破坏角色交替/回放。追踪活在
自己的文件里，按 session id 归档，可安全删除。

默认关闭。关闭时唯一开销是一次 ``_traces_enabled_and_dir()`` 配置读（廉价）——无文件
I/O、无序列化。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _traces_enabled_and_dir() -> Optional[Path]:
    """``moa.save_traces`` 开启时返回追踪目录，否则 None。

    每次调用惰性读配置（配置读廉价，且这只在缓存 MISS 的 MoA 轮跑，即每用户轮一次，
    不是每工具迭代）。用 ``load_config()``（DEFAULT_CONFIG + YAML + env 合并）而非
    ``get_config_value``（只读 DEFAULT_CONFIG），因为 ``save_traces``/``trace_dir`` 是用户
    可配置的开关。``moa.trace_dir`` 覆盖默认的 ``<spirit_home>/moa-traces/``。
    """
    try:
        from spirit.config import load_config

        moa = load_config().get("moa") or {}
        if not moa.get("save_traces", False):
            return None
        override = moa.get("trace_dir")
    except Exception:  # pragma: no cover - 防御性：追踪绝不因配置读失败而中断轮次
        return None
    if override:
        return Path(os.path.expandvars(os.path.expanduser(str(override))))
    try:
        from spirit.config import get_spirit_home

        return get_spirit_home() / "moa-traces"
    except Exception:  # pragma: no cover - 防御性
        return None


def _sanitize_session_id(session_id: Optional[str]) -> str:
    """把 session id 变成安全的文件名片段。"""
    if not session_id:
        return "unknown-session"
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(session_id))


def _slot_trace(acct: Any, label: str) -> dict:
    """把一个参考的 ``_RefAccounting`` 渲染成完整追踪 dict。

    含参考收到的**完整**输入 messages 与其**完整**输出——不是截断的展示预览。
    """
    usage = getattr(acct, "usage", None) or {}
    if isinstance(usage, dict):
        usage_dict = {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
    else:  # pragma: no cover - 兼容对象形态 usage
        usage_dict = {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0),
            "completion_tokens": getattr(usage, "completion_tokens", 0),
            "total_tokens": getattr(usage, "total_tokens", 0),
        }
    return {
        "label": label,
        "model": getattr(acct, "model", None),
        "provider": getattr(acct, "provider", None),
        "temperature": getattr(acct, "temperature", None),
        "input_messages": getattr(acct, "messages", None),
        "output": getattr(acct, "output", None),
        "usage": usage_dict,
    }


def save_moa_turn(
    *,
    session_id: Optional[str],
    preset_name: str,
    reference_outputs: list,
    aggregator_label: str,
    aggregator_model: Optional[str],
    aggregator_provider: Optional[str],
    aggregator_temperature: Any,
    aggregator_input_messages: Any,
    aggregator_output: Optional[str],
    aggregator_streamed: bool,
) -> None:
    """把一条完整 MoA 轮次记录追加到该会话的追踪 JSONL（若启用）。

    尽力而为：任何失败都记 debug 并吞掉——追踪绝不能中断实时轮次。每轮在参考缓存
    MISS 时调用一次。

    ``aggregator_output`` 是聚合器合成文本。非流式路径在调用时内联捕获；流式路径事后
    从调用方解析出的 assistant 文本捕获（``consume_and_save_trace`` 的
    ``aggregator_output_fallback``），使追踪在两种模式下都自包含；若解析文本不可得则
    回退 None，记录用 ``output_location`` 指向会话库。
    """
    base = _traces_enabled_and_dir()
    if base is None:
        return
    try:
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{_sanitize_session_id(session_id)}.jsonl"
        _have_output = bool(aggregator_output)
        if not aggregator_streamed:
            _output_location = "inline"
        elif _have_output:
            _output_location = "inline_from_stream"
        else:
            _output_location = "assistant_message_in_session_db"
        record = {
            "ts": time.time(),
            "session_id": session_id,
            "preset": preset_name,
            "references": [
                _slot_trace(acct, label)
                for label, _text, acct in reference_outputs
            ],
            "aggregator": {
                "label": aggregator_label,
                "model": aggregator_model,
                "provider": aggregator_provider,
                "temperature": aggregator_temperature,
                "input_messages": aggregator_input_messages,
                "output": aggregator_output,
                "streamed": aggregator_streamed,
                "output_location": _output_location,
            },
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # pragma: no cover - 追踪绝不能中断轮次
        logger.debug("MoA 追踪写入失败 (session=%s): %s", session_id, exc)


__all__ = ["save_moa_turn"]
