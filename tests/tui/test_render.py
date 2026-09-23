"""``spirit.tui.render`` 渲染桥接回退行为测试（Phase 4.4）。

对标 Hermes ``tests/tui_gateway/test_render.py``：无渲染器 → None；有渲染器 → 格式化；
TypeError（渲染器不接受 ``cols``）→ 无 cols 回退；其它异常 → None。Spirit 额外验证
``renderer=`` seam 注入路径与缺省惰性 ``import_module`` 路径。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from spirit.tui.render import (
    DEFAULT_RENDERER_MODULE,
    make_stream_renderer,
    render_diff,
    render_message,
)

from .conftest import FakeRenderer, NoColsRenderer


# ── 无渲染器 → None（Spirit 默认模块缺失，天然回退到前端渲染）──

def test_render_message_none_without_module():
    assert render_message("hello") is None


def test_render_diff_none_without_module():
    assert render_diff("+line") is None


def test_stream_renderer_none_without_module():
    assert make_stream_renderer() is None


# ── renderer= seam 注入 → 格式化 ──

def test_render_message_formatted_via_seam():
    mod = FakeRenderer()
    assert render_message("hi", 100, renderer=mod) == "<msg:hi:100>"


def test_render_message_passes_cols():
    mod = FakeRenderer()
    render_message("hi", 120, renderer=mod)
    assert mod.calls[-1] == {"fn": "format_response", "text": "hi", "cols": 120}


def test_render_diff_formatted_via_seam():
    mod = FakeRenderer()
    assert render_diff("+x", 100, renderer=mod) == "<diff:+x:100>"


def test_stream_renderer_returns_instance_via_seam():
    mod = FakeRenderer()
    assert make_stream_renderer(120, renderer=mod) is mod.stream_instance


# ── TypeError 回退（渲染器不接受 cols 关键字）──

def test_render_message_type_error_fallback():
    mod = NoColsRenderer()
    assert render_message("hi", renderer=mod) == "<nocols:hi>"


def test_render_diff_type_error_fallback():
    mod = NoColsRenderer()
    assert render_diff("+x", renderer=mod) == "<nocolsdiff:+x>"


def test_stream_renderer_type_error_fallback():
    mod = NoColsRenderer()
    assert make_stream_renderer(renderer=mod) == "stream-nocols"


# ── 其它异常 → None（渲染器崩溃不得掀翻 TUI）──

def test_render_message_exception_returns_none():
    mod = FakeRenderer(raise_on="format_response")
    assert render_message("hi", renderer=mod) is None


def test_render_diff_exception_returns_none():
    mod = FakeRenderer(raise_on="render_diff")
    assert render_diff("+x", renderer=mod) is None


def test_stream_renderer_exception_returns_none():
    mod = FakeRenderer(raise_on="StreamingRenderer")
    assert make_stream_renderer(renderer=mod) is None


# ── MagicMock 模块（对标 Hermes 用 MagicMock 断言）──

def test_render_message_with_magicmock_module():
    mod = MagicMock()
    mod.format_response.return_value = "<b>hi</b>"
    assert render_message("hi", 100, renderer=mod) == "<b>hi</b>"


def test_render_message_type_error_fallback_magicmock():
    mod = MagicMock()
    mod.format_response.side_effect = [TypeError, "fallback"]
    assert render_message("hi", renderer=mod) == "fallback"


# ── 缺省惰性 import 路径（对标 Hermes patch.dict sys.modules）──

def test_lazy_import_path_via_sys_modules():
    fake = MagicMock()
    fake.format_response.return_value = "LAZY"
    with patch.dict("sys.modules", {DEFAULT_RENDERER_MODULE: fake}):
        assert render_message("x") == "LAZY"


def test_lazy_import_absent_module_returns_none():
    # DEFAULT_RENDERER_MODULE 在 Spirit 中不存在 → import 失败 → None（无 patch）。
    with patch.dict("sys.modules", {DEFAULT_RENDERER_MODULE: None}):
        assert render_message("x") is None
