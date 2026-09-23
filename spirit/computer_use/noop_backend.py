"""Noop（内存）Computer Use 后端 — Spirit Agent（Phase 4.5）。

对标 Hermes ``tools/computer_use/tool.py::_NoopBackend``，但在 Spirit 里提升为**一等公民**
（非 ``pragma: no cover`` 桩）：它是本包「可测试抽象层」的核心 seam——记录每一次调用、
返回平凡但形状正确的结果，绝不触碰真实桌面。用途：

1. **单元测试**——:mod:`spirit.computer_use.tool` 的派发 / 安全 / 审批逻辑全部对着
   ``NoopBackend`` 断言，无需真实 cua-driver 或图形会话。
2. **CI / 无头环境**——没有桌面驱动时，``computer_use`` 工具仍能以 noop 后端安全注册
   （``is_available()`` 恒 True，但动作只记录不生效），避免导入期崩溃。
3. **真实后端的契约样板**——任何新后端都可直接照抄本类的方法签名与返回形状。

可选地，``NoopBackend`` 能被喂一个「脚本」（``elements`` / ``apps`` / ``windows``），
让 capture / list_apps 返回非空数据，以便测试 SOM 索引 → 元素映射等下游塑形逻辑。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from spirit.computer_use.backend import (
    ActionResult,
    CaptureResult,
    ComputerUseBackend,
    UIElement,
)


class NoopBackend(ComputerUseBackend):
    """内存后端：记录调用、返回平凡结果，绝不触碰真实桌面。"""

    def __init__(
        self,
        *,
        available: bool = True,
        elements: Optional[List[UIElement]] = None,
        apps: Optional[List[Dict[str, Any]]] = None,
        windows: Optional[List[Dict[str, Any]]] = None,
        width: int = 1024,
        height: int = 768,
    ) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self._started = False
        self._available = available
        self._elements = list(elements or [])
        self._apps = list(apps or [])
        self._windows = list(windows or [])
        self._width = width
        self._height = height

    # ── 生命周期 ──────────────────────────────────────────────────
    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False

    def is_available(self) -> bool:
        return self._available

    @property
    def started(self) -> bool:
        return self._started

    def call_names(self) -> List[str]:
        """已记录调用的动作名序列（便于断言派发路由）。"""
        return [name for name, _ in self.calls]

    def last_call(self) -> Optional[Tuple[str, Dict[str, Any]]]:
        return self.calls[-1] if self.calls else None

    # ── 捕获 ──────────────────────────────────────────────────────
    def capture(
        self,
        mode: str = "som",
        app: Optional[str] = None,
        pid: Optional[int] = None,
        window_id: Optional[int] = None,
    ) -> CaptureResult:
        self.calls.append((
            "capture",
            {"mode": mode, "app": app, "pid": pid, "window_id": window_id},
        ))
        # noop 后端无真实像素：png_b64 恒 None；vision 模式不返回元素，其余回传脚本元素。
        elements = [] if mode == "vision" else list(self._elements)
        return CaptureResult(
            mode=mode, width=self._width, height=self._height, png_b64=None,
            elements=elements, app=app or "", window_title="",
        )

    # ── 指针动作 ──────────────────────────────────────────────────
    def click(self, **kw: Any) -> ActionResult:
        self.calls.append(("click", kw))
        return ActionResult(ok=True, action="click", message=f"click {kw.get('element')}")

    def drag(self, **kw: Any) -> ActionResult:
        self.calls.append(("drag", kw))
        return ActionResult(ok=True, action="drag")

    def scroll(self, **kw: Any) -> ActionResult:
        self.calls.append(("scroll", kw))
        return ActionResult(ok=True, action="scroll")

    # ── 键盘 ──────────────────────────────────────────────────────
    def type_text(self, text: str) -> ActionResult:
        self.calls.append(("type", {"text": text}))
        return ActionResult(ok=True, action="type")

    def key(self, keys: str) -> ActionResult:
        self.calls.append(("key", {"keys": keys}))
        return ActionResult(ok=True, action="key")

    # ── 内省 ──────────────────────────────────────────────────────
    def list_apps(self) -> List[Dict[str, Any]]:
        self.calls.append(("list_apps", {}))
        return list(self._apps)

    def list_windows(self) -> List[Dict[str, Any]]:
        self.calls.append(("list_windows", {}))
        return list(self._windows)

    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        self.calls.append(("focus_app", {"app": app, "raise": raise_window}))
        return ActionResult(ok=True, action="focus_app")

    # ── 原生值修改 ────────────────────────────────────────────────
    def set_value(self, value: str, element: Optional[int] = None) -> ActionResult:
        self.calls.append(("set_value", {"value": value, "element": element}))
        return ActionResult(ok=True, action="set_value")


__all__ = ["NoopBackend"]
