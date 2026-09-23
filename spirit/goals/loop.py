"""Ralph Loop 外层驱动 — 自主推进一个目标直到 judge 判定完成。

参考 Hermes ``hermes_cli/goals.py`` 的 ``run_kanban_goal_loop``，但通用化：
Hermes 那个绑定 kanban 卡片（worker 用 kanban_complete/kanban_block 终止），
Spirit 没有 kanban，所以这里抽象成一个纯粹的"目标循环驱动器"，通过依赖注入
``run_turn`` 与具体会话/任务执行解耦：

- 会话场景：``run_turn`` = 在同一个 SpiritAgent 上再跑一轮 ``chat``。
- 任务场景：``run_turn`` = 让 ``task/executor`` 再执行一次带续传 prompt 的任务。
- 测试场景：``run_turn`` = 一个返回预设字符串的假函数（无需真实 LLM）。

循环每一步都调 :meth:`GoalManager.evaluate_after_turn`——这就是"任务检查"：
judge 裁决目标是否达成，未达成则喂续传 prompt 再跑一轮，直到 done / paused /
waiting / 硬预算耗尽。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from spirit.goals.judge import _truncate
from spirit.goals.manager import GoalManager

logger = logging.getLogger(__name__)


def run_goal_loop(
    *,
    manager: GoalManager,
    run_turn: Callable[[str], str],
    first_response: str = "",
    background_processes_fn: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    on_decision: Optional[Callable[[Dict[str, Any]], None]] = None,
    log: Optional[Callable[[str], None]] = None,
    hard_max_turns: Optional[int] = None,
) -> Dict[str, Any]:
    """驱动 Ralph Loop，直到目标 done / paused / waiting 或硬预算耗尽。

    调用方已经跑完了"触发目标的那一轮"，其响应即 ``first_response``。从这里起：

    1. 用 judge 评估 ``first_response`` 是否满足目标。
    2. ``continue`` → 取续传 prompt，通过 ``run_turn`` 在**同一会话**再跑一轮，
       回到第 1 步。``done`` / ``waiting`` / ``paused`` → 停止并返回。
    3. ``hard_max_turns`` 是外层硬上限（兜底，防 manager 预算之外的意外无限循环）。

    完全与 CLI/桌宠解耦以便测试：调用方注入 ``run_turn`` (str -> str)。

    Args:
        manager: 已经 ``set()`` 好目标的 GoalManager。
        run_turn: ``(continuation_prompt: str) -> 该轮最终响应文本``。
        first_response: 触发轮已跑完的响应。
        background_processes_fn: 可选，返回实时后台进程快照交给 judge（wait 裁决用）。
        on_decision: 可选，每次 evaluate_after_turn 后回调决策 dict（UI 广播用）。
        log: 可选的日志回调 ``(msg: str) -> None``。
        hard_max_turns: 外层硬上限；默认 manager 预算的 2 倍（最少 40）。

    Returns:
        ``{"outcome", "turns_used", "reason", "last_response"}``，outcome 是
        ``"done"`` / ``"paused"`` / ``"waiting"`` / ``"inactive"`` / ``"budget"`` /
        ``"stopped"`` / ``"error"`` 之一。
    """

    def _log(msg: str) -> None:
        if log is not None:
            try:
                log(msg)
            except Exception:
                pass

    if not manager.is_active():
        return {
            "outcome": "inactive",
            "turns_used": manager.state.turns_used if manager.state else 0,
            "reason": "no active goal",
            "last_response": first_response or "",
        }

    hard_cap = int(hard_max_turns or max(40, manager.default_max_turns * 2))
    if hard_cap < 1:
        hard_cap = 40

    last_response = first_response or ""
    loop_turns = 0

    while True:
        bg = None
        if background_processes_fn is not None:
            try:
                bg = background_processes_fn()
            except Exception as exc:
                _log(f"goal loop: background_processes_fn 失败 ({exc})")
                bg = None

        decision = manager.evaluate_after_turn(
            last_response,
            user_initiated=False,
            background_processes=bg,
        )
        if on_decision is not None:
            try:
                on_decision(decision)
            except Exception:
                _log("goal loop: on_decision 回调失败")

        verdict = decision.get("verdict")
        status = decision.get("status")
        reason = decision.get("reason", "")
        turns_used = manager.state.turns_used if manager.state else loop_turns
        _log(f"goal loop: turn {turns_used} verdict={verdict} reason={_truncate(reason, 120)}")

        if verdict == "done" or status == "done":
            return {"outcome": "done", "turns_used": turns_used, "reason": reason, "last_response": last_response}

        if verdict in ("wait", "waiting"):
            return {"outcome": "waiting", "turns_used": turns_used, "reason": reason, "last_response": last_response}

        if status == "paused":
            # 预算耗尽 或 judge 弱模型连续解析失败 —— manager 已置 paused。
            return {"outcome": "paused", "turns_used": turns_used, "reason": reason, "last_response": last_response}

        if not decision.get("should_continue"):
            return {"outcome": "stopped", "turns_used": turns_used, "reason": reason or "loop stopped", "last_response": last_response}

        prompt = decision.get("continuation_prompt")
        if not prompt:
            return {"outcome": "stopped", "turns_used": turns_used, "reason": "no continuation prompt", "last_response": last_response}

        loop_turns += 1
        if loop_turns >= hard_cap:
            _log(f"goal loop: 触达硬上限 {loop_turns}/{hard_cap}；停止")
            return {"outcome": "budget", "turns_used": turns_used, "reason": f"hard cap {hard_cap} reached", "last_response": last_response}

        # 在同一会话再跑一轮。
        try:
            last_response = run_turn(prompt) or ""
        except Exception as exc:
            _log(f"goal loop: run_turn 失败 ({exc})；停止")
            return {"outcome": "error", "turns_used": turns_used, "reason": f"run_turn error: {type(exc).__name__}", "last_response": last_response}


__all__ = ["run_goal_loop"]
