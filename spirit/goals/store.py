"""Goal 持久化后端。

参考 Hermes ``hermes_cli/goals.py`` 的 load_goal / save_goal / clear_goal /
migrate_goal_to_session，但抽象成可注入的 :class:`GoalStore`，让 GoalManager
与具体存储解耦（对齐 Spirit ``task/`` 的依赖注入风格，也便于测试）：

- :class:`SessionDBGoalStore` — 生产默认。用 SessionDB 的 ``state_meta`` 表存
  ``key=goal:<session_id>`` 的 JSON，因此 ``/resume`` 能重新捡起未完成的目标。
- :class:`InMemoryGoalStore` — 测试 / 无 DB 环境。同样存 JSON、get 返回反序列化
  副本，行为与 DB 版逐点一致（避免测试与生产语义漂移）。

``migrate_goal_to_session`` 处理上下文压缩轮换 session_id 的边界：压缩把
session 转到新的子会话，而 goal 是 ``goal:<session_id>`` 平查、无父系回溯——
不迁移的话活跃目标会在压缩边界静默死亡。
"""

from __future__ import annotations

import logging
from typing import Optional

from spirit.goals.goal_state import GoalState

logger = logging.getLogger(__name__)


def _meta_key(session_id: str) -> str:
    return f"goal:{session_id}"


class GoalStore:
    """目标状态存储抽象接口。"""

    def get(self, session_id: str) -> Optional[GoalState]:
        raise NotImplementedError

    def save(self, session_id: str, state: GoalState) -> None:
        raise NotImplementedError

    def clear(self, session_id: str) -> None:
        """标记为 cleared 但保留记录（审计用），而非物理删除。"""
        raise NotImplementedError


class InMemoryGoalStore(GoalStore):
    """内存存储（测试 / 无 DB 环境）。存 JSON、get 返回副本，语义对齐 DB 版。"""

    def __init__(self):
        self._data: dict = {}  # meta_key -> json str

    def get(self, session_id: str) -> Optional[GoalState]:
        if not session_id:
            return None
        raw = self._data.get(_meta_key(session_id))
        if not raw:
            return None
        try:
            return GoalState.from_json(raw)
        except Exception as exc:
            logger.warning("InMemoryGoalStore: 无法解析 %s 的目标: %s", session_id, exc)
            return None

    def save(self, session_id: str, state: GoalState) -> None:
        if not session_id:
            return
        self._data[_meta_key(session_id)] = state.to_json()

    def clear(self, session_id: str) -> None:
        state = self.get(session_id)
        if state is None:
            return
        state.status = "cleared"
        self.save(session_id, state)


class SessionDBGoalStore(GoalStore):
    """用 SessionDB 的 state_meta 表持久化（key = ``goal:<session_id>``）。

    对 DB 不可用/异常全程防御性降级：get 返回 None、save 静默 no-op，绝不抛——
    存储故障不能卡死目标循环（内存态仍可用，只是不落盘、/resume 捡不回）。
    """

    def __init__(self, db=None):
        # db 可显式注入（如 SpiritAgent._session_db）；为 None 时延迟创建默认实例。
        self._db = db

    def _resolve_db(self):
        if self._db is not None:
            return self._db
        try:
            from spirit.storage.session_db import SessionDB

            self._db = SessionDB()
        except Exception as exc:  # pragma: no cover - 防御
            logger.debug("SessionDBGoalStore: SessionDB 引导失败 (%s)", exc)
            self._db = None
        return self._db

    def get(self, session_id: str) -> Optional[GoalState]:
        if not session_id:
            return None
        db = self._resolve_db()
        if db is None:
            return None
        try:
            raw = db.get_meta(_meta_key(session_id))
        except Exception as exc:
            logger.debug("SessionDBGoalStore: get_meta 失败: %s", exc)
            return None
        if not raw:
            return None
        try:
            return GoalState.from_json(raw)
        except Exception as exc:
            logger.warning("SessionDBGoalStore: 无法解析 %s 的目标: %s", session_id, exc)
            return None

    def save(self, session_id: str, state: GoalState) -> None:
        if not session_id:
            return
        db = self._resolve_db()
        if db is None:
            return
        try:
            db.set_meta(_meta_key(session_id), state.to_json())
        except Exception as exc:
            logger.debug("SessionDBGoalStore: set_meta 失败: %s", exc)

    def clear(self, session_id: str) -> None:
        state = self.get(session_id)
        if state is None:
            return
        state.status = "cleared"
        self.save(session_id, state)


def migrate_goal_to_session(
    old_session_id: str,
    new_session_id: str,
    *,
    store: GoalStore,
    reason: str = "",
) -> bool:
    """把一个持久 /goal 从父会话带到它的续接会话。

    上下文压缩会把 ``session_id`` 轮换到一个新的子会话，而 goal 是平查
    ``goal:<session_id>``、无父系回溯——活跃目标会在压缩边界静默死亡。把目标
    复制到新会话，并把旧行归档为 ``cleared``，使每个逻辑对话恰好只有一条活跃
    目标行（避免纯复制导致的"两条活跃目标"隐患）。

    迁移了返回 True；无可迁移或存储不可用返回 False。尽力而为、绝不抛——
    这里的失败不能阻塞压缩。
    """
    if not old_session_id or not new_session_id or old_session_id == new_session_id:
        return False
    try:
        state = store.get(old_session_id)
        if state is None or getattr(state, "status", None) == "cleared":
            return False
        # 不要覆盖子会话上已设置的目标（如一个 resume 的血统重建了自己的目标）。
        if store.get(new_session_id) is not None:
            return False
        store.save(new_session_id, state)
        # 归档父会话的行，免得被重复计为活跃。
        store.clear(old_session_id)
        logger.debug(
            "goals: 已迁移目标 %s -> %s (%s)",
            old_session_id, new_session_id, reason or "rotation",
        )
        return True
    except Exception as exc:  # pragma: no cover - 防御
        logger.debug("goals: 目标迁移失败: %s", exc)
        return False


__all__ = [
    "GoalStore",
    "InMemoryGoalStore",
    "SessionDBGoalStore",
    "migrate_goal_to_session",
]
