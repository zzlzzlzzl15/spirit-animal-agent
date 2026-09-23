"""后台进程会话数据模型 — Spirit Agent。

对标 Hermes ``tools/process_registry.py`` 的 ``ProcessSession``（2349 行中的
数据层部分）。一个 ``ProcessSession`` 代表一次 ``terminal(background=true)``
派生出来的后台进程，携带：

- 滚动输出缓冲（默认 200KB 窗口，超出丢头保尾）
- 退出状态四元组：``exited`` / ``exit_code`` / ``completion_reason`` /
  ``termination_source``
- 完成事件 ``_completion_event``，供 ``registry.wait()`` 阻塞唤醒
- watch 模式匹配的限流状态机（每会话冷却 + 连续 strike 熔断）

设计取舍（与 Hermes 的差异）：
- Hermes 支持 sandbox env 后端（Docker/SSH/Modal）与 PTY；Spirit 目前只有
  本地 subprocess，因此 ``env_ref`` / ``pid_scope`` 保留字段但不驱动分支，
  ``_pty`` 保留以兼容交互式 CLI 场景（未安装 ptyprocess 时自动降级为管道）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

from spirit.config import get_config_value

# ---------------------------------------------------------------------------
# 限额常量（模块级，测试可 monkeypatch）
# ---------------------------------------------------------------------------

MAX_OUTPUT_CHARS = int(get_config_value("process.max_output_chars", 200_000))
"""单个后台进程的滚动输出缓冲上限（字符）。超出后丢头保尾。"""

FINISHED_TTL_SECONDS = int(get_config_value("process.finished_ttl_seconds", 1800))
"""已退出进程的保留时长（秒）。超过后被 ``_prune_if_needed`` 清理。"""

MAX_PROCESSES = int(get_config_value("process.max_processes", 64))
"""同时追踪的进程上限（running + finished）。超出按 started_at 淘汰最旧的已完成项。"""

MAX_ACTIVE_PROCESS_AGE = int(get_config_value("process.max_active_age_seconds", 86400))
"""会话空闲/重置判定时，运行超过该时长（秒）的进程视为"陈旧"不再阻塞生命周期。"""

# watch 模式限流 —— **每会话**。
# 硬规则：每 WATCH_MIN_INTERVAL_SECONDS 最多放行一条 watch 匹配通知。
# 冷却窗口内到达的匹配被丢弃并记一次 strike；连续 WATCH_STRIKE_LIMIT 个
# strike 窗口后，该会话的 watch_patterns 被永久关闭，降级为
# notify_on_complete 语义（进程真正退出时给一条通知）。
WATCH_MIN_INTERVAL_SECONDS = float(get_config_value("process.watch_min_interval_seconds", 15))
WATCH_STRIKE_LIMIT = int(get_config_value("process.watch_strike_limit", 3))

# 全局熔断 —— 跨所有会话的二级保险，防止并发的兄弟进程各自不超限、
# 合起来却把用户刷屏。
WATCH_GLOBAL_MAX_PER_WINDOW = int(get_config_value("process.watch_global_max_per_window", 15))
WATCH_GLOBAL_WINDOW_SECONDS = float(get_config_value("process.watch_global_window_seconds", 10))
WATCH_GLOBAL_COOLDOWN_SECONDS = float(get_config_value("process.watch_global_cooldown_seconds", 30))


def format_uptime_short(seconds: float) -> str:
    """把秒数格式化成 ``12s`` / ``3m 5s`` / ``2h 10m``（用于状态栏与列表）。"""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    mins, secs = divmod(s, 60)
    if mins < 60:
        return f"{mins}m {secs}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}m"


@dataclass
class ProcessSession:
    """一个被追踪的后台进程（含输出缓冲与通知元数据）。"""

    id: str                                      # 唯一会话 ID（"proc_xxxxxxxxxxxx"）
    command: str                                 # 原始命令字符串
    task_id: str = ""                            # 任务/沙箱隔离键
    session_key: str = ""                        # 会话键（跨任务发现被遗忘的后台进程）
    pid: Optional[int] = None                    # OS 进程 ID
    process: Any = None                          # subprocess.Popen 句柄（本地）
    env_ref: Any = None                          # 环境后端引用（Spirit 暂未使用，保留对齐）
    cwd: Optional[str] = None                    # 工作目录
    started_at: float = 0.0                      # 派生时刻（wall clock）
    host_start_time: Optional[int] = None        # 进程创建时间基线 —— PID 复用防护
    exited: bool = False                         # 是否已退出
    exit_code: Optional[int] = None              # 退出码（仍在运行时为 None）
    completion_reason: str = "exited"            # exited|killed|lost|failed_start|already_exited
    termination_source: str = ""                 # process.kill|kill_all|backend_lost|failed_start
    output_buffer: str = ""                      # 滚动输出（最后 MAX_OUTPUT_CHARS 字符）
    max_output_chars: int = MAX_OUTPUT_CHARS
    detached: bool = False                       # True = 崩溃恢复得到（无管道句柄）
    pid_scope: str = "host"                      # "host" 本地 PID / "sandbox" 环境内 PID

    # 通知元数据（随 checkpoint 持久化，供桌面端/网关路由回复）
    watcher_platform: str = ""
    watcher_chat_id: str = ""
    watcher_user_id: str = ""
    watcher_user_name: str = ""
    watcher_thread_id: str = ""
    watcher_message_id: str = ""
    watcher_interval: int = 0                    # 0 = 未配置 watcher
    notify_on_complete: bool = False             # 退出时向 Agent 排队一条通知

    # watch 模式：输出命中任一模式即触发通知
    watch_patterns: List[str] = field(default_factory=list)
    _watch_hits: int = field(default=0, repr=False)          # 已投递的匹配总数
    _watch_suppressed: int = field(default=0, repr=False)    # 被限流丢弃的匹配数
    _watch_disabled: bool = field(default=False, repr=False) # strike 超限后永久关闭
    _watch_last_emit_at: float = field(default=0.0, repr=False)
    _watch_cooldown_until: float = field(default=0.0, repr=False)
    _watch_strike_candidate: bool = field(default=False, repr=False)
    _watch_consecutive_strikes: int = field(default=0, repr=False)

    _completion_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _reader_thread: Optional[threading.Thread] = field(default=None, repr=False)
    _pty: Any = field(default=None, repr=False)  # ptyprocess/winpty 句柄（use_pty=True 时）

    # ----- 便捷属性 -----

    @property
    def uptime_seconds(self) -> int:
        """已运行（或已结束的总）秒数。"""
        return int(time.time() - self.started_at)

    @property
    def is_running(self) -> bool:
        """是否仍在运行（``exited`` 的反义，供 UI/兼容层使用）。"""
        return not self.exited

    def append_output(self, chunk: str) -> None:
        """线程安全地追加输出并按 ``max_output_chars`` 滚动截断。"""
        if not chunk:
            return
        with self._lock:
            self.output_buffer += chunk
            if len(self.output_buffer) > self.max_output_chars:
                self.output_buffer = self.output_buffer[-self.max_output_chars:]

    def output_tail(self, chars: int = 2000) -> str:
        """线程安全地取输出尾部（不裁剪 ANSI，由调用方决定）。"""
        with self._lock:
            return self.output_buffer[-chars:] if self.output_buffer else ""

    def read_output(self, start: int = 0, count: Optional[int] = None) -> str:
        """按行读取输出（兼容 ``terminal_ext.ProcessEntry.read_output`` 旧接口）。"""
        lines = self.output_buffer.splitlines()
        end = start + count if count else len(lines)
        return "\n".join(lines[start:end])

    @property
    def output_lines(self) -> List[str]:
        """输出行列表（兼容旧 ``ProcessEntry.output_lines`` 接口）。"""
        return self.output_buffer.splitlines()

    def to_checkpoint_dict(self) -> dict:
        """序列化为 checkpoint JSON 条目（仅可持久化的标量字段）。"""
        return {
            "session_id": self.id,
            "command": self.command,
            "pid": self.pid,
            "pid_scope": self.pid_scope,
            "host_start_time": self.host_start_time,
            "cwd": self.cwd,
            "started_at": self.started_at,
            "task_id": self.task_id,
            "session_key": self.session_key,
            "watcher_platform": self.watcher_platform,
            "watcher_chat_id": self.watcher_chat_id,
            "watcher_user_id": self.watcher_user_id,
            "watcher_user_name": self.watcher_user_name,
            "watcher_thread_id": self.watcher_thread_id,
            "watcher_message_id": self.watcher_message_id,
            "watcher_interval": self.watcher_interval,
            "notify_on_complete": self.notify_on_complete,
            "watch_patterns": list(self.watch_patterns),
        }


__all__ = [
    "ProcessSession",
    "format_uptime_short",
    "MAX_OUTPUT_CHARS",
    "FINISHED_TTL_SECONDS",
    "MAX_PROCESSES",
    "MAX_ACTIVE_PROCESS_AGE",
    "WATCH_MIN_INTERVAL_SECONDS",
    "WATCH_STRIKE_LIMIT",
    "WATCH_GLOBAL_MAX_PER_WINDOW",
    "WATCH_GLOBAL_WINDOW_SECONDS",
    "WATCH_GLOBAL_COOLDOWN_SECONDS",
]
