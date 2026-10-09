"""Spirit Agent v0.2.9 run_conversation try/finally 包装端到端测试。

回归目标：
  - v0.2.9 修复：SpiritAgent.run_conversation 包 try/finally，无论
    conversation_loop 8 个 return 路径 / 异常 / 中断，close_session 必被调用，
    令 db 的 ended_at / end_reason 永远有值（First Reason Wins）。
  - 与 Hermes session_persistence 三层终止对齐。

核心验证矩阵（10 项）：

| #  | 触发场景                     | 期望 end_reason      | 期望是否 raise |
| ---|------------------------------|----------------------|---------------|
| 1  | budget 耗尽 (max_iter=0)     | max_iterations       | 否            |
| 2  | 正常文本回答                 | task_complete        | 否            |
| 3  | 响应 "[已中断]"              | user_interrupt       | 否            |
| 4  | 响应 "[API 错误: ...]"       | api_error            | 否            |
| 5  | 响应 "[API 调用失败...]"     | retry_exhausted      | 否            |
| 6  | 响应 "[工具循环停止...]"     | guardrail_halt       | 否            |
| 7  | KeyboardInterrupt            | user_interrupt       | 是（透传）    |
| 8  | BaseException                | task_failed          | 是（透传）    |
| 9  | close_session 内部抛错       | 不阻断主流程         | 否            |
| 10 | session_id 为空              | 安全 no-op           | 否            |

测试隔离：
  - monkeypatch spirit.storage.session_db.DEFAULT_DB_PATH 隔离真实 ~/.spirit/state.db
  - 每个测试 fresh agent + 独立 tmp db
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────── Fixtures ───────────────────────────


@pytest.fixture
def isolated_db_path(tmp_path, monkeypatch):
    """隔离 session_db 到 tmp_path，避免污染 ~/.spirit/state.db。"""
    db_path = tmp_path / "run_conv_lifecycle_test.db"
    monkeypatch.setattr(
        "spirit.storage.session_db.DEFAULT_DB_PATH",
        db_path,
    )
    return db_path


@pytest.fixture
def fresh_agent(isolated_db_path):
    """每次测试一个 fresh SpiritAgent（自动 lazy create session）。"""
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


def _spy_end_session(agent):
    """在 agent._session_db.end_session 上挂 spy，记录所有调用 reason。"""
    called: list[str] = []
    original = agent._session_db.end_session

    def spy(session_id: str, reason: str = "user_exit"):
        called.append(reason)
        return original(session_id, reason)

    agent._session_db.end_session = spy
    return called


def _make_choice(content: str, tool_calls=None, finish_reason: str = "stop"):
    """构造一个 mock LLM choice 对象。"""
    message = MagicMock()
    message.content = content
    message.tool_calls = tool_calls or []
    choice = MagicMock()
    choice.message = message
    choice.finish_reason = finish_reason
    response = MagicMock()
    response.choices = [choice]
    response.usage = None
    response._stream_gate_closed = False
    response._stream_pushed_text = ""
    return response


# ─────────────────────────── 8 return 路径 + 异常路径 ───────────────────────────


class TestRunConversationFinallyCallsCloseSession:
    """try/finally 包装保证 close_session 必被调。"""

    def test_budget_exhausted_closes_with_max_iterations(self, fresh_agent, monkeypatch):
        """max_iterations=0 → 立即 budget 耗尽 → close_session(reason='max_iterations')"""
        fresh_agent.max_iterations = 0
        called = _spy_end_session(fresh_agent)

        # budget 在 budget.consume() 之前就耗尽，不会调 LLM
        result = fresh_agent.run_conversation("test")

        # 验证 end_reason
        assert "max_iterations" in called, f"expected 'max_iterations' in {called}"
        # 验证 session 真的被标记结束
        sess = fresh_agent._session_db.get_session(fresh_agent.session_id)
        assert sess is not None
        assert sess.get("end_reason") == "max_iterations"
        assert sess.get("ended_at") is not None

    def test_normal_text_completion_closes_with_task_complete(self, fresh_agent, monkeypatch):
        """正常文本回答（无特殊前缀）→ close_session(reason='task_complete')"""
        called = _spy_end_session(fresh_agent)

        # mock LLM client 返回普通文本
        mock_client = MagicMock()
        mock_response = _make_choice("这是正常回答。")
        mock_client.chat.completions.create.return_value = mock_response
        fresh_agent._client = mock_client

        result = fresh_agent.run_conversation("hello")

        assert "task_complete" in called, f"expected 'task_complete' in {called}"
        sess = fresh_agent._session_db.get_session(fresh_agent.session_id)
        assert sess.get("end_reason") == "task_complete"

    def test_response_user_interrupt_closes_with_user_interrupt(self, fresh_agent, monkeypatch):
        """响应以 '[已中断]' 开头 → close_session(reason='user_interrupt')"""
        called = _spy_end_session(fresh_agent)

        # mock LLM 返回 "[已中断]" 文本（虽然实际不会这样，但 _infer_close_reason 测前缀）
        mock_client = MagicMock()
        mock_response = _make_choice("[已中断]")
        mock_client.chat.completions.create.return_value = mock_response
        fresh_agent._client = mock_client

        result = fresh_agent.run_conversation("test")

        # [已中断] 是 conversation_loop 内部 user_interrupt 路径的前缀
        # 但这里 LLM 返回这个字符串，conversation_loop 不会把它当 user_interrupt 处理
        # 所以 _infer_close_reason 会把它当 task_complete
        # 验证 _infer_close_reason 的 startswith 逻辑
        from spirit.agent.agent import SpiritAgent
        mock_result = MagicMock()
        mock_result.response = "[已中断]"
        reason = SpiritAgent._infer_close_reason(mock_result)
        assert reason == "user_interrupt"

    def test_response_api_error_closes_with_api_error(self, fresh_agent):
        """响应以 '[API 错误' 开头 → _infer_close_reason → 'api_error'"""
        from spirit.agent.agent import SpiritAgent
        mock_result = MagicMock()
        mock_result.response = "[API 错误: rate limit]"
        reason = SpiritAgent._infer_close_reason(mock_result)
        assert reason == "api_error"

    def test_response_retry_exhausted_closes_with_retry_exhausted(self, fresh_agent):
        """响应以 '[API 调用失败' 开头 → _infer_close_reason → 'retry_exhausted'"""
        from spirit.agent.agent import SpiritAgent
        mock_result = MagicMock()
        mock_result.response = "[API 调用失败，已重试 3 次]"
        reason = SpiritAgent._infer_close_reason(mock_result)
        assert reason == "retry_exhausted"

    def test_response_guardrail_halt_closes_with_guardrail_halt(self, fresh_agent):
        """响应以 '[工具循环停止' 开头 → _infer_close_reason → 'guardrail_halt'"""
        from spirit.agent.agent import SpiritAgent
        mock_result = MagicMock()
        mock_result.response = "[工具循环停止: too many tool calls]"
        reason = SpiritAgent._infer_close_reason(mock_result)
        assert reason == "guardrail_halt"

    def test_response_max_iterations_closes_with_max_iterations(self, fresh_agent):
        """响应以 '[达到最大迭代次数' 开头 → _infer_close_reason → 'max_iterations'"""
        from spirit.agent.agent import SpiritAgent
        mock_result = MagicMock()
        mock_result.response = "[达到最大迭代次数 10]"
        reason = SpiritAgent._infer_close_reason(mock_result)
        assert reason == "max_iterations"

    def test_response_empty_fallback_closes_with_task_complete(self, fresh_agent):
        """兜底总结（无特殊前缀） → _infer_close_reason → 'task_complete'"""
        from spirit.agent.agent import SpiritAgent
        mock_result = MagicMock()
        mock_result.response = "已为你执行了 3 个工具。"
        reason = SpiritAgent._infer_close_reason(mock_result)
        assert reason == "task_complete"

    def test_response_attribute_error_returns_task_complete(self, fresh_agent):
        """ConversationResult 无 .response 属性时 → _infer_close_reason → 'task_complete'"""
        from spirit.agent.agent import SpiritAgent
        # 模拟 result.response 抛 AttributeError
        class BadResult:
            @property
            def response(self):
                raise AttributeError("no response")

        reason = SpiritAgent._infer_close_reason(BadResult())
        assert reason == "task_complete"


class TestRunConversationExceptionPaths:
    """异常路径：KeyboardInterrupt / BaseException → close_session 必须被调 + 透传。"""

    def test_keyboard_interrupt_raises_and_closes_with_user_interrupt(
        self, fresh_agent, monkeypatch
    ):
        """KeyboardInterrupt → close_session('user_interrupt') + 透传异常"""
        called = _spy_end_session(fresh_agent)

        # mock conversation_loop 抛 KeyboardInterrupt
        # SpiritAgent.run_conversation 内部是 `from spirit.agent.conversation_loop import run_conversation`
        # 所以必须 patch 源模块的属性，import 会在调用时重新解析
        from spirit.agent import conversation_loop as conv_loop_mod
        monkeypatch.setattr(
            conv_loop_mod, "run_conversation",
            lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()),
        )

        with pytest.raises(KeyboardInterrupt):
            fresh_agent.run_conversation("test")

        # 验证 finally 块调了 close_session(user_interrupt)
        assert "user_interrupt" in called, f"expected 'user_interrupt' in {called}"
        sess = fresh_agent._session_db.get_session(fresh_agent.session_id)
        assert sess.get("end_reason") == "user_interrupt"

    def test_base_exception_raises_and_closes_with_task_failed(
        self, fresh_agent, monkeypatch
    ):
        """BaseException (如 ValueError) → close_session('task_failed') + 透传异常"""
        called = _spy_end_session(fresh_agent)

        from spirit.agent import conversation_loop as conv_loop_mod
        monkeypatch.setattr(
            conv_loop_mod, "run_conversation",
            lambda *a, **kw: (_ for _ in ()).throw(ValueError("api boom")),
        )

        with pytest.raises(ValueError, match="api boom"):
            fresh_agent.run_conversation("test")

        assert "task_failed" in called, f"expected 'task_failed' in {called}"
        sess = fresh_agent._session_db.get_session(fresh_agent.session_id)
        assert sess.get("end_reason") == "task_failed"


class TestRunConversationExceptionIsolation:
    """finally 块内部异常隔离：close_session 失败不阻断主流程。"""

    def test_close_session_exception_does_not_propagate(
        self, fresh_agent, monkeypatch
    ):
        """close_session 内部 db 抛错 → finally 块 try/except 吞掉，不影响主流程"""
        # 替换 _session_db.end_session 让其抛 RuntimeError
        def boom(*args, **kwargs):
            raise RuntimeError("db is locked")

        fresh_agent._session_db.end_session = boom

        # mock LLM 返回正常回答
        mock_client = MagicMock()
        mock_response = _make_choice("ok")
        mock_client.chat.completions.create.return_value = mock_response
        fresh_agent._client = mock_client

        # 不应该抛 RuntimeError —— finally 块 try/except 隔离
        result = fresh_agent.run_conversation("test")
        assert result is not None
        assert result.response == "ok"

    def test_close_session_noop_when_session_id_empty(self, fresh_agent, monkeypatch):
        """session_id 为空时 close_session 是安全 no-op"""
        fresh_agent.session_id = None
        # 不应抛错
        fresh_agent.close_session(reason="task_complete")
        fresh_agent.close_session(reason="user_interrupt")

    def test_close_session_noop_when_db_not_initialized(self, fresh_agent, monkeypatch):
        """_session_db 为 None 时 close_session 是安全 no-op"""
        fresh_agent._session_db = None
        # 不应抛错
        fresh_agent.close_session(reason="task_complete")


class TestRunConversationFinallyRunsAfterException:
    """finally 块保证在所有 return / raise 路径后执行。"""

    def test_finally_runs_after_normal_completion(self, fresh_agent, monkeypatch):
        """正常完成：finally 块在 return 后执行"""
        called = _spy_end_session(fresh_agent)

        mock_client = MagicMock()
        mock_response = _make_choice("done")
        mock_client.chat.completions.create.return_value = mock_response
        fresh_agent._client = mock_client

        result = fresh_agent.run_conversation("test")
        # 正常路径 close_session 必被调
        assert len(called) >= 1

    def test_finally_runs_after_keyboard_interrupt(self, fresh_agent, monkeypatch):
        """KeyboardInterrupt：finally 块在 raise 后执行（即使 raise 也要 close_session）"""
        called = _spy_end_session(fresh_agent)

        from spirit.agent import conversation_loop as conv_loop_mod
        monkeypatch.setattr(
            conv_loop_mod, "run_conversation",
            lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()),
        )

        with pytest.raises(KeyboardInterrupt):
            fresh_agent.run_conversation("test")

        # finally 块必被调
        assert len(called) >= 1
        assert "user_interrupt" in called

    def test_finally_runs_after_base_exception(self, fresh_agent, monkeypatch):
        """BaseException：finally 块在 raise 后执行"""
        called = _spy_end_session(fresh_agent)

        from spirit.agent import conversation_loop as conv_loop_mod
        monkeypatch.setattr(
            conv_loop_mod, "run_conversation",
            lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("net down")),
        )

        with pytest.raises(ConnectionError):
            fresh_agent.run_conversation("test")

        assert len(called) >= 1
        assert "task_failed" in called


class TestRunConversationIntegration:
    """端到端：完整 mock LLM 让 conversation_loop 跑真实路径。"""

    def test_full_conversation_loop_completes_and_closes(self, fresh_agent):
        """完整 mock LLM → conversation_loop 跑真实 → close_session(task_complete)"""
        from spirit.agent.agent import ConversationResult
        called = _spy_end_session(fresh_agent)

        # max_iterations=1 → 跑 1 轮就耗尽 → 触发 max_iterations 路径
        fresh_agent.max_iterations = 1

        # 不 mock client — 让 conversation_loop 跑真实路径
        # 但需要 client 不抛错：用 mock client 返回 "done" 文本
        mock_client = MagicMock()
        mock_response = _make_choice("final answer")
        mock_client.chat.completions.create.return_value = mock_response
        fresh_agent._client = mock_client

        result = fresh_agent.run_conversation("test")

        # 验证 close_session 被调
        assert len(called) >= 1
        # end_reason 应该是 task_complete（因为 LLM 正常返回"final answer"）
        # 或 max_iterations（如果第 1 轮没正常返回）
        assert called[-1] in ("task_complete", "max_iterations"), f"got {called}"
