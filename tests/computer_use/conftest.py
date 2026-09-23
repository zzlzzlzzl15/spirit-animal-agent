"""tests/computer_use 共享 fixtures — Computer Use 可测试抽象层（Phase 4.5）。

对标 Hermes ``tests/tools/test_computer_use.py`` 顶部的 fixture 段：每个测试前后拆掉缓存
后端 + 强制 noop 后端 + 清审批回调/会话状态，保证测试零串扰。所有测试对着
:class:`~spirit.computer_use.noop_backend.NoopBackend` 断言，无需真实桌面驱动或图形会话。

提供的 fixtures：

``_reset_backend``（autouse）
    每个测试前后 ``reset_backend_for_tests()`` + ``clear_approval_callback()``，并强制
    ``SPIRIT_COMPUTER_USE_BACKEND=noop``（monkeypatch 自动还原）。
``noop_backend``
    返回当前活动的 noop 后端实例，供测试检查 ``.calls`` / ``.call_names()``。
``approval_recorder``
    工厂 fixture：安装一个 :class:`ApprovalRecorder` 审批回调并返回它，用于断言审批门
    的 verdict 状态机（approve_once / approve_session / always_approve / deny / 异常）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import pytest

from spirit.computer_use import tool as cu_tool
from spirit.computer_use.noop_backend import NoopBackend


# ---------------------------------------------------------------------------
# 后端隔离
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_backend(monkeypatch):
    """每个测试前后拆缓存后端 + 强制 noop 后端 + 清审批回调/状态。"""
    cu_tool.reset_backend_for_tests()
    cu_tool.clear_approval_callback()
    monkeypatch.setenv("SPIRIT_COMPUTER_USE_BACKEND", "noop")
    yield
    cu_tool.reset_backend_for_tests()
    cu_tool.clear_approval_callback()


@pytest.fixture
def noop_backend() -> NoopBackend:
    """返回当前活动的 noop 后端实例（惰性实例化 + 缓存 + start），供测试检查调用。"""
    backend = cu_tool._get_backend()
    assert isinstance(backend, NoopBackend)
    return backend


# ---------------------------------------------------------------------------
# 审批回调 recorder
# ---------------------------------------------------------------------------

Verdict = Union[str, Exception]


class ApprovalRecorder:
    """记录审批回调调用并按预设 verdict 序列应答的假回调。

    - 传单个字符串 → 每次调用都返回它。
    - 传列表 → 依次弹出，弹到只剩 1 个后重复返回最后一个。
    - verdict 为 ``Exception`` 实例 → 抛出（模拟回调崩溃 → tool 折叠成 deny）。
    """

    def __init__(self, verdicts: Union[Verdict, List[Verdict]] = "approve_once") -> None:
        self._verdicts: List[Verdict] = (
            [verdicts] if isinstance(verdicts, (str, Exception)) else list(verdicts)
        )
        self.calls: List[Tuple[str, Dict[str, Any], str]] = []

    def __call__(self, action: str, args: Dict[str, Any], summary: str) -> str:
        self.calls.append((action, args, summary))
        if len(self._verdicts) > 1:
            verdict = self._verdicts.pop(0)
        elif self._verdicts:
            verdict = self._verdicts[0]
        else:
            verdict = "deny"
        if isinstance(verdict, Exception):
            raise verdict
        return verdict

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def actions(self) -> List[str]:
        return [a for a, _, _ in self.calls]


@pytest.fixture
def approval_recorder() -> Callable[[Union[Verdict, List[Verdict]]], ApprovalRecorder]:
    """工厂 fixture：安装一个 :class:`ApprovalRecorder` 审批回调并返回它。"""

    def _install(verdicts: Union[Verdict, List[Verdict]] = "approve_once") -> ApprovalRecorder:
        rec = ApprovalRecorder(verdicts)
        cu_tool.set_approval_callback(rec)
        return rec

    return _install


# ---------------------------------------------------------------------------
# 权限探测 seam helpers（假 which / 假 runner）
# ---------------------------------------------------------------------------

class FakeCompleted:
    """``subprocess.CompletedProcess`` 形状的最小替身（.stdout / .returncode）。"""

    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def make_which(mapping: Optional[Dict[str, Optional[str]]] = None,
               default: Optional[str] = None) -> Callable[[str], Optional[str]]:
    """造一个假 ``shutil.which``：按 ``mapping`` 查名字，未命中回退 ``default``。"""
    table = dict(mapping or {})

    def _which(name: str) -> Optional[str]:
        if name in table:
            return table[name]
        return default

    return _which


def make_runner(responses: Optional[Dict[str, Any]] = None,
                default: Any = None,
                recorder: Optional[List[List[str]]] = None) -> Callable[[List[str], float], Any]:
    """造一个假 ``subprocess.run``。

    ``responses`` 的键是 ``" ".join(cmd[1:])``（子命令串，如 ``"doctor --json"``），值可以是
    :class:`FakeCompleted`、字符串（当作 stdout）、可调用（收 ``(cmd, timeout)``）、或
    ``Exception`` 实例（抛出）。未命中回退 ``default``。每次调用的 cmd 追加进 ``recorder``。
    """
    table = dict(responses or {})

    def _run(cmd: List[str], timeout: float) -> Any:
        if recorder is not None:
            recorder.append(list(cmd))
        key = " ".join(str(c) for c in cmd[1:])
        resp = table.get(key, default)
        if callable(resp) and not isinstance(resp, FakeCompleted):
            resp = resp(cmd, timeout)
        if isinstance(resp, Exception):
            raise resp
        if isinstance(resp, str):
            return FakeCompleted(stdout=resp)
        if resp is None:
            return FakeCompleted(stdout="")
        return resp

    return _run
