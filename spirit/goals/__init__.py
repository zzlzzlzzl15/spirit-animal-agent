"""spirit.goals — Ralph Loop 持久目标系统（Phase 4）。

一个 goal 是跨轮次保持活跃的自由用户目标。每轮结束后，一个轻量 judge 调用问
辅助模型"助手最后的响应满足这个目标了吗"。若否，Spirit 把一条续传 prompt 喂回
同一会话继续干，直到目标完成 / turn 预算耗尽 / 用户暂停或清除 / 用户发新消息。

对标 Hermes ``hermes_cli/goals.py``，适配 Spirit 架构（无 auxiliary_client →
注入 llm_caller；session_db 补 state_meta → GoalStore 抽象）。

公共 API::

    from spirit.goals import GoalManager, run_goal_loop, build_agent_llm_caller

    mgr = GoalManager(session_id, llm_caller=build_agent_llm_caller(agent))
    mgr.set("把所有测试跑通并修复失败项")
    result = run_goal_loop(manager=mgr, run_turn=lambda p: agent.chat(p)["response"],
                           first_response=first_turn_response)
"""

from __future__ import annotations

from spirit.goals.goal_state import (
    DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES,
    DEFAULT_MAX_TURNS,
    GoalContract,
    GoalState,
    parse_contract,
)
from spirit.goals.judge import (
    DEFAULT_JUDGE_MAX_TOKENS,
    DEFAULT_JUDGE_TIMEOUT,
    build_agent_llm_caller,
    draft_contract,
    gather_background_processes,
    judge_goal,
)
from spirit.goals.loop import run_goal_loop
from spirit.goals.manager import GoalManager
from spirit.goals.commands import (
    get_goal_manager,
    handle_goal_command,
    handle_subgoal_command,
    run_goal_turn_loop,
)
from spirit.goals.store import (
    GoalStore,
    InMemoryGoalStore,
    SessionDBGoalStore,
    migrate_goal_to_session,
)

__all__ = [
    # 数据模型
    "GoalContract",
    "GoalState",
    "parse_contract",
    # 裁决
    "judge_goal",
    "draft_contract",
    "build_agent_llm_caller",
    "gather_background_processes",
    # 持久化
    "GoalStore",
    "InMemoryGoalStore",
    "SessionDBGoalStore",
    "migrate_goal_to_session",
    # 管理器 + 循环
    "GoalManager",
    "run_goal_loop",
    # 命令分发（CLI / ws_server 共用）
    "get_goal_manager",
    "handle_goal_command",
    "handle_subgoal_command",
    "run_goal_turn_loop",
    # 常量
    "DEFAULT_MAX_TURNS",
    "DEFAULT_MAX_CONSECUTIVE_PARSE_FAILURES",
    "DEFAULT_JUDGE_TIMEOUT",
    "DEFAULT_JUDGE_MAX_TOKENS",
]
