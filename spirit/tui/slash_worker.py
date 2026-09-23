"""持久化斜杠命令 worker — 每个 TUI 会话一个（Phase 4.4）。

对标 Hermes ``tui_gateway/slash_worker.py``：协议为从 stdin 读 JSON 行 ``{id, command}``，向
stdout 写 ``{id, ok, output|error}``。父进程死亡看门狗（:func:`_is_orphaned`，经可注入
``getppid`` seam）在网关崩溃时回收孤儿子进程，避免残留进程持有会话锁。

**可测试抽象层**：把协议循环从 stdin/stdout 解耦成 :func:`serve_stream`（吃 file-like
``inp`` / ``out`` + 可注入 ``runner`` seam），测试用 ``StringIO`` + 假 runner 离线断言协议帧、
命令规范化、异常折叠，无需 spawn 真实 CLI 子进程。:func:`main` 只是把真实 stdin/stdout 与
真实 runner 接到 :func:`serve_stream` 上的薄壳。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Optional

# 环境可覆盖，使集成测试能驱动亚秒级时序。
def _env_float(name: str, default: float) -> float:
    """解析 float 环境旋钮，缺失/畸形值回退 ``default``。

    裸 ``float(os.environ.get(...))`` 会在 import 期因笔误（如 ``...POLL_S=2s``）抛 ValueError
    并在 worker 服务首个命令前杀死它——故此处吞掉畸形值。
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


_WATCHDOG_POLL_S = max(0.05, _env_float("SPIRIT_SLASH_WATCHDOG_POLL_S", 2.0))
_ORPHAN_GRACE_S = max(0.0, _env_float("SPIRIT_SLASH_WATCHDOG_GRACE_S", 5.0))
_in_flight = threading.Event()  # 命令执行期间置位


def _is_orphaned(original_ppid, getppid=os.getppid) -> bool:
    """本 worker 是否已失去其原始 POSIX 父进程（被 reparent 到 subreaper/init）。"""
    return getppid() != original_ppid


def _start_parent_death_watchdog(original_ppid) -> None:
    """后台守护线程：父进程消失后，宽限在途命令刷完，然后硬退出。"""
    def _loop():
        while not _is_orphaned(original_ppid):
            time.sleep(_WATCHDOG_POLL_S)
        deadline = time.monotonic() + _ORPHAN_GRACE_S
        while _in_flight.is_set() and time.monotonic() < deadline:
            time.sleep(0.05)  # 让在途命令完成 / 刷出
        os._exit(0)

    threading.Thread(target=_loop, daemon=True).start()


def normalize_command(command: str) -> str:
    """规范化斜杠命令：strip；缺 ``/`` 前缀则补；空命令返回空串。"""
    cmd = (command or "").strip()
    if not cmd:
        return ""
    return cmd if cmd.startswith("/") else f"/{cmd}"


def serve_stream(inp: Any, out: Any, runner: Callable[[str], str]) -> int:
    """JSON-line 协议循环（从 :func:`main` 解耦，供离线单测）。

    从 ``inp`` 逐行读 ``{id, command}``，用 ``runner(command) -> output`` 执行，向 ``out`` 写
    ``{id, ok, output}``（成功）或 ``{id, ok:False, error}``（失败）。空行跳过。返回处理的请求数。

    Args:
        inp: 可迭代的行来源（file-like，如 ``sys.stdin`` 或 ``StringIO``）。
        out: 有 ``.write`` / ``.flush`` 的汇（如 ``sys.stdout`` 或 ``StringIO``）。
        runner: 命令执行 seam（``command -> 纯文本输出``）；抛异常则折叠成 ``ok:False``。
    """
    handled = 0
    for raw in inp:
        line = (raw or "").strip()
        if not line:
            continue

        _in_flight.set()
        rid: Optional[Any] = None
        try:
            req = json.loads(line)
            rid = req.get("id")
            command = normalize_command(req.get("command", ""))
            output = runner(command) if command else ""
            out.write(json.dumps({"id": rid, "ok": True, "output": output}) + "\n")
        except Exception as exc:  # 协议帧或 runner 崩溃 → 折叠成错误帧，绝不杀死 worker
            out.write(json.dumps({"id": rid, "ok": False, "error": str(exc)}) + "\n")
        finally:
            out.flush()
            _in_flight.clear()
            handled += 1
    return handled


def make_cli_runner(model: str = "") -> Callable[[str], str]:
    """构造真实 runner：驱动 Spirit CLI 执行斜杠命令并把输出捕获为纯文本（剥 ANSI）。

    尽力而为：无可用斜杠处理器时返回占位串，绝不抛出（worker 必须存活服务后续命令）。
    """
    def _runner(command: str) -> str:
        import contextlib
        import io

        from spirit.tools.infra_utils import strip_ansi

        buf = io.StringIO()
        try:
            from spirit.cli.main_enhanced import process_slash_command  # 惰性 import
        except Exception:
            return f"(no slash handler available for {command})"
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                process_slash_command(command, model=model or None)
        except Exception as exc:
            return strip_ansi(f"{buf.getvalue()}\nerror: {exc}".strip())
        return strip_ansi(buf.getvalue().rstrip())

    return _runner


def main(argv: Optional[list] = None) -> int:
    """worker 入口：装看门狗 + 建 runner + 把真实 stdin/stdout 接到 :func:`serve_stream`。"""
    import argparse
    import sys

    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--session-key", required=True)
    p.add_argument("--model", default="")
    args = p.parse_args(argv)

    os.environ["SPIRIT_SESSION_KEY"] = args.session_key
    os.environ["SPIRIT_INTERACTIVE"] = "1"

    # 在（数百毫秒的）CLI 构建之前启动看门狗——那段窗口本身也是网关中途死亡时的孤儿风险。
    orig_ppid = os.getppid()
    _start_parent_death_watchdog(orig_ppid)

    runner = make_cli_runner(args.model or "")
    return serve_stream(sys.stdin, sys.stdout, runner)


__all__ = [
    "normalize_command",
    "serve_stream",
    "make_cli_runner",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
