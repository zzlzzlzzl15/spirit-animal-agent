"""后台进程管理 — Spirit Agent（Phase 4.7）。

对标 Hermes ``tools/process_registry.py``（2349 行）。本包提供：

``session.py``
    ``ProcessSession`` 数据模型 + 限额常量 + ``format_uptime_short``。
``registry.py``
    ``ProcessRegistry``：派生、reader 线程、poll/read_log/wait、进程树终止、
    stdin 交互、watch 模式限流、完成队列与 ``drain_notifications``、
    checkpoint 崩溃恢复、活跃查询；模块级单例 ``process_registry``。
``notifications.py``
    ``format_process_notification``：把队列事件渲染成可注入对话的文本。

工具层入口在 ``spirit/tools/process_tool.py``（``process`` 工具的 schema 与
handler），派生入口在 ``spirit/tools/terminal_tool.py``（``background=true``）。

典型用法::

    from spirit.process import process_registry

    # terminal(background=true) 内部调用
    session = process_registry.spawn_local(
        "npm run dev", task_id="t1", session_key="default",
        notify_on_complete=True, watch_patterns=["ready in"],
    )

    # 对话循环在每轮结束后回灌后台事件
    for evt, text in process_registry.drain_notifications(session_key="default"):
        agent.add_message("user", text)
"""

from spirit.process.notifications import (
    format_async_delegation,
    format_process_notification,
)
from spirit.process.registry import (
    ProcessRegistry,
    get_process_registry,
    process_registry,
)
from spirit.process.session import (
    FINISHED_TTL_SECONDS,
    MAX_ACTIVE_PROCESS_AGE,
    MAX_OUTPUT_CHARS,
    MAX_PROCESSES,
    ProcessSession,
    format_uptime_short,
)

__all__ = [
    # 注册表
    "ProcessRegistry",
    "process_registry",
    "get_process_registry",
    # 数据模型
    "ProcessSession",
    "format_uptime_short",
    # 通知渲染
    "format_process_notification",
    "format_async_delegation",
    # 限额常量
    "MAX_OUTPUT_CHARS",
    "FINISHED_TTL_SECONDS",
    "MAX_PROCESSES",
    "MAX_ACTIVE_PROCESS_AGE",
]
