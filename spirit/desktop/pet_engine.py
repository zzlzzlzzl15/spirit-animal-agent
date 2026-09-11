"""PetEngine — 桌宠核心引擎。

组合 pet_constants + pet_state + pet_store，提供统一 API：
- 状态管理：接收 Agent 活动信号 → 推导 PetState → 通知前端
- 宠物管理：加载/切换/注册/列出已安装宠物
- 配置持久化：当前宠物 slug、位置、缩放等偏好
- 事件广播：状态变更时通知所有 WebSocket 订阅者

架构定位：
    PetEngine 是 Python 后端的"中枢"，
    ws_server 通过它读写状态，
    tray_icon 通过它切换宠物，
    前端通过 WebSocket 间接调用它的 API。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from spirit.desktop.pet_constants import (
    DEFAULT_SCALE,
    FRAME_W,
    FRAME_H,
    PetState,
    clamp_scale,
    state_row_index,
)
from spirit.desktop.pet_state import derive_pet_state, todos_all_done
from spirit.desktop import pet_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class ActivitySignals:
    """Agent 活动信号集合 — 用于推导 PetState。

    各表面（CLI/TUI/Desktop/Gateway）将自己的信号写入，
    PetEngine 按优先级推导最终动画状态。
    """

    busy: bool = False
    awaiting_input: bool = False
    error: bool = False
    celebrate: bool = False
    just_completed: bool = False
    tool_running: bool = False
    reasoning: bool = False

    def reset_transient(self) -> None:
        """清除瞬态信号（每轮结束调用）。

        瞬态信号：error / celebrate / just_completed
        持久信号：busy / awaiting_input / tool_running / reasoning（不清除）
        """
        self.error = False
        self.celebrate = False
        self.just_completed = False

    def to_dict(self) -> Dict[str, bool]:
        return {
            "busy": self.busy,
            "awaiting_input": self.awaiting_input,
            "error": self.error,
            "celebrate": self.celebrate,
            "just_completed": self.just_completed,
            "tool_running": self.tool_running,
            "reasoning": self.reasoning,
        }


@dataclass
class PetPreferences:
    """宠物展示偏好（持久化到 ~/.spirit/pet_prefs.json）。"""

    active_slug: str = ""
    scale: float = DEFAULT_SCALE
    pos_x: int = -1  # -1 表示未设置（前端居中）
    pos_y: int = -1
    speech_enabled: bool = True
    notifications_enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active_slug": self.active_slug,
            "scale": self.scale,
            "pos_x": self.pos_x,
            "pos_y": self.pos_y,
            "speech_enabled": self.speech_enabled,
            "notifications_enabled": self.notifications_enabled,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PetPreferences":
        prefs = cls()
        for key in ("active_slug", "scale", "pos_x", "pos_y",
                     "speech_enabled", "notifications_enabled"):
            if key in data and data[key] is not None:
                setattr(prefs, key, data[key])
        # 安全校验：缩放不能小于 1.0（防止旧配置搞事）
        prefs.scale = max(0.5, min(3.0, prefs.scale))
        return prefs


# ---------------------------------------------------------------------------
# 回调类型
# ---------------------------------------------------------------------------

StateChangeCallback = Callable[[PetState, PetState, Dict[str, Any]], None]
# (old_state, new_state, context) -> None

EventCallback = Callable[[str, Dict[str, Any]], None]
# (event_type, payload) -> None


# ---------------------------------------------------------------------------
# PetEngine 核心
# ---------------------------------------------------------------------------

class PetEngine:
    """桌宠核心引擎。

    职责：
    1. 管理当前宠物（加载/切换/注册）
    2. 管理活动信号 → PetState 推导
    3. 管理展示偏好（缩放/位置/语音）
    4. 广播状态变更事件
    """

    def __init__(self, *, config_slug: str = ""):
        """初始化引擎。

        Args:
            config_slug: 配置文件中指定的宠物 slug（可选）
        """
        # 偏好
        self._prefs = self._load_preferences()
        if config_slug and not self._prefs.active_slug:
            self._prefs.active_slug = config_slug

        # 活动信号
        self._signals = ActivitySignals()

        # 当前状态
        self._current_state = PetState.IDLE
        self._previous_state = PetState.IDLE
        self._state_changed_at = time.monotonic()
        self._last_activity_at = time.monotonic()

        # 事件订阅
        self._state_callbacks: List[StateChangeCallback] = []
        self._event_callbacks: List[EventCallback] = []

        # 当前宠物缓存
        self._current_pet: Optional[pet_store.InstalledPet] = None
        self._resolve_current_pet()

        # 对话/任务统计
        self._stats: Dict[str, Any] = {
            "total_turns": 0,
            "total_tool_calls": 0,
            "total_errors": 0,
            "last_response_text": "",
            "current_task": "",
        }

        logger.info(
            "PetEngine 初始化: pet=%s, state=%s",
            self._current_pet.slug if self._current_pet else "none",
            self._current_state.value,
        )

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def current_state(self) -> PetState:
        """当前动画状态。"""
        return self._current_state

    @property
    def previous_state(self) -> PetState:
        """上一个动画状态。"""
        return self._previous_state

    @property
    def signals(self) -> ActivitySignals:
        """当前活动信号（只读引用）。"""
        return self._signals

    @property
    def preferences(self) -> PetPreferences:
        """展示偏好（可修改）。"""
        return self._prefs

    @property
    def current_pet(self) -> Optional[pet_store.InstalledPet]:
        """当前激活的宠物。"""
        return self._current_pet

    @property
    def state_duration(self) -> float:
        """当前状态持续时间（秒）。"""
        return time.monotonic() - self._state_changed_at

    # ------------------------------------------------------------------
    # 状态管理
    # ------------------------------------------------------------------

    def update_signals(self, **kwargs: bool) -> PetState:
        """更新活动信号并推导新状态。

        Args:
            **kwargs: 要更新的信号（如 busy=True, error=False）

        Returns:
            推导后的新 PetState
        """
        for key, value in kwargs.items():
            if hasattr(self._signals, key):
                setattr(self._signals, key, bool(value))

        new_state = derive_pet_state(
            busy=self._signals.busy,
            awaiting_input=self._signals.awaiting_input,
            error=self._signals.error,
            celebrate=self._signals.celebrate,
            just_completed=self._signals.just_completed,
            tool_running=self._signals.tool_running,
            reasoning=self._signals.reasoning,
        )

        self._transition_to(new_state)
        self._last_activity_at = time.monotonic()
        return new_state

    def set_state(self, state: PetState) -> None:
        """强制设置动画状态（跳过信号推导）。"""
        self._transition_to(state)

    def on_turn_start(self) -> None:
        """Agent 对话轮开始。"""
        self._signals.busy = True
        self._signals.just_completed = False
        self._transition_to(derive_pet_state(
            busy=self._signals.busy,
            awaiting_input=self._signals.awaiting_input,
            error=self._signals.error,
            celebrate=self._signals.celebrate,
            just_completed=self._signals.just_completed,
            tool_running=self._signals.tool_running,
            reasoning=self._signals.reasoning,
        ))

    def on_turn_complete(self, *, response_text: str = "", error: bool = False) -> None:
        """Agent 对话轮完成。"""
        self._signals.busy = False
        self._signals.reasoning = False
        self._signals.tool_running = False
        self._stats["total_turns"] += 1
        if response_text:
            self._stats["last_response_text"] = response_text[:200]

        if error:
            self._signals.error = True
            self._stats["total_errors"] += 1
            new_state = PetState.FAILED
        else:
            self._signals.just_completed = True
            new_state = PetState.WAVE

        self._transition_to(new_state)

        # 延迟清除瞬态信号
        self._signals.reset_transient()

    def on_tool_start(self, tool_name: str = "") -> None:
        """工具执行开始。"""
        self._signals.tool_running = True
        self._transition_to(PetState.RUN)

    def on_tool_complete(self, tool_name: str = "", *, error: bool = False) -> None:
        """工具执行完成。"""
        self._signals.tool_running = False
        self._stats["total_tool_calls"] += 1
        if error:
            self._signals.error = True
            self._stats["total_errors"] += 1
            self._transition_to(PetState.FAILED)
        else:
            self._transition_to(derive_pet_state(
                busy=self._signals.busy,
                awaiting_input=self._signals.awaiting_input,
                error=self._signals.error,
                celebrate=self._signals.celebrate,
                just_completed=self._signals.just_completed,
                tool_running=self._signals.tool_running,
                reasoning=self._signals.reasoning,
            ))

    def on_reasoning_start(self) -> None:
        """模型开始思考。"""
        self._signals.reasoning = True
        self._transition_to(PetState.REVIEW)

    def on_reasoning_complete(self) -> None:
        """模型思考完成。"""
        self._signals.reasoning = False
        self._transition_to(derive_pet_state(
            busy=self._signals.busy,
            awaiting_input=self._signals.awaiting_input,
            error=self._signals.error,
            celebrate=self._signals.celebrate,
            just_completed=self._signals.just_completed,
            tool_running=self._signals.tool_running,
            reasoning=self._signals.reasoning,
        ))

    def on_awaiting_input(self) -> None:
        """等待用户输入。"""
        self._signals.awaiting_input = True
        self._signals.busy = False
        self._transition_to(PetState.WAITING)

    def on_todos_complete(self, todos: list) -> None:
        """待办全部完成 → 庆祝。"""
        if todos_all_done(todos):
            self._signals.celebrate = True
            self._transition_to(PetState.JUMP)

    def idle(self) -> None:
        """回到空闲状态。"""
        self._signals = ActivitySignals()
        self._transition_to(PetState.IDLE)

    # ------------------------------------------------------------------
    # 宠物管理
    # ------------------------------------------------------------------

    def switch_pet(self, slug: str) -> bool:
        """切换到另一个宠物。

        Returns:
            是否切换成功
        """
        pet = pet_store.load_pet(slug)
        if pet is None or not pet.exists:
            logger.warning("无法切换宠物: %s (不存在或精灵图缺失)", slug)
            return False

        old_slug = self._current_pet.slug if self._current_pet else ""
        self._current_pet = pet
        self._prefs.active_slug = slug
        self._save_preferences()

        self._emit_event("pet_switch", {
            "old_slug": old_slug,
            "new_slug": slug,
            "display_name": pet.display_name,
        })
        logger.info("宠物已切换: %s → %s", old_slug, slug)
        return True

    def install_pet(
        self,
        spritesheet_source,
        *,
        slug: str = "",
        display_name: str = "",
        description: str = "",
    ) -> pet_store.InstalledPet:
        """安装新宠物。"""
        pet = pet_store.register_pet(
            spritesheet_source,
            slug=slug,
            display_name=display_name,
            description=description,
        )
        self._emit_event("pet_installed", {
            "slug": pet.slug,
            "display_name": pet.display_name,
        })
        return pet

    def remove_pet(self, slug: str) -> bool:
        """卸载宠物。如果卸载的是当前宠物，自动切换到下一个。"""
        ok = pet_store.remove_pet(slug)
        if not ok:
            return False

        # 如果卸载的是当前宠物，切换
        if self._current_pet and self._current_pet.slug == slug:
            self._current_pet = None
            self._resolve_current_pet()

        self._emit_event("pet_removed", {"slug": slug})
        return True

    def list_pets(self) -> List[Dict[str, Any]]:
        """列出所有已安装的宠物。"""
        pets = pet_store.installed_pets()
        return [
            {
                "slug": p.slug,
                "displayName": p.display_name,
                "description": p.description,
                "spritesheetExists": p.exists,
                "isActive": (
                    self._current_pet is not None
                    and self._current_pet.slug == p.slug
                ),
            }
            for p in pets
        ]

    # ------------------------------------------------------------------
    # 偏好管理
    # ------------------------------------------------------------------

    def set_scale(self, scale: float) -> None:
        """设置缩放。"""
        self._prefs.scale = clamp_scale(scale)
        self._save_preferences()

    def set_position(self, x: int, y: int) -> None:
        """设置窗口位置。"""
        self._prefs.pos_x = x
        self._prefs.pos_y = y
        self._save_preferences()

    # ------------------------------------------------------------------
    # 序列化（WebSocket 传输用）
    # ------------------------------------------------------------------

    def get_full_status(self) -> Dict[str, Any]:
        """获取完整状态快照（用于 WebSocket 初始同步）。"""
        pet_info = {}
        if self._current_pet:
            pet_info = pet_store.get_pet_info(self._current_pet.slug)

        return {
            "pet": {
                "slug": self._current_pet.slug if self._current_pet else "",
                "displayName": self._current_pet.display_name if self._current_pet else "",
                "spritesheetExists": self._current_pet.exists if self._current_pet else False,
                **pet_info,
            },
            "state": {
                "current": self._current_state.value,
                "previous": self._previous_state.value,
                "duration": round(self.state_duration, 1),
                "row": state_row_index(self._current_state),
            },
            "signals": self._signals.to_dict(),
            "preferences": self._prefs.to_dict(),
            "stats": dict(self._stats),
            "frame": {
                "width": FRAME_W,
                "height": FRAME_H,
            },
        }

    def get_pet_info(self) -> Dict[str, Any]:
        """获取当前宠物信息。"""
        if not self._current_pet:
            return {}
        return pet_store.get_pet_info(self._current_pet.slug)

    # ------------------------------------------------------------------
    # 事件订阅
    # ------------------------------------------------------------------

    def on_state_change(self, callback: StateChangeCallback) -> None:
        """注册状态变更回调。"""
        self._state_callbacks.append(callback)

    def on_event(self, callback: EventCallback) -> None:
        """注册通用事件回调。"""
        self._event_callbacks.append(callback)

    def remove_callback(self, callback: Callable) -> None:
        """移除回调。"""
        if callback in self._state_callbacks:
            self._state_callbacks.remove(callback)
        if callback in self._event_callbacks:
            self._event_callbacks.remove(callback)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _transition_to(self, new_state: PetState) -> None:
        """执行状态转换。"""
        if new_state == self._current_state:
            return

        old_state = self._current_state
        self._previous_state = old_state
        self._current_state = new_state
        self._state_changed_at = time.monotonic()

        context = {
            "old_state": old_state.value,
            "new_state": new_state.value,
            "signals": self._signals.to_dict(),
        }

        # 通知状态变更回调
        for cb in self._state_callbacks:
            try:
                cb(old_state, new_state, context)
            except Exception as exc:
                logger.warning("状态回调异常: %s", exc)

        # 广播事件
        self._emit_event("state_change", context)

    def _emit_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """广播通用事件。"""
        for cb in self._event_callbacks:
            try:
                cb(event_type, payload)
            except Exception as exc:
                logger.warning("事件回调异常: %s", exc)

    def _resolve_current_pet(self) -> None:
        """解析当前应展示的宠物。"""
        self._current_pet = pet_store.resolve_active_pet(
            self._prefs.active_slug or None
        )
        if self._current_pet:
            self._prefs.active_slug = self._current_pet.slug

    def _load_preferences(self) -> PetPreferences:
        """从磁盘加载偏好。"""
        prefs_path = self._prefs_path()
        if not prefs_path.is_file():
            return PetPreferences()
        try:
            data = json.loads(prefs_path.read_text(encoding="utf-8"))
            return PetPreferences.from_dict(data)
        except (OSError, ValueError) as exc:
            logger.debug("偏好加载失败: %s", exc)
            return PetPreferences()

    def _save_preferences(self) -> None:
        """保存偏好到磁盘。"""
        prefs_path = self._prefs_path()
        try:
            prefs_path.parent.mkdir(parents=True, exist_ok=True)
            prefs_path.write_text(
                json.dumps(self._prefs.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("偏好保存失败: %s", exc)

    @staticmethod
    def _prefs_path() -> Path:
        """偏好文件路径。"""
        import os
        home = os.environ.get("SPIRIT_HOME", "")
        base = Path(home) if home else Path.home() / ".spirit"
        return base / "pet_prefs.json"


__all__ = [
    "PetEngine",
    "ActivitySignals",
    "PetPreferences",
]
