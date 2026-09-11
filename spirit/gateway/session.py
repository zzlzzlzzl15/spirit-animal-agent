"""会话上下文管理。

参考 Hermes 的 gateway/session.py：
- SessionContext: 会话上下文（消息来源、用户、平台）
- SessionStore: 会话存储（LRU 缓存 + 持久化）
- SessionSource: 消息来源标识
- 会话重置策略评估
- 动态系统提示词注入（让 Agent 知道上下文）
"""

import hashlib
import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from spirit.gateway.config import (
    GatewayConfig,
    Platform,
    PlatformConfig,
    ResetPolicy,
    SessionResetPolicy,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 消息来源
# ---------------------------------------------------------------------------

@dataclass
class SessionSource:
    """消息来源标识。

    参考 Hermes 的 SessionSource：
    - platform: 来源平台
    - user_id: 平台用户 ID
    - chat_id: 聊天/频道 ID
    - thread_id: 线程/话题 ID（可选）
    - message_id: 原始消息 ID（用于回复）
    """
    platform: Platform
    user_id: str
    chat_id: str
    thread_id: Optional[str] = None
    message_id: Optional[str] = None
    chat_type: str = "dm"  # dm / group / forum / channel
    username: Optional[str] = None
    display_name: Optional[str] = None

    @property
    def session_key(self) -> str:
        """生成会话键 — 用于唯一标识一个会话。

        规则：platform:chat_id[:thread_id]
        """
        parts = [self.platform.value, self.chat_id]
        if self.thread_id:
            parts.append(self.thread_id)
        return ":".join(parts)

    @property
    def user_key(self) -> str:
        """生成用户键 — 用于用户级追踪。"""
        return f"{self.platform.value}:{self.user_id}"

    def to_context_dict(self) -> Dict[str, Any]:
        """转换为上下文字典（用于钩子/Agent）。"""
        return {
            "platform": self.platform.value,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id or "",
            "chat_type": self.chat_type,
            "message_id": self.message_id or "",
            "username": self.username or "",
            "display_name": self.display_name or "",
        }


# ---------------------------------------------------------------------------
# 会话上下文
# ---------------------------------------------------------------------------

@dataclass
class SessionContext:
    """会话上下文 — 跟踪一个活跃会话的状态。

    参考 Hermes 的 SessionContext：
    - session_id: 会话唯一 ID
    - source: 消息来源
    - created_at: 创建时间
    - last_activity: 最后活动时间
    - message_count: 消息计数
    - token_count: Token 计数（估计）
    - metadata: 额外元数据
    """
    session_id: str
    source: SessionSource
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    message_count: int = 0
    token_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    # 内部状态
    _agent: Any = None  # 关联的 SpiritAgent 实例
    _is_zombie: bool = False  # 是否已失效

    def touch(self):
        """更新最后活动时间。"""
        self.last_activity = datetime.now()

    def increment_message(self, estimated_tokens: int = 0):
        """记录一条新消息。"""
        self.message_count += 1
        self.token_count += estimated_tokens
        self.touch()

    def is_idle_expired(self, policy: SessionResetPolicy) -> bool:
        """检查是否因空闲超时而需要重置。"""
        if policy.policy != ResetPolicy.TIME_BASED:
            return False
        idle_time = datetime.now() - self.last_activity
        return idle_time > timedelta(minutes=policy.idle_timeout_minutes)

    def is_message_limit_exceeded(self, policy: SessionResetPolicy) -> bool:
        """检查是否超过消息数限制。"""
        if policy.policy != ResetPolicy.MESSAGE_COUNT:
            return False
        return self.message_count >= policy.max_messages

    def is_token_budget_exceeded(self, policy: SessionResetPolicy) -> bool:
        """检查是否超过 Token 预算。"""
        if policy.policy != ResetPolicy.TOKEN_BUDGET:
            return False
        return self.token_count >= policy.max_tokens

    def should_reset(self, policy: SessionResetPolicy) -> bool:
        """评估是否应该重置会话。"""
        return (
            self.is_idle_expired(policy)
            or self.is_message_limit_exceeded(policy)
            or self.is_token_budget_exceeded(policy)
        )

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典。"""
        return {
            "session_id": self.session_id,
            "source": self.source.to_context_dict(),
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
            "message_count": self.message_count,
            "token_count": self.token_count,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# 会话存储
# ---------------------------------------------------------------------------

class SessionStore:
    """会话存储 — LRU 缓存 + 可选持久化。

    参考 Hermes 的 SessionStore：
    - 内存中的 LRU 缓存（按 session_key 索引）
    - 可选的磁盘持久化
    - 线程安全
    - Agent 缓存绑定

    Usage:
        store = SessionStore(max_size=32)
        ctx = store.get_or_create(source, platform_config)
        ctx.increment_message()
    """

    def __init__(
        self,
        max_size: int = 32,
        idle_ttl_seconds: float = 3600.0,
        persist_dir: Path = None,
    ):
        """初始化会话存储。

        Args:
            max_size: 最大缓存会话数
            idle_ttl_seconds: 空闲会话过期时间（秒）
            persist_dir: 持久化目录（None=不持久化）
        """
        self.max_size = max_size
        self.idle_ttl_seconds = idle_ttl_seconds
        self.persist_dir = persist_dir

        # LRU 缓存：session_key -> SessionContext
        self._cache: OrderedDict[str, SessionContext] = OrderedDict()
        self._lock = threading.Lock()

        # 反向索引：user_key -> [session_key, ...]
        self._user_sessions: Dict[str, List[str]] = {}

        if persist_dir:
            persist_dir.mkdir(parents=True, exist_ok=True)

    def get_or_create(
        self,
        source: SessionSource,
        platform_config: PlatformConfig = None,
    ) -> SessionContext:
        """获取或创建会话上下文。

        如果会话已存在则返回现有上下文，否则创建新的。
        如果旧会话应该重置（根据策略），则创建新会话。

        Args:
            source: 消息来源
            platform_config: 平台配置（用于重置策略评估）

        Returns:
            SessionContext 实例
        """
        session_key = source.session_key

        with self._lock:
            # 检查现有会话
            if session_key in self._cache:
                ctx = self._cache[session_key]

                # 评估是否需要重置
                if platform_config and ctx.should_reset(platform_config.reset_policy):
                    logger.info(
                        "会话 %s 触发重置策略 (%s), 创建新会话",
                        session_key,
                        platform_config.reset_policy.policy.value,
                    )
                    self._close_session(ctx)
                else:
                    # 移动到 LRU 末尾（最近使用）
                    self._cache.move_to_end(session_key)
                    ctx.touch()
                    return ctx

            # 创建新会话
            session_id = str(uuid.uuid4())
            ctx = SessionContext(
                session_id=session_id,
                source=source,
            )

            # 添加到缓存
            self._cache[session_key] = ctx

            # 更新用户索引
            user_key = source.user_key
            if user_key not in self._user_sessions:
                self._user_sessions[user_key] = []
            self._user_sessions[user_key].append(session_key)

            # LRU 淘汰
            self._evict_if_needed()

            logger.info(
                "创建新会话: %s (platform=%s, user=%s)",
                session_id[:8],
                source.platform.value,
                source.user_id,
            )

            return ctx

    def get(self, session_key: str) -> Optional[SessionContext]:
        """通过 session_key 获取会话。"""
        with self._lock:
            ctx = self._cache.get(session_key)
            if ctx:
                self._cache.move_to_end(session_key)
                ctx.touch()
            return ctx

    def get_by_session_id(self, session_id: str) -> Optional[SessionContext]:
        """通过 session_id 获取会话。"""
        with self._lock:
            for ctx in self._cache.values():
                if ctx.session_id == session_id:
                    return ctx
            return None

    def get_user_sessions(self, user_key: str) -> List[SessionContext]:
        """获取用户的所有会话。"""
        with self._lock:
            session_keys = self._user_sessions.get(user_key, [])
            return [
                self._cache[k]
                for k in session_keys
                if k in self._cache
            ]

    def remove(self, session_key: str) -> Optional[SessionContext]:
        """移除会话。"""
        with self._lock:
            ctx = self._cache.pop(session_key, None)
            if ctx:
                self._close_session(ctx)
                # 清理用户索引
                user_key = ctx.source.user_key
                if user_key in self._user_sessions:
                    self._user_sessions[user_key] = [
                        k for k in self._user_sessions[user_key]
                        if k != session_key
                    ]
            return ctx

    def list_sessions(self) -> List[SessionContext]:
        """列出所有活跃会话。"""
        with self._lock:
            return list(self._cache.values())

    def clear_expired(self):
        """清理过期会话。"""
        now = datetime.now()
        expired_keys = []

        with self._lock:
            for key, ctx in self._cache.items():
                idle_seconds = (now - ctx.last_activity).total_seconds()
                if idle_seconds > self.idle_ttl_seconds:
                    expired_keys.append(key)

            for key in expired_keys:
                self.remove(key)

        if expired_keys:
            logger.info("清理了 %d 个过期会话", len(expired_keys))

    @property
    def size(self) -> int:
        """当前缓存大小。"""
        return len(self._cache)

    def _evict_if_needed(self):
        """LRU 淘汰（内部方法，需在锁内调用）。"""
        while len(self._cache) > self.max_size:
            # 弹出最久未使用的
            key, ctx = self._cache.popitem(last=False)
            logger.debug("LRU 淘汰会话: %s", key)
            self._close_session(ctx)

    def _close_session(self, ctx: SessionContext):
        """关闭会话（内部方法）。"""
        ctx._is_zombie = True
        # 可选：持久化到磁盘
        if self.persist_dir:
            self._persist_session(ctx)

    def _persist_session(self, ctx: SessionContext):
        """持久化会话到磁盘。"""
        try:
            import json
            path = self.persist_dir / f"{ctx.session_id}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(ctx.to_dict(), f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning("持久化会话失败: %s", e)


# ---------------------------------------------------------------------------
# 会话上下文提示词构建
# ---------------------------------------------------------------------------

def build_session_context_prompt(
    source: SessionSource,
    session_id: str,
    extra_context: Dict[str, Any] = None,
) -> str:
    """构建会话上下文提示词 — 注入到系统提示词中。

    让 Agent 知道消息来自哪个平台、哪个用户、什么类型的聊天。

    Args:
        source: 消息来源
        session_id: 会话 ID
        extra_context: 额外上下文

    Returns:
        上下文提示词字符串
    """
    parts = [
        f"[Session Context]",
        f"- Platform: {source.platform.value}",
        f"- Chat Type: {source.chat_type}",
        f"- Session ID: {session_id[:8]}",
    ]

    if source.username:
        parts.append(f"- User: {source.username}")
    elif source.display_name:
        parts.append(f"- User: {source.display_name}")

    if source.chat_type == "group":
        parts.append("- Note: This is a group chat. Be concise and only respond when addressed.")
    elif source.chat_type == "forum":
        parts.append("- Note: This is a forum thread. Stay on topic.")

    if extra_context:
        for key, value in extra_context.items():
            parts.append(f"- {key}: {value}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def hash_id(value: str) -> str:
    """生成确定性哈希 ID（用于隐私保护）。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def hash_user_id(value: str) -> str:
    """哈希用户 ID。"""
    return f"user_{hash_id(value)}"


def hash_chat_id(value: str) -> str:
    """哈希聊天 ID（保留平台前缀）。"""
    colon = value.find(":")
    if colon > 0:
        prefix = value[:colon]
        return f"{prefix}:{hash_id(value[colon + 1:])}"
    return hash_id(value)
