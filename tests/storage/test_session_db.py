"""tests/storage/test_session_db.py — 会话存储测试。"""

import pytest
from pathlib import Path


class TestSessionDB:
    """SessionDB 会话存储测试。"""

    def test_create_session(self, session_db):
        """测试创建会话。"""
        sid = session_db.create_session(model="gpt-4o", source="test")
        assert sid
        session = session_db.get_session(sid)
        assert session is not None
        assert session["model"] == "gpt-4o"
        assert session["source"] == "test"

    def test_create_session_with_id(self, session_db):
        """测试指定 ID 创建会话。"""
        sid = session_db.create_session(session_id="my-session", model="gpt-4o")
        assert sid == "my-session"

    def test_end_session(self, session_db):
        """测试结束会话。"""
        sid = session_db.create_session(model="gpt-4o")
        session_db.end_session(sid, reason="test_exit")
        session = session_db.get_session(sid)
        assert session["end_reason"] == "test_exit"
        assert session["ended_at"] is not None

    def test_list_sessions(self, session_db):
        """测试列出会话。"""
        session_db.create_session(model="gpt-4o", source="cli")
        session_db.create_session(model="gpt-4o", source="web")
        sessions = session_db.list_sessions()
        assert len(sessions) == 2

    def test_list_sessions_by_source(self, session_db):
        """测试按来源过滤。"""
        session_db.create_session(model="gpt-4o", source="cli")
        session_db.create_session(model="gpt-4o", source="web")
        cli_sessions = session_db.list_sessions(source="cli")
        assert len(cli_sessions) == 1

    def test_save_and_get_messages(self, session_db):
        """测试保存和获取消息。"""
        sid = session_db.create_session(model="gpt-4o")
        session_db.save_message(sid, "user", "hello")
        session_db.save_message(sid, "assistant", "hi there")
        messages = session_db.get_messages(sid)
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "hello"
        assert messages[1]["role"] == "assistant"

    def test_save_messages_batch(self, session_db):
        """测试批量保存消息。"""
        sid = session_db.create_session(model="gpt-4o")
        batch = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
        ]
        session_db.save_messages(sid, batch)
        messages = session_db.get_messages(sid)
        assert len(messages) == 3

    def test_search_messages_fts(self, session_db):
        """测试全文搜索。"""
        sid = session_db.create_session(model="gpt-4o")
        session_db.save_message(sid, "user", "Python is great for data science")
        session_db.save_message(sid, "assistant", "Yes Python has excellent libraries")
        session_db.save_message(sid, "user", "Tell me about JavaScript")

        results = session_db.search_messages("Python")
        assert len(results) >= 1
        assert any("Python" in r["content"] for r in results)

    def test_get_nonexistent_session(self, session_db):
        """测试获取不存在的会话。"""
        assert session_db.get_session("nonexistent") is None

    def test_messages_isolated_by_session(self, session_db):
        """测试消息按会话隔离。"""
        sid1 = session_db.create_session(model="gpt-4o")
        sid2 = session_db.create_session(model="gpt-4o")
        session_db.save_message(sid1, "user", "session 1 msg")
        session_db.save_message(sid2, "user", "session 2 msg")

        msgs1 = session_db.get_messages(sid1)
        msgs2 = session_db.get_messages(sid2)
        assert len(msgs1) == 1
        assert len(msgs2) == 1
        assert msgs1[0]["content"] == "session 1 msg"
        assert msgs2[0]["content"] == "session 2 msg"
