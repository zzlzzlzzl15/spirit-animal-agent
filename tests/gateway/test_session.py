"""会话管理测试。"""

import pytest
from datetime import datetime, timedelta

from spirit.gateway.config import Platform, SessionResetPolicy, ResetPolicy
from spirit.gateway.session import (
    SessionContext,
    SessionSource,
    SessionStore,
    build_session_context_prompt,
    hash_id,
)


class TestSessionSource:
    """SessionSource 测试。"""

    def test_create_source(self, telegram_source):
        """测试创建消息来源。"""
        assert telegram_source.platform == Platform.TELEGRAM
        assert telegram_source.user_id == "user_001"
        assert telegram_source.chat_id == "chat_123"

    def test_session_key(self, telegram_source):
        """测试会话键生成。"""
        key = telegram_source.session_key
        assert key == "telegram:chat_123"

    def test_session_key_with_thread(self, discord_source):
        """测试带线程的会话键。"""
        key = discord_source.session_key
        assert key == "discord:channel_456:thread_789"

    def test_user_key(self, telegram_source):
        """测试用户键生成。"""
        key = telegram_source.user_key
        assert key == "telegram:user_001"

    def test_to_context_dict(self, telegram_source):
        """测试转换为上下文字典。"""
        ctx = telegram_source.to_context_dict()
        assert ctx["platform"] == "telegram"
        assert ctx["user_id"] == "user_001"
        assert ctx["chat_type"] == "dm"


class TestSessionContext:
    """SessionContext 测试。"""

    def test_create_context(self, telegram_source):
        """测试创建会话上下文。"""
        ctx = SessionContext(
            session_id="test-session-123",
            source=telegram_source,
        )
        assert ctx.session_id == "test-session-123"
        assert ctx.message_count == 0

    def test_touch(self, telegram_source):
        """测试更新活动时间。"""
        ctx = SessionContext(
            session_id="test-session-123",
            source=telegram_source,
        )
        old_time = ctx.last_activity
        import time
        time.sleep(0.01)
        ctx.touch()
        assert ctx.last_activity >= old_time

    def test_increment_message(self, telegram_source):
        """测试消息计数。"""
        ctx = SessionContext(
            session_id="test-session-123",
            source=telegram_source,
        )
        ctx.increment_message(estimated_tokens=100)
        assert ctx.message_count == 1
        assert ctx.token_count == 100

    def test_idle_timeout(self, telegram_source):
        """测试空闲超时检测。"""
        ctx = SessionContext(
            session_id="test-session-123",
            source=telegram_source,
        )
        # 模拟旧会话
        ctx.last_activity = datetime.now() - timedelta(hours=2)

        policy = SessionResetPolicy(
            policy=ResetPolicy.TIME_BASED,
            idle_timeout_minutes=60,
        )
        assert ctx.is_idle_expired(policy) is True

    def test_message_limit(self, telegram_source):
        """测试消息数限制。"""
        ctx = SessionContext(
            session_id="test-session-123",
            source=telegram_source,
        )
        ctx.message_count = 600

        policy = SessionResetPolicy(
            policy=ResetPolicy.MESSAGE_COUNT,
            max_messages=500,
        )
        assert ctx.is_message_limit_exceeded(policy) is True

    def test_should_reset(self, telegram_source):
        """测试重置判断。"""
        ctx = SessionContext(
            session_id="test-session-123",
            source=telegram_source,
        )
        # 手动策略永不重置
        policy = SessionResetPolicy(policy=ResetPolicy.MANUAL)
        assert ctx.should_reset(policy) is False


class TestSessionStore:
    """SessionStore 测试。"""

    def test_create_store(self, session_store):
        """测试创建存储。"""
        assert session_store.size == 0

    def test_get_or_create(self, session_store, telegram_source):
        """测试获取或创建会话。"""
        ctx = session_store.get_or_create(telegram_source)
        assert ctx is not None
        assert session_store.size == 1

    def test_get_same_session(self, session_store, telegram_source):
        """测试获取相同会话。"""
        ctx1 = session_store.get_or_create(telegram_source)
        ctx2 = session_store.get_or_create(telegram_source)
        assert ctx1.session_id == ctx2.session_id

    def test_different_sources_different_sessions(self, session_store, telegram_source, discord_source):
        """测试不同来源不同会话。"""
        ctx1 = session_store.get_or_create(telegram_source)
        ctx2 = session_store.get_or_create(discord_source)
        assert ctx1.session_id != ctx2.session_id
        assert session_store.size == 2

    def test_lru_eviction(self, tmp_path):
        """测试 LRU 淘汰。"""
        store = SessionStore(max_size=3)

        # 创建 4 个不同来源的会话
        for i in range(4):
            source = SessionSource(
                platform=Platform.TELEGRAM,
                user_id=f"user_{i}",
                chat_id=f"chat_{i}",
            )
            store.get_or_create(source)

        # 应该只有 3 个（淘汰最旧的）
        assert store.size == 3

    def test_remove_session(self, session_store, telegram_source):
        """测试移除会话。"""
        ctx = session_store.get_or_create(telegram_source)
        assert session_store.size == 1

        removed = session_store.remove(telegram_source.session_key)
        assert removed is not None
        assert session_store.size == 0

    def test_list_sessions(self, session_store, telegram_source, discord_source):
        """测试列出所有会话。"""
        session_store.get_or_create(telegram_source)
        session_store.get_or_create(discord_source)

        sessions = session_store.list_sessions()
        assert len(sessions) == 2


class TestSessionContextPrompt:
    """会话上下文提示词测试。"""

    def test_build_prompt(self, telegram_source):
        """测试构建上下文提示词。"""
        prompt = build_session_context_prompt(
            source=telegram_source,
            session_id="test-session-123",
        )
        assert "telegram" in prompt
        assert "dm" in prompt

    def test_build_prompt_with_username(self, telegram_source):
        """测试带用户名的提示词。"""
        prompt = build_session_context_prompt(
            source=telegram_source,
            session_id="test-session-123",
        )
        assert "testuser" in prompt

    def test_build_prompt_group_chat(self, discord_source):
        """测试群聊提示词。"""
        prompt = build_session_context_prompt(
            source=discord_source,
            session_id="test-session-123",
        )
        assert "group" in prompt
        assert "group chat" in prompt.lower()


class TestHashFunctions:
    """哈希函数测试。"""

    def test_hash_id(self):
        """测试 ID 哈希。"""
        h1 = hash_id("test")
        h2 = hash_id("test")
        assert h1 == h2
        assert len(h1) == 12

    def test_hash_id_different(self):
        """测试不同输入不同哈希。"""
        h1 = hash_id("user1")
        h2 = hash_id("user2")
        assert h1 != h2
