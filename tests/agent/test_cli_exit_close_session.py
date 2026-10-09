"""Spirit Agent v0.2.9 CLI 退出 close_session 兜底测试。

回归目标：
  - v0.2.9 修复：cli/main.py chat() 在 while 循环结束后（/quit / EOFError）
    调 agent.close_session(reason="cli_exit")，让 db 标记 end_reason=cli_exit。
  - 即使前面 run_conversation 已 close_session（First Reason Wins），
    CLI 退出再调一次也是幂等的——不会覆盖前面的 reason。
  - close_session 抛错时不影响 CLI 退出（异常隔离）。

设计说明：
  cli/main.py 的 chat() 函数依赖整个 CLI 框架（PromptSession + Live + Spinner
  + console + _print_welcome_panel + _print_connection_status + _print_recent_tasks
  + register_progress_sink + click），端到端测需 mock 整个框架，性价比低。

  本测试聚焦**关键行为**：
  1. close_session(reason="cli_exit") 真的标记 session 结束
  2. 幂等性（First Reason Wins 不会被覆盖）
  3. 异常隔离（close_session 抛错不抛给 CLI）
  4. session_id 为空时的 no-op
  5. 多次 close_session 不报错
"""

from __future__ import annotations

import pytest


# ─────────────────────────── Fixtures ───────────────────────────


@pytest.fixture
def isolated_db_path(tmp_path, monkeypatch):
    """隔离 session_db 到 tmp_path。"""
    db_path = tmp_path / "cli_exit_test.db"
    monkeypatch.setattr(
        "spirit.storage.session_db.DEFAULT_DB_PATH",
        db_path,
    )
    return db_path


@pytest.fixture
def fresh_agent(isolated_db_path):
    """Fresh SpiritAgent，自动 lazy create session。"""
    from spirit.agent.agent import SpiritAgent, AgentConfig
    config = AgentConfig(
        model="gpt-4o-mini",
        api_key="test-key-12345",
        max_iterations=50,
    )
    agent = SpiritAgent(config=config)
    yield agent
    try:
        if agent._session_db is not None:
            agent._session_db.close()
    except Exception:
        pass


# ─────────────────────────── close_session(reason='cli_exit') 行为 ───────────────────────────


class TestCloseSessionWithCliExit:
    """close_session(reason='cli_exit') 真的标记 session。"""

    def test_cli_exit_marks_session_ended_with_correct_reason(self, fresh_agent):
        """close_session(reason='cli_exit') 后 session.end_reason='cli_exit'"""
        fresh_agent.close_session(reason="cli_exit")
        sess = fresh_agent._session_db.get_session(fresh_agent.session_id)
        assert sess is not None
        assert sess.get("end_reason") == "cli_exit"
        assert sess.get("ended_at") is not None
        assert sess.get("end_reason") == "cli_exit"

    def test_cli_exit_after_user_message_persists_messages(self, fresh_agent):
        """close_session(cli_exit) 前 add_message 的内容仍落库"""
        fresh_agent.add_message("user", "你好")
        fresh_agent.add_message("assistant", "你好！")
        fresh_agent.close_session(reason="cli_exit")
        msgs = fresh_agent._session_db.get_messages(fresh_agent.session_id)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "你好"
        assert msgs[1]["role"] == "assistant"
        assert msgs[1]["content"] == "你好！"

    def test_cli_exit_marks_db_session_state_ended(self, fresh_agent):
        """close_session(cli_exit) 后 session 行 ended_at 不为 NULL"""
        # 取创建时的 ended_at
        before = fresh_agent._session_db.get_session(fresh_agent.session_id)
        assert before.get("ended_at") is None
        # 调 close_session
        fresh_agent.close_session(reason="cli_exit")
        # 验证 ended_at 真的被标记
        after = fresh_agent._session_db.get_session(fresh_agent.session_id)
        assert after.get("ended_at") is not None
        assert after.get("end_reason") == "cli_exit"


class TestCloseSessionCliExitIdempotency:
    """close_session(cli_exit) 幂等性：First Reason Wins。"""

    def test_second_cli_exit_does_not_overwrite_first_reason(self, fresh_agent):
        """close_session(cli_exit) 调两次，end_reason 保持第一次的"""
        # 第一次：task_complete
        fresh_agent.close_session(reason="task_complete")
        sess1 = fresh_agent._session_db.get_session(fresh_agent.session_id)
        assert sess1.get("end_reason") == "task_complete"
        first_ended_at = sess1.get("ended_at")

        # 第二次：cli_exit（应被 First Reason Wins 拒绝）
        fresh_agent.close_session(reason="cli_exit")
        sess2 = fresh_agent._session_db.get_session(fresh_agent.session_id)
        # 保持第一次的 reason
        assert sess2.get("end_reason") == "task_complete"
        # ended_at 不变
        assert sess2.get("ended_at") == first_ended_at

    def test_cli_exit_before_run_conversation_keeps_cli_exit(self, fresh_agent):
        """如果先 close_session(cli_exit) 再跑 run_conversation，cli_exit 保持"""
        fresh_agent.close_session(reason="cli_exit")
        # 模拟 run_conversation 完成后又调 close_session(task_complete)
        fresh_agent.close_session(reason="task_complete")
        sess = fresh_agent._session_db.get_session(fresh_agent.session_id)
        # First Reason Wins：cli_exit 保持
        assert sess.get("end_reason") == "cli_exit"

    def test_close_session_called_twice_with_different_reasons(self, fresh_agent):
        """多次调 close_session 不同 reason 不会报错，First Reason Wins"""
        reasons = ["task_complete", "cli_exit", "agent_close", "signal_term"]
        for r in reasons:
            fresh_agent.close_session(reason=r)
        # 验证 reason 保持第一次的
        sess = fresh_agent._session_db.get_session(fresh_agent.session_id)
        assert sess.get("end_reason") == "task_complete"


class TestCloseSessionCliExitExceptionIsolation:
    """close_session 内部异常隔离：不影响主流程。"""

    def test_close_session_with_db_raising_runtime_error_does_not_propagate(
        self, fresh_agent
    ):
        """close_session 内部 db 抛 RuntimeError 不抛给调用方"""
        # 让 end_session 抛 RuntimeError
        def boom(*args, **kwargs):
            raise RuntimeError("db is locked")

        fresh_agent._session_db.end_session = boom
        # 不应抛错（close_session 内部 try/except）
        fresh_agent.close_session(reason="cli_exit")

    def test_close_session_with_db_raising_value_error_does_not_propagate(
        self, fresh_agent
    ):
        """close_session 内部 db 抛 ValueError 不抛给调用方"""
        def boom(*args, **kwargs):
            raise ValueError("corrupted db")

        fresh_agent._session_db.end_session = boom
        # 不应抛错
        fresh_agent.close_session(reason="cli_exit")

    def test_close_session_with_db_returning_none_does_not_propagate(
        self, fresh_agent
    ):
        """close_session 内部 db 返回 None 不抛给调用方"""
        fresh_agent._session_db.end_session = lambda *a, **kw: None
        # 不应抛错
        fresh_agent.close_session(reason="cli_exit")


class TestCloseSessionNoopSafety:
    """close_session 在异常状态下的安全 no-op。"""

    def test_close_session_when_session_id_is_none(self, fresh_agent):
        """session_id=None 时 close_session 是 no-op"""
        fresh_agent.session_id = None
        # 不应抛错
        fresh_agent.close_session(reason="cli_exit")
        fresh_agent.close_session(reason="agent_close")
        fresh_agent.close_session(reason="user_interrupt")

    def test_close_session_when_db_is_none(self, fresh_agent):
        """_session_db=None 时 close_session 是 no-op"""
        fresh_agent._session_db = None
        # 不应抛错
        fresh_agent.close_session(reason="cli_exit")

    def test_close_session_when_both_session_id_and_db_are_none(self, fresh_agent):
        """两个都 None 时 close_session 是 no-op"""
        fresh_agent.session_id = None
        fresh_agent._session_db = None
        # 不应抛错
        fresh_agent.close_session(reason="cli_exit")


class TestCloseSessionCliExitAfterRunConversation:
    """close_session(cli_exit) 在 run_conversation 之后：First Reason Wins 仍生效。"""

    def test_cli_exit_after_run_conversation_keeps_original_reason(self, fresh_agent):
        """run_conversation 完成后 close_session(cli_exit) 不会覆盖前面的 reason"""
        # 模拟 run_conversation 完成后 close_session(task_complete)
        fresh_agent.add_message("user", "test")
        fresh_agent.close_session(reason="task_complete")
        # 模拟 CLI 退出兜底 close_session(cli_exit)
        fresh_agent.close_session(reason="cli_exit")
        # First Reason Wins：task_complete 保持
        sess = fresh_agent._session_db.get_session(fresh_agent.session_id)
        assert sess.get("end_reason") == "task_complete"

    def test_cli_exit_after_max_iterations_keeps_max_iterations(self, fresh_agent):
        """run_conversation 因 max_iterations 退出后 close_session(cli_exit) 不覆盖"""
        fresh_agent.max_iterations = 0
        try:
            fresh_agent.run_conversation("test")
        except Exception:
            pass
        # run_conversation finally 已 close_session(max_iterations)
        sess1 = fresh_agent._session_db.get_session(fresh_agent.session_id)
        assert sess1.get("end_reason") == "max_iterations"
        # 模拟 CLI 退出兜底
        fresh_agent.close_session(reason="cli_exit")
        sess2 = fresh_agent._session_db.get_session(fresh_agent.session_id)
        # First Reason Wins：max_iterations 保持
        assert sess2.get("end_reason") == "max_iterations"


class TestCloseSessionAllReasons:
    """close_session 10 个 reason 字符串都能被 db 接受。"""

    @pytest.mark.parametrize(
        "reason",
        [
            "task_complete",
            "user_interrupt",
            "max_iterations",
            "api_error",
            "retry_exhausted",
            "guardrail_halt",
            "cli_exit",
            "agent_close",
            "signal_term",
            "signal_break",
        ],
    )
    def test_close_session_accepts_all_documented_reasons(self, fresh_agent, reason):
        """close_session 能接受所有 10 个文档化的 reason 字符串"""
        fresh_agent.close_session(reason=reason)
        sess = fresh_agent._session_db.get_session(fresh_agent.session_id)
        # 第一次调，reason 必被设置
        assert sess.get("end_reason") == reason
        assert sess.get("ended_at") is not None
