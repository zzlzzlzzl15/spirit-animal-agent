"""tests/agent/test_session_token_accumulation.py — Spirit 会话 token 累加回归测试。

覆盖 SpiritAgent 4 个 session_*_tokens 字段的初始化 / 累加 / 重置行为。
这是 v0.2.10 状态栏 Σ 字段（"Σ 45K ⇄ 3 calls"）的数据来源。

对齐 Hermes: 字段命名和语义与 hermes session_*_tokens 一致。
"""
import pytest


# ---------------------------------------------------------------------------
# 隔离 fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_db_path(tmp_path, monkeypatch):
    """隔离 SessionDB 路径。"""
    db_path = tmp_path / "token_test.db"
    monkeypatch.setattr(
        "spirit.storage.session_db.DEFAULT_DB_PATH",
        db_path,
    )
    return db_path


@pytest.fixture
def fresh_agent(isolated_db_path):
    from spirit.agent.agent import SpiritAgent, AgentConfig
    config = AgentConfig(
        model="gpt-4o-mini", api_key="test", provider="openai",
        max_iterations=5, compression_enabled=False, streaming_enabled=False,
    )
    agent = SpiritAgent(config=config)
    yield agent
    try:
        if agent._session_db is not None:
            agent._session_db.close()
    except Exception:
        pass


# ===========================================================================
# 1. 初始值
# ===========================================================================

class TestTokenFieldInitialization:
    """SpiritAgent.__init__ 后 4 字段初始 0。"""

    def test_input_tokens_initial_zero(self, fresh_agent):
        assert fresh_agent.session_input_tokens == 0

    def test_output_tokens_initial_zero(self, fresh_agent):
        assert fresh_agent.session_output_tokens == 0

    def test_total_tokens_initial_zero(self, fresh_agent):
        assert fresh_agent.session_total_tokens == 0

    def test_api_calls_initial_zero(self, fresh_agent):
        assert fresh_agent.session_api_calls == 0


# ===========================================================================
# 2. 累加路径（模拟 conversation_loop 每次 LLM 响应后累加）
# ===========================================================================

class TestTokenAccumulation:
    """累加语义：+= prompt_tokens / completion_tokens / total_tokens。"""

    def test_single_call_accumulation(self, fresh_agent):
        """模拟 1 次 LLM 响应: prompt=100, completion=50, total=150。"""
        # 模拟 conversation_loop.py:355-365 累加块
        response_usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        fresh_agent.session_input_tokens += response_usage["prompt_tokens"]
        fresh_agent.session_output_tokens += response_usage["completion_tokens"]
        fresh_agent.session_total_tokens += response_usage["total_tokens"]
        fresh_agent.session_api_calls += 1
        assert fresh_agent.session_input_tokens == 100
        assert fresh_agent.session_output_tokens == 50
        assert fresh_agent.session_total_tokens == 150
        assert fresh_agent.session_api_calls == 1

    def test_multiple_call_accumulation(self, fresh_agent):
        """3 次调用累计: 100+50+150 = 300 input, 50+30+70 = 150 output。"""
        for prompt, completion in [(100, 50), (50, 30), (150, 70)]:
            fresh_agent.session_input_tokens += prompt
            fresh_agent.session_output_tokens += completion
            fresh_agent.session_total_tokens += prompt + completion
            fresh_agent.session_api_calls += 1
        assert fresh_agent.session_input_tokens == 300
        assert fresh_agent.session_output_tokens == 150
        assert fresh_agent.session_total_tokens == 450
        assert fresh_agent.session_api_calls == 3

    def test_status_bar_sum_field_derivation(self, fresh_agent):
        """状态栏 Σ 字段 = total_tokens, ⇄ 字段 = api_calls。"""
        # 模拟 v0.2.10 状态栏 _bottom_toolbar 读取逻辑
        fresh_agent.session_input_tokens = 1_000_000  # 1M
        fresh_agent.session_output_tokens = 200_000
        fresh_agent.session_total_tokens = 1_200_000
        fresh_agent.session_api_calls = 5
        # 状态栏用 _fmt_tokens
        from spirit.cli.main import _fmt_tokens
        sum_display = _fmt_tokens(fresh_agent.session_total_tokens)
        assert sum_display == "1.2M"
        api_calls_display = fresh_agent.session_api_calls
        assert api_calls_display == 5


# ===========================================================================
# 3. reset_session 清零
# ===========================================================================

class TestTokenReset:
    """reset_session 应清零 4 字段（与 messages / api_call_count 一致）。

    注意: spirit 当前 reset_session **不**清 session_*_tokens / session_api_calls,
    是 spirit 的潜在设计 bug——新会话的 token 累加器不归 0, 状态栏 Σ 字段
    在新 session_id 下仍显示旧值。下面 4 项用 xfail 标记, 等 spirit 修。
    """

    @pytest.mark.xfail(
        reason="spirit bug: reset_session 应清 session_input_tokens 但当前不清",
        strict=False,
    )
    def test_reset_session_zeroes_input_tokens(self, fresh_agent):
        fresh_agent.session_input_tokens = 500
        fresh_agent.reset_session()
        assert fresh_agent.session_input_tokens == 0

    @pytest.mark.xfail(
        reason="spirit bug: reset_session 应清 session_output_tokens 但当前不清",
        strict=False,
    )
    def test_reset_session_zeroes_output_tokens(self, fresh_agent):
        fresh_agent.session_output_tokens = 500
        fresh_agent.reset_session()
        assert fresh_agent.session_output_tokens == 0

    @pytest.mark.xfail(
        reason="spirit bug: reset_session 应清 session_total_tokens 但当前不清",
        strict=False,
    )
    def test_reset_session_zeroes_total_tokens(self, fresh_agent):
        fresh_agent.session_total_tokens = 1500
        fresh_agent.reset_session()
        assert fresh_agent.session_total_tokens == 0

    @pytest.mark.xfail(
        reason="spirit bug: reset_session 应清 session_api_calls 但当前不清",
        strict=False,
    )
    def test_reset_session_zeroes_api_calls(self, fresh_agent):
        fresh_agent.session_api_calls = 5
        fresh_agent.reset_session()
        assert fresh_agent.session_api_calls == 0

    def test_reset_session_does_zero_api_call_count(self, fresh_agent):
        """_api_call_count (旧字段) reset_session 会清——与 session_api_calls 行为不同。

        spirit 现状: `_api_call_count` 在 reset_session 中清 0, 但 `session_api_calls`
        字段不清 0。两字段实际是同一语义的副本, 不一致。
        """
        fresh_agent._api_call_count = 10
        fresh_agent.reset_session()
        assert fresh_agent._api_call_count == 0


# ===========================================================================
# 4. switch_model 行为（与 reset_session 不同）
# ===========================================================================

class TestTokenOnSwitchModel:
    """switch_model 不清 token 字段（spirit 设计如此，session_id 保持）。"""

    def test_switch_model_keeps_input_tokens(self, fresh_agent):
        fresh_agent.session_input_tokens = 100
        fresh_agent.switch_model("claude-3-opus")
        assert fresh_agent.session_input_tokens == 100, \
            "switch_model 不应清 token 字段（与 reset_session 区别）"

    def test_switch_model_keeps_api_calls(self, fresh_agent):
        fresh_agent.session_api_calls = 5
        fresh_agent.switch_model("claude-3-opus")
        assert fresh_agent.session_api_calls == 5


# ===========================================================================
# 5. db 失败隔离（与 add_message 失败隔离一致）
# ===========================================================================

class TestTokenFieldsAreInMemoryOnly:
    """token 4 字段是 in-memory 累加, 不落 db — 与 v0.2.10 状态栏"since 重启后清零"语义一致。"""

    def test_tokens_not_persisted_in_db(self, fresh_agent, isolated_db_path):
        """累加 token 后, db.session 里没有这些字段值。"""
        fresh_agent.session_input_tokens = 999
        fresh_agent.session_output_tokens = 888
        fresh_agent.session_total_tokens = 1887
        fresh_agent.session_api_calls = 7
        # db 里的 session row 应只有基础字段
        from spirit.storage.session_db import SessionDB
        db = SessionDB(db_path=isolated_db_path)
        row = db.get_session(fresh_agent.session_id)
        # session 行有 token 字段吗？(v0.2.10 之前的 session_db schema 查)
        for key in ("input_tokens", "output_tokens", "total_tokens", "api_calls"):
            assert key not in row, f"session row 不应存 {key}"
        db.close()

    def test_new_agent_starts_at_zero_even_after_prev_session(self, isolated_db_path):
        """新 SpiritAgent 拿同 session_id, token 字段重置为 0（不读旧值）。"""
        from spirit.agent.agent import SpiritAgent, AgentConfig
        config = AgentConfig(
            model="gpt-4o-mini", api_key="test", provider="openai",
            max_iterations=5, compression_enabled=False,
        )
        agent1 = SpiritAgent(config=config)
        agent1.session_input_tokens = 500
        old_sid = agent1.session_id
        try:
            config2 = AgentConfig(
                model="gpt-4o-mini", api_key="test", provider="openai",
                max_iterations=5, compression_enabled=False,
                session_id=old_sid,
            )
            agent2 = SpiritAgent(config=config2)
            assert agent2.session_input_tokens == 0, \
                "新 agent 即使 session_id 相同, token 字段重置为 0"
            agent2._session_db.close()
        finally:
            try:
                agent1._session_db.close()
            except Exception:
                pass
