"""后台进程注册表 — Spirit Agent。

对标 Hermes ``tools/process_registry.py``（2349 行），追踪经
``terminal(background=true)`` 派生的后台进程，提供：

- 滚动输出缓冲（200KB 窗口）+ 增量读取（``read1``，避免"退出时才一次性吐完"）
- 状态轮询（``poll``）与日志分页（``read_log``）
- 阻塞等待（``wait``，支持超时钳制与中断唤醒）
- 进程树终止（``kill_process`` / ``kill_all``；Windows ``taskkill /T /F``，
  POSIX psutil 遍历 + SIGTERM→SIGKILL 升级）
- stdin 交互（``write_stdin`` / ``submit_stdin`` / ``close_stdin``）
- watch 模式匹配通知（每会话限流 + 连续 strike 熔断 + 全局熔断）
- 统一完成队列 ``completion_queue`` + ``drain_notifications``（把后台事件
  回灌成 Agent 的下一轮输入）
- JSON checkpoint 崩溃恢复（``_write_checkpoint`` / ``recover_from_checkpoint``）
- 会话/任务维度的活跃查询（供会话重置与空闲检测使用）

与 Hermes 的差异（刻意裁剪）：
- 无 sandbox env 后端（Docker/SSH/Modal）→ 去掉 ``spawn_via_env`` /
  ``_env_poller_loop``，只保留本地 subprocess 路径。
- PTY 保留为可选（``use_pty=True``），未安装 ptyprocess/winpty 时降级为管道。
- PID 复用防护改用 ``psutil.create_time``（跨平台），不再读 ``/proc/<pid>/stat``。

线程安全：执行器线程（工具 handler）、对话循环、清理线程都会并发访问。

用法::

    from spirit.process import process_registry

    session = process_registry.spawn_local("npm run dev", task_id="t1",
                                          notify_on_complete=True)
    process_registry.poll(session.id)
    process_registry.wait(session.id, timeout=30)
    process_registry.kill_process(session.id)
"""

from __future__ import annotations

import json
import logging
import os
import platform
import queue as _queue_mod
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from spirit.process.notifications import format_process_notification
from spirit.process.session import (
    FINISHED_TTL_SECONDS,
    MAX_PROCESSES,
    ProcessSession,
    WATCH_GLOBAL_COOLDOWN_SECONDS,
    WATCH_GLOBAL_MAX_PER_WINDOW,
    WATCH_GLOBAL_WINDOW_SECONDS,
    WATCH_MIN_INTERVAL_SECONDS,
    WATCH_STRIKE_LIMIT,
)

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"


# ---------------------------------------------------------------------------
# 平台辅助
# ---------------------------------------------------------------------------

def _checkpoint_path() -> Path:
    """checkpoint 文件路径（延迟解析，便于测试 monkeypatch SPIRIT_HOME）。"""
    from spirit.config import get_spirit_home

    return get_spirit_home() / "processes.json"


def _windows_hide_flags() -> int:
    """Windows 下隐藏子进程控制台窗口的 creationflags。"""
    flags = 0
    for name in ("CREATE_NO_WINDOW", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, name, 0)
    return flags


def _find_shell() -> List[str]:
    """返回执行命令用的 shell 前缀（与 ``terminal_tool`` 保持一致）。"""
    if _IS_WINDOWS:
        return ["cmd", "/c"]
    shell = os.environ.get("SHELL") or "/bin/bash"
    return [shell, "-c"]


def _resolve_safe_cwd(cwd: Optional[str]) -> str:
    """解析并校验工作目录；不存在或不可访问时回退到当前目录。"""
    if not cwd:
        return os.getcwd()
    try:
        p = Path(cwd).expanduser()
        if p.is_dir():
            return str(p.resolve())
    except Exception:
        pass
    return os.getcwd()


def _atomic_json_write(path: Path, payload: Any) -> None:
    """原子写 JSON（先写临时文件再 replace，避免崩溃时留下半截文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

class ProcessRegistry:
    """运行中与已结束的后台进程的内存注册表（线程安全）。

    访问来源：
      - 执行器线程（terminal / process 工具 handler）
      - 对话循环（轮次结束后 drain 通知）
      - 桌面端 WebSocket 服务（进程面板）
      - 清理线程（会话重置时 kill_all）
    """

    _SHELL_NOISE_SUBSTRINGS = (
        "bash: cannot set terminal process group",
        "bash: no job control in this shell",
        "no job control in this shell",
        "cannot set terminal process group",
        "tcsetattr: Inappropriate ioctl for device",
    )

    def __init__(self, *, rehydrate_delegations: bool = True):
        self._running: Dict[str, ProcessSession] = {}
        self._finished: Dict[str, ProcessSession] = {}
        self._lock = threading.Lock()

        # 统一通知队列 —— 完成通知（notify_on_complete）、watch 匹配、
        # 限流熔断摘要、异步委托完成全部落在这里，用 "type" 字段区分。
        # 对话循环在每轮结束后 drain，把事件回灌成新一轮输入。
        self.completion_queue: "_queue_mod.Queue" = _queue_mod.Queue()

        # 仅在注册表启动时回灌持久化的异步委托完成事件；消费方仍按既有
        # 通道把它们当作新一轮注入。
        if rehydrate_delegations:
            try:
                from spirit.tools.async_delegation import restore_undelivered_completions

                restore_undelivered_completions(self.completion_queue)
            except Exception as exc:
                logger.debug("无法恢复异步委托完成事件: %s", exc)

        # 已被 Agent 通过 wait/log/kill 消费掉输出的会话。drain 时跳过它们的
        # 完成通知——阻塞等待或完整读日志意味着 Agent 这一轮已经拿到输出并在
        # 据此行动，再注入一条 [IMPORTANT: ...] 就是重复。
        self._completion_consumed: set = set()

        # 仅通过 poll() **观察到**退出的会话。poll 是只读状态查询，因此
        # *不* 标记 _completion_consumed（否则一次状态查询就会抑制掉自主投递
        # 轮次）。但 CLI 场景下 poll 结果就在同一轮内联返回，轮次结束的 drain
        # 仍需跳过它以免重复注入。drain_notifications 会查这个集合，
        # 桌面端/网关的 watcher 刻意不查（传 skip_poll_observed=False）。
        self._poll_observed: set = set()

        # watch 匹配的全局熔断状态（跨所有会话的二级保险）
        self._global_watch_lock = threading.Lock()
        self._global_watch_window_start: float = 0.0
        self._global_watch_window_hits: int = 0
        self._global_watch_tripped_until: float = 0.0
        self._global_watch_suppressed_during_trip: int = 0

        # 驱动方（桌面端）设置的实时输出 sink：从 reader 线程以
        # (session, chunk) 调用，把输出流式推给 UI，而不是靠轮询取尾部。
        self.on_output: Optional[Callable[[ProcessSession, str], None]] = None
        # 驱动方设置的关闭视图 sink：以 (session_or_none, process_id) 调用，
        # 用于 Agent 请求关闭只读终端标签。与 kill 不同——进程继续运行，
        # 只是丢掉 UI 视图（用户可从状态栈重新打开）。
        self.on_close: Optional[Callable[[Optional[ProcessSession], str], None]] = None

        # checkpoint 恢复得到的 watcher 元数据（桌面端在 Agent 运行后读取）
        self.pending_watchers: List[Dict[str, Any]] = []

    # ===================================================================
    # 输出处理
    # ===================================================================

    @staticmethod
    def _clean_shell_noise(text: str) -> str:
        """剥掉输出开头的 shell 启动告警。"""
        lines = text.split("\n")
        while lines and any(noise in lines[0] for noise in ProcessRegistry._SHELL_NOISE_SUBSTRINGS):
            lines.pop(0)
        return "\n".join(lines)

    def _emit_output(self, session: ProcessSession, chunk: str) -> None:
        """把刚读到的 chunk 转给实时输出 sink（若有）。

        从 reader 线程调用；绝不能把异常抛回读取循环。
        """
        sink = self.on_output
        if sink is None or not chunk:
            return
        try:
            sink(session, chunk)
        except Exception:
            logger.debug("on_output sink 抛错（已忽略）", exc_info=True)

    # ===================================================================
    # watch 模式匹配 + 限流
    # ===================================================================

    def _check_watch_patterns(self, session: ProcessSession, new_text: str) -> None:
        """扫描新输出里的 watch 模式并排队通知。

        从 reader 线程调用，``new_text`` 是刚读到的增量。

        每会话限流：每 ``WATCH_MIN_INTERVAL_SECONDS`` 最多放行一条匹配通知。
        冷却窗口内到达的匹配被丢弃并为该窗口记**一次** strike；连续
        ``WATCH_STRIKE_LIMIT`` 个 strike 窗口后关闭本会话的 watch_patterns，
        升级为 notify_on_complete 语义——进程真正退出时给一条，不再有中途刷屏。
        """
        if not session.watch_patterns or session._watch_disabled:
            return
        # 退出后抑制：reader 循环一旦宣告进程退出，之后看到的任何 chunk 都是
        # 退出后的噪音。丢弃它们可以消除"进程结束几分钟后还投递陈旧通知"的刷屏。
        if session.exited:
            return

        matched_lines: List[str] = []
        matched_pattern: Optional[str] = None
        for line in new_text.splitlines():
            for pat in session.watch_patterns:
                if pat and pat in line:
                    matched_lines.append(line.rstrip())
                    if matched_pattern is None:
                        matched_pattern = pat
                    break  # 一行命中一次就够

        if not matched_lines:
            return

        now = time.time()
        should_disable = False
        with session._lock:
            # 情况 1：仍在上次投递的冷却窗口内。丢弃并为该窗口记一次 strike
            # （每窗口只记一次）。达到 strike 上限则关闭 watch 并升级为
            # notify_on_complete。
            if session._watch_cooldown_until and now < session._watch_cooldown_until:
                session._watch_suppressed += len(matched_lines)
                if not session._watch_strike_candidate:
                    session._watch_strike_candidate = True
                    session._watch_consecutive_strikes += 1
                    if session._watch_consecutive_strikes >= WATCH_STRIKE_LIMIT:
                        session._watch_disabled = True
                        session.notify_on_complete = True
                        should_disable = True
                return_early = True
            else:
                # 情况 2：冷却已过。若上一个冷却窗口是"干净"的（没有丢弃），
                # 重置连续 strike 计数——恢复了健康的投递节奏。
                if session._watch_cooldown_until and not session._watch_strike_candidate:
                    session._watch_consecutive_strikes = 0
                session._watch_strike_candidate = False

                session._watch_last_emit_at = now
                session._watch_cooldown_until = now + WATCH_MIN_INTERVAL_SECONDS
                session._watch_hits += 1
                suppressed = session._watch_suppressed
                session._watch_suppressed = 0
                return_early = False

        if return_early:
            if should_disable:
                # 只发一条"watch 已关闭，降级为 notify_on_complete"的摘要，
                # 让 Agent/用户明白为什么突然安静了。
                self.completion_queue.put({
                    "session_id": session.id,
                    "session_key": session.session_key,
                    "command": session.command,
                    "type": "watch_disabled",
                    "suppressed": session._watch_suppressed,
                    "message": (
                        f"Watch patterns disabled for process {session.id} — "
                        f"{WATCH_STRIKE_LIMIT} consecutive rate-limit windows triggered "
                        f"(min spacing {WATCH_MIN_INTERVAL_SECONDS}s). "
                        f"Falling back to notify_on_complete semantics; you'll get "
                        f"exactly one notification when the process exits."
                    ),
                })
            return

        output = "\n".join(matched_lines[:20])
        if len(output) > 2000:
            output = output[:2000] + "\n...(truncated)"

        # 全局熔断（跨会话的二级保险）
        if not self._global_watch_admit(now):
            return

        self.completion_queue.put({
            "session_id": session.id,
            "session_key": session.session_key,
            "command": session.command,
            "type": "watch_match",
            "pattern": matched_pattern,
            "output": output,
            "suppressed": suppressed,
            "platform": session.watcher_platform,
            "chat_id": session.watcher_chat_id,
            "user_id": session.watcher_user_id,
            "user_name": session.watcher_user_name,
            "thread_id": session.watcher_thread_id,
            "message_id": session.watcher_message_id,
        })

    def _global_watch_admit(self, now: float) -> bool:
        """判断这条 watch_match 是否允许通过全局熔断。

        语义：
        - 处于冷却期 → 丢弃并计数。
        - 否则滑动窗口并检查全局上限。
        - 超限 → 熔断 ``WATCH_GLOBAL_COOLDOWN_SECONDS``，并发**一条**摘要事件，
          让 Agent/用户看到"N 条通知被抑制"而不是被逐条刷屏。
        - 冷却结束时发一条释放摘要并重置计数。
        """
        with self._global_watch_lock:
            # 先处理冷却到期，才能发释放摘要
            release_msg: Optional[dict] = None
            if self._global_watch_tripped_until and now >= self._global_watch_tripped_until:
                suppressed = self._global_watch_suppressed_during_trip
                self._global_watch_tripped_until = 0.0
                self._global_watch_suppressed_during_trip = 0
                self._global_watch_window_start = now
                self._global_watch_window_hits = 0
                if suppressed > 0:
                    release_msg = {
                        "session_id": "",
                        "session_key": "",
                        "command": "",
                        "type": "watch_overflow_released",
                        "suppressed": suppressed,
                        "message": (
                            f"Watch-pattern notifications resumed. "
                            f"{suppressed} match event(s) were suppressed during the flood."
                        ),
                    }

            trip_now: Optional[float] = None
            if self._global_watch_tripped_until and now < self._global_watch_tripped_until:
                # 仍在冷却 —— 丢弃并计数
                self._global_watch_suppressed_during_trip += 1
                admit = False
            else:
                if now - self._global_watch_window_start >= WATCH_GLOBAL_WINDOW_SECONDS:
                    self._global_watch_window_start = now
                    self._global_watch_window_hits = 0

                if self._global_watch_window_hits >= WATCH_GLOBAL_MAX_PER_WINDOW:
                    self._global_watch_tripped_until = now + WATCH_GLOBAL_COOLDOWN_SECONDS
                    self._global_watch_suppressed_during_trip += 1
                    trip_now = now
                    admit = False
                else:
                    self._global_watch_window_hits += 1
                    admit = True

        # 摘要事件在锁外入队
        if release_msg is not None:
            self.completion_queue.put(release_msg)
        if trip_now is not None:
            self.completion_queue.put({
                "session_id": "",
                "session_key": "",
                "command": "",
                "type": "watch_overflow_tripped",
                "message": (
                    f"Watch-pattern overflow: >{WATCH_GLOBAL_MAX_PER_WINDOW} "
                    f"notifications in {WATCH_GLOBAL_WINDOW_SECONDS}s across all processes. "
                    f"Suppressing further watch_match events for "
                    f"{WATCH_GLOBAL_COOLDOWN_SECONDS}s."
                ),
            })
        return admit

    # ===================================================================
    # PID 身份与终止
    # ===================================================================

    @staticmethod
    def _is_host_pid_alive(pid: Optional[int]) -> bool:
        """尽力而为的宿主 PID 存活检查。

        **不能用 ``os.kill(pid, 0)``**：它在 Windows 上不是 no-op，会路由到
        CTRL_C_EVENT 硬杀目标的控制台进程组（bpo-14484）。
        """
        if not pid or pid <= 0:
            return False
        try:
            import psutil  # type: ignore

            return bool(psutil.pid_exists(int(pid)))
        except Exception:
            return False

    @staticmethod
    def _safe_host_start_time(pid: Optional[int]) -> Optional[int]:
        """宿主 PID 的进程创建时间（毫秒整数），不可得时返回 None。"""
        if not pid or pid <= 0:
            return None
        try:
            import psutil  # type: ignore

            return int(psutil.Process(int(pid)).create_time() * 1000)
        except Exception:
            return None

    @classmethod
    def _host_pid_is_ours(cls, pid: Optional[int], expected_start: Optional[int]) -> bool:
        """仅当 ``pid`` 存活**且仍是我们派生的那个进程**时返回 True。

        内核会在进程退出并被回收后复用 PID，所以存下来的 PID 之后可能指向一个
        **无关**进程——现实中观察到过复用的号落到桌面浏览器的会话首领上，
        随后被我们的进程树终止 SIGTERM（浏览器不定期暴毙）。这里用派生时
        捕获的创建时间与实时值比对；不匹配说明号被复用了，绝不能发信号。

        没有基线时（老 checkpoint，或平台拿不到创建时间）降级为纯存活检查，
        而不是拒绝行动，保留既有的尽力而为行为。
        """
        if not cls._is_host_pid_alive(pid):
            return False
        if expected_start is None:
            return True
        return cls._safe_host_start_time(pid) == expected_start

    @staticmethod
    def _proc_alive(proc) -> bool:
        """psutil.Process 是否在运行且不是僵尸（僵尸已死，无需 SIGKILL）。"""
        try:
            import psutil  # type: ignore

            if not proc.is_running():
                return False
            return proc.status() != psutil.STATUS_ZOMBIE
        except Exception:
            return False

    @staticmethod
    def _daemon_term_grace_seconds() -> float:
        """SIGTERM 到升级 SIGKILL 之间的宽限窗口（秒）。

        读 ``process.daemon_term_grace_seconds``；下限 0（0 = 关闭升级）。
        配置不可读时回退默认值，保证调用方总能拿到合理数字。
        """
        try:
            from spirit.config import get_config_value

            return max(float(get_config_value("process.daemon_term_grace_seconds", 2.0)), 0.0)
        except Exception:
            return 2.0

    @classmethod
    def _terminate_host_pid(cls, pid: int, expected_start: Optional[int] = None) -> None:
        """终止一个宿主可见的 PID 及其后代。

        ``expected_start`` 是派生时捕获的创建时间。提供时会在发任何信号前
        重新校验实时 PID；不匹配（或已死）说明号被复用到了无关进程上，
        我们拒绝触碰——泄漏一个孤儿进程严格优于杀掉一个陌生人（比如浏览器）。

        Windows：``taskkill /PID <pid> /T /F``。这是微软文档化的进程树终止
        原语；``/F`` 本身已是硬杀，无需再升级。不能复用 POSIX 的 psutil 路径：
        Windows 不维护 Unix 式进程树（``children(recursive=True)`` 走的 PPID
        链在中间进程退出后就失效），且 ``terminate()`` 就是
        ``TerminateProcess()``——只杀目标句柄，没有会沿进程组级联的 SIGTERM 等价物。

        POSIX：用 psutil 遍历进程树，**先子后父**逐个 SIGTERM，这样
        subprocess 树（例如守护进程派生的渲染器/GPU 助手）不会被 reparent
        到 init 而躲过清理。宽限窗口（``process.daemon_term_grace_seconds``）
        之后，任何无视 SIGTERM 的树成员（卡在信号处理里的守护进程）升级为
        SIGKILL，避免无限泄漏。宽限设 0 可关闭升级（只发 SIGTERM）。
        """
        if expected_start is not None and not cls._host_pid_is_ours(pid, expected_start):
            logger.warning(
                "拒绝终止宿主 pid %d：创建时间不匹配 —— PID 已被复用到无关进程上。",
                pid,
            )
            return

        if _IS_WINDOWS:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    creationflags=_windows_hide_flags(),
                    stdin=subprocess.DEVNULL,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except (OSError, ProcessLookupError, PermissionError):
                    pass
            return

        try:
            import psutil  # type: ignore
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError, PermissionError):
                pass
            return

        try:
            parent = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        except (OSError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError, PermissionError):
                pass
            return

        # 快照整棵树（先子后父）并逐个 SIGTERM
        try:
            targets = parent.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            targets = []
        targets.append(parent)

        for proc in targets:
            try:
                proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                pass

        # 对宽限窗口内无视 SIGTERM 的成员升级为 SIGKILL
        grace = cls._daemon_term_grace_seconds()
        if grace <= 0:
            return
        # 睡完宽限窗口后独立重新探测每个目标并 SIGKILL 幸存者。刻意**不**信任
        # psutil.wait_procs 的 gone/alive 划分：它通过 Process.wait() 回收，
        # 在目标经过僵尸态或父子树之间回收竞争时会误判，导致幸存者漏杀。
        # 直接存活重探是确定性的。
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not any(cls._proc_alive(p) for p in targets):
                break
            time.sleep(0.05)
        for proc in targets:
            try:
                if not cls._proc_alive(proc):
                    continue
                proc.kill()
                logger.info(
                    "pid %d 无视 SIGTERM（宽限 %.1fs），已升级为 SIGKILL", proc.pid, grace,
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                pass

    # ===================================================================
    # 会话状态刷新
    # ===================================================================

    def _refresh_detached_session(
        self, session: Optional[ProcessSession]
    ) -> Optional[ProcessSession]:
        """当底层进程已退出时，更新崩溃恢复得到的宿主 PID 会话。"""
        if session is None or session.exited or not session.detached or session.pid_scope != "host":
            return session

        # 身份感知的存活检查：被复用的 PID（存活但不是我们派生的进程）必须
        # 视为"我们的进程已退出"，从而移入 finished，且绝不会被后续 kill()
        # 做进程树终止。
        if self._host_pid_is_ours(session.pid, session.host_start_time):
            return session

        with session._lock:
            if session.exited:
                return session
            session.exited = True
            # 恢复得到的会话没有可等待的句柄，原进程对象消失后真实退出码不可得
            session.exit_code = None

        self._move_to_finished(session)
        return session

    def _reconcile_local_exit(self, session: ProcessSession) -> None:
        """把 ``session.exited`` 与真实子进程状态对齐。

        防的是"孤儿管道 reader 挂住"：直接子进程已退出，但孙进程继承了管道
        写端，reader 线程仍阻塞在 read 上，于是 ``exited`` 迟迟不翻转，
        ``wait()`` 白等到超时。这里在 poll/wait 路径上直接问 Popen。
        """
        proc = session.process
        if proc is None or session.exited:
            return
        try:
            code = proc.poll()
        except Exception:
            return
        if code is None:
            return
        with session._lock:
            if session.exited:
                return
            session.exited = True
            if session.completion_reason != "killed":
                session.exit_code = code
                session.completion_reason = "exited"
        self._move_to_finished(session)

    # ===================================================================
    # 派生
    # ===================================================================

    def spawn_local(
        self,
        command: str,
        cwd: Optional[str] = None,
        task_id: str = "",
        session_key: str = "",
        env_vars: Optional[dict] = None,
        *,
        notify_on_complete: bool = False,
        watch_patterns: Optional[List[str]] = None,
        interactive: bool = False,
        use_pty: bool = False,
    ) -> ProcessSession:
        """在本地派生一个后台进程并纳入追踪。

        Args:
            command: 交给 shell 执行的命令字符串。
            cwd: 工作目录（不存在时回退当前目录）。
            task_id: 任务隔离键（``list_sessions`` / ``kill_all`` 过滤用）。
            session_key: 会话键（跨任务发现被遗忘的后台进程 + 通知路由）。
            env_vars: 追加/覆盖的环境变量。
            notify_on_complete: 进程退出时向 ``completion_queue`` 排一条通知。
            watch_patterns: 输出命中任一子串即触发通知（带限流与熔断）。
            interactive: True 时开 stdin 管道，支持 write/submit/close。
            use_pty: True 时尝试 PTY（交互式 CLI 工具）；缺依赖自动降级管道。
        """
        session = ProcessSession(
            id=f"proc_{uuid.uuid4().hex[:12]}",
            command=command,
            task_id=task_id,
            session_key=session_key,
            cwd=_resolve_safe_cwd(cwd),
            started_at=time.time(),
            notify_on_complete=bool(notify_on_complete),
            watch_patterns=[str(p) for p in (watch_patterns or []) if p],
        )

        bg_env = dict(os.environ)
        # 强制 Python 无缓冲，否则 tqdm/datasets 这类库在 stdout 是管道时会
        # 缓冲，导致 process(action="poll") 看不到进度。
        bg_env["PYTHONUNBUFFERED"] = "1"
        bg_env["PYTHONIOENCODING"] = "utf-8"
        if env_vars:
            bg_env.update({str(k): str(v) for k, v in env_vars.items()})

        if use_pty:
            pty_session = self._try_spawn_pty(session, command, bg_env)
            if pty_session is not None:
                return pty_session
            logger.info("PTY 不可用，降级为管道模式: %s", session.id)

        popen_kwargs: Dict[str, Any] = {}
        if _IS_WINDOWS:
            popen_kwargs["creationflags"] = _windows_hide_flags()
        else:
            # 新会话 → 独立进程组，kill 时可用 killpg 兜底整棵树
            popen_kwargs["start_new_session"] = True

        argv = _find_shell() + [command]
        if _IS_WINDOWS:
            # Windows：以**字符串**形式把整条命令行交给 Popen，而不是 argv 列表。
            # 列表形式会触发 subprocess.list2cmdline 把命令里的引号转义成 \"，
            # 而 cmd 不认转义引号，于是 ``"C:\Program Files\x.exe" args`` 这类以
            # 引号开头的命令会被解析成 ``\"C:\Program Files\x.exe\"`` → 启动失败
            # （exit 1，"不是内部或外部命令"）。``cmd /S /c "<command>"`` 是标准
            # 稳妥写法：/S 强制旧的引号剥离规则，外层引号包住整条命令，cmd 只
            # 剥掉最外层一对，内层命令原样执行。
            argv = f'cmd /S /c "{command}"'
        try:
            proc = subprocess.Popen(
                argv,
                text=True,
                cwd=session.cwd,
                env=bg_env,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
                **popen_kwargs,
            )
        except Exception as exc:
            # 派生本身就失败 —— 记录一个 failed_start 会话，让调用方能拿到
            # 明确状态而不是异常穿透到工具层。
            session.exited = True
            session.exit_code = -1
            session.completion_reason = "failed_start"
            session.termination_source = "failed_start"
            session.output_buffer = f"[派生失败] {exc}"
            with self._lock:
                self._prune_if_needed()
                self._finished[session.id] = session
            session._completion_event.set()
            if session.notify_on_complete:
                self._enqueue_completion(session)
            return session

        session.process = proc
        session.pid = proc.pid
        session.host_start_time = self._safe_host_start_time(proc.pid)

        try:
            reader = threading.Thread(
                target=self._reader_loop,
                args=(session,),
                daemon=True,
                name=f"proc-reader-{session.id}",
            )
            session._reader_thread = reader
            reader.start()

            with self._lock:
                self._prune_if_needed()
                self._running[session.id] = session

            self._write_checkpoint()
        except Exception:
            # Popen 之后的初始化失败 —— 杀掉孤儿子进程（含 setsid 派生的后代）
            # 再抛出，避免泄漏成无人追踪的后台进程。
            try:
                self._terminate_host_pid(proc.pid, session.host_start_time)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            raise

        return session

    def _try_spawn_pty(
        self, session: ProcessSession, command: str, bg_env: Dict[str, str]
    ) -> Optional[ProcessSession]:
        """尝试用 PTY 派生（交互式 CLI 工具）。不可用时返回 None。"""
        try:
            if _IS_WINDOWS:
                from winpty import PtyProcess as _PtyCls  # type: ignore
            else:
                from ptyprocess import PtyProcess as _PtyCls  # type: ignore
        except Exception:
            return None

        try:
            shell_argv = _find_shell()
            pty_proc = _PtyCls.spawn(
                shell_argv + [command],
                cwd=session.cwd,
                env=bg_env,
                dimensions=(30, 120),
            )
        except Exception as exc:
            logger.warning("PTY 派生失败（%s），回退管道模式", exc)
            return None

        session.pid = pty_proc.pid
        session.host_start_time = self._safe_host_start_time(session.pid)
        session._pty = pty_proc

        reader = threading.Thread(
            target=self._pty_reader_loop,
            args=(session,),
            daemon=True,
            name=f"proc-pty-reader-{session.id}",
        )
        session._reader_thread = reader
        reader.start()

        with self._lock:
            self._prune_if_needed()
            self._running[session.id] = session
        self._write_checkpoint()
        return session

    # ===================================================================
    # reader 线程
    # ===================================================================

    def _reader_loop(self, session: ProcessSession) -> None:
        """后台线程：从本地 Popen 读 stdout。

        **重要**：这里避免用 ``TextIOWrapper.read(4096)``。在管道上那个调用会
        阻塞到 EOF（或大缓冲填满），导致"实时"输出在进程退出时一次性涌出。
        ``buffer.read1(4096)`` 在有字节可读时就返回增量 chunk，再解码成文本。
        """
        first_chunk = True
        try:
            stdout = session.process.stdout
            if stdout is None:
                return

            raw_read = getattr(getattr(stdout, "buffer", None), "read1", None)
            while True:
                if raw_read is not None:
                    raw = raw_read(4096)
                    if not raw:
                        break
                    chunk = raw.decode("utf-8", errors="replace")
                else:
                    # 没有缓冲原始接口的（被打桩/替换的）流的回退路径。
                    # 实时性差一些，但保持兼容。
                    chunk = stdout.read(4096)
                    if not chunk:
                        break

                if first_chunk:
                    chunk = self._clean_shell_noise(chunk)
                    first_chunk = False
                session.append_output(chunk)
                self._check_watch_patterns(session, chunk)
                self._emit_output(session, chunk)
        except Exception as exc:
            logger.debug("进程 stdout reader 结束: %s", exc)
        finally:
            # 无论如何都回收子进程，防止僵尸
            try:
                session.process.wait(timeout=5)
            except Exception as exc:
                logger.debug("进程 wait 超时或失败: %s", exc)
            session.exited = True
            if session.completion_reason != "killed":
                session.exit_code = session.process.returncode
                session.completion_reason = "exited"
            self._move_to_finished(session)

    def _pty_reader_loop(self, session: ProcessSession) -> None:
        """后台线程：从 PTY 进程读输出。"""
        pty = session._pty
        try:
            while pty.isalive():
                try:
                    chunk = pty.read(4096)
                    if chunk:
                        text = chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="replace")
                        session.append_output(text)
                        self._check_watch_patterns(session, text)
                        self._emit_output(session, text)
                except EOFError:
                    break
                except Exception:
                    break
        except Exception as exc:
            logger.debug("PTY stdout reader 结束: %s", exc)

        try:
            pty.wait()
        except Exception as exc:
            logger.debug("PTY wait 超时或失败: %s", exc)
        session.exited = True
        if session.completion_reason != "killed":
            session.exit_code = getattr(pty, "exitstatus", -1)
            session.completion_reason = "exited"
        self._move_to_finished(session)

    # ===================================================================
    # 完成 / 通知队列
    # ===================================================================

    def _enqueue_completion(self, session: ProcessSession) -> None:
        """把一条完成通知排入统一队列（输出取尾部 2000 字符并去 ANSI）。"""
        from spirit.tools.infra_utils import strip_ansi

        output_tail = strip_ansi(session.output_buffer[-2000:]) if session.output_buffer else ""
        self.completion_queue.put({
            "type": "completion",
            "session_id": session.id,
            "session_key": session.session_key,
            "task_id": session.task_id,
            "command": session.command,
            "exit_code": session.exit_code,
            "completion_reason": session.completion_reason,
            "termination_source": session.termination_source,
            "output": output_tail,
            # 跨 checkpoint 恢复稳定的生产者身份；不像消费方观察到的完成时间戳，
            # 它不会因为哪个 watcher 先注意到退出而变化。
            "started_at": session.started_at,
            "platform": session.watcher_platform,
            "chat_id": session.watcher_chat_id,
            "user_id": session.watcher_user_id,
            "user_name": session.watcher_user_name,
            "thread_id": session.watcher_thread_id,
            "message_id": session.watcher_message_id,
        })

    def _move_to_finished(self, session: ProcessSession) -> None:
        """把会话从 running 移到 finished。

        幂等：若已被移过（例如 kill_process 与 reader 线程竞争），第二次调用
        是 no-op —— 不会入队重复的完成通知。
        """
        with self._lock:
            was_running = self._running.pop(session.id, None) is not None
            self._finished[session.id] = session
        session._completion_event.set()
        self._write_checkpoint()

        # 只在**首次**移动时入队完成通知。没有这个守卫，kill_process() 与
        # reader 线程可能都调用 _move_to_finished()，产生重复的 [IMPORTANT: ...]。
        if was_running and session.notify_on_complete:
            self._enqueue_completion(session)

    def is_completion_consumed(self, session_id: str) -> bool:
        """该会话的完成通知是否已被 wait/log 消费掉。"""
        return session_id in self._completion_consumed

    def is_session_waiting(self, session_id: str) -> bool:
        """泊在该会话上的目标循环是否应继续泊车。

        供目标系统的 wait 屏障（``spirit.goals``）使用，支持等待一个进程
        **自己的触发器**，而不只是它的退出。会话"仍在等待"当且仅当：
          - 它仍在运行，且
          - 若配置了 ``watch_patterns``，还没有任何一个命中过（于是一个
            中途就触发、可能永不退出的长驻 watcher 会在模式命中的那一刻
            解锁，而不是等到退出）。

        会话已退出、watch 模式已命中、或会话未知时返回 False（不要等）——
        这样陈旧或已触发的屏障永远不会把循环卡死。
        """
        if not session_id:
            return False
        with self._lock:
            session = self._running.get(session_id) or self._finished.get(session_id)
        if session is None:
            return False
        try:
            self._refresh_detached_session(session)
            self._reconcile_local_exit(session)
        except Exception:
            pass
        if session.exited:
            return False
        # watch 模式进程：触发器是模式匹配，不是退出。任何一次匹配投递之后，
        # 即使进程继续运行（服务器/守护进程/watcher 场景），等待也已满足。
        if session.watch_patterns and not session._watch_disabled:
            if session._watch_hits > 0:
                return False
        return True

    def _drain_should_skip(self, session_id: str, *, skip_poll_observed: bool = True) -> bool:
        """本次 drain 是否应跳过该会话的完成事件。

        Agent 要么真正消费了输出（wait/log → ``_completion_consumed``），
        要么通过 poll() 内联观察到了退出（``_poll_observed``）时跳过。两种
        情况下 CLI Agent 这一轮都已拿到结果，再注入完成通知就是重复。
        桌面端/网关的 watcher **不**用这个 —— 它们只查
        ``is_completion_consumed``，好让只读 poll 永不抑制自主投递轮次。
        """
        return session_id in self._completion_consumed or (
            skip_poll_observed and session_id in self._poll_observed
        )

    def drain_notifications(
        self,
        session_key: str = "",
        owns_event: Optional[Callable[[dict], bool]] = None,
        *,
        skip_poll_observed: bool = True,
    ) -> List[tuple]:
        """取出所有待处理通知事件，返回 ``(raw_event, formatted_text)`` 对列表。

        跳过 Agent 已通过 wait/log 消费、或通过 poll() 内联观察到退出的完成
        事件（见 ``_drain_should_skip``）。桌面端/网关调用方传
        ``skip_poll_observed=False``，因为只读轮询在那里不应抑制自主投递。

        提供路由过滤器时，带地址的通知绝不能被 drain 进错误的会话。异步委托
        事件永远要求对话载荷；普通事件在携带 ``session_key`` 时要求路由。
        支持两种过滤模式，强的优先：

        - ``owns_event(evt) -> bool``：正向证明所有权的回调。提供时，带路由的
          事件**只有**回调返回 True 才被消费；其余全部重新入队留给它的主人。
          回调抛错按 False 处理（fail closed，绝不在检查损坏时泄漏）。
        - ``session_key``：纯键相等（CLI 等单会话调用方）。不匹配的带地址
          事件重新入队。

        两者都没设时消费所有事件（旧的单会话行为，向后兼容）。无主的普通
        通知即使提供了过滤器也保留旧行为；无主的异步委托事件在提供过滤器时
        仍 fail-closed，要求正向证明。
        """
        results: List[tuple] = []
        requeue: List[dict] = []
        while not self.completion_queue.empty():
            try:
                evt = self.completion_queue.get_nowait()
            except Exception:
                break
            if not isinstance(evt, dict):
                continue
            # 正向证明所有权优先于裸键相等。委托载荷永远要求证明；普通事件
            # 一旦携带路由元数据也要求证明。无主的普通事件保留旧的单会话投递。
            is_async_delegation = evt.get("type") == "async_delegation"
            evt_session_key = str(evt.get("session_key") or "")
            requires_positive_proof = is_async_delegation or bool(evt_session_key)
            if owns_event is not None and requires_positive_proof:
                try:
                    owned = bool(owns_event(evt))
                except Exception:
                    owned = False  # fail closed —— 检查损坏时绝不泄漏
                if not owned:
                    requeue.append(evt)
                    continue
            elif session_key and requires_positive_proof:
                if evt_session_key != session_key:
                    requeue.append(evt)
                    continue
            elif is_async_delegation and evt.get("restored"):
                # 持久化恢复会把上一进程的载荷入队到一个全新的注册表。
                # 无过滤器的旧式 drain 无法证明所有权，留给主人。
                requeue.append(evt)
                continue

            # 本地的 consumed/observed 状态只能抑制**本会话拥有**（或旧的无主
            # 普通）事件。路由必须先发生，否则外来会话能丢掉主人的事件。
            _evt_sid = evt.get("session_id", "")
            if evt.get("type") == "completion" and self._drain_should_skip(
                _evt_sid, skip_poll_observed=skip_poll_observed
            ):
                continue

            text = format_process_notification(evt)
            if text:
                results.append((evt, text))
        for evt in requeue:
            self.completion_queue.put(evt)
        return results

    # ===================================================================
    # 查询
    # ===================================================================

    def get(self, session_id: str) -> Optional[ProcessSession]:
        """按 ID 取会话（running 或 finished）。"""
        with self._lock:
            session = self._running.get(session_id) or self._finished.get(session_id)
        return self._refresh_detached_session(session)

    def poll(self, session_id: str) -> dict:
        """检查后台进程状态并取新输出。"""
        from spirit.tools.infra_utils import strip_ansi

        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"没有 ID 为 {session_id} 的进程"}

        # 读 session.exited 前先与真实子进程状态对齐（防孤儿管道 reader 挂住）
        self._reconcile_local_exit(session)

        output_preview = strip_ansi(session.output_tail(1000))

        result = {
            "session_id": session.id,
            "command": session.command,
            "status": "exited" if session.exited else "running",
            "pid": session.pid,
            "uptime_seconds": int(time.time() - session.started_at),
            "output_preview": output_preview,
        }
        if session.exited:
            result["exit_code"] = session.exit_code
            result["completion_reason"] = session.completion_reason
            result["termination_source"] = session.termination_source
            # 注意：poll() 是只读状态查询，刻意**不**标记 _completion_consumed。
            # wait()/read_log() 才代表真正的输出消费并做标记。在这里标记会让
            # 一次状态查询静默抑制掉 notify_on_complete watcher 的自主投递轮次。
            # 但确实记入 _poll_observed，让 CLI 的内联 drain 仍能去重（Agent
            # 这一轮的 poll 结果里已经看到退出了），而不影响只查
            # _completion_consumed 的桌面端 watcher。
            self._poll_observed.add(session_id)
        if session.detached:
            result["detached"] = True
            result["note"] = "进程在重启后恢复 —— 输出历史不可用"
        return result

    def read_log(self, session_id: str, offset: int = 0, limit: int = 200) -> dict:
        """读取完整输出日志，支持按行分页。"""
        from spirit.tools.infra_utils import strip_ansi

        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"没有 ID 为 {session_id} 的进程"}

        full_output = strip_ansi(session.output_tail(session.max_output_chars))

        lines = full_output.splitlines()
        total_lines = len(lines)

        if offset == 0 and limit > 0:
            # 默认：最后 N 行
            selected = lines[-limit:]
            observed_completion_output = bool(selected) or total_lines == 0
        else:
            selected = lines[offset:offset + limit]
            stop = slice(offset, offset + limit).indices(total_lines)[1]
            observed_completion_output = (
                total_lines == 0 or (bool(selected) and stop == total_lines)
            )

        result = {
            "session_id": session.id,
            "command": session.command,
            "status": "exited" if session.exited else "running",
            "output": "\n".join(selected),
            "total_lines": total_lines,
            "showing": f"{len(selected)} lines",
        }
        # 只有真的看到了收尾输出才标记消费；读中间分页不算，否则 Agent 还没
        # 看到结尾就把自主通知抑制掉了。
        if session.exited and observed_completion_output:
            self._completion_consumed.add(session_id)
        return result

    def wait(self, session_id: str, timeout: Optional[int] = None) -> dict:
        """阻塞直到进程退出、超时或被中断。

        Returns:
            dict，``status`` ∈ ``exited`` / ``timeout`` / ``interrupted`` /
            ``not_found``，附输出快照。
        """
        from spirit.config import get_config_value
        from spirit.tools.infra_utils import strip_ansi

        try:
            default_timeout = int(get_config_value("process.wait_default_seconds", 180))
        except (ValueError, TypeError):
            default_timeout = 180
        max_timeout = default_timeout
        requested_timeout = timeout
        timeout_note = None

        # 钳制：请求超过配置上限时按上限等，并明确告知被钳制了（静默截断会让
        # 模型误以为"等了 600s 还没结束"）。
        if requested_timeout and requested_timeout > max_timeout:
            effective_timeout = max_timeout
            timeout_note = (
                f"Requested wait of {requested_timeout}s was clamped "
                f"to configured limit of {max_timeout}s"
            )
        else:
            effective_timeout = requested_timeout or max_timeout

        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"没有 ID 为 {session_id} 的进程"}

        try:
            from spirit.tools.extra_tools import is_interrupted as _is_interrupted
        except Exception:
            def _is_interrupted() -> bool:
                return False

        deadline = time.monotonic() + effective_timeout

        while time.monotonic() < deadline:
            session = self._refresh_detached_session(session)
            if session is None:
                return {"status": "not_found", "error": f"没有 ID 为 {session_id} 的进程"}
            # 与真实子进程状态对齐 —— 防 reader 阻塞但直接子进程已退出的情况
            self._reconcile_local_exit(session)
            if session.exited:
                self._completion_consumed.add(session_id)
                result = {
                    "status": "exited",
                    "session_id": session.id,
                    "command": session.command,
                    "exit_code": session.exit_code,
                    "completion_reason": session.completion_reason,
                    "termination_source": session.termination_source,
                    "output": strip_ansi(session.output_tail(2000)),
                }
                if timeout_note:
                    result["timeout_note"] = timeout_note
                return result

            if _is_interrupted():
                result = {
                    "status": "interrupted",
                    "session_id": session.id,
                    "command": session.command,
                    "output": strip_ansi(session.output_tail(1000)),
                    "note": "用户发来了新消息 —— wait 被中断",
                }
                if timeout_note:
                    result["timeout_note"] = timeout_note
                return result

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # 睡在 completion_event 上：进程一结束立即唤醒，不用等满 1s 轮询
            session._completion_event.wait(timeout=min(1.0, remaining))

        result = {
            "status": "timeout",
            "session_id": session.id,
            "command": session.command,
            "output": strip_ansi(session.output_tail(1000)),
        }
        if timeout_note:
            result["timeout_note"] = timeout_note
        else:
            result["timeout_note"] = f"Waited {effective_timeout}s, process still running"
        return result

    # ===================================================================
    # 终止 / stdin
    # ===================================================================

    def kill_process(
        self,
        session_id: str,
        *,
        source: str = "process.kill",
        consume_output: bool = True,
    ) -> dict:
        """杀掉一个后台进程并返回其输出快照。

        显式的工具/RPC kill 传 ``consume_output=True``（调用方会看返回的输出）；
        批量清理传 False：它丢弃每个结果，因此绝不能抑制掉一条自主的、
        携带输出的完成通知。
        """
        from spirit.tools.infra_utils import strip_ansi

        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"没有 ID 为 {session_id} 的进程"}

        if session.exited:
            with session._lock:
                result = {
                    "status": "already_exited",
                    "session_id": session.id,
                    "command": session.command,
                    "exit_code": session.exit_code,
                    "completion_reason": session.completion_reason,
                    "termination_source": session.termination_source,
                    "output": strip_ansi(session.output_buffer[-2000:]),
                }
            # 只有在输出确实出现在显式 kill 的结果里时才抑制自主轮次，
            # 与 wait/log 的消费语义一致。
            if consume_output:
                self._completion_consumed.add(session_id)
            return result

        try:
            if session._pty:
                try:
                    session._pty.terminate(force=True)
                except Exception:
                    if session.pid:
                        os.kill(session.pid, signal.SIGTERM)
            elif session.process:
                # 本地进程 —— 杀整棵进程树。Windows 上必须 taskkill /T /F；
                # Popen.terminate() 只杀 shell 包装器，会把后代留在身后。
                self._terminate_host_pid(session.process.pid, session.host_start_time)
            elif session.detached and session.pid_scope == "host" and session.pid:
                # 身份检查而非裸存活：PID 已消失**或**被复用到无关进程上，
                # 都视为我们的进程已退出，且绝不对陌生人做进程树终止。
                if not self._host_pid_is_ours(session.pid, session.host_start_time):
                    with session._lock:
                        session.exited = True
                        session.exit_code = None
                        output = strip_ansi(session.output_buffer[-2000:])
                    if consume_output:
                        self._completion_consumed.add(session_id)
                    self._move_to_finished(session)
                    return {
                        "status": "already_exited",
                        "session_id": session.id,
                        "exit_code": session.exit_code,
                        "output": output,
                    }
                self._terminate_host_pid(session.pid, session.host_start_time)
            else:
                return {
                    "status": "error",
                    "error": (
                        "恢复得到的进程在重启后无法被杀掉，因为它的原始运行时句柄已不可用"
                    ),
                }
            # 先抓输出，再标记消费，最后才把 exited 暴露给 watcher 任务。
            # 这个顺序关掉了"延迟通知"竞争，同时不丢掉终端转录。
            with session._lock:
                output = strip_ansi(session.output_buffer[-2000:])
                if consume_output:
                    self._completion_consumed.add(session_id)
                session.exited = True
                session.exit_code = -15  # SIGTERM
                session.completion_reason = "killed"
                session.termination_source = source
            self._move_to_finished(session)
            self._write_checkpoint()
            return {
                "status": "killed",
                "session_id": session.id,
                "completion_reason": session.completion_reason,
                "termination_source": session.termination_source,
                "output": output,
            }
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def write_stdin(self, session_id: str, data: str) -> dict:
        """向运行中的进程 stdin 发送原始数据（不追加换行）。"""
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"没有 ID 为 {session_id} 的进程"}
        if session.exited:
            return {"status": "already_exited", "error": "进程已经结束"}

        # PTY 模式 —— 通过 pty 句柄写
        if getattr(session, "_pty", None):
            try:
                # pywinpty 在 Windows 上要 str；ptyprocess 在 POSIX 上要 bytes
                if _IS_WINDOWS:
                    pty_data = data.decode("utf-8") if isinstance(data, bytes) else str(data)
                else:
                    pty_data = data.encode("utf-8") if isinstance(data, str) else data
                session._pty.write(pty_data)
                return {"status": "ok", "bytes_written": len(data)}
            except Exception as exc:
                return {"status": "error", "error": str(exc)}

        # Popen 模式 —— 通过 stdin 管道写
        if not session.process or not session.process.stdin:
            return {
                "status": "error",
                "error": "进程 stdin 不可用（非交互派生或 stdin 已关闭）",
            }
        try:
            session.process.stdin.write(data)
            session.process.stdin.flush()
            return {"status": "ok", "bytes_written": len(data)}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def submit_stdin(self, session_id: str, data: str = "") -> dict:
        """向运行中的进程 stdin 发送数据 + 换行（相当于按回车）。"""
        return self.write_stdin(session_id, data + "\n")

    def close_stdin(self, session_id: str) -> dict:
        """关闭运行中进程的 stdin / 发送 EOF，但不杀进程。"""
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"没有 ID 为 {session_id} 的进程"}
        if session.exited:
            return {"status": "already_exited", "error": "进程已经结束"}

        if getattr(session, "_pty", None):
            try:
                session._pty.sendeof()
                return {"status": "ok", "message": "EOF sent"}
            except Exception as exc:
                return {"status": "error", "error": str(exc)}

        if not session.process or not session.process.stdin:
            return {
                "status": "error",
                "error": "进程 stdin 不可用（非交互派生或 stdin 已关闭）",
            }
        try:
            session.process.stdin.close()
            return {"status": "ok", "message": "stdin closed"}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def request_close_terminal(self, session_id: str) -> dict:
        """请求桌面 GUI 关闭镜像该后台进程的只读终端标签。

        这**不**杀进程 —— 只丢掉视图。输出继续流入（有上限的）缓冲，用户可以从
        状态栈重新打开标签。仅桌面端可用：没有接 UI close sink 时（CLI / 消息
        平台）返回明确错误而不是抛异常。
        """
        sink = self.on_close
        if sink is None:
            return {
                "status": "error",
                "error": "close_terminal 仅在 Spirit 桌面端可用。",
            }
        # 会话可能已结束（甚至已被 prune）—— 标签仍可能残留并需要关闭，
        # 所以这里"会话不存在"不算错误。
        session = self.get(session_id)
        try:
            sink(session, session_id)
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        return {
            "status": "ok",
            "closed": session_id,
            "note": (
                "已关闭只读终端标签。进程未被杀掉；它的输出仍可用，"
                "用户可以从状态栈重新打开标签。"
            ),
        }

    # ===================================================================
    # 列表 / 活跃查询
    # ===================================================================

    def count_running(self) -> int:
        """当前运行中的后台进程数（O(1)，适合状态栏每帧轮询）。

        CPython 的 dict ``len()`` 是原子的，调用方无需持锁。只反映
        ``_running``：子进程退出时会话被移入 ``_finished``。
        """
        try:
            return len(self._running)
        except Exception:
            return 0

    def list_sessions(self, task_id: Optional[str] = None,
                      session_key: Optional[str] = None) -> List[dict]:
        """列出所有运行中与近期结束的进程。

        给了 ``task_id`` 时包含该任务的进程。同时给了 ``session_key`` 时，
        注册在该会话下的**会话级**后台进程（``background: true``）也会被
        浮出来，即使属于别的任务 —— 这样 Agent 能发现一个被遗忘的、正阻塞
        会话重置的预览服务器。这类跨任务条目会被打上 ``session_scoped: true``。
        """
        with self._lock:
            all_sessions = list(self._running.values()) + list(self._finished.values())

        all_sessions = [self._refresh_detached_session(s) for s in all_sessions]

        if task_id or session_key:
            all_sessions = [
                s for s in all_sessions
                if (task_id and s.task_id == task_id)
                or (session_key and s.session_key == session_key)
            ]

        result = []
        for s in all_sessions:
            entry = {
                "session_id": s.id,
                "command": s.command[:200],
                "cwd": s.cwd,
                "pid": s.pid,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(s.started_at)),
                "uptime_seconds": int(time.time() - s.started_at),
                "status": "exited" if s.exited else "running",
                "output_preview": s.output_buffer[-200:] if s.output_buffer else "",
            }
            # 标记仅因共享会话（而非当前任务）才浮出来的进程 —— 这些正是用户
            # 可能已经忘掉的长驻后台进程。
            if task_id and session_key and s.task_id != task_id and s.session_key == session_key:
                entry["session_scoped"] = True
            # 触发器元数据，让目标循环的 judge 能决定泊在这个进程**自己的信号**
            # （watch 匹配或完成）上，而不只是它的退出。带 watch_patterns 的
            # watcher 可能永不退出。
            if s.watch_patterns and not s._watch_disabled:
                entry["watch_patterns"] = list(s.watch_patterns)
                entry["watch_hit"] = s._watch_hits > 0
            if s.notify_on_complete:
                entry["notify_on_complete"] = True
            if s.exited:
                entry["exit_code"] = s.exit_code
            if s.detached:
                entry["detached"] = True
            result.append(entry)
        return result

    def has_active_processes(self, task_id: str) -> bool:
        """某个 task_id 下是否有活跃（运行中）的进程。"""
        with self._lock:
            sessions = list(self._running.values())
        for session in sessions:
            self._refresh_detached_session(session)
        with self._lock:
            return any(
                s.task_id == task_id and not s.exited
                for s in self._running.values()
            )

    def has_active_for_session(
        self, session_key: str, max_active_age: Optional[float] = None,
    ) -> bool:
        """某个会话键下是否有活跃进程。

        设了 *max_active_age*（秒）时，启动超过该时长的进程被**忽略** ——
        它们仍在运行，但被视为陈旧，不得阻塞会话空闲/每日重置。这防止一个
        被遗忘的 ``http.server``（或任何长驻预览进程）永久冻结会话生命周期。

        ``None`` 保留旧行为（任何运行中的进程都阻塞）。
        """
        with self._lock:
            sessions = list(self._running.values())
        for session in sessions:
            self._refresh_detached_session(session)
        now = time.time()
        with self._lock:
            return any(
                s.session_key == session_key
                and not s.exited
                and (max_active_age is None or (now - s.started_at) < max_active_age)
                for s in self._running.values()
            )

    def has_any_active(self) -> bool:
        """是否**任何**后台进程仍在运行（跨所有会话）。

        供空闲检测使用：有活的后台进程（terminal background=true）就不算空闲，
        不能被挂起，否则进程会丢。先刷新 detached 会话，好让"已结束但未回收"
        的进程读作不活跃。
        """
        with self._lock:
            sessions = list(self._running.values())
        for session in sessions:
            self._refresh_detached_session(session)
        with self._lock:
            return any(not s.exited for s in self._running.values())

    def kill_all(self, task_id: Optional[str] = None) -> int:
        """杀掉所有运行中的进程（可按 task_id 过滤），返回杀掉的数量。"""
        with self._lock:
            targets = [
                s for s in self._running.values()
                if (task_id is None or s.task_id == task_id) and not s.exited
            ]
        killed = 0
        for session in targets:
            result = self.kill_process(
                session.id, source="kill_all", consume_output=False,
            )
            if result.get("status") in {"killed", "already_exited"}:
                killed += 1
        return killed

    # ===================================================================
    # 清理 / checkpoint
    # ===================================================================

    def _prune_if_needed(self) -> None:
        """超过 ``MAX_PROCESSES`` 时淘汰最旧的已完成会话。**必须持有 _lock**。"""
        now = time.time()
        expired = [
            sid for sid, s in self._finished.items()
            if (now - s.started_at) > FINISHED_TTL_SECONDS
        ]
        for sid in expired:
            del self._finished[sid]
            self._completion_consumed.discard(sid)
            self._poll_observed.discard(sid)

        total = len(self._running) + len(self._finished)
        if total >= MAX_PROCESSES and self._finished:
            oldest_id = min(self._finished, key=lambda sid: self._finished[sid].started_at)
            del self._finished[oldest_id]
            self._completion_consumed.discard(oldest_id)
            self._poll_observed.discard(oldest_id)

        # 丢掉那些会话已完全不再被追踪的 consumed/observed 条目 —— 双保险，
        # 防止注册表查询路径上不经过 dict prune 而导致模块生命周期内无限增长。
        tracked = self._running.keys() | self._finished.keys()
        stale = self._completion_consumed - tracked
        if stale:
            self._completion_consumed -= stale
        stale_polls = self._poll_observed - tracked
        if stale_polls:
            self._poll_observed -= stale_polls

    def _write_checkpoint(self) -> None:
        """把运行中进程的元数据原子写入 checkpoint 文件。

        受 ``process.checkpoint_enabled`` 控制（关掉可避免频繁磁盘写）。
        写失败只记 debug —— checkpoint 是尽力而为的恢复手段，绝不能因为
        它报错而让一次正常的进程派生/退出失败。
        """
        try:
            from spirit.config import get_config_value

            if not get_config_value("process.checkpoint_enabled", True):
                return
        except Exception:
            pass
        try:
            with self._lock:
                entries = []
                for s in self._running.values():
                    if not s.exited:
                        # 惰性回填宿主 PID 的创建时间基线，让重启后的恢复即使
                        # 对早于该字段存在的会话也能检测 PID 复用。
                        if s.host_start_time is None and s.pid_scope == "host" and s.pid:
                            s.host_start_time = self._safe_host_start_time(s.pid)
                        entries.append(s.to_checkpoint_dict())
            _atomic_json_write(_checkpoint_path(), entries)
        except Exception as exc:
            logger.debug("写 checkpoint 文件失败: %s", exc, exc_info=True)

    def recover_from_checkpoint(self) -> int:
        """启动时探测 checkpoint 文件里的 PID，把仍存活的接管为 detached 会话。

        Returns:
            恢复的进程数量。
        """
        path = _checkpoint_path()
        if not path.exists():
            return 0
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("checkpoint 文件损坏，忽略: %s", exc)
            return 0
        if not isinstance(entries, list):
            return 0

        recovered = 0
        kept: List[dict] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            sid = entry.get("session_id")
            pid = entry.get("pid")
            if not sid:
                continue

            # 仍在本注册表里活着的条目（例如重复调用 recover）原样保留
            with self._lock:
                already_live = sid in self._running
            if already_live:
                kept.append(entry)
                continue

            # 显式的 sandbox 后端条目在本地注册表里无法接管（PID 是环境内的）
            if entry.get("pid_scope") not in (None, "host"):
                continue

            start_time = entry.get("host_start_time")
            if pid and not self._host_pid_is_ours(pid, start_time):
                # PID 已死或被复用 —— 不接管，也绝不后续去杀它
                continue

            session = ProcessSession(
                id=sid,
                command=entry.get("command", ""),
                task_id=entry.get("task_id", "") or "",
                session_key=entry.get("session_key", "") or "",
                pid=pid,
                cwd=entry.get("cwd"),
                started_at=entry.get("started_at") or time.time(),
                host_start_time=start_time,
                detached=True,
                pid_scope=entry.get("pid_scope") or "host",
                watcher_platform=entry.get("watcher_platform", "") or "",
                watcher_chat_id=entry.get("watcher_chat_id", "") or "",
                watcher_user_id=entry.get("watcher_user_id", "") or "",
                watcher_user_name=entry.get("watcher_user_name", "") or "",
                watcher_thread_id=entry.get("watcher_thread_id", "") or "",
                watcher_message_id=entry.get("watcher_message_id", "") or "",
                watcher_interval=int(entry.get("watcher_interval") or 0),
                notify_on_complete=bool(entry.get("notify_on_complete")),
                watch_patterns=list(entry.get("watch_patterns") or []),
            )
            if not pid:
                # 没有 PID 就无法判断死活 —— 直接视为已退出，别泊住任何屏障
                session.exited = True
                session.exit_code = None
                session.completion_reason = "lost"
                with self._lock:
                    self._finished[sid] = session
                continue

            with self._lock:
                self._running[sid] = session
            recovered += 1
            kept.append(entry)
            if session.watcher_interval:
                self.pending_watchers.append(entry)

        # 只把仍然有效的条目写回，避免 checkpoint 无限增长
        try:
            _atomic_json_write(path, kept)
        except Exception as exc:
            logger.debug("回写 checkpoint 失败: %s", exc)
        return recovered


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

process_registry = ProcessRegistry()
"""全局后台进程注册表（工具层与对话循环共享同一实例）。"""


def get_process_registry() -> ProcessRegistry:
    """取全局注册表（显式入口，便于测试替换）。"""
    return process_registry


__all__ = [
    "ProcessRegistry",
    "ProcessSession",
    "process_registry",
    "get_process_registry",
]
