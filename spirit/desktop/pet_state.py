"""Pet 状态推导 — Agent 活动信号 → PetState 动画状态。

借鉴 Hermes agent/pet/state.py 设计：
- 优先级决策：最高优先级信号胜出
- 单一职责：只负责状态映射，不关心渲染
- 跨表面复用：CLI/TUI/Desktop 共用同一套优先级逻辑

优先级（从高到低）：
1. error          → FAILED  （工具/回合失败）
2. celebrate      → JUMP    （任务全部完成）
3. just_completed → WAVE    （回合正常结束）
4. awaiting_input → WAITING （等待用户输入/审批）
5. tool_running   → RUN     （工具执行中）
6. reasoning      → REVIEW  （模型思考中）
7. busy           → RUN     （有活跃任务）
8. otherwise      → IDLE    （空闲）
"""

from __future__ import annotations

from typing import Any, Iterable

from spirit.desktop.pet_constants import PetState


def todos_all_done(todos: Iterable[Any] | None) -> bool:
    """检查是否所有待办都已完成/取消。

    接受 dict ({"status": ...}) 或带 status 属性的对象。
    """
    items = list(todos or [])
    if not items:
        return False

    def _status(t: Any) -> Any:
        return t.get("status") if isinstance(t, dict) else getattr(t, "status", None)

    return all(_status(t) in ("completed", "cancelled") for t in items)


def derive_pet_state(
    *,
    busy: bool = False,
    awaiting_input: bool = False,
    error: bool = False,
    celebrate: bool = False,
    just_completed: bool = False,
    tool_running: bool = False,
    reasoning: bool = False,
) -> PetState:
    """从 Agent 活动信号推导宠物动画状态。

    每个表面（CLI/TUI/Desktop）将自己跟踪的信号传入，
    此函数按优先级选出最合适的动画状态。

    Args:
        busy: 是否有活跃回合（turn in flight）
        awaiting_input: 是否等待用户输入（clarify/approval）
        error: 是否刚发生错误
        celebrate: 是否触发庆祝（todos 全部完成）
        just_completed: 回合是否刚完成
        tool_running: 工具是否正在执行
        reasoning: 模型是否正在思考

    Returns:
        应该展示的 PetState
    """
    if error:
        return PetState.FAILED
    if celebrate:
        return PetState.JUMP
    if just_completed:
        return PetState.WAVE
    if awaiting_input:
        return PetState.WAITING
    if tool_running:
        return PetState.RUN
    if reasoning:
        return PetState.REVIEW
    if busy:
        return PetState.RUN
    return PetState.IDLE


__all__ = [
    "derive_pet_state",
    "todos_all_done",
]
