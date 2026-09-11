"""Pet 精灵图常量 — 帧几何 + 状态枚举 + 行列映射。

借鉴 Hermes agent/pet/constants.py 设计：
- 192×208px 帧尺寸（petdex 标准）
- 8 列 × 9 行精灵图布局
- PetState 枚举（7 种动画状态）
- 状态别名映射（兼容 petdex 命名）

精灵图行映射（8col × 9row = 1536×1872px）：
    Row 0: idle          → 空闲待机（呼吸动画）
    Row 1: running-right → 向右跑
    Row 2: running-left  → 向左跑
    Row 3: waving        → 挥手（任务完成）
    Row 4: jumping       → 跳跃（庆祝）
    Row 5: failed        → 失败（错误）
    Row 6: waiting       → 等待（需要输入）
    Row 7: running       → 工作中（工具执行）
    Row 8: review        → 思考中（推理）
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, List, Tuple

# ---------------------------------------------------------------------------
# 帧几何（petdex 标准）
# ---------------------------------------------------------------------------

FRAME_W = 192  # 帧宽度（像素）
FRAME_H = 208  # 帧高度（像素）

FRAMES_PER_STATE = 6  # 每个状态消耗的帧数
LOOP_MS = 1100  # 一个状态循环的时长（毫秒）

# 默认显示缩放
DEFAULT_SCALE = 0.75
MIN_SCALE = 0.2
MAX_SCALE = 3.0


def clamp_scale(scale: float) -> float:
    """将缩放值限制在 [MIN_SCALE, MAX_SCALE] 范围内。"""
    return max(MIN_SCALE, min(MAX_SCALE, scale))


# ---------------------------------------------------------------------------
# PetState 枚举
# ---------------------------------------------------------------------------

class PetState(str, Enum):
    """宠物动画状态。

    每个状态对应精灵图的一行。优先级从高到低：
    1. FAILED  — 工具/回合失败
    2. JUMP    — 任务完成庆祝
    3. WAVE    — 回合完成/打招呼
    4. WAITING — 等待用户输入
    5. RUN     — 工具执行中
    6. REVIEW  — 模型思考中
    7. IDLE    — 空闲待机
    """

    IDLE = "idle"
    WAVE = "wave"
    RUN = "run"
    FAILED = "failed"
    REVIEW = "review"
    JUMP = "jump"
    WAITING = "waiting"


# ---------------------------------------------------------------------------
# 精灵图行映射
# ---------------------------------------------------------------------------

# Codex/petdex 标准 9 行布局
STATE_ROWS: List[str] = [
    PetState.IDLE.value,        # Row 0
    "running-right",            # Row 1
    "running-left",             # Row 2
    "waving",                   # Row 3
    "jumping",                  # Row 4
    PetState.FAILED.value,      # Row 5
    PetState.WAITING.value,     # Row 6
    "running",                  # Row 7
    PetState.REVIEW.value,      # Row 8
]

# 状态别名映射（内部名 → 精灵图行名）
STATE_ALIASES: Dict[str, Tuple[str, ...]] = {
    PetState.IDLE.value: (PetState.IDLE.value,),
    PetState.WAVE.value: (PetState.WAVE.value, "waving"),
    PetState.JUMP.value: (PetState.JUMP.value, "jumping"),
    PetState.RUN.value: (PetState.RUN.value, "running"),
    PetState.FAILED.value: (PetState.FAILED.value,),
    PetState.REVIEW.value: (PetState.REVIEW.value,),
    PetState.WAITING.value: (PetState.WAITING.value,),
}


def state_aliases_for(state: PetState | str) -> Tuple[str, ...]:
    """返回状态的所有可接受行名别名。"""
    value = state.value if isinstance(state, PetState) else str(state)
    return STATE_ALIASES.get(value, (value,))


def state_row_index(state: PetState | str) -> int:
    """返回状态在精灵图中的行索引（0-based）。

    按别名优先级查找，找不到则回退到 0（idle 行）。
    """
    for name in state_aliases_for(state):
        try:
            return STATE_ROWS.index(name)
        except ValueError:
            continue
    return 0


def display_size(scale: float = DEFAULT_SCALE) -> Tuple[int, int]:
    """返回缩放后的显示尺寸（宽, 高）。"""
    return (
        max(1, int(FRAME_W * scale)),
        max(1, int(FRAME_H * scale)),
    )


__all__ = [
    "FRAME_W",
    "FRAME_H",
    "FRAMES_PER_STATE",
    "LOOP_MS",
    "DEFAULT_SCALE",
    "MIN_SCALE",
    "MAX_SCALE",
    "clamp_scale",
    "PetState",
    "STATE_ROWS",
    "STATE_ALIASES",
    "state_aliases_for",
    "state_row_index",
    "display_size",
]
