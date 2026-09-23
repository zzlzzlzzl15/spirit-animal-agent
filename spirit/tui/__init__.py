"""Spirit TUI 网关 — 终端界面的 Python 侧精简子集（Phase 4.4）。

对标 Hermes ``tui_gateway/``（Python 网关层；前端 Ink/React 界面不在此列）。本包把 TUI 前端
需要的 Python 侧能力收敛成 4 个**可测试抽象层**模块——所有外部副作用（git 探测、富文本渲染、
斜杠命令子进程、WebSocket 传输、时钟、id 生成）都是可注入 seam，故全部逻辑离线单测。

模块地图：

``project_tree.py``
    权威的 项目 → 仓库 → 泳道 → 会话 树构建器（纯 stdlib 移植自 Hermes，逻辑逐字对齐）。
    ``build_tree(projects, sessions, discovered_repos, resolve, ...)`` 是唯一真相源，git 解析经
    ``resolve`` 注入。id / 泳道键与前端持久化状态字节兼容。
``render.py``
    渲染桥接：``render_message`` / ``render_diff`` / ``make_stream_renderer`` 惰性路由到可选的
    Python 侧渲染器（``renderer=`` seam 注入）；缺失则返回 None，前端回退自渲染。
``slash_worker.py``
    持久化斜杠命令 worker：``serve_stream(inp, out, runner)`` 是解耦的 JSON-line 协议循环
    （``{id, command}`` → ``{id, ok, output|error}``）；``_is_orphaned`` 经 ``getppid`` seam 做
    父进程死亡看门狗。``main`` 是接真实 stdin/stdout 的薄壳。
``tui_server.py``
    最小传输无关网关：``TuiGateway.handle_message(msg) -> 响应信封`` 是纯同步派发，接线上述三
    模块成 RPC（``projects.tree`` / ``render.*`` / ``slash.exec`` / ``session.*``）；信封与
    ``spirit.desktop.ws_server.WSServer`` 的 ``type: response/event`` 约定一致。
"""

from __future__ import annotations

from spirit.tui.render import make_stream_renderer, render_diff, render_message
from spirit.tui.slash_worker import normalize_command, serve_stream
from spirit.tui.tui_server import EVENT_TYPE, RESPONSE_TYPE, SessionRegistry, TuiGateway

__all__ = [
    "TuiGateway",
    "SessionRegistry",
    "RESPONSE_TYPE",
    "EVENT_TYPE",
    "build_tree",
    "render_message",
    "render_diff",
    "make_stream_renderer",
    "normalize_command",
    "serve_stream",
]


def __getattr__(name: str):
    # project_tree 是纯逻辑大树，惰性暴露 build_tree 避免包导入即拉起 re 编译以外的开销。
    if name == "build_tree":
        from spirit.tui.project_tree import build_tree

        return build_tree
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
