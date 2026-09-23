"""GoalManager — 每会话的目标状态 + 续传决策（Ralph Loop 的大脑）。

参考 Hermes ``hermes_cli/goals.py`` 的 GoalManager，逐分支对齐其
``evaluate_after_turn`` 决策逻辑，但做两处 Spirit 适配：

1. 持久化通过注入的 :class:`~spirit.goals.store.GoalStore`（默认 SessionDB），
   而非 Hermes 的模块级 ``_get_session_db()``——解耦、可测试。
2. judge 的 LLM 调用通过注入的 ``llm_caller``（Spirit 无 auxiliary_client），
   由 :func:`spirit.goals.judge.build_agent_llm_caller` 从 SpiritAgent 构造。

CLI / gateway / 桌宠各自为每个活跃会话持有一个 GoalManager。核心方法：

- ``set(goal)`` — 开启一个新的常设目标。
- ``clear()`` / ``pause()`` / ``resume()`` — 用户控制。
- ``status_line()`` — 可打印的一行状态。
- ``evaluate_after_turn(last_response)`` — 调 judge、更新状态、返回一个决策 dict
  供调用方驱动下一轮。
- ``next_continuation_prompt()`` — 喂回 ``run_conversation`` 的规范 user-role 消息。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from spirit.goals.goal_state import (
    DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES,
    DEFAULT_MAX_TURNS,
    GoalContract,
    GoalState,
)
from spirit.goals.judge import _pid_alive, _session_waiting, judge_goal
from spirit.goals.prompts import (
    CONTINUATION_PROMPT_TEMPLATE,
    CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE,
    CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE,
)
from spirit.goals.store import GoalStore, SessionDBGoalStore

logger = logging.getLogger(__name__)


def _config_max_turns() -> int:
    """读 goals.max_turns 配置，回退到 DEFAULT_MAX_TURNS。"""
    try:
        from spirit.config import get_config_value

        v = int(get_config_value("goals.max_turns", DEFAULT_MAX_TURNS))
        if v > 0:
            return v
    except Exception:
        pass
    return DEFAULT_MAX_TURNS


class GoalManager:
    """每会话目标状态 + 续传决策。"""

    def __init__(
        self,
        session_id: str,
        *,
        default_max_turns: Optional[int] = None,
        store: Optional[GoalStore] = None,
        llm_caller: Optional[Any] = None,
    ):
        self.session_id = session_id
        if default_max_turns is None:
            default_max_turns = _config_max_turns()
        self.default_max_turns = int(default_max_turns or DEFAULT_MAX_TURNS)
        self._store: GoalStore = store if store is not None else SessionDBGoalStore()
        self._llm_caller = llm_caller
        self._state: Optional[GoalState] = self._store.get(session_id)

    # --- 内省 ---------------------------------------------------------

    @property
    def state(self) -> Optional[GoalState]:
        return self._state

    @property
    def store(self) -> GoalStore:
        return self._store

    def is_active(self) -> bool:
        return self._state is not None and self._state.status == "active"

    def has_goal(self) -> bool:
        return self._state is not None and self._state.status in {"active", "paused"}

    def has_contract(self) -> bool:
        return self._state is not None and self._state.has_contract()

    def status_line(self) -> str:
        s = self._state
        if s is None or s.status in {"cleared"}:
            return "No active goal. Set one with /goal <text>."
        turns = f"{s.turns_used}/{s.max_turns} turns"
        sub = f", {len(s.subgoals)} subgoal{'s' if len(s.subgoals) != 1 else ''}" if s.subgoals else ""
        con = ", contract" if self.has_contract() else ""
        meta = f"{turns}{sub}{con}"
        if s.status == "active":
            if s.waiting_on_session and _session_waiting(s.waiting_on_session):
                wr = s.waiting_reason or f"session {s.waiting_on_session}"
                return f"⏳ Goal (parked on {wr}, {meta}): {s.goal}"
            if s.waiting_on_pid and _pid_alive(s.waiting_on_pid):
                wr = s.waiting_reason or f"pid {s.waiting_on_pid}"
                return f"⏳ Goal (parked on {wr}, {meta}): {s.goal}"
            if s.waiting_until and time.time() < s.waiting_until:
                remaining = int(s.waiting_until - time.time())
                wr = s.waiting_reason or f"{remaining}s"
                return f"⏳ Goal (parked {remaining}s — {wr}, {meta}): {s.goal}"
            return f"⊙ Goal (active, {meta}): {s.goal}"
        if s.status == "paused":
            extra = f" — {s.paused_reason}" if s.paused_reason else ""
            return f"⏸ Goal (paused, {meta}{extra}): {s.goal}"
        if s.status == "done":
            return f"✓ Goal done ({meta}): {s.goal}"
        return f"Goal ({s.status}, {meta}): {s.goal}"

    # --- 变更 ---------------------------------------------------------

    def set(
        self,
        goal: str,
        *,
        max_turns: Optional[int] = None,
        contract: Optional[GoalContract] = None,
    ) -> GoalState:
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("goal text is empty")
        state = GoalState(
            goal=goal,
            status="active",
            turns_used=0,
            max_turns=int(max_turns) if max_turns else self.default_max_turns,
            created_at=time.time(),
            last_turn_at=0.0,
            contract=contract if contract is not None else GoalContract(),
        )
        self._state = state
        self._store.save(self.session_id, state)
        return state

    def set_contract(self, contract: GoalContract) -> Optional[GoalState]:
        """给活跃目标附加/替换完成契约。无目标可附时返回 None。"""
        if self._state is None:
            return None
        self._state.contract = contract or GoalContract()
        self._store.save(self.session_id, self._state)
        return self._state

    def pause(self, reason: str = "user-paused") -> Optional[GoalState]:
        if not self._state:
            return None
        self._state.status = "paused"
        self._state.paused_reason = reason
        # 暂停后等待屏障无意义 —— 丢弃。
        self._clear_barrier_fields()
        self._store.save(self.session_id, self._state)
        return self._state

    def resume(self, *, reset_budget: bool = True) -> Optional[GoalState]:
        if not self._state:
            return None
        self._state.status = "active"
        self._state.paused_reason = None
        # 恢复即重新开始 —— 清掉任何陈旧屏障。
        self._clear_barrier_fields()
        if reset_budget:
            self._state.turns_used = 0
        self._store.save(self.session_id, self._state)
        return self._state

    def clear(self) -> None:
        if self._state is None:
            return
        self._state.status = "cleared"
        self._store.save(self.session_id, self._state)
        self._state = None

    def mark_done(self, reason: str) -> None:
        if not self._state:
            return
        self._state.status = "done"
        self._state.last_verdict = "done"
        self._state.last_reason = reason
        self._store.save(self.session_id, self._state)

    def _clear_barrier_fields(self) -> None:
        """清空所有 wait barrier 字段（pause/resume/stop_waiting 共用）。"""
        if self._state is None:
            return
        self._state.waiting_on_pid = None
        self._state.waiting_on_session = None
        self._state.waiting_until = 0.0
        self._state.waiting_reason = None
        self._state.waiting_since = 0.0

    # --- /subgoal 用户控制 --------------------------------------------

    def add_subgoal(self, text: str) -> str:
        """给活跃目标追加一条用户标准。需 has_goal()，否则抛 RuntimeError。

        返回清洗后的文本，供调用方回显给用户。
        """
        if self._state is None or not self.has_goal():
            raise RuntimeError("no active goal")
        text = (text or "").strip()
        if not text:
            raise ValueError("subgoal text is empty")
        self._state.subgoals.append(text)
        self._store.save(self.session_id, self._state)
        return text

    def remove_subgoal(self, index_1based: int) -> str:
        """按 1-based 索引移除子目标。返回被移除的文本。"""
        if self._state is None or not self.has_goal():
            raise RuntimeError("no active goal")
        idx = int(index_1based) - 1
        if idx < 0 or idx >= len(self._state.subgoals):
            raise IndexError(f"index out of range (1..{len(self._state.subgoals)})")
        removed = self._state.subgoals.pop(idx)
        self._store.save(self.session_id, self._state)
        return removed

    def clear_subgoals(self) -> int:
        """清空所有子目标。返回此前的数量。"""
        if self._state is None or not self.has_goal():
            raise RuntimeError("no active goal")
        prev = len(self._state.subgoals)
        self._state.subgoals = []
        self._store.save(self.session_id, self._state)
        return prev

    def render_subgoals(self) -> str:
        """/subgoal slash 命令的公共辅助。"""
        if self._state is None:
            return "(no active goal)"
        if not self._state.subgoals:
            return "(no subgoals — use /subgoal <text> to add criteria)"
        return self._state.render_subgoals_block()

    # --- /goal wait 屏障 ----------------------------------------------

    def wait_on(self, pid: int, reason: str = "") -> GoalState:
        """把目标循环泊在一个后台进程 PID 上。

        PID 存活期间 ``evaluate_after_turn`` 返回 ``should_continue=False``，不烧
        turn、不调 judge——循环静默而非把 Agent 反复戳去做无用功。进程退出后屏障
        自动清除。需要活跃目标。带 watch_patterns/notify_on_complete 触发器的进程
        优先用 ``wait_on_session``，好让中途触发器（不只是退出）也能释放屏障。
        """
        if self._state is None or self._state.status != "active":
            raise RuntimeError("no active goal to park")
        pid = int(pid)
        if pid <= 0:
            raise ValueError("pid must be a positive integer")
        self._state.waiting_on_pid = pid
        self._state.waiting_on_session = None
        self._state.waiting_until = 0.0
        self._state.waiting_reason = (reason or "").strip() or None
        self._state.waiting_since = time.time()
        self._store.save(self.session_id, self._state)
        return self._state

    def wait_on_session(self, session_id: str, reason: str = "") -> GoalState:
        """把目标循环泊在一个后台会话自己的触发器上。

        与 ``wait_on``（仅 PID 退出释放）不同，这个在会话触发器命中时释放：退出，
        或——若以 watch_patterns 启动——模式匹配。适合中途发信号、可能永不退出的
        长驻 watcher/server/poller。需要活跃目标。
        """
        if self._state is None or self._state.status != "active":
            raise RuntimeError("no active goal to park")
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ValueError("session_id must be a non-empty string")
        self._state.waiting_on_session = session_id
        self._state.waiting_on_pid = None
        self._state.waiting_until = 0.0
        self._state.waiting_reason = (reason or "").strip() or None
        self._state.waiting_since = time.time()
        self._store.save(self.session_id, self._state)
        return self._state

    def wait_for_seconds(self, seconds: int, reason: str = "") -> GoalState:
        """把目标循环泊到从现在起 ``seconds`` 秒后。

        ``wait_on`` 的时间版——用于没有进程可跟踪的退避/冷却等待（如 Agent 被限流）。
        截止时刻到达后屏障自动清除。需要活跃目标。
        """
        if self._state is None or self._state.status != "active":
            raise RuntimeError("no active goal to park")
        seconds = int(seconds)
        if seconds <= 0:
            raise ValueError("seconds must be a positive integer")
        self._state.waiting_on_pid = None
        self._state.waiting_on_session = None
        self._state.waiting_until = time.time() + seconds
        self._state.waiting_reason = (reason or "").strip() or None
        self._state.waiting_since = time.time()
        self._store.save(self.session_id, self._state)
        return self._state

    def stop_waiting(self) -> bool:
        """清除任何活跃 wait barrier（pid/session/time）。清掉了返回 True。"""
        if self._state is None:
            return False
        if (
            self._state.waiting_on_pid is None
            and self._state.waiting_on_session is None
            and not self._state.waiting_until
        ):
            return False
        self._clear_barrier_fields()
        self._store.save(self.session_id, self._state)
        return True

    def is_waiting(self) -> bool:
        """当且仅当屏障已设且尚未满足时为 True。

        会话屏障：活跃到进程退出或其 watch-pattern 触发器命中。PID 屏障：活跃到
        进程存活。时间屏障：活跃到截止时刻到达。副作用：已满足的屏障在此被清除
        （惰性自动清除），下次评估恢复正常裁决。
        """
        s = self._state
        if s is None:
            return False
        if s.waiting_on_session is not None:
            if _session_waiting(s.waiting_on_session):
                return True
            self.stop_waiting()  # 会话退出或触发器命中
            return False
        if s.waiting_on_pid is not None:
            if _pid_alive(s.waiting_on_pid):
                return True
            self.stop_waiting()  # 进程没了
            return False
        if s.waiting_until:
            if time.time() < s.waiting_until:
                return True
            self.stop_waiting()  # 截止时刻已过
            return False
        return False

    # --- 每轮结束后调用的主入口 ---------------------------------------

    def evaluate_after_turn(
        self,
        last_response: str,
        *,
        user_initiated: bool = True,
        background_processes: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """跑 judge 并更新状态。返回一个决策 dict。

        ``user_initiated`` 区分真实用户 prompt（True）与我们自己喂的续传 prompt
        （False）。两者都递增 ``turns_used``，因为都消耗模型预算。

        ``background_processes`` 是本会话的实时后台进程快照，交给 judge 让它能
        决定 WAIT 在一个在途进程上（CI 轮询、构建……）而非反复戳 Agent。

        决策键：
          - ``status``: 更新后的当前目标状态
          - ``should_continue``: bool —— 调用方是否应再发起一轮
          - ``continuation_prompt``: str 或 None
          - ``verdict``: "done" | "continue" | "wait" | "waiting" | "skipped" | "inactive"
          - ``reason``: str
          - ``message``: 可打印/发送给用户的一行消息
        """
        state = self._state
        if state is None or state.status != "active":
            return {
                "status": state.status if state else None,
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "inactive",
                "reason": "no active goal",
                "message": "",
            }

        # 等待屏障：若循环已泊（在一个存活进程 OR 一个未到的时间截止上），静默——
        # 不烧 turn、不调 judge。屏障清除后自动恢复。
        if self.is_waiting():
            if state.waiting_on_session is not None:
                tgt = f"session {state.waiting_on_session}"
            elif state.waiting_on_pid is not None:
                tgt = f"pid {state.waiting_on_pid}"
            else:
                remaining = max(0, int(state.waiting_until - time.time()))
                tgt = f"{remaining}s remaining"
            reason = state.waiting_reason or tgt
            return {
                "status": "active",
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "waiting",
                "reason": reason,
                "message": f"⏳ Goal parked — waiting on {tgt}: {reason}",
            }

        # 计入刚结束的这一轮。
        state.turns_used += 1
        state.last_turn_at = time.time()

        verdict, reason, parse_failed, wait_directive = judge_goal(
            state.goal,
            last_response,
            llm_caller=self._llm_caller,
            subgoals=state.subgoals or None,
            background_processes=background_processes,
            contract=state.contract if state.has_contract() else None,
        )
        state.last_verdict = verdict
        state.last_reason = reason

        # 跟踪连续 judge 解析失败。任何可用回复都重置——包括 API/传输错误
        # （parse_failed=False），这样一个闪烁的网络不会触发 meant-for-坏-judge-模型
        # 的自动暂停。
        if parse_failed:
            state.consecutive_parse_failures += 1
        else:
            state.consecutive_parse_failures = 0

        # WAIT 裁决：judge 判定 Agent 卡在异步活上、现在再戳是无用功。设置屏障并
        # 泊车——刚计入的这一轮作数（judge 调用发生了），但不发起续传。pid 退出或
        # 截止到达后循环自动恢复。
        if verdict == "wait" and wait_directive:
            if wait_directive.get("session_id"):
                self.wait_on_session(str(wait_directive["session_id"]), reason=reason)
                tgt = f"session {wait_directive['session_id']}"
            elif wait_directive.get("pid"):
                self.wait_on(int(wait_directive["pid"]), reason=reason)
                tgt = f"pid {wait_directive['pid']}"
            else:
                self.wait_for_seconds(int(wait_directive["seconds"]), reason=reason)
                tgt = f"{wait_directive['seconds']}s"
            return {
                "status": "active",
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "wait",
                "reason": reason,
                "message": f"⏳ Goal parked (judge) — waiting on {tgt}: {reason}",
            }

        if verdict == "done":
            state.status = "done"
            self._store.save(self.session_id, state)
            return {
                "status": "done",
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "done",
                "reason": reason,
                "message": f"✓ Goal achieved: {reason}",
            }

        # 当 judge 模型连续 N 轮产不出预期 JSON 裁决时自动暂停。把用户指向
        # goals.judge 配置，好把这个 side task 路由到一个遵守契约的模型。没有这道
        # 护栏，弱 judge 模型会烧光整个 turn 预算、每次回复都是散文或空串。
        if state.consecutive_parse_failures >= DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES:
            state.status = "paused"
            state.paused_reason = (
                f"judge model returned unparseable output {state.consecutive_parse_failures} turns in a row"
            )
            self._store.save(self.session_id, state)
            return {
                "status": "paused",
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "continue",
                "reason": reason,
                "message": (
                    f"⏸ Goal paused — the judge model ({state.consecutive_parse_failures} turns) "
                    "isn't returning the required JSON verdict. Route the judge to a stricter "
                    "model in ~/.spirit/config.yaml:\n"
                    "  goals:\n"
                    "    judge:\n"
                    "      model: <a stricter model>\n"
                    "Then /goal resume to continue."
                ),
            }

        if state.turns_used >= state.max_turns:
            state.status = "paused"
            state.paused_reason = f"turn budget exhausted ({state.turns_used}/{state.max_turns})"
            self._store.save(self.session_id, state)
            return {
                "status": "paused",
                "should_continue": False,
                "continuation_prompt": None,
                "verdict": "continue",
                "reason": reason,
                "message": (
                    f"⏸ Goal paused — {state.turns_used}/{state.max_turns} turns used. "
                    "Use /goal resume to keep going, or /goal clear to stop."
                ),
            }

        self._store.save(self.session_id, state)
        return {
            "status": "active",
            "should_continue": True,
            "continuation_prompt": self.next_continuation_prompt(),
            "verdict": "continue",
            "reason": reason,
            "message": (
                f"↻ Continuing toward goal ({state.turns_used}/{state.max_turns}): {reason}"
            ),
        }

    def next_continuation_prompt(self) -> Optional[str]:
        if not self._state or self._state.status != "active":
            return None
        # 契约优先：它携带 Agent 必须瞄准的验证面和约束。子目标折进契约块作为
        # 追加的额外标准。
        if self._state.has_contract():
            contract_block = self._state.contract.render_block()
            if self._state.subgoals:
                extra = "\n".join(
                    f"- Extra criterion {i}: {text}"
                    for i, text in enumerate(self._state.subgoals, start=1)
                )
                contract_block = f"{contract_block}\n{extra}"
            return CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE.format(
                goal=self._state.goal,
                contract_block=contract_block,
            )
        if self._state.subgoals:
            return CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE.format(
                goal=self._state.goal,
                subgoals_block=self._state.render_subgoals_block(),
            )
        return CONTINUATION_PROMPT_TEMPLATE.format(goal=self._state.goal)

    def render_contract(self) -> str:
        """/goal show + /goal draft slash 命令的公共辅助。"""
        if self._state is None:
            return "(no active goal)"
        if not self._state.has_contract():
            return "(no completion contract — set one with /goal draft <objective> or inline field: value lines)"
        return self._state.contract.render_block()


__all__ = ["GoalManager"]
