"""流式事件定义 — Agent → 前端的通信协议。

参考 Hermes 的 gateway/stream_events.py 设计：
- 冻结 dataclass，纯数据无行为
- 事件描述"发生了什么"，不涉及"如何投递"
- 前端适配器决定如何渲染每种事件
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Union


# ── 消息事件 ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MessageChunk:
    """流式文本增量 — LLM 正在生成中。"""
    text: str


@dataclass(frozen=True)
class MessageStop:
    """消息段结束。final=True 表示整个 turn 完成。"""
    final: bool = False


@dataclass(frozen=True)
class Commentary:
    """工具调用之间的中间文本（如"让我看看这个文件"）。"""
    text: str


# ── 工具事件 ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolCallStart:
    """工具开始执行。"""
    tool_name: str
    tool_call_id: str
    args: Optional[Dict[str, Any]] = None
    index: int = 0


@dataclass(frozen=True)
class ToolCallResult:
    """工具执行完成。"""
    tool_name: str
    tool_call_id: str
    result: str
    duration: float = 0.0
    ok: bool = True
    index: int = 0


# ── 控制事件 ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StatusUpdate:
    """状态变更通知。"""
    status: str  # "thinking" | "tool_executing" | "idle" | "error"
    message: str = ""


@dataclass(frozen=True)
class ErrorEvent:
    """错误事件。"""
    code: str
    message: str
    details: Optional[Dict[str, Any]] = None


# ── 编辑事件 ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EditProposal:
    """代码编辑提案 — 前端展示 diff 并等待用户确认。

    Agent 通过工具（write_file / patch / insert_at）生成编辑提案，
    VSCode 扩展接收后展示 diff 预览，用户可选择 Accept 或 Reject。
    """
    file_path: str
    original_content: str
    new_content: str
    description: str = ""  # Agent 对修改的说明
    edit_id: str = ""  # 唯一标识，用于接受/拒绝回调


@dataclass(frozen=True)
class EditResult:
    """编辑提案的用户响应。"""
    edit_id: str
    accepted: bool
    message: str = ""  # 用户附加的备注（可选）


# ── 联合类型 ────────────────────────────────────────────────────────────────

StreamEvent = Union[
    MessageChunk,
    MessageStop,
    Commentary,
    ToolCallStart,
    ToolCallResult,
    StatusUpdate,
    ErrorEvent,
    EditProposal,
    EditResult,
]


def event_to_dict(event: StreamEvent) -> Dict[str, Any]:
    """将事件序列化为 WebSocket JSON 格式。"""
    if isinstance(event, MessageChunk):
        return {"type": "message_chunk", "text": event.text}
    elif isinstance(event, MessageStop):
        return {"type": "message_stop", "final": event.final}
    elif isinstance(event, Commentary):
        return {"type": "commentary", "text": event.text}
    elif isinstance(event, ToolCallStart):
        return {
            "type": "tool_call_start",
            "tool_name": event.tool_name,
            "tool_call_id": event.tool_call_id,
            "args": event.args,
            "index": event.index,
        }
    elif isinstance(event, ToolCallResult):
        return {
            "type": "tool_call_result",
            "tool_name": event.tool_name,
            "tool_call_id": event.tool_call_id,
            "result": event.result[:500],  # 截断大结果
            "duration": event.duration,
            "ok": event.ok,
            "index": event.index,
        }
    elif isinstance(event, StatusUpdate):
        return {"type": "status", "status": event.status, "message": event.message}
    elif isinstance(event, ErrorEvent):
        return {
            "type": "error",
            "code": event.code,
            "message": event.message,
            "details": event.details,
        }
    elif isinstance(event, EditProposal):
        return {
            "type": "edit_proposal",
            "file_path": event.file_path,
            "original_content": event.original_content,
            "new_content": event.new_content,
            "description": event.description,
            "edit_id": event.edit_id,
        }
    elif isinstance(event, EditResult):
        return {
            "type": "edit_result",
            "edit_id": event.edit_id,
            "accepted": event.accepted,
            "message": event.message,
        }
    return {"type": "unknown"}


__all__ = [
    "MessageChunk",
    "MessageStop",
    "Commentary",
    "ToolCallStart",
    "ToolCallResult",
    "StatusUpdate",
    "ErrorEvent",
    "EditProposal",
    "EditResult",
    "StreamEvent",
    "event_to_dict",
]
