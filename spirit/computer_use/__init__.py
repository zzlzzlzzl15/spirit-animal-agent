"""Computer Use 桌面控制 — Spirit Agent（Phase 4.5）。

对标 Hermes ``tools/computer_use/``，但落地为**可测试抽象层**：把「桌面控制的形状」
（:mod:`backend`）、「安全 / 审批 / 派发策略」（:mod:`safety` / :mod:`tool`）、「就绪度探测」
（:mod:`permissions`）、「视觉路由决策」（:mod:`vision_routing`）与「具体平台驱动」彻底解耦。

真实驱动（cua-driver 之类）不在本包内实现——它经 :func:`spirit.computer_use.tool.set_backend_factory`
注册即可启用；缺省以 :class:`~spirit.computer_use.noop_backend.NoopBackend` 安全降级
（动作只记录、不触碰真实桌面），故整套逻辑可在无图形会话的 CI 里离线单测。

模块地图（对齐 Hermes）::

    backend.py         抽象 ComputerUseBackend + UIElement/CaptureResult/ActionResult
    noop_backend.py    一等内存后端（记录调用，测试 / CI seam）
    schema.py          通用 computer_use 工具 schema（模型无关）
    safety.py          动作分级 + 硬阻止组合键 + 危险输入模式
    permissions.py     跨平台就绪度 / 权限探测（可注入 subprocess seam）
    vision_routing.py  捕获截图的多模态 vs 辅助视觉路由决策（可注入查询 seam）
    tool.py            安全 → 审批门 → 后端派发 → 响应塑形

工具注册在发现 shim ``spirit/tools/computer_use_tool.py``（对标 Hermes
``tools/computer_use_tool.py``），使 ``discover_tools()`` 扁平扫描 ``spirit/tools/*.py``
时能发现本子包的工具。
"""

from __future__ import annotations

from spirit.computer_use.backend import (
    ActionResult,
    CaptureResult,
    ComputerUseBackend,
    UIElement,
)
from spirit.computer_use.noop_backend import NoopBackend
from spirit.computer_use.permissions import (
    computer_use_status,
    request_permissions_grant,
)
from spirit.computer_use.schema import (
    COMPUTER_USE_SCHEMA,
    get_computer_use_schema,
)
from spirit.computer_use.tool import (
    check_computer_use_requirements,
    handle_computer_use,
    reset_backend_for_tests,
    set_approval_callback,
    set_backend,
    set_backend_factory,
)
from spirit.computer_use.vision_routing import should_route_capture_to_aux_vision

__all__ = [
    # backend 抽象
    "ComputerUseBackend",
    "UIElement",
    "CaptureResult",
    "ActionResult",
    "NoopBackend",
    # schema
    "COMPUTER_USE_SCHEMA",
    "get_computer_use_schema",
    # tool 派发 + seam
    "handle_computer_use",
    "check_computer_use_requirements",
    "set_approval_callback",
    "set_backend",
    "set_backend_factory",
    "reset_backend_for_tests",
    # permissions / vision routing
    "computer_use_status",
    "request_permissions_grant",
    "should_route_capture_to_aux_vision",
]
