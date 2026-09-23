"""tests/tui 共享 fixtures — TUI 网关可测试抽象层（Phase 4.4）。

对标 Hermes ``tests/tui_gateway/`` 的测试组织：把跨文件复用的假件（渲染器、slash runner、
确定性时钟 / id 工厂）收敛到这里，各测试文件对着注入的 seam 断言纯逻辑，无需真实依赖。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

import pytest


class FakeRenderer:
    """假 Python 侧渲染器（duck-typed 匹配 render.py 期望的表面）。

    - ``format_response(text, cols=...)`` / ``render_diff(text, cols=...)`` 返回可预测串。
    - ``StreamingRenderer(cols=...)`` 返回可辨识实例。
    - ``raise_on`` 指定某方法抛异常，验证 render.py 的 None 折叠。

    不接受 cols 关键字的渲染器见 :class:`NoColsRenderer`（验证 TypeError 回退路径）。
    """

    def __init__(self, *, raise_on: Optional[str] = None) -> None:
        self.raise_on = raise_on
        self.calls: List[Dict[str, Any]] = []
        self.stream_instance = object()

    def _maybe_raise(self, name: str) -> None:
        if self.raise_on == name:
            raise RuntimeError(f"{name} exploded")

    def format_response(self, text, cols=None):
        self.calls.append({"fn": "format_response", "text": text, "cols": cols})
        self._maybe_raise("format_response")
        return f"<msg:{text}:{cols}>"

    def render_diff(self, text, cols=None):
        self.calls.append({"fn": "render_diff", "text": text, "cols": cols})
        self._maybe_raise("render_diff")
        return f"<diff:{text}:{cols}>"

    def StreamingRenderer(self, cols=None):  # noqa: N802 - 对标 Hermes 类名
        self.calls.append({"fn": "StreamingRenderer", "cols": cols})
        self._maybe_raise("StreamingRenderer")
        return self.stream_instance


class NoColsRenderer:
    """渲染器**不**接受 cols 关键字 → 触发 render.py 的 TypeError 回退路径。"""

    def __init__(self) -> None:
        self.texts: List[str] = []

    def format_response(self, text):
        self.texts.append(text)
        return f"<nocols:{text}>"

    def render_diff(self, text):
        self.texts.append(text)
        return f"<nocolsdiff:{text}>"

    def StreamingRenderer(self):  # noqa: N802
        return "stream-nocols"


@pytest.fixture
def fake_renderer() -> FakeRenderer:
    return FakeRenderer()


@pytest.fixture
def echo_slash_runner() -> Callable[[str], str]:
    """假 slash runner：把命令回显成 ``ran:<command>``，并记录调用。"""
    calls: List[str] = []

    def _runner(command: str) -> str:
        calls.append(command)
        return f"ran:{command}"

    _runner.calls = calls  # type: ignore[attr-defined]
    return _runner


@pytest.fixture
def fixed_clock() -> Callable[[], float]:
    """确定性时钟：每次调用返回递增值（1000.0, 1001.0, ...）。"""
    state = {"t": 999.0}

    def _clock() -> float:
        state["t"] += 1.0
        return state["t"]

    return _clock
