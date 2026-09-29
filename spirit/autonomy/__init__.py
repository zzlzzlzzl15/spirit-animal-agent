"""Spirit Autonomy —— 常驻自主探索子系统（Phase 7）。

把 Phase 6 的自进化能力单元（Curriculum / EvolutionLoop / HITL / Scheduler / Memory）
串成**永不停机的自主外层循环**，解决"任务做完就停"的问题：空闲/定时触发 → 提议探索
目标 → 深挖执行 → 遇分叉/停滞问用户（无应答 fail-open 自主决策）→ 沙箱写笔记 → 沉淀
记忆 → 提议下一个…… 形态为桌宠内置常驻，写操作限定沙箱目录。

模块：
    - :mod:`spirit.autonomy.sandbox`  — 沙箱写入护栏（路径 containment）
    - :mod:`spirit.autonomy.explorer` — AutonomyLoop 常驻自主循环
    - :mod:`spirit.autonomy.bridge`   — 桌宠 WS 桥接（HITL ask ↔ 气泡/答复）
"""

from spirit.autonomy.sandbox import Sandbox, SandboxViolation, SANDBOX_DIRNAME
from spirit.autonomy.explorer import (
    AutonomyLoop,
    ExploreCycle,
    ProposeFn,
    IDLE,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_DAILY_AT,
)
from spirit.autonomy.bridge import (
    DesktopBridge,
    bridge_from_ws,
    EVENT_DECISION,
    CMD_ANSWER,
    CMD_STATUS,
    CMD_START,
    CMD_STOP,
)
from spirit.autonomy.integration import (
    build_autonomy,
    make_llm_caller,
    make_memory,
    make_execute,
    make_proposer,
    resolve_interval_seconds,
    resolve_stop_policy,
    resolve_max_rounds,
    DEFAULT_RESIDENT_INTERVAL_SECONDS,
    start_autonomy,
)

__all__ = [
    # sandbox
    "Sandbox",
    "SandboxViolation",
    "SANDBOX_DIRNAME",
    # explorer
    "AutonomyLoop",
    "ExploreCycle",
    "ProposeFn",
    "IDLE",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_DAILY_AT",
    # bridge
    "DesktopBridge",
    "bridge_from_ws",
    "EVENT_DECISION",
    "CMD_ANSWER",
    "CMD_STATUS",
    "CMD_START",
    "CMD_STOP",
    # integration
    "build_autonomy",
    "start_autonomy",
    "make_llm_caller",
    "make_memory",
    "make_execute",
    "make_proposer",
    "resolve_interval_seconds",
    "resolve_stop_policy",
    "resolve_max_rounds",
    "DEFAULT_RESIDENT_INTERVAL_SECONDS",
]
