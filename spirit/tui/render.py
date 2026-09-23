"""渲染桥接 — 把 TUI 内容经 Python 侧渲染器路由（Phase 4.4）。

对标 Hermes ``tui_gateway/render.py``：当 Python 侧富文本渲染器存在时，用其函数把 markdown /
diff 渲染成带样式的终端文本；不存在时一律返回 ``None``，TUI 前端回退到自己的 ``markdown.tsx``
渲染。

Spirit 现状：暂无 Python 侧富文本渲染器（:data:`DEFAULT_RENDERER_MODULE` 缺失），故默认全部
返回 ``None``（前端渲染）。本桥接的价值在于**留好接缝**——一旦 Spirit 引入 ``rich_output``，
TUI 无需改动即可复用同一渲染路径。

**可测试抽象层**：渲染器可经 ``renderer=`` 关键字 seam 直接注入（duck-typed：有
``format_response`` / ``render_diff`` / ``StreamingRenderer`` 即可），测试无需真实依赖；缺省则
惰性 :func:`importlib.import_module` 默认模块（缺失 → None 回退）。
"""

from __future__ import annotations

import importlib
from typing import Any, Optional

# 可选的 Python 侧渲染器模块路径（Spirit 暂缺 → import 失败 → None 回退，前端渲染）。
DEFAULT_RENDERER_MODULE = "spirit.cli.rich_output"


def _resolve_renderer(renderer: Optional[Any]) -> Optional[Any]:
    """返回注入的 ``renderer``，或惰性 import 默认渲染器模块（缺失 → None）。"""
    if renderer is not None:
        return renderer
    try:
        return importlib.import_module(DEFAULT_RENDERER_MODULE)
    except ImportError:
        return None


def render_message(text: str, cols: int = 80, *, renderer: Optional[Any] = None) -> Optional[str]:
    """把 markdown 文本渲染成终端字符串；无渲染器 / 失败 → None（前端回退）。"""
    mod = _resolve_renderer(renderer)
    fmt = getattr(mod, "format_response", None)
    if fmt is None:
        return None
    try:
        return fmt(text, cols=cols)
    except TypeError:
        # 渲染器不接受 cols 关键字 → 退回到无 cols 调用（对齐 Hermes）。
        return fmt(text)
    except Exception:
        return None


def render_diff(text: str, cols: int = 80, *, renderer: Optional[Any] = None) -> Optional[str]:
    """把 unified diff 渲染成带色终端字符串；无渲染器 / 失败 → None。"""
    mod = _resolve_renderer(renderer)
    rd = getattr(mod, "render_diff", None)
    if rd is None:
        return None
    try:
        return rd(text, cols=cols)
    except TypeError:
        return rd(text)
    except Exception:
        return None


def make_stream_renderer(cols: int = 80, *, renderer: Optional[Any] = None) -> Optional[Any]:
    """构造流式渲染器实例；无渲染器 / 失败 → None。"""
    mod = _resolve_renderer(renderer)
    cls = getattr(mod, "StreamingRenderer", None)
    if cls is None:
        return None
    try:
        return cls(cols=cols)
    except TypeError:
        return cls()
    except Exception:
        return None


__all__ = [
    "DEFAULT_RENDERER_MODULE",
    "render_message",
    "render_diff",
    "make_stream_renderer",
]
