"""全局 HITL 决策通道 — C-P1.2 决策类型分类 + 跨模块人在环询问。

C-P1.2 落盘
----------
V3 规划 C-P1.2：HITL 决策面板（docs/17 §3 v0.4.0）。

现有 HITL（:mod:`spirit.evolution.hitl` + :mod:`spirit.autonomy.bridge`）只在
**自主探索循环**触发，且决策**无类型区分**——桌宠收到 ``autonomy_decision`` 一律弹
"🤔 探索遇到选择"。主对话循环（工具连续失败）、failover（跨 provider 切换）、
evolution（经验沉淀确认）这些同样需要"问用户拍板"的场景，此前**无通道可用**。

功能
----
建立一个**全局 HITL 请求通道**（对称 :mod:`spirit.agent.progress_notifier` 的全局
sink 模式），让任意模块都能发起带 ``kind`` 分类的询问：

- ``tool_failure``      —— 工具连续失败 > N 次（conversation_loop 护栏 halt 前）
- ``experience_review`` —— 经验沉淀确认（evolution 写记忆前：存 / 跳过 / 改写）
- ``provider_switch``   —— 跨 provider 切换询问（failover 前）
- ``exploration``       —— 自主探索分叉 / 停滞（现有，向后兼容默认值）

事件 schema（复用 bridge 的 ``autonomy_decision`` 广播，新增 ``kind`` 字段）
--------------------------------------------------------------------------
::

    {
        "event": "autonomy_decision",
        "data": {
            "id": 3,
            "kind": "tool_failure",        # ← C-P1.2 新增：决策类型
            "question": "terminal 连续失败 4 次，怎么办？",
            "options": ["继续重试", "换方法", "放弃任务"],
            "context": "最近错误：...",
            "is_fork": false,
            "timeout": 300.0
        }
    }

集成方式
--------
``start_autonomy`` 组装 ``DesktopBridge`` 后，把 ``bridge.ask`` 注册为全局通道::

    from spirit.agent.hitl_channel import register_hitl_channel
    register_hitl_channel(bridge.ask)

任意模块发起询问（同步阻塞，fail-open）::

    from spirit.agent.hitl_channel import DecisionKind, request_decision
    reply = request_decision(
        DecisionKind.TOOL_FAILURE,
        "terminal 连续失败 4 次，怎么办？",
        options=["继续重试", "换方法", "放弃任务"],
    )
    if reply is None:      # 无通道 / 超时 / 异常 → 自主决策（不卡死）
        ...

设计要点
--------
- **fail-open 铁律**：无 channel / 超时 / 任何异常 → 返回 ``None``，调用方自主决策，
  绝不因"问不到人"卡死主循环（与 :class:`~spirit.evolution.hitl.HumanInTheLoop` 一致）。
- **复用而非重造**：通道即 ``DesktopBridge.ask``（已实现广播 + 阻塞等答复 + 超时）；
  查询体即 :class:`~spirit.evolution.hitl.HumanQuery`（本模块只加 ``kind`` 字段）。
- **线程安全**：channel 注册有锁保护；conversation_loop 在 executor 线程发起询问，
  bridge 内部经 ``run_coroutine_threadsafe`` 跨线程广播到事件循环。
- **零 channel 零开销**：未注册通道时 ``request_decision`` 立即返回 ``None``。
"""

from __future__ import annotations

import enum
import logging
import threading
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)

# 询问超时缺省（秒）——与 evolution.hitl.DEFAULT_TIMEOUT 对齐
DEFAULT_DECISION_TIMEOUT = 300.0


# =========================================================================
# 决策类型枚举
# =========================================================================

class DecisionKind(str, enum.Enum):
    """HITL 决策类型（桌宠据此渲染不同气泡文案 / 图标 / 面板标题）。"""

    EXPLORATION = "exploration"
    """自主探索遇到分叉 / 停滞（现有行为，向后兼容默认值）。"""

    TOOL_FAILURE = "tool_failure"
    """工具连续失败超过阈值，问用户：继续重试 / 换方法 / 放弃任务。"""

    EXPERIENCE_REVIEW = "experience_review"
    """经验沉淀确认，问用户：存入记忆 / 跳过 / 改写后再存。"""

    PROVIDER_SWITCH = "provider_switch"
    """跨 provider 切换询问，问用户：切到备用 / 留在当前重试 / 放弃。"""


def parse_kind(raw: Any) -> DecisionKind:
    """把任意输入容错解析为 :class:`DecisionKind`（未知 → EXPLORATION）。"""
    if isinstance(raw, DecisionKind):
        return raw
    text = str(raw or "").strip().lower()
    for member in DecisionKind:
        if member.value == text:
            return member
    return DecisionKind.EXPLORATION


# =========================================================================
# 全局通道注册表
# =========================================================================

# 询问 seam：接收 HumanQuery + timeout，返回答复文本或 None（超时 / 无响应）。
# 与 evolution.hitl.AskFn 同签名 —— DesktopBridge.ask 可直接作为通道。
HitlChannel = Callable[[Any, float], Optional[str]]

_channel: Optional[HitlChannel] = None
_channel_lock = threading.Lock()


def register_hitl_channel(channel: HitlChannel) -> None:
    """注册全局 HITL 通道（真实部署即 ``DesktopBridge.ask``）。

    后注册覆盖先注册（桌宠重启 / 重连时重新绑定到最新 bridge）。
    """
    global _channel
    with _channel_lock:
        _channel = channel
    logger.debug("[C-P1.2] 全局 HITL 通道已注册")


def unregister_hitl_channel(channel: HitlChannel) -> None:
    """反注册通道（仅当当前通道正是它时才清，避免误清后注册者）。"""
    global _channel
    with _channel_lock:
        if _channel is channel:
            _channel = None


def clear_hitl_channel() -> None:
    """无条件清空全局通道。仅供测试 / 关停使用。"""
    global _channel
    with _channel_lock:
        _channel = None


def has_channel() -> bool:
    """当前是否已注册全局 HITL 通道（诊断 / 测试用）。"""
    with _channel_lock:
        return _channel is not None


# =========================================================================
# 主入口：发起一次带类型分类的询问
# =========================================================================

def request_decision(
    kind: DecisionKind | str,
    question: str,
    *,
    options: Optional[List[str]] = None,
    context: str = "",
    confidence: float = 1.0,
    is_fork: bool = False,
    timeout: float = DEFAULT_DECISION_TIMEOUT,
    task_id: str = "",
) -> Optional[str]:
    """发起一次 HITL 询问，阻塞等待用户答复。

    参数
    ----
    kind : DecisionKind | str
        决策类型（桌宠据此分类渲染）。
    question : str
        问题描述。
    options : list[str], optional
        可选项（桌宠渲染为按钮，点选即应答）。
    context : str
        背景说明（如最近错误摘要）。
    confidence : float
        触发方置信度（< 阈值时 AUTO 模式才问；此处默认 1.0，是否问由上游判定）。
    is_fork : bool
        是否路线分叉 / 重大决策。
    timeout : float
        等待答复超时（秒）；超时 → None（fail-open）。
    task_id : str
        关联任务 ID（写入记忆时用）。

    返回
    ----
    Optional[str]
        用户答复文本；**无 channel / 超时 / 异常 → None**（调用方据此 fail-open
        自主决策，绝不卡死）。
    """
    with _channel_lock:
        channel = _channel
    if channel is None:
        # 无通道（桌宠未连 / CLI 场景 / 未注册）→ fail-open
        return None

    try:
        from spirit.evolution.hitl import HumanQuery

        query = HumanQuery(
            question=question,
            options=list(options or []),
            context=context,
            confidence=confidence,
            is_fork=is_fork,
            task_id=task_id,
            kind=parse_kind(kind).value,
        )
    except Exception as exc:  # noqa: BLE001 - 构造失败降级为 fail-open
        logger.debug("[C-P1.2] 构造 HumanQuery 失败（fail-open）: %s", exc)
        return None

    try:
        reply = channel(query, timeout)
    except Exception as exc:  # noqa: BLE001 - 通道异常一律 fail-open
        logger.warning("[C-P1.2] HITL 通道异常 → fail-open: %s", exc)
        return None

    if reply is None or not str(reply).strip():
        # 超时 / 无响应 → fail-open
        return None
    return str(reply).strip()


__all__ = [
    "DecisionKind",
    "HitlChannel",
    "parse_kind",
    "register_hitl_channel",
    "unregister_hitl_channel",
    "clear_hitl_channel",
    "has_channel",
    "request_decision",
    "DEFAULT_DECISION_TIMEOUT",
]
