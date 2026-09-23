"""tests/process 共享 fixture 与辅助函数。

移植 Hermes ``tests/tools/test_process_registry.py`` 的 fixture 套路：
- ``registry`` fixture：每个测试一个全新注册表（``rehydrate_delegations=False``
  避免异步委托恢复的副作用）。
- ``_make_session``：直接构造 ``ProcessSession`` 塞进 ``registry._running`` /
  ``_finished``，绕开真实派生，做纯查询/状态逻辑的确定性单测。
- ``_spawn_python_sleep`` / ``_wait_until``：需要真实子进程时的可移植辅助。
- ``checkpoint_home``：把 ``SPIRIT_HOME`` 隔离到 ``tmp_path``，让 checkpoint
  读写落在临时目录。
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from spirit.process.registry import ProcessRegistry
from spirit.process.session import ProcessSession


@pytest.fixture()
def registry():
    """每个测试一个全新的 ProcessRegistry（不回灌异步委托）。"""
    return ProcessRegistry(rehydrate_delegations=False)


@pytest.fixture()
def checkpoint_home(tmp_path, monkeypatch):
    """把 SPIRIT_HOME 隔离到 tmp_path，checkpoint 文件落在临时目录。"""
    import spirit.config as config

    monkeypatch.setattr(config, "SPIRIT_HOME", tmp_path, raising=False)
    return tmp_path


def _make_session(
    sid: str = "proc_test123",
    command: str = "echo hello",
    task_id: str = "t1",
    session_key: str = "",
    exited: bool = False,
    exit_code=None,
    output: str = "",
    started_at=None,
    **kwargs,
) -> ProcessSession:
    """构造一个 ProcessSession 用于测试（对齐 Hermes _make_session）。"""
    return ProcessSession(
        id=sid,
        command=command,
        task_id=task_id,
        session_key=session_key,
        started_at=started_at if started_at is not None else time.time(),
        exited=exited,
        exit_code=exit_code,
        output_buffer=output,
        **kwargs,
    )


def _spawn_python_sleep(seconds: float) -> subprocess.Popen:
    """派生一个可移植的短命 Python sleep 进程。"""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
    )


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """轮询 predicate 直到返回真值或超时。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False
