"""传输无关的 /goal · /subgoal 命令分发层。

参考 Hermes ``hermes_cli/cli_commands_mixin.py`` 的 ``_handle_goal_command`` /
``_handle_subgoal_command``，但把"解析 + 派发 + 结果"从具体 UI（CLI 的 Rich
console、桌宠的 WebSocket）里剥离出来：这里只吃一个 ``agent`` 和一段 ``arg``
字符串，吐一个结构化 dict。CLI 与 ws_server 各自把 dict 渲染成自己的形式。

这样做有两个好处：

1. **可测试**——命令解析/状态流转无需真实终端或事件循环即可单测（对齐用户
   "和 Hermes 一样的任务检查测试用例"的要求）。
2. **单一事实源**——set/pause/resume/clear/draft/show/wait/subgoal 的语义只在
   这里定义一次，CLI 和桌宠不会各写一套而漂移。

返回 dict 的约定键::

    ok          bool        命令是否成功执行（未知子命令/参数错误 → False）
    action      str         归一化后的动作名（set/status/pause/...）
    message     str         一行主消息（给用户看）
    lines       List[str]   多行输出（契约块、子目标列表等）
    status_line str         当前目标的一行状态（便于 UI 常驻显示）
    kick_off    str|None    新设目标时，需要立刻跑第一轮的目标文本（否则 None）

``kick_off`` 是把 Hermes "设完目标立即启动循环" 的行为交给调用方：CLI 直接在
当前进程驱动 :func:`run_goal_turn_loop`；ws_server 可以选择在 executor 里跑或
让前端下一条消息触发。命令层本身不阻塞、不调 LLM（draft 除外，它显式要 LLM）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from spirit.goals.goal_state import parse_contract
from spirit.goals.judge import draft_contract
from spirit.goals.loop import run_goal_loop

logger = logging.getLogger(__name__)


def _result(
    ok: bool,
    action: str,
    message: str = "",
    *,
    lines: Optional[List[str]] = None,
    status_line: str = "",
    kick_off: Optional[str] = None,
) -> Dict[str, Any]:
    """构造统一的结果 dict。"""
    return {
        "ok": ok,
        "action": action,
        "message": message,
        "lines": lines or [],
        "status_line": status_line,
        "kick_off": kick_off,
    }


def get_goal_manager(agent) -> Optional[Any]:
    """从 agent 取 GoalManager；不可用（无 agent / 无属性）返回 None。"""
    if agent is None:
        return None
    try:
        return agent.goal_manager
    except Exception as exc:  # pragma: no cover - 防御
        logger.debug("commands: 取 goal_manager 失败: %s", exc)
        return None


# ──────────────────────────────────────────────────────────────────────
# /goal
# ──────────────────────────────────────────────────────────────────────

def handle_goal_command(agent, arg: str) -> Dict[str, Any]:
    """派发 ``/goal`` 子命令。

    形式（对齐 Hermes）::

        /goal                     显示当前状态
        /goal status              同上
        /goal show                状态 + 完成契约
        /goal draft <objective>   把大白话扩成结构化契约并设为目标
        /goal pause               暂停
        /goal resume              恢复（重置 turn 预算）
        /goal clear|stop|done     清除
        /goal wait <pid> [reason] 把循环泊在一个后台进程 PID 上
        /goal unwait              清除等待屏障
        /goal <text>              设为新目标（inline `field: value` 行解析为契约）

    Args:
        agent: 持有 ``goal_manager`` property 的 SpiritAgent。
        arg: ``/goal`` 之后的整段参数（可为空）。

    Returns:
        结构化结果 dict（见模块 docstring）。
    """
    mgr = get_goal_manager(agent)
    if mgr is None:
        return _result(False, "unavailable", "目标系统不可用（无活跃 Agent 会话）。")

    arg = (arg or "").strip()
    lower = arg.lower()

    # 裸 /goal 或 /goal status → 显示当前状态
    if not arg or lower == "status":
        return _result(True, "status", mgr.status_line(), status_line=mgr.status_line())

    # /goal show → 状态 + 完成契约
    if lower == "show":
        return _result(
            True,
            "show",
            mgr.status_line(),
            lines=[mgr.render_contract()],
            status_line=mgr.status_line(),
        )

    # /goal draft <objective> → 用辅助模型把大白话扩成结构化契约
    if lower == "draft" or lower.startswith("draft "):
        objective = arg[len("draft"):].strip()
        if not objective:
            return _result(False, "draft", "用法: /goal draft <用大白话描述的目标>")
        return _handle_goal_draft(mgr, objective)

    # /goal pause
    if lower == "pause":
        state = mgr.pause(reason="user-paused")
        if state is None:
            return _result(False, "pause", "没有已设置的目标。", status_line=mgr.status_line())
        return _result(True, "pause", f"⏸ 目标已暂停: {state.goal}", status_line=mgr.status_line())

    # /goal resume
    if lower == "resume":
        state = mgr.resume()
        if state is None:
            return _result(False, "resume", "没有可恢复的目标。", status_line=mgr.status_line())
        return _result(
            True,
            "resume",
            f"▶ 目标已恢复: {state.goal}",
            status_line=mgr.status_line(),
            kick_off=state.goal,  # 恢复后立即续跑
        )

    # /goal clear|stop|done
    if lower in {"clear", "stop", "done"}:
        had = mgr.has_goal()
        mgr.clear()
        if had:
            return _result(True, "clear", "✓ 目标已清除。", status_line=mgr.status_line())
        return _result(False, "clear", "没有活跃目标。", status_line=mgr.status_line())

    # /goal wait <pid> [reason] — 把循环泊在后台进程上，PID 退出后自动清除
    if lower == "wait" or lower.startswith("wait "):
        wait_arg = arg[len("wait"):].strip()
        if not wait_arg:
            return _result(False, "wait", "用法: /goal wait <pid> [reason]")
        wtokens = wait_arg.split(None, 1)
        try:
            pid = int(wtokens[0])
        except ValueError:
            return _result(False, "wait", "/goal wait: <pid> 必须是整数进程 id。")
        reason = wtokens[1].strip() if len(wtokens) > 1 else ""
        try:
            mgr.wait_on(pid, reason=reason)
        except (RuntimeError, ValueError) as exc:
            return _result(False, "wait", f"/goal wait: {exc}", status_line=mgr.status_line())
        rtxt = f"（{reason}）" if reason else ""
        return _result(
            True,
            "wait",
            f"⏳ 目标已泊在 pid {pid}{rtxt}。循环暂停直到它退出。",
            status_line=mgr.status_line(),
        )

    # /goal unwait — 清除等待屏障
    if lower == "unwait":
        if mgr.stop_waiting():
            return _result(True, "unwait", "▶ 等待屏障已清除——目标循环恢复。", status_line=mgr.status_line())
        return _result(False, "unwait", "没有设置等待屏障。", status_line=mgr.status_line())

    # 其余：把 arg 当作目标文本。inline `field: value` 行解析为完成契约，
    # 剩下的散文是 headline。无这类行的纯自由目标行为与从前完全一致。
    headline, contract = parse_contract(arg)
    goal_text = headline or arg
    try:
        state = mgr.set(goal_text, contract=contract if not contract.is_empty() else None)
    except ValueError as exc:
        return _result(False, "set", f"无效目标: {exc}")

    lines: List[str] = []
    if state.has_contract():
        lines.append("完成契约:")
        lines.extend(f"  {ln}" for ln in state.contract.render_block().splitlines())
    message = f"⊙ 目标已设置（{state.max_turns} 轮预算）: {state.goal}"
    return _result(
        True,
        "set",
        message,
        lines=lines,
        status_line=mgr.status_line(),
        kick_off=state.goal,  # 设完立即启动循环（对齐 Hermes）
    )


def _handle_goal_draft(mgr, objective: str) -> Dict[str, Any]:
    """用辅助模型把大白话目标扩成结构化完成契约并设为目标。

    辅助模型不可用/无法产出契约时，回退为裸自由目标——缺失或弱的辅助模型绝不
    阻塞设目标（对齐 Hermes fail-open 原则）。
    """
    llm_caller = getattr(mgr, "_llm_caller", None)
    contract = None
    if llm_caller is not None:
        try:
            contract = draft_contract(objective, llm_caller=llm_caller)
        except Exception as exc:
            logger.debug("goal draft 失败: %s", exc)
            contract = None

    try:
        state = mgr.set(objective, contract=contract)
    except ValueError as exc:
        return _result(False, "draft", f"无效目标: {exc}")

    lines: List[str] = []
    if state.has_contract():
        lines.append("起草的完成契约:")
        lines.extend(f"  {ln}" for ln in state.contract.render_block().splitlines())
        lines.append("  可用 inline 行（如 verify: <命令>）收紧任一字段，再 /goal resume。")
    else:
        lines.append("  无法起草契约（辅助模型不可用）——作为自由目标运行，每轮 judge 仍生效。")
    return _result(
        True,
        "draft",
        f"⊙ 目标已设置（{state.max_turns} 轮预算）: {state.goal}",
        lines=lines,
        status_line=mgr.status_line(),
        kick_off=state.goal,
    )


# ──────────────────────────────────────────────────────────────────────
# /subgoal
# ──────────────────────────────────────────────────────────────────────

def handle_subgoal_command(agent, arg: str) -> Dict[str, Any]:
    """派发 ``/subgoal`` 子命令（对齐 Hermes）。

    形式::

        /subgoal                  显示当前子目标
        /subgoal <text>           追加一条验收标准
        /subgoal remove <n>       移除第 n 条（1-based）
        /subgoal clear            清空所有子目标

    子目标是用户中途追加的额外标准，会在下一个轮次边界同时并入 judge prompt
    （裁决须考虑它们）和续传 prompt（Agent 能看到它们）。
    """
    mgr = get_goal_manager(agent)
    if mgr is None:
        return _result(False, "unavailable", "目标系统不可用（无活跃 Agent 会话）。")
    if not mgr.has_goal():
        return _result(False, "no_goal", "没有活跃目标。用 /goal <text> 设一个。", status_line=mgr.status_line())

    arg = (arg or "").strip()

    # 无参 → 列出当前子目标
    if not arg:
        return _result(
            True,
            "list",
            mgr.status_line(),
            lines=[mgr.render_subgoals()],
            status_line=mgr.status_line(),
        )

    tokens = arg.split(None, 1)
    verb = tokens[0].lower()
    rest = tokens[1].strip() if len(tokens) > 1 else ""

    if verb == "remove":
        if not rest:
            return _result(False, "remove", "用法: /subgoal remove <n>")
        try:
            idx = int(rest.split()[0])
        except ValueError:
            return _result(False, "remove", "/subgoal remove: <n> 必须是整数（1-based 索引）。")
        try:
            removed = mgr.remove_subgoal(idx)
        except (IndexError, RuntimeError) as exc:
            return _result(False, "remove", f"/subgoal remove: {exc}", status_line=mgr.status_line())
        return _result(True, "remove", f"✓ 已移除子目标 {idx}: {removed}", status_line=mgr.status_line())

    if verb == "clear":
        try:
            prev = mgr.clear_subgoals()
        except RuntimeError as exc:
            return _result(False, "clear", f"/subgoal clear: {exc}", status_line=mgr.status_line())
        if prev:
            return _result(True, "clear", f"✓ 已清除 {prev} 条子目标。", status_line=mgr.status_line())
        return _result(False, "clear", "没有可清除的子目标。", status_line=mgr.status_line())

    # 其余 —— 把整段 arg 作为新子目标追加
    try:
        text = mgr.add_subgoal(arg)
    except (ValueError, RuntimeError) as exc:
        return _result(False, "add", f"/subgoal: {exc}", status_line=mgr.status_line())
    idx = len(mgr.state.subgoals) if mgr.state else 0
    return _result(True, "add", f"✓ 已添加子目标 {idx}: {text}", status_line=mgr.status_line())


# ──────────────────────────────────────────────────────────────────────
# Ralph Loop 驱动（CLI / ws_server 共用）
# ──────────────────────────────────────────────────────────────────────

def run_goal_turn_loop(
    agent,
    first_input: str,
    *,
    on_turn: Optional[Callable[[str, int], None]] = None,
    on_decision: Optional[Callable[[Dict[str, Any]], None]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """跑触发目标的第一轮，然后驱动 Ralph Loop 直到 done/paused/waiting/预算耗尽。

    这是把 :func:`spirit.goals.loop.run_goal_loop` 接到 SpiritAgent 上的胶水：
    ``run_turn`` = 在**同一会话**上再跑一轮 ``agent.chat``。每轮响应通过
    ``on_turn(response, turn_index)`` 回调出去（CLI 打印 / ws_server 广播）。

    无活跃目标时，退化为"只跑一轮"（等价于普通 chat），outcome=``"inactive"``。

    Args:
        agent: SpiritAgent（须有 ``chat(message) -> dict`` 与 ``goal_manager``）。
        first_input: 触发轮的用户输入（通常是刚设的目标文本）。
        on_turn: 可选 ``(response_text, turn_index) -> None``，每轮响应回调。
        on_decision: 可选 ``(decision_dict) -> None``，每次 judge 裁决后回调。
        log: 可选 ``(msg) -> None`` 日志回调。

    Returns:
        ``run_goal_loop`` 的结果 dict，外加 ``"responses"``（每轮响应文本列表）。
    """
    mgr = get_goal_manager(agent)
    responses: List[str] = []

    def _run_turn(prompt: str) -> str:
        result = agent.chat(prompt)
        text = result.get("response", "") if isinstance(result, dict) else str(result or "")
        responses.append(text)
        if on_turn is not None:
            try:
                on_turn(text, len(responses))
            except Exception:
                pass
        return text

    # 无目标 → 只跑一轮，行为等价普通 chat。
    if mgr is None or not mgr.is_active():
        first_response = _run_turn(first_input)
        return {
            "outcome": "inactive",
            "turns_used": 0,
            "reason": "no active goal",
            "last_response": first_response,
            "responses": responses,
        }

    # 第一轮：用触发输入（目标文本）跑，之后由 loop 喂续传 prompt。
    first_response = _run_turn(first_input)

    def _on_decision(decision: Dict[str, Any]) -> None:
        if on_decision is not None:
            try:
                on_decision(decision)
            except Exception:
                pass

    result = run_goal_loop(
        manager=mgr,
        run_turn=_run_turn,
        first_response=first_response,
        on_decision=_on_decision,
        log=log,
    )
    result["responses"] = responses
    return result


__all__ = [
    "get_goal_manager",
    "handle_goal_command",
    "handle_subgoal_command",
    "run_goal_turn_loop",
]
