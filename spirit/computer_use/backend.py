"""Computer Use 后端抽象接口 — Spirit Agent（Phase 4.5）。

对标 Hermes ``tools/computer_use/backend.py``：任何实现（真实 cua-driver / pyautogui /
noop / 未来的 Linux·Windows 后端）都必须返回这里描述的形状。所有方法同步；异步在后端
实现内部自行处理。

**可测试抽象层**是本包第一设计约束（对齐用户「和 Hermes 一样的任务检查测试用例」）：
把「桌面控制的形状」与「具体平台驱动」彻底解耦——:class:`ComputerUseBackend` 是纯 ABC，
:mod:`spirit.computer_use.noop_backend` 是一等的内存实现（记录调用、返回平凡结果），
:mod:`spirit.computer_use.tool` 的派发 / 安全 / 审批逻辑只吃抽象接口，故全部可离线单测，
真实驱动（若接入）经 :func:`spirit.computer_use.tool.set_backend_factory` 注入即可。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class UIElement:
    """当前屏幕上的一个可交互元素。"""

    index: int                       # 1-based SOM 索引
    role: str                        # AX 角色（AXButton / AXTextField / ...）
    label: str = ""                  # AXTitle / AXDescription / AXValue 片段
    bounds: Tuple[int, int, int, int] = (0, 0, 0, 0)  # x, y, w, h（逻辑像素）
    app: str = ""                    # 所属 bundle ID 或应用名
    pid: int = 0                     # 所属进程 PID
    window_id: int = 0               # 原生窗口 ID
    attributes: Dict[str, Any] = field(default_factory=dict)
    # 每次快照的不透明元素句柄（用于显式陈旧检测：陈旧 token 由驱动返回错误，
    # 而非静默重解析到另一个元素）。旧驱动不带此字段时为 None。
    element_token: Optional[str] = None

    def center(self) -> Tuple[int, int]:
        """元素包围盒的中心点 ``(x, y)``。"""
        x, y, w, h = self.bounds
        return x + w // 2, y + h // 2


@dataclass
class CaptureResult:
    """一次截屏调用的结果。

    依捕获模式，``png_b64`` / ``elements`` 至少有一个被填充：

    * ``mode="vision"`` → 仅 ``png_b64``
    * ``mode="ax"``     → 仅 ``elements``
    * ``mode="som"``    → 两者（默认）：PNG 已由后端画好编号覆盖层，``elements``
      持有对应的 索引 → 元素 映射。
    """

    mode: str
    width: int                       # 截图宽（逻辑像素，缩放前）
    height: int
    png_b64: Optional[str] = None
    elements: List[UIElement] = field(default_factory=list)
    app: str = ""                    # 元素所属的目标应用 / 窗口
    window_title: str = ""
    png_bytes_len: int = 0           # 发给模型的原始字节数（供 token 估算）
    image_mime_type: Optional[str] = None  # 显式 MIME（None 时下游按 base64 前缀嗅探）


@dataclass
class ActionResult:
    """任意动作（click / type / scroll / drag / key / wait）的结果。"""

    ok: bool
    action: str
    message: str = ""                # 人类可读摘要
    capture: Optional[CaptureResult] = None  # 动作后截图（调用方要求或后端总是返回时）
    meta: Dict[str, Any] = field(default_factory=dict)  # 调试 / 遥测附加字段


class ComputerUseBackend(ABC):
    """桌面控制后端抽象基类。生命周期：首次使用前 ``start()``，关闭时 ``stop()``。"""

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def is_available(self) -> bool:
        """后端此刻是否可在本机使用（供 check_fn 门控与安装向导使用）。"""

    # ── 捕获 ──────────────────────────────────────────────────────
    @abstractmethod
    def capture(
        self,
        mode: str = "som",
        app: Optional[str] = None,
        pid: Optional[int] = None,
        window_id: Optional[int] = None,
    ) -> CaptureResult: ...

    # ── 指针动作 ──────────────────────────────────────────────────
    @abstractmethod
    def click(
        self,
        *,
        element: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",           # left | right | middle
        click_count: int = 1,
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult: ...

    @abstractmethod
    def drag(
        self,
        *,
        from_element: Optional[int] = None,
        to_element: Optional[int] = None,
        from_xy: Optional[Tuple[int, int]] = None,
        to_xy: Optional[Tuple[int, int]] = None,
        button: str = "left",
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult: ...

    @abstractmethod
    def scroll(
        self,
        *,
        direction: str,                 # up | down | left | right
        amount: int = 3,                # 滚轮刻度数
        element: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult: ...

    # ── 键盘 ──────────────────────────────────────────────────────
    @abstractmethod
    def type_text(self, text: str) -> ActionResult: ...

    @abstractmethod
    def key(self, keys: str) -> ActionResult:
        """发送组合键，如 ``'cmd+s'`` / ``'ctrl+alt+t'`` / ``'return'``。"""

    # ── 内省 ──────────────────────────────────────────────────────
    @abstractmethod
    def list_apps(self) -> List[Dict[str, Any]]:
        """返回运行中的应用（含 bundle ID / PID / 窗口数）。"""

    def list_windows(self) -> List[Dict[str, Any]]:
        """返回可见原生窗口（含 PID 与窗口标识）。

        可选兼容钩子：早于窗口发现能力的后端仍可实例化，只是报告无窗口。
        """
        return []

    @abstractmethod
    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        """把输入路由到 ``app``（按名或 bundle ID）。默认聚焦但不前置。"""

    # ── 原生值修改 ────────────────────────────────────────────────
    @abstractmethod
    def set_value(self, value: str, element: Optional[int] = None) -> ActionResult:
        """在元素上设置原生值（如 AXPopUpButton 选择）。

        ``element`` 是先前 capture 调用返回的 1-based SOM 索引。
        """

    # ── 计时 ──────────────────────────────────────────────────────
    def wait(self, seconds: float) -> ActionResult:
        """默认实现：``time.sleep``（钳制到 [0, ``computer_use.wait_max_seconds``] 秒）。"""
        import time

        from spirit.config import get_config_value
        cap = float(get_config_value("computer_use.wait_max_seconds", 30))
        time.sleep(max(0.0, min(seconds, cap)))
        return ActionResult(ok=True, action="wait", message=f"waited {seconds:.2f}s")


__all__ = [
    "UIElement",
    "CaptureResult",
    "ActionResult",
    "ComputerUseBackend",
]
