"""消息配对不变量 — 保证 OpenAI 兼容 payload 的 tool_call/tool_result 合法性。

背景（v0.3.5 修复）：
上下文压缩把消息列表拼成 ``head + [摘要] + tail``，会把 ``assistant(tool_calls)``
与它对应的 ``role:"tool"`` 结果切到不同区段，产出**服务端直接拒绝**的请求体：

- ``400 invalid params, tool result's tool id(call_3) not found (2013)``
  —— tool 结果找不到父 assistant
- ``400 invalid params, tool call result does not follow tool call (2013)``
  —— assistant 的 tool_calls 没有对应结果

该 400 不匹配任何已知错误模式 → 被归类为 ``unknown``（可重试）→ 重试循环把
**同一个非法 payload 原样重发 3 次**，且坏掉的消息列表已写回 agent 内存，
此后每一轮都复现同一失败，只有重启进程才能恢复。

provider 的实际约束（OpenAI 兼容 / MiniMax 等）：

- 每个 ``role:"tool"`` 消息必须紧跟在声明了同�� ``tool_call_id`` 的 assistant 之后
- assistant 的每个 tool_call 都必须有对应的 tool 结果

本模块提供：

- :func:`validate_tool_pairing` —— 检出违规（发请求前自检 / 写日志）
- :func:`repair_tool_pairing` —— 修复违规（压缩后 / 报错后自愈）

设计约定：
- **纯函数、零 Spirit 依赖** —— 仅用标准库，便于单测与在任意上下文安全导入。
- **绝不抛异常** —— 遇到畸形消息一律保守放行（宁可放行也不要因为校验器崩掉主流程）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Set, Tuple

__all__ = [
    "PairingReport",
    "validate_tool_pairing",
    "repair_tool_pairing",
    "is_valid_tool_pairing",
]


# ── 报告结构 ────────────────────────────────────────────────────────────────

@dataclass
class PairingReport:
    """配对校验结果。

    Attributes:
        orphan_tool_results: ``tool_call_id`` 找不到父 assistant 的 tool 消息 id
        unanswered_tool_calls: 没有对应 tool 结果的 assistant tool_call id
        misordered_tool_results: 未紧随其父 assistant 的 tool 消息 id
        dropped_messages: repair 阶段丢弃的消息条数
        repaired_tool_calls: repair 阶段从 assistant 上摘除的 tool_call 数
    """

    orphan_tool_results: List[str] = field(default_factory=list)
    unanswered_tool_calls: List[str] = field(default_factory=list)
    misordered_tool_results: List[str] = field(default_factory=list)
    dropped_messages: int = 0
    repaired_tool_calls: int = 0

    @property
    def ok(self) -> bool:
        """没有任何违规。"""
        return not (
            self.orphan_tool_results
            or self.unanswered_tool_calls
            or self.misordered_tool_results
        )

    @property
    def issue_count(self) -> int:
        return (
            len(self.orphan_tool_results)
            + len(self.unanswered_tool_calls)
            + len(self.misordered_tool_results)
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "issue_count": self.issue_count,
            "orphan_tool_results": list(self.orphan_tool_results),
            "unanswered_tool_calls": list(self.unanswered_tool_calls),
            "misordered_tool_results": list(self.misordered_tool_results),
            "dropped_messages": self.dropped_messages,
            "repaired_tool_calls": self.repaired_tool_calls,
        }

    def summary(self) -> str:
        """一行摘要（写日志用）。"""
        if self.ok and not self.dropped_messages and not self.repaired_tool_calls:
            return "tool 配对正常"
        return (
            f"tool 配对违规 {self.issue_count} 处 "
            f"(孤儿结果 {len(self.orphan_tool_results)} / "
            f"无应答调用 {len(self.unanswered_tool_calls)} / "
            f"错位 {len(self.misordered_tool_results)}); "
            f"已丢弃消息 {self.dropped_messages} 条, "
            f"已摘除 tool_call {self.repaired_tool_calls} 个"
        )


# ── 内部工具 ────────────────────────────────────────────────────────────────

def _as_id(value: Any) -> str:
    """把 tool_call_id 规范成字符串（None / 非字符串都退化成空串）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _message_tool_call_ids(msg: Dict[str, Any]) -> List[str]:
    """取出一条 assistant 消息携带的 tool_call id 列表（忽略畸形项）。"""
    raw = msg.get("tool_calls")
    if not raw or not isinstance(raw, (list, tuple)):
        return []
    ids: List[str] = []
    for tc in raw:
        if not isinstance(tc, dict):
            continue
        tid = _as_id(tc.get("id"))
        if tid:
            ids.append(tid)
    return ids


def _tool_result_id(msg: Dict[str, Any]) -> str:
    return _as_id(msg.get("tool_call_id"))


def _assistant_has_content(msg: Dict[str, Any]) -> bool:
    """assistant 消息是否还有可见文本（决定能不能降级成纯文本消息）。"""
    content = msg.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, (list, tuple)):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                if (block.get("text") or "").strip():
                    return True
    return False


def _contiguous_tool_block(messages: Sequence[Dict[str, Any]], start: int) -> List[int]:
    """返回 ``messages[start:]`` 中**紧接**的连续 tool 消息下标。

    provider 要求 tool 结果紧随其父 assistant —— 一旦出现非 tool 消息
    （下一条 assistant / user / system），这段 tool 块就结束了。
    """
    block: List[int] = []
    for idx in range(start, len(messages)):
        msg = messages[idx]
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            break
        block.append(idx)
    return block


# ── 校验 ────────────────────────────────────────────────────────────────────

def validate_tool_pairing(messages: Sequence[Dict[str, Any]]) -> PairingReport:
    """校验消息列表的 tool_call / tool_result 配对是否合法。

    检查三类违规：

    1. ``orphan_tool_results`` —— tool 消息的 ``tool_call_id`` 在前面找不到
       对应的 assistant tool_calls
    2. ``unanswered_tool_calls`` —— assistant 的 tool_calls 没有紧跟其后的
       tool 结果
    3. ``misordered_tool_results`` —— tool 消息没有紧跟在其父 assistant 之后
       （父存在，但位置不对）

    Args:
        messages: 内部格式的消息列表（``role`` / ``content`` / ``tool_calls`` /
            ``tool_call_id``）

    Returns:
        :class:`PairingReport`（永不为 None；输入畸形时返回 ok=True）
    """
    report = PairingReport()
    if not messages or not isinstance(messages, (list, tuple)):
        return report

    # 1) 收集 assistant 声明的 tool_call id → 声明位置（首次声明优先）
    declared_at: Dict[str, int] = {}
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tid in _message_tool_call_ids(msg):
            declared_at.setdefault(tid, idx)

    # 2) 每个 assistant 紧跟的 tool 块 → 它实际应答了哪些 id
    answered: Dict[str, Set[str]] = {}   # assistant idx -> 已应答的 id 集合
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        ids = _message_tool_call_ids(msg)
        if not ids:
            continue
        block = _contiguous_tool_block(messages, idx + 1)
        block_ids = {
            _tool_result_id(messages[j]) for j in block
        }
        block_ids.discard("")
        answered[idx] = block_ids

    # 3) assistant 的 tool_call 是否都有结果
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        ids = _message_tool_call_ids(msg)
        if not ids:
            continue
        block_ids = answered.get(idx, set())
        for tid in ids:
            if tid not in block_ids:
                report.unanswered_tool_calls.append(tid)

    # 4) tool 消息：父是否存在 + 位置是否正确
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        tid = _tool_result_id(msg)
        if not tid:
            report.orphan_tool_results.append("<空 tool_call_id>")
            continue
        parent_idx = declared_at.get(tid)
        if parent_idx is None:
            report.orphan_tool_results.append(tid)
            continue
        # 父 assistant 的紧邻 tool 块里必须有它
        if idx not in _contiguous_tool_block(messages, parent_idx + 1):
            report.misordered_tool_results.append(tid)

    return report


def is_valid_tool_pairing(messages: Sequence[Dict[str, Any]]) -> bool:
    """消息列表的 tool 配对是否合法（便捷判定）。"""
    return validate_tool_pairing(messages).ok


# ── 修复 ────────────────────────────────────────────────────────────────────

def repair_tool_pairing(
    messages: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], PairingReport]:
    """修复消息列表的 tool 配对违规，返回 ``(修复后的列表, 报告)``。

    修复策略（保守，优先保证 payload 可被 provider 接受）：

    1. **孤儿 / 错位的 tool 结果** —— 父 assistant 已被裁掉、或位置不对的
       tool 消息直接丢弃
    2. **无应答的 assistant tool_calls** —— 从 assistant 上摘除这些 tool_call；
       若摘除后该 assistant 没有任何 tool_call 且也没有可见文本，整条丢弃
       （否则降级成纯文本 assistant 消息）

    消息的其余字段（``role`` / ``content`` / 扩展元数据）原样保留。

    Args:
        messages: 内部格式的消息列表

    Returns:
        ``(new_messages, report)``；输入不是列表时原样返回并给出空报告。
    """
    if not messages or not isinstance(messages, (list, tuple)):
        return (list(messages or []), PairingReport())

    before = validate_tool_pairing(messages)

    # 每个 tool_call id 是否有一个"紧跟其父"的 tool 结果
    answered_after: Dict[str, bool] = {}
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        ids = _message_tool_call_ids(msg)
        if not ids:
            continue
        block_ids = {
            _tool_result_id(messages[j])
            for j in _contiguous_tool_block(messages, idx + 1)
        }
        block_ids.discard("")
        for tid in ids:
            answered_after[tid] = tid in block_ids

    repaired: List[Dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            repaired.append(msg)
            continue

        role = msg.get("role")

        # ── 1. tool 消息：父不存在 / 位置不对 → 丢弃 ──
        if role == "tool":
            tid = _tool_result_id(msg)
            if not tid or not answered_after.get(tid, False):
                before.dropped_messages += 1
                continue
            repaired.append(msg)
            continue

        # ── 2. assistant：摘除没有结果的 tool_calls ──
        if role == "assistant" and msg.get("tool_calls"):
            keep: List[Any] = []
            for tc in msg["tool_calls"]:
                tid = _as_id(tc.get("id")) if isinstance(tc, dict) else ""
                if tid and answered_after.get(tid, False):
                    keep.append(tc)
                else:
                    before.repaired_tool_calls += 1
            if len(keep) == len(msg["tool_calls"]):
                repaired.append(msg)
                continue
            if keep:
                new_msg = dict(msg)
                new_msg["tool_calls"] = keep
                repaired.append(new_msg)
                continue
            # 一个 tool_call 都不剩 → 降级成纯文本 assistant，或整条丢弃
            if _assistant_has_content(msg):
                new_msg = dict(msg)
                new_msg.pop("tool_calls", None)
                repaired.append(new_msg)
                continue
            before.dropped_messages += 1
            continue

        repaired.append(msg)

    # 修复后的状态：违规应清零，但保留「丢弃/摘除」的计数
    final = validate_tool_pairing(repaired)
    final.dropped_messages = before.dropped_messages
    final.repaired_tool_calls = before.repaired_tool_calls
    return (repaired, final)
