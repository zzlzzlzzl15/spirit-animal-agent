"""流式输出支持 — Spirit Agent 的实时文本推送。

参考 Hermes 的 stream_single_writer.py + chat_completion_helpers.py 的流式部分。
提供：
- StreamingHandler: 流式 API 调用处理器
- StreamCollector: 流式内容收集器
- 单写者围栏（防止过期流覆盖新流）
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# =========================================================================
# 流式数据类
# =========================================================================

@dataclass
class StreamDelta:
    """流式增量片段。"""

    content: str = ""
    tool_calls_delta: List[Dict[str, Any]] = field(default_factory=list)
    finish_reason: Optional[str] = None
    role: Optional[str] = None

    # 元数据
    timestamp: float = field(default_factory=time.time)
    is_final: bool = False


@dataclass
class StreamResult:
    """完整流式结果。"""

    content: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: Dict[str, int] = field(default_factory=dict)

    # 诊断
    first_token_time: float = 0.0
    last_token_time: float = 0.0
    total_tokens: int = 0


# =========================================================================
# 单写者围栏
# =========================================================================

class StreamWriterFence:
    """单写者围栏 — 确保同一时刻只有一个流在写入。

    当新的流式尝试开始时，它声明自己为当前写者。
    旧的流在下次写入时会发现自己不再是当前写者，自动停止。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._token = 0

    def claim(self) -> int:
        """声明为当前写者。返回 token。"""
        with self._lock:
            self._token += 1
            return self._token

    def is_current(self, token: int) -> bool:
        """检查给定 token 是否仍然是当前写者。"""
        if not token:
            return True  # 无围栏模式
        with self._lock:
            return self._token == token


# =========================================================================
# 流式内容收集器
# =========================================================================

class StreamCollector:
    """收集流式增量并组装为完整结果。

    处理：
    - 文本内容增量拼接
    - 工具调用增量组装（按 index 分组）
    - finish_reason 提取
    - 可选的 delta 回调
    """

    def __init__(self, delta_callback: Optional[Callable[[StreamDelta], None]] = None):
        self._content_parts: List[str] = []
        self._tool_call_buffers: Dict[int, Dict[str, Any]] = {}
        self._finish_reason: Optional[str] = None
        self._delta_callback = delta_callback
        self._start_time = time.time()
        self._first_token_time = 0.0
        self._last_token_time = 0.0

    def feed(self, delta: StreamDelta) -> None:
        """处理一个流式增量。"""
        now = time.time()

        if self._first_token_time == 0.0 and (delta.content or delta.tool_calls_delta):
            self._first_token_time = now

        self._last_token_time = now

        # 文本内容
        if delta.content:
            self._content_parts.append(delta.content)

        # 工具调用增量
        for tc_delta in delta.tool_calls_delta:
            idx = tc_delta.get("index", 0)
            if idx not in self._tool_call_buffers:
                self._tool_call_buffers[idx] = {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                }

            buf = self._tool_call_buffers[idx]

            # 增量更新
            if tc_delta.get("id"):
                buf["id"] = tc_delta["id"]

            func_delta = tc_delta.get("function", {})
            if func_delta.get("name"):
                buf["function"]["name"] += func_delta["name"]
            if func_delta.get("arguments"):
                buf["function"]["arguments"] += func_delta["arguments"]

        # 结束原因
        if delta.finish_reason:
            self._finish_reason = delta.finish_reason

        # 回调
        if self._delta_callback:
            try:
                self._delta_callback(delta)
            except Exception as exc:
                logger.debug("流式回调异常: %s", exc)

    def build_result(self) -> StreamResult:
        """组装完整结果。"""
        # 工具调用按 index 排序
        tool_calls = []
        for idx in sorted(self._tool_call_buffers.keys()):
            tool_calls.append(self._tool_call_buffers[idx])

        return StreamResult(
            content="".join(self._content_parts),
            tool_calls=tool_calls,
            finish_reason=self._finish_reason or "stop",
            first_token_time=self._first_token_time,
            last_token_time=self._last_token_time,
        )


# =========================================================================
# 流式处理器
# =========================================================================

class StreamingHandler:
    """流式 API 调用处理器。

    用法：
        handler = StreamingHandler(agent)
        result = handler.stream_chat(api_messages, tools=tool_defs)
    """

    def __init__(
        self,
        agent: Any,
        *,
        delta_callback: Optional[Callable[[StreamDelta], None]] = None,
    ):
        self._agent = agent
        self._delta_callback = delta_callback or getattr(agent, "on_stream_delta", None)
        self._fence = StreamWriterFence()

    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        tools: Optional[List[dict]] = None,
        model: Optional[str] = None,
    ) -> StreamResult:
        """执行流式聊天补全。

        Args:
            messages: API 格式的消息列表
            tools: 工具定义列表
            model: 模型名称（覆盖 agent 默认）

        Returns:
            StreamResult: 完整结果
        """
        agent = self._agent
        writer_token = self._fence.claim()

        collector = StreamCollector(delta_callback=self._delta_callback)

        request_kwargs = {
            "model": model or agent.model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            request_kwargs["tools"] = tools

        try:
            stream = agent.client.chat.completions.create(**request_kwargs)

            for chunk in stream:
                # 检查是否被新流取代
                if not self._fence.is_current(writer_token):
                    logger.debug("流被新写者取代，停止接收")
                    break

                if not chunk.choices:
                    continue

                choice = chunk.choices[0]
                delta = choice.delta

                if delta is None:
                    continue

                # 解析增量
                stream_delta = StreamDelta(
                    content=getattr(delta, "content", None) or "",
                    finish_reason=choice.finish_reason,
                )

                # 工具调用增量
                if hasattr(delta, "tool_calls") and delta.tool_calls:
                    for tc in delta.tool_calls:
                        tc_delta = {"index": tc.index}
                        if hasattr(tc, "id") and tc.id:
                            tc_delta["id"] = tc.id
                        if hasattr(tc, "function") and tc.function:
                            func = {}
                            if hasattr(tc.function, "name") and tc.function.name:
                                func["name"] = tc.function.name
                            if hasattr(tc.function, "arguments") and tc.function.arguments:
                                func["arguments"] = tc.function.arguments
                            tc_delta["function"] = func
                        stream_delta.tool_calls_delta.append(tc_delta)

                collector.feed(stream_delta)

                # 结束
                if choice.finish_reason:
                    break

        except Exception as exc:
            logger.error("流式 API 调用失败: %s", exc)
            raise

        result = collector.build_result()

        # 提取 usage（如果提供者支持）
        if hasattr(stream, "get") and callable(stream.get):
            usage = stream.get("usage")
            if usage:
                result.usage = dict(usage)

        return result


# =========================================================================
# 非流式到流式的兼容层
# =========================================================================

def create_mock_stream(response) -> List[StreamDelta]:
    """将非流式响应转换为 StreamDelta 列表（兼容层）。"""
    choice = response.choices[0] if response.choices else None
    if not choice:
        return [StreamDelta(is_final=True)]

    message = choice.message
    deltas = []

    # 内容
    if message.content:
        deltas.append(StreamDelta(content=message.content))

    # 工具调用
    if message.tool_calls:
        for i, tc in enumerate(message.tool_calls):
            deltas.append(StreamDelta(
                tool_calls_delta=[{
                    "index": i,
                    "id": tc.id,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }],
            ))

    # 结束
    deltas.append(StreamDelta(
        finish_reason=choice.finish_reason or "stop",
        is_final=True,
    ))

    return deltas


__all__ = [
    "StreamDelta",
    "StreamResult",
    "StreamWriterFence",
    "StreamCollector",
    "StreamingHandler",
    "create_mock_stream",
]
