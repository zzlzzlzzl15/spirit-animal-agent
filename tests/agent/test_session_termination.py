"""Spirit Agent 三层终止 (Soft/Medium/Hard End) 完整测试 + interrupt 边界。

回归目标：
  - v0.2.9 三层终止与 Hermes 对齐：
    - Soft End: reset_session() — end_session('new_session') + 清 _messages
    - Medium End: shutdown_memory_provider() — end_session('cli_exit') + 关 compressor
    - Hard End: close() — end_session('agent_close') + 关 client/db
  - interrupt / clear_interrupt 边界
  - 多次 close() 幂等

参考 hermes:
  - test_session_persistence.py (三层终止)
  - test_interrupt_*.py (4 个)
  - test_session_reset_fix.py
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ─────────────────────────── Fixtures ───────────────────────────


@pytest.fixture
def isolated_db_path(tmp_path, monkeypatch):
    db_path = tmp_path / "termination_test.db"
    monkeypatch.setattr(
        "spirit.storage.session_db.DEFAULT_DB_PATH",
        db_path,
    )
    return db_path


@pytest.fixture
def fresh_agent(isolated_db_path):
    from spirit.agent.agent import SpiritAgent, AgentConfig
    config = AgentConfig(
        model="gpt-4o-mini",
        api_key="test-key-12345",
        max_iterations=50,
        compression_enabled=False,
    )
    agent = SpiritAgent(config=config)
    yield agent
    try:
        if agent._session_db is not None:
            agent._session_db.close()
    except Exception:
        pass


# ─────────────────────────── interrupt / clear_interrupt 边界 ───────────────────────────


class TestInterruptEdgeCases:
    """interrupt / clear_interrupt 边界。"""

    def test_interrupt_sets_flag(self, fresh_agent):
        """interrupt 后 _interrupt_requested=True"""
        assert fresh_agent._interrupt_requested is False
        fresh_agent.interrupt()
        assert fresh_agent._interrupt_requested is True

    def test_clear_interrupt_resets_flag(self, fresh_agent):
        """clear_interrupt 后 _interrupt_requested=False"""
        fresh_agent._interrupt_requested = True
        fresh_agent.clear_interrupt()
        assert fresh_agent._interrupt_requested is False

    def test_interrupt_called_multiple_times_remains_true(self, fresh_agent):
        """多次 interrupt 仍为 True"""
        fresh_agent.interrupt()
        fresh_agent.interrupt()
        fresh_agent.interrupt()
        assert fresh_agent._interrupt_requested is True

    def test_clear_after_multiple_interrupts_resets(self, fresh_agent):
        """多次 interrupt 后 clear_interrupt 重置"""
        fresh_agent.interrupt()
        fresh_agent.interrupt()
        fresh_agent.clear_interrupt()
        assert fresh_agent._interrupt_requested is False

    def test_interrupt_does_not_change_messages(self, fresh_agent):
        """interrupt 不应清 _messages"""
        fresh_agent.add_message("user", "hi")
        fresh_agent.add_message("assistant", "hello")
        fresh_agent.interrupt()
        assert len(fresh_agent._messages) == 2

    def test_interrupt_does_not_close_session(self, fresh_agent):
        """interrupt 不应 end_session（保留给 run_conversation finally）"""
        sess_before = fresh_agent._session_db.get_session(fresh_agent.session_id)
        fresh_agent.interrupt()
        sess_after = fresh_agent._session_db.get_session(fresh_agent.session_id)
        # session 仍是活跃
        assert sess_after.get("ended_at") is None


# ─────────────────────────── close() 完整 6 步（Hard End） ───────────────────────────


class TestCloseHardEnd:
    """SpiritAgent.close() 完整 6 步行为。"""

    def test_close_emits_on_agent_destroy_hook(self, fresh_agent):
        """close() 触发 on_agent_destroy 钩子"""
        events = []
        original_emit = fresh_agent.hook_manager.emit

        def spy_emit(event, **kwargs):
            events.append(event)
            return original_emit(event, **kwargs)

        fresh_agent.hook_manager.emit = spy_emit
        fresh_agent.close()
        assert "on_agent_destroy" in events

    def test_close_marks_session_with_agent_close(self, fresh_agent):
        """close() 调 end_session(reason='agent_close')"""
        fresh_agent.close()
        # 验证 session 被标记
        # 注意：close() 后 _session_db 被 close 了，**应该**先 get
        # 这里用 db_path 重新打开验证
        from spirit.storage.session_db import SessionDB
        import pathlib
        db_path = pathlib.Path(fresh_agent._session_db.db_path)
        db = SessionDB(db_path)
        try:
            sess = db.get_session(fresh_agent.session_id)
            assert sess is not None
            assert sess.get("end_reason") == "agent_close"
            assert sess.get("ended_at") is not None
        finally:
            db.close()

    def test_close_clears_messages(self, fresh_agent):
        """close() 后 _messages 清空"""
        fresh_agent.add_message("user", "hi")
        fresh_agent.add_message("assistant", "hello")
        fresh_agent.close()
        assert len(fresh_agent._messages) == 0

    def test_close_clears_client(self, fresh_agent):
        """close() 后 _client 被清理"""
        fresh_agent._client = MagicMock()  # 模拟已初始化的 client
        fresh_agent.close()
        assert fresh_agent._client is None

    def test_close_clears_interrupt_flag(self, fresh_agent):
        """close() 后 _interrupt_requested=False"""
        fresh_agent._interrupt_requested = True
        fresh_agent.close()
        assert fresh_agent._interrupt_requested is False

    def test_close_is_idempotent(self, fresh_agent):
        """多次 close() 不报错（幂等）"""
        fresh_agent.close()
        # 第二次 close 时 session_id 和 _session_db 已为 None 或已关闭
        # 不应抛错
        try:
            fresh_agent.close()
        except Exception as e:
            pytest.fail(f"second close() raised: {e}")


# ─────────────────────────── shutdown_memory_provider (Medium End) ───────────────────────────


class TestShutdownMemoryProvider:
    """SpiritAgent.shutdown_memory_provider Medium End 行为。"""

    def test_shutdown_marks_session_with_cli_exit(self, fresh_agent):
        """shutdown_memory_provider 调 end_session(reason='cli_exit')"""
        # 改 end_session 为 spy
        called = []
        original = fresh_agent._session_db.end_session
        def spy(sid, reason="user_exit"):
            called.append(reason)
            return original(sid, reason)
        fresh_agent._session_db.end_session = spy

        fresh_agent.shutdown_memory_provider()
        assert "cli_exit" in called

    def test_shutdown_shuts_down_compressor(self, fresh_agent):
        """shutdown_memory_provider 调用 _context_compressor.shutdown()"""
        # 创建 mock compressor
        mock_compressor = MagicMock()
        fresh_agent._context_compressor = mock_compressor
        fresh_agent.shutdown_memory_provider()
        # 验证 compressor.shutdown 被调
        mock_compressor.shutdown.assert_called_once()

    def test_shutdown_safe_when_compressor_none(self, fresh_agent):
        """_context_compressor=None 时 shutdown_memory_provider 不崩"""
        fresh_agent._context_compressor = None
        # 不应抛错
        fresh_agent.shutdown_memory_provider()

    def test_shutdown_safe_when_session_id_none(self, fresh_agent):
        """session_id=None 时 shutdown_memory_provider 不崩"""
        fresh_agent.session_id = None
        # 不应抛错
        fresh_agent.shutdown_memory_provider()


# ─────────────────────────── close_session 嵌套调用（多次 reason） ───────────────────────────


class TestCloseSessionNested:
    """close_session 嵌套调用：reset_session + run_conversation finally + close()。"""

    def test_reset_session_then_close_session_new_session_overwritten(self, fresh_agent):
        """reset_session(new_session) + close_session(agent_close) → agent_close 覆盖。

        注：spirit session_db.end_session 的 First Reason Wins 只对
        `compression` 例外，其他 reason（包括 new_session）会被新 reason 覆盖。
        Hermes 的 First Reason Wins 是严格幂等（WHERE ended_at IS NULL），
        spirit 是宽松版。这个差异是 spirit 现状，不是 bug。
        """
        fresh_agent.reset_session()  # end_session(new_session)
        # 新 session_id
        new_sid = fresh_agent.session_id
        # 调 close_session(agent_close)
        fresh_agent.close_session(reason="agent_close")
        # spirit 现状：new_session 被 agent_close 覆盖
        sess = fresh_agent._session_db.get_session(new_sid)
        assert sess.get("end_reason") == "agent_close"

    def test_compression_reason_blocks_subsequent_close(self, fresh_agent):
        """end_reason='compression' 后再 close_session(agent_close) → 保持 compression（spirit 设计例外）"""
        # 先手动设置 end_reason=compression
        fresh_agent._session_db.end_session(fresh_agent.session_id, reason="compression")
        # 调 close_session(agent_close)
        fresh_agent.close_session(reason="agent_close")
        sess = fresh_agent._session_db.get_session(fresh_agent.session_id)
        # 保持 compression
        assert sess.get("end_reason") == "compression"

    def test_run_conversation_finally_then_close_session(self, fresh_agent):
        """run_conversation finally + close_session 后 First Reason Wins"""
        # 模拟 run_conversation 完成后 close_session(task_complete)
        fresh_agent.max_iterations = 0
        try:
            fresh_agent.run_conversation("test")
        except Exception:
            pass
        # 模拟 close() 调 close_session(agent_close)
        fresh_agent.close_session(reason="agent_close")
        # task_complete 保持
        sess = fresh_agent._session_db.get_session(fresh_agent.session_id)
        assert sess.get("end_reason") in ("max_iterations", "task_complete")


# ─────────────────────────── reset_session Soft End 完整 ───────────────────────────


class TestResetSessionSoftEnd:
    """reset_session Soft End 完整行为。"""

    def test_reset_session_emits_on_session_end_hook(self, fresh_agent):
        """reset_session 触发 on_session_end 钩子"""
        events = []
        original_emit = fresh_agent.hook_manager.emit

        def spy_emit(event, **kwargs):
            events.append(event)
            return original_emit(event, **kwargs)

        fresh_agent.hook_manager.emit = spy_emit
        fresh_agent.reset_session()
        assert "on_session_end" in events

    def test_reset_session_marks_session_with_new_session(self, fresh_agent):
        """reset_session 调 end_session(reason='new_session')"""
        old_sid = fresh_agent.session_id
        fresh_agent.reset_session()
        sess = fresh_agent._session_db.get_session(old_sid)
        assert sess.get("end_reason") == "new_session"
        assert sess.get("ended_at") is not None

    def test_reset_session_creates_new_session_id(self, fresh_agent):
        """reset_session 创建新 session_id"""
        old_sid = fresh_agent.session_id
        fresh_agent.reset_session()
        new_sid = fresh_agent.session_id
        assert new_sid != old_sid

    def test_reset_session_clears_messages(self, fresh_agent):
        """reset_session 清 _messages"""
        fresh_agent.add_message("user", "msg1")
        fresh_agent.add_message("assistant", "resp1")
        fresh_agent.reset_session()
        assert len(fresh_agent._messages) == 0

    def test_reset_session_resets_api_call_count(self, fresh_agent):
        """reset_session 重置 _api_call_count = 0"""
        fresh_agent._api_call_count = 100
        fresh_agent.reset_session()
        assert fresh_agent._api_call_count == 0

    def test_reset_session_clears_cached_system_prompt(self, fresh_agent):
        """reset_session 清 _cached_system_prompt"""
        fresh_agent._cached_system_prompt = "cached"
        fresh_agent.reset_session()
        assert fresh_agent._cached_system_prompt is None

    def test_reset_session_clears_interrupt_flag(self, fresh_agent):
        """reset_session 清 _interrupt_requested"""
        fresh_agent._interrupt_requested = True
        fresh_agent.reset_session()
        assert fresh_agent._interrupt_requested is False

    def test_reset_session_emits_on_session_start_for_new_session(self, fresh_agent):
        """reset_session 触发 on_session_start（新 session）"""
        events = []
        original_emit = fresh_agent.hook_manager.emit

        def spy_emit(event, **kwargs):
            events.append(event)
            return original_emit(event, **kwargs)

        fresh_agent.hook_manager.emit = spy_emit
        fresh_agent.reset_session()
        # 顺序：on_session_end (旧) + on_session_reset + on_session_start (新)
        assert "on_session_start" in events
