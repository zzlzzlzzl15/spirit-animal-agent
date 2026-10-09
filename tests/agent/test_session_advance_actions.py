"""Spirit Agent v0.2.8-11 resume / branch / session_db 高级方法 / session_id env 注入。

回归目标：
  - SpiritAgent.resume_session: 加载历史 messages、切换 sid、重置计数器（v0.2.8）
  - SpiritAgent.branch_session: 创建分支、保留消息、parent_session_id 链路（v0.2.8）
  - session_db.branch_session: db 层分支（v0.2.8）
  - session_db.reopen_session: 重新打开已结束的 session（v0.2.8）
  - session_db.list_sessions: 各种过滤（v0.2.8）
  - session_db.end_session: First Reason Wins 边界（v0.2.9）
  - session_id env 注入（v0.2.8）
  - resume 后系统提示缓存清理（v0.2.8）

参考 hermes:
  - test_resume_stale_active_task.py (53 lines)
  - test_session_rotation_flush_cold_resume.py (117 lines)
  - test_session_meta_filtering.py (56 lines)
  - test_session_id_env.py (37 lines)
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest


# ─────────────────────────── Fixtures ───────────────────────────


@pytest.fixture
def isolated_db_path(tmp_path, monkeypatch):
    db_path = tmp_path / "session_advance_test.db"
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
    )
    agent = SpiritAgent(config=config)
    yield agent
    try:
        if agent._session_db is not None:
            agent._session_db.close()
    except Exception:
        pass


# ─────────────────────────── SpiritAgent.resume_session ───────────────────────────


class TestResumeSession:
    """SpiritAgent.resume_session 行为（加载历史 / 切换 sid / 重置计数器）。"""

    def test_resume_nonexistent_session_returns_false(self, fresh_agent):
        """恢复不存在的 session_id → 返回 False"""
        result = fresh_agent.resume_session("nonexistent-session-id")
        assert result is False

    def test_resume_no_db_returns_false(self, fresh_agent):
        """_session_db=None 时 resume_session 返回 False"""
        fresh_agent._session_db = None
        result = fresh_agent.resume_session("any-id")
        assert result is False

    def test_resume_loads_history_messages(self, fresh_agent):
        """resume 成功：_messages 被加载历史"""
        # 创建历史 session，加几条 message
        old_sid = fresh_agent._session_db.create_session(
            source="cli", model="gpt-4o-mini"
        )
        fresh_agent._session_db.save_message(old_sid, "user", "hi")
        fresh_agent._session_db.save_message(old_sid, "assistant", "hello")
        fresh_agent._session_db.save_message(old_sid, "user", "how are you?")

        # 切到 fresh_agent 上，resume
        result = fresh_agent.resume_session(old_sid)
        assert result is True
        # 验证 _messages 被加载
        assert len(fresh_agent._messages) == 3
        assert fresh_agent._messages[0]["role"] == "user"
        assert fresh_agent._messages[0]["content"] == "hi"
        assert fresh_agent._messages[1]["role"] == "assistant"
        assert fresh_agent._messages[1]["content"] == "hello"
        assert fresh_agent._messages[2]["role"] == "user"
        assert fresh_agent._messages[2]["content"] == "how are you?"

    def test_resume_switches_session_id(self, fresh_agent):
        """resume 后 agent.session_id 切换到目标 session_id"""
        old_sid = fresh_agent._session_db.create_session(
            source="cli", model="gpt-4o-mini"
        )
        original_sid = fresh_agent.session_id
        assert original_sid != old_sid

        result = fresh_agent.resume_session(old_sid)
        assert result is True
        assert fresh_agent.session_id == old_sid
        assert fresh_agent.session_id != original_sid

    def test_resume_ends_current_session_with_resumed_other(self, fresh_agent):
        """resume 时当前 session 被标记 end_reason=resumed_other"""
        old_sid = fresh_agent._session_db.create_session(
            source="cli", model="gpt-4o-mini"
        )
        original_sid = fresh_agent.session_id
        fresh_agent.resume_session(old_sid)
        # 原 session 标记 ended_at + end_reason=resumed_other
        sess = fresh_agent._session_db.get_session(original_sid)
        assert sess.get("end_reason") == "resumed_other"
        assert sess.get("ended_at") is not None

    def test_resume_reopens_target_session(self, fresh_agent):
        """resume 时目标 session 被 reopen（ended_at 清空）"""
        # 创建 + 结束
        old_sid = fresh_agent._session_db.create_session(
            source="cli", model="gpt-4o-mini"
        )
        fresh_agent._session_db.end_session(old_sid, reason="task_complete")
        # 验证已结束
        sess = fresh_agent._session_db.get_session(old_sid)
        assert sess.get("end_reason") == "task_complete"
        # resume
        result = fresh_agent.resume_session(old_sid)
        # 验证被 reopen
        sess_after = fresh_agent._session_db.get_session(old_sid)
        assert sess_after.get("end_reason") is None
        assert sess_after.get("ended_at") is None

    def test_resume_resets_api_call_count(self, fresh_agent):
        """resume 后 _api_call_count 重置为 0"""
        fresh_agent._api_call_count = 42
        old_sid = fresh_agent._session_db.create_session(
            source="cli", model="gpt-4o-mini"
        )
        fresh_agent.resume_session(old_sid)
        assert fresh_agent._api_call_count == 0

    def test_resume_clears_cached_system_prompt(self, fresh_agent):
        """resume 后 _cached_system_prompt 清空（强制重建）"""
        fresh_agent._cached_system_prompt = "cached system prompt"
        old_sid = fresh_agent._session_db.create_session(
            source="cli", model="gpt-4o-mini"
        )
        fresh_agent.resume_session(old_sid)
        assert fresh_agent._cached_system_prompt is None

    def test_resume_preserves_tool_calls_in_history(self, fresh_agent):
        """resume 加载历史时 tool_calls 字段被正确反序列化"""
        old_sid = fresh_agent._session_db.create_session(
            source="cli", model="gpt-4o-mini"
        )
        # 保存带 tool_calls 的消息
        tool_calls_json = json.dumps([{"function": {"name": "search", "arguments": "{}"}}])
        fresh_agent._session_db.save_message(
            old_sid, "assistant", "let me search",
            tool_calls=tool_calls_json,
        )
        # resume
        fresh_agent.resume_session(old_sid)
        # 验证 tool_calls 被反序列化
        assert len(fresh_agent._messages) == 1
        assert "tool_calls" in fresh_agent._messages[0]
        assert fresh_agent._messages[0]["tool_calls"] == [
            {"function": {"name": "search", "arguments": "{}"}}
        ]


# ─────────────────────────── SpiritAgent.branch_session ───────────────────────────


class TestBranchSession:
    """SpiritAgent.branch_session 行为（创建分支、parent 链路）。"""

    def test_branch_without_db_raises_runtime_error(self, fresh_agent):
        """_session_db=None 时 branch_session 抛 RuntimeError"""
        fresh_agent._session_db = None
        with pytest.raises(RuntimeError, match="会话数据库未初始化"):
            fresh_agent.branch_session(branch_name="test")

    def test_branch_creates_new_session_id(self, fresh_agent):
        """branch 后 session_id 切换到新 sid"""
        original_sid = fresh_agent.session_id
        new_sid = fresh_agent.branch_session(branch_name="feature-x")
        assert new_sid is not None
        assert new_sid != original_sid
        assert fresh_agent.session_id == new_sid

    def test_branch_sets_parent_session_id(self, fresh_agent):
        """branch 后 db 里新 session 的 parent_session_id 指向原 session"""
        original_sid = fresh_agent.session_id
        new_sid = fresh_agent.branch_session(branch_name="feature-y")
        new_sess = fresh_agent._session_db.get_session(new_sid)
        assert new_sess is not None
        assert new_sess.get("parent_session_id") == original_sid

    def test_branch_resets_api_call_count(self, fresh_agent):
        """branch 后 _api_call_count 重置为 0（保留消息历史但重置计数器）"""
        fresh_agent._api_call_count = 99
        fresh_agent.branch_session(branch_name="z")
        assert fresh_agent._api_call_count == 0

    def test_branch_clears_cached_system_prompt(self, fresh_agent):
        """branch 后 _cached_system_prompt 清空"""
        fresh_agent._cached_system_prompt = "cached"
        fresh_agent.branch_session(branch_name="z")
        assert fresh_agent._cached_system_prompt is None

    def test_branch_default_name(self, fresh_agent):
        """branch 不传 branch_name 时使用默认名（auto-<timestamp>）"""
        new_sid = fresh_agent.branch_session()
        new_sess = fresh_agent._session_db.get_session(new_sid)
        metadata_json = new_sess.get("metadata", "{}")
        # metadata 是 JSON 字符串
        metadata = json.loads(metadata_json) if metadata_json else {}
        assert "branch_name" in metadata


# ─────────────────────────── session_db.branch_session ───────────────────────────


class TestSessionDBBranch:
    """session_db.branch_session db 层行为。"""

    def test_branch_nonexistent_parent_raises_value_error(self, fresh_agent):
        """branch 父 session 不存在时抛 ValueError"""
        with pytest.raises(ValueError, match="父会话不存在"):
            fresh_agent._session_db.branch_session(
                parent_session_id="nonexistent",
                branch_name="x",
            )

    def test_branch_copies_all_messages(self, fresh_agent):
        """branch 复制父 session 的所有消息到新 session"""
        parent_sid = fresh_agent._session_db.create_session(
            source="cli", model="gpt-4"
        )
        fresh_agent._session_db.save_message(parent_sid, "user", "msg1")
        fresh_agent._session_db.save_message(parent_sid, "assistant", "resp1")
        fresh_agent._session_db.save_message(parent_sid, "user", "msg2")

        new_sid = fresh_agent._session_db.branch_session(
            parent_session_id=parent_sid,
            branch_name="x",
        )
        msgs = fresh_agent._session_db.get_messages(new_sid)
        assert len(msgs) == 3
        assert msgs[0]["content"] == "msg1"
        assert msgs[1]["content"] == "resp1"
        assert msgs[2]["content"] == "msg2"

    def test_branch_copies_tool_calls(self, fresh_agent):
        """branch 复制 tool_calls 字段（dict/list 自动 json 序列化）"""
        parent_sid = fresh_agent._session_db.create_session(
            source="cli", model="gpt-4"
        )
        tool_calls_dict = [{"function": {"name": "search", "arguments": "{}"}}]
        fresh_agent._session_db.save_message(
            parent_sid, "assistant", "searching",
            tool_calls=json.dumps(tool_calls_dict),
        )
        new_sid = fresh_agent._session_db.branch_session(
            parent_session_id=parent_sid, branch_name="x"
        )
        msgs = fresh_agent._session_db.get_messages(new_sid)
        assert len(msgs) == 1
        # 验证 tool_calls 被复制（db 里是 json 字符串）
        assert msgs[0]["tool_calls"] is not None
        assert json.loads(msgs[0]["tool_calls"]) == tool_calls_dict

    def test_branch_metadata_contains_branch_name_and_parent(self, fresh_agent):
        """branch 后新 session 的 metadata 含 branch_name 和 branched_from"""
        parent_sid = fresh_agent._session_db.create_session(
            source="cli", model="gpt-4"
        )
        new_sid = fresh_agent._session_db.branch_session(
            parent_session_id=parent_sid, branch_name="my-feature"
        )
        new_sess = fresh_agent._session_db.get_session(new_sid)
        metadata = json.loads(new_sess["metadata"])
        assert metadata["branch_name"] == "my-feature"
        assert metadata["branched_from"] == parent_sid

    def test_branch_with_provided_new_session_id(self, fresh_agent):
        """branch 可显式指定 new_session_id"""
        parent_sid = fresh_agent._session_db.create_session(
            source="cli", model="gpt-4"
        )
        explicit_sid = "explicit-branch-id-12345"
        new_sid = fresh_agent._session_db.branch_session(
            parent_session_id=parent_sid,
            new_session_id=explicit_sid,
            branch_name="x",
        )
        assert new_sid == explicit_sid


# ─────────────────────────── session_db.reopen_session ───────────────────────────


class TestSessionDBReopen:
    """session_db.reopen_session 行为。"""

    def test_reopen_nonexistent_returns_false(self, fresh_agent):
        """reopen 不存在的 session_id → False"""
        result = fresh_agent._session_db.reopen_session("nonexistent-id")
        assert result is False

    def test_reopen_active_session_returns_true_no_change(self, fresh_agent):
        """reopen 活跃（未结束）的 session → True，ended_at 不变"""
        sid = fresh_agent._session_db.create_session(
            source="cli", model="gpt-4"
        )
        result = fresh_agent._session_db.reopen_session(sid)
        assert result is True
        sess = fresh_agent._session_db.get_session(sid)
        assert sess.get("ended_at") is None

    def test_reopen_ended_session_clears_ended_at(self, fresh_agent):
        """reopen 已结束的 session → ended_at 清空"""
        sid = fresh_agent._session_db.create_session(
            source="cli", model="gpt-4"
        )
        fresh_agent._session_db.end_session(sid, reason="task_complete")
        # 验证已结束
        sess = fresh_agent._session_db.get_session(sid)
        assert sess.get("ended_at") is not None
        # reopen
        result = fresh_agent._session_db.reopen_session(sid)
        assert result is True
        # 验证 ended_at 清空
        sess_after = fresh_agent._session_db.get_session(sid)
        assert sess_after.get("ended_at") is None
        assert sess_after.get("end_reason") is None

    def test_reopen_then_end_session_works(self, fresh_agent):
        """reopen 后再 end_session 可以正常标记结束"""
        sid = fresh_agent._session_db.create_session(
            source="cli", model="gpt-4"
        )
        fresh_agent._session_db.end_session(sid, reason="task_complete")
        fresh_agent._session_db.reopen_session(sid)
        fresh_agent._session_db.end_session(sid, reason="cli_exit")
        sess = fresh_agent._session_db.get_session(sid)
        assert sess.get("end_reason") == "cli_exit"


# ─────────────────────────── session_db.list_sessions ───────────────────────────


class TestSessionDBListSessions:
    """session_db.list_sessions 各种过滤。"""

    def test_list_sessions_returns_all_by_default(self, fresh_agent):
        """list_sessions 默认返回所有 session（按 started_at DESC）"""
        sid1 = fresh_agent._session_db.create_session(source="cli", model="gpt-4")
        sid2 = fresh_agent._session_db.create_session(source="cli", model="gpt-4")
        sessions = fresh_agent._session_db.list_sessions()
        # 包含 fresh_agent 自动创建的 + 我们手动创建的
        sids = [s["id"] for s in sessions]
        assert sid1 in sids
        assert sid2 in sids

    def test_list_sessions_filter_by_source(self, fresh_agent):
        """list_sessions 按 source 过滤"""
        cli_sid = fresh_agent._session_db.create_session(source="cli", model="gpt-4")
        api_sid = fresh_agent._session_db.create_session(source="api", model="gpt-4")
        cli_sessions = fresh_agent._session_db.list_sessions(source="cli")
        sids = [s["id"] for s in cli_sessions]
        assert cli_sid in sids
        assert api_sid not in sids

    def test_list_sessions_limit(self, fresh_agent):
        """list_sessions limit 参数限制返回数量"""
        for _ in range(5):
            fresh_agent._session_db.create_session(source="cli", model="gpt-4")
        sessions = fresh_agent._session_db.list_sessions(limit=3)
        assert len(sessions) == 3


# ─────────────────────────── session_db.end_session First Reason Wins ───────────────────────────


class TestSessionDBEndSessionFirstReasonWins:
    """session_db.end_session First Reason Wins 边界。"""

    def test_end_session_called_twice_keeps_first_reason(self, fresh_agent):
        """end_session 调两次，end_reason 保持第一次的"""
        sid = fresh_agent._session_db.create_session(source="cli", model="gpt-4")
        fresh_agent._session_db.end_session(sid, reason="task_complete")
        first_ended_at = fresh_agent._session_db.get_session(sid)["ended_at"]
        fresh_agent._session_db.end_session(sid, reason="cli_exit")
        sess = fresh_agent._session_db.get_session(sid)
        assert sess["end_reason"] == "task_complete"
        assert sess["ended_at"] == first_ended_at

    def test_compression_reason_blocks_later_close(self, fresh_agent):
        """end_reason='compression' 时后续的 agent_close 不会覆盖（保留压缩链）"""
        sid = fresh_agent._session_db.create_session(source="cli", model="gpt-4")
        fresh_agent._session_db.end_session(sid, reason="compression")
        fresh_agent._session_db.end_session(sid, reason="agent_close")
        sess = fresh_agent._session_db.get_session(sid)
        # 保持 compression
        assert sess["end_reason"] == "compression"

    def test_end_session_default_reason(self, fresh_agent):
        """end_session 不传 reason → 默认 'user_exit'"""
        sid = fresh_agent._session_db.create_session(source="cli", model="gpt-4")
        fresh_agent._session_db.end_session(sid)
        sess = fresh_agent._session_db.get_session(sid)
        assert sess["end_reason"] == "user_exit"


# ─────────────────────────── session_id env 注入 ───────────────────────────


class TestSessionIdEnvInjection:
    """session_id 从环境变量注入。

    注：spirit AgentConfig 当前不读 SPIRIT_SESSION_ID env。
    下面 2 项用 xfail 标记，记录设计意图（hermes 已实现，spirit 未实现）。
    """

    @pytest.mark.xfail(
        reason="spirit 现状：AgentConfig 不读 SPIRIT_SESSION_ID env，session_id 总是 uuid 随机",
        strict=False,
    )
    def test_spirit_session_id_env_var_creates_agent_with_given_sid(
        self, isolated_db_path, monkeypatch
    ):
        """SPIRIT_SESSION_ID env 变量被 SpiritAgent 使用"""
        target_sid = "fixed-test-session-id-001"
        monkeypatch.setenv("SPIRIT_SESSION_ID", target_sid)
        from spirit.agent.agent import SpiritAgent, AgentConfig
        config = AgentConfig(
            model="gpt-4o-mini",
            api_key="test-key-12345",
        )
        agent = SpiritAgent(config=config)
        assert agent.session_id == target_sid
        agent._session_db.close()

    def test_no_env_var_generates_random_sid(self, isolated_db_path, monkeypatch):
        """没设 SPIRIT_SESSION_ID env 时 sid 是 uuid 随机"""
        monkeypatch.delenv("SPIRIT_SESSION_ID", raising=False)
        from spirit.agent.agent import SpiritAgent, AgentConfig
        config = AgentConfig(
            model="gpt-4o-mini",
            api_key="test-key-12345",
        )
        agent = SpiritAgent(config=config)
        # 验证 sid 是 uuid 格式（36 字符含 4 个连字符）
        assert len(agent.session_id) == 36
        assert agent.session_id.count("-") == 4
        agent._session_db.close()
