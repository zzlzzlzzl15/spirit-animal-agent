"""``computer_use`` 工具发现 shim — 把桌面控制工具注册进 tools.registry。

真实实现在 :mod:`spirit.computer_use` 子包里（backend 抽象 + noop 后端 + schema +
安全分级 + 审批门 + 派发 + 响应塑形 + 就绪度探测 + 视觉路由）。本 shim 只因
:func:`spirit.tools.registry.discover_tools` 扁平扫描 ``spirit/tools/*.py`` 而存在——
需要一个顶层模块触发注册（逐字对标 Hermes ``tools/computer_use_tool.py``）。

注册在此处（而非子包 ``tool.py``）保持单一权威：``discover_tools()`` 经 AST 检测到本文件
顶层的 ``registry.register(...)`` 调用后导入它，从而完成注册。
"""

from __future__ import annotations

from spirit.computer_use.schema import COMPUTER_USE_SCHEMA
from spirit.computer_use.tool import (
    check_computer_use_requirements,
    handle_computer_use,
    set_approval_callback,
)
from spirit.tools.registry import registry


registry.register(
    name="computer_use",
    toolset="computer_use",
    schema=COMPUTER_USE_SCHEMA,
    handler=handle_computer_use,
    check_fn=check_computer_use_requirements,
    emoji="🖥️",
    description=(
        "通用桌面控制（macOS / Windows / Linux），任何具备工具调用能力的模型都可驱动。"
        "落地为可测试抽象层：真实驱动经 set_backend_factory 注入；缺省以 noop 后端安全降级"
        "（动作只记录、不触碰真实桌面）。视觉模型用 SOM（set-of-mark）捕获按元素序号点击，"
        "非视觉模型可仅凭 AX 树驱动。后台控制不抢占用户的鼠标 / 键盘焦点。"
    ),
)


__all__ = [
    "handle_computer_use",
    "set_approval_callback",
    "check_computer_use_requirements",
]
