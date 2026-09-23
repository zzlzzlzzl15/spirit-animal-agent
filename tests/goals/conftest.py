"""tests/goals — Ralph Loop 持久目标系统测试包。

移植自 Hermes 的同款"任务检查"测试用例：
- hermes-agent-main/tests/hermes_cli/test_goals.py（核心单元：judge/manager/state/contract/wait）
- hermes-agent-main/tests/hermes_cli/test_kanban_goal_mode.py（循环驱动逻辑）
- hermes-agent-main/tests/tui_gateway/test_goal_command.py（命令分发）
- hermes-agent-main/tests/gateway/test_goal_max_turns_config.py（预算配置）
- hermes-agent-main/tests/cli/test_cli_goal_interrupt.py（中断/空响应跳过 judge）

关键架构适配（Spirit vs Hermes）：
- Hermes 用 ``patch("agent.auxiliary_client.call_llm")`` mock 辅助模型；Spirit 的
  ``judge_goal`` 接收可注入的 ``llm_caller``，测试用 :class:`FakeCaller` 代替。
- Hermes 用模块级 SessionDB（``hermes_home`` fixture 隔离 HERMES_HOME）；Spirit 用
  依赖注入的 :class:`~spirit.goals.store.GoalStore`——单测默认 ``InMemoryGoalStore``，
  持久化测试用 ``SessionDB(tmp_path/...)`` + ``SessionDBGoalStore(db=...)``。
- Hermes ``patch.object(goals, "judge_goal")`` → Spirit ``patch("spirit.goals.manager.judge_goal")``
  （manager.py 把 judge_goal 导入了自己的命名空间）。
"""

from __future__ import annotations

import pytest

from spirit.goals import GoalManager, InMemoryGoalStore
from spirit.storage.session_db import SessionDB


# ──────────────────────────────────────────────────────────────────────
# FakeCaller — Spirit 版的 "mock 辅助模型"
# ──────────────────────────────────────────────────────────────────────


class FakeCaller:
    """可注入的假 ``llm_caller``，等价于 Hermes 测试里 patch 的 ``call_llm``。

    签名与 :data:`spirit.goals.judge.LLMCaller` 一致：
    ``(messages, temperature, max_tokens, timeout) -> str``。

    - ``content`` 为字符串 → 每次调用都返回它。
    - ``content`` 为列表 → 按序弹出（耗尽后重复最后一个），用于脚本化多轮裁决。
    - ``content`` 为 Exception 实例 → 抛出它（模拟 API/传输错误，验证 fail-open）。
    - ``content`` 为可调用 → 每次调用它取返回值。

    每次调用的入参记录在 ``self.calls``，``last_user_msg`` / ``last_messages``
    便于断言"发给 judge 的 prompt 里带了契约/子目标/后台进程"。
    """

    def __init__(self, content='{"verdict": "continue", "reason": "more work"}'):
        self.content = content
        self.calls: list = []

    def _next_content(self):
        c = self.content
        if isinstance(c, list):
            if not c:
                return ""
            return c.pop(0) if len(c) > 1 else c[0]
        return c

    def __call__(self, messages, temperature, max_tokens, timeout):
        self.calls.append({
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": timeout,
        })
        content = self._next_content()
        if isinstance(content, Exception):
            raise content
        if callable(content):
            return content()
        return content

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_messages(self):
        return self.calls[-1]["messages"] if self.calls else []

    @property
    def last_user_msg(self) -> str:
        return next(
            (m["content"] for m in self.last_messages if m.get("role") == "user"), ""
        )

    @property
    def last_system_msg(self) -> str:
        return next(
            (m["content"] for m in self.last_messages if m.get("role") == "system"), ""
        )


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def mem_store():
    """内存 GoalStore — 大多数单测无需真实 DB。"""
    return InMemoryGoalStore()


@pytest.fixture
def tmp_db(tmp_path):
    """隔离到 tmp_path 的真实 SessionDB — 持久化/迁移测试用。"""
    db = SessionDB(db_path=tmp_path / "goal_test.db")
    try:
        yield db
    finally:
        try:
            db.close()
        except Exception:
            pass


def make_manager(
    session_id: str = "test-sid",
    *,
    store=None,
    max_turns: int = 20,
    llm_caller=None,
) -> GoalManager:
    """构造一个绑定到内存 store 的 GoalManager（测试便利函数）。"""
    return GoalManager(
        session_id,
        default_max_turns=max_turns,
        store=store if store is not None else InMemoryGoalStore(),
        llm_caller=llm_caller,
    )
