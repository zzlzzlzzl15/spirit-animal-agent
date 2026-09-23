"""``spirit.computer_use.tool`` 捕获响应成形测试（Phase 4.5）。

对标 Hermes ``tests/tools/test_computer_use.py::TestCaptureResponse``：验证 AX/文本路径的
``_capture_payload``（元素截断 + summary 行 + 截断说明）、``_capture_response``（有图且非 ax →
多模态信封 dict；否则 JSON 串）、MIME 嗅探（jpeg ``/9j/`` 前缀 / png 缺省 / 显式 image_mime_type），
以及 ``_element_to_dict`` / ``_format_elements`` / ``_text_response`` / ``_maybe_follow_capture``
的成形契约。

注：Spirit 版比 Hermes 简单——无「tiny image → text」PNG 尺寸解析，vision_routing 是独立决策
helper（未接进捕获路径），故此处不测那些分支。
"""

from __future__ import annotations

import json

import pytest

from spirit.computer_use import tool as cu_tool
from spirit.computer_use.backend import ActionResult, CaptureResult, UIElement


def _elem(index=1, role="AXButton", label="", bounds=(0, 0, 0, 0), app=""):
    return UIElement(index=index, role=role, label=label, bounds=bounds, app=app)


# ---------------------------------------------------------------------------
# _element_to_dict / _format_elements
# ---------------------------------------------------------------------------

class TestElementToDict:
    def test_full_shape(self):
        e = _elem(1, "AXButton", "OK", (10, 20, 30, 40), "Safari")
        assert cu_tool._element_to_dict(e) == {
            "index": 1, "role": "AXButton", "label": "OK",
            "bounds": {"x": 10, "y": 20, "w": 30, "h": 40},
            "center": [25, 40], "app": "Safari",
        }

    def test_center_is_bounds_center(self):
        e = _elem(2, "AXTextField", bounds=(0, 0, 100, 50))
        assert cu_tool._element_to_dict(e)["center"] == [50, 25]


class TestFormatElements:
    def test_with_label_uses_repr(self):
        lines = cu_tool._format_elements([_elem(1, "AXButton", "OK")])
        assert lines == ["  [1] AXButton 'OK' @ (0, 0)"]

    def test_without_label_omits_it(self):
        lines = cu_tool._format_elements([_elem(2, "AXTextField")])
        assert lines == ["  [2] AXTextField @ (0, 0)"]

    def test_empty_list(self):
        assert cu_tool._format_elements([]) == []


# ---------------------------------------------------------------------------
# _capture_payload — 截断 + summary
# ---------------------------------------------------------------------------

class TestCapturePayload:
    def test_truncates_to_max_elements(self):
        elems = [_elem(i, "AXButton") for i in range(1, 11)]
        cap = CaptureResult(mode="ax", width=100, height=50, elements=elems)
        payload = cu_tool._capture_payload(cap, max_elements=3)
        assert payload["total_elements"] == 10
        assert len(payload["elements"]) == 3
        assert payload["truncated_elements"] == 7
        assert "truncated to 3 of 10 elements" in payload["summary"]

    def test_no_truncation_when_under_limit(self):
        elems = [_elem(i, "AXButton") for i in range(1, 4)]
        cap = CaptureResult(mode="ax", width=100, height=50, elements=elems)
        payload = cu_tool._capture_payload(cap, max_elements=100)
        assert payload["total_elements"] == 3
        assert "truncated_elements" not in payload
        assert "truncated" not in payload["summary"]

    def test_summary_header_includes_app_and_window(self):
        cap = CaptureResult(
            mode="som", width=800, height=600, app="Safari",
            window_title="Apple", elements=[_elem(1)],
        )
        summary = cu_tool._capture_payload(cap)["summary"]
        assert summary.startswith("capture mode=som 800x600 app=Safari window='Apple'")
        assert "1 interactable element(s):" in summary

    def test_summary_header_without_app_window(self):
        cap = CaptureResult(mode="ax", width=10, height=10, elements=[])
        summary = cu_tool._capture_payload(cap)["summary"]
        assert summary.startswith("capture mode=ax 10x10")
        assert "app=" not in summary.splitlines()[0]

    def test_payload_carries_dimensions(self):
        cap = CaptureResult(mode="ax", width=1280, height=720, elements=[])
        payload = cu_tool._capture_payload(cap)
        assert payload["mode"] == "ax"
        assert payload["width"] == 1280
        assert payload["height"] == 720


# ---------------------------------------------------------------------------
# _capture_response — 多模态信封 vs JSON 串
# ---------------------------------------------------------------------------

class TestCaptureResponse:
    def test_image_non_ax_returns_multimodal_envelope(self):
        cap = CaptureResult(
            mode="som", width=100, height=50, png_b64="iVBORw0KGgo=",
            elements=[_elem(1)], png_bytes_len=999,
        )
        resp = cu_tool._capture_response(cap)
        assert resp["_multimodal"] is True
        assert resp["content"][0]["type"] == "text"
        assert resp["content"][1]["type"] == "image_url"
        assert resp["meta"] == {
            "mode": "som", "width": 100, "height": 50, "elements": 1, "png_bytes": 999,
        }
        assert resp["text_summary"] == resp["content"][0]["text"]

    def test_ax_mode_returns_json_string_even_with_image(self):
        cap = CaptureResult(mode="ax", width=10, height=10, png_b64="iVBOR=", elements=[_elem(1)])
        resp = cu_tool._capture_response(cap)
        assert isinstance(resp, str)
        assert json.loads(resp)["mode"] == "ax"

    def test_no_image_returns_json_string(self):
        cap = CaptureResult(mode="som", width=10, height=10, png_b64=None, elements=[_elem(1)])
        resp = cu_tool._capture_response(cap)
        assert isinstance(resp, str)
        assert json.loads(resp)["mode"] == "som"

    def test_vision_mode_image_is_multimodal(self):
        cap = CaptureResult(mode="vision", width=10, height=10, png_b64="iVBOR=")
        resp = cu_tool._capture_response(cap)
        assert resp["_multimodal"] is True
        assert resp["meta"]["mode"] == "vision"


class TestCaptureMimeSniffing:
    def test_jpeg_prefix_sniffed(self):
        cap = CaptureResult(mode="vision", width=10, height=10, png_b64="/9j/4AAQSkZJRg==")
        url = cu_tool._capture_response(cap)["content"][1]["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")

    def test_png_default_sniffed(self):
        cap = CaptureResult(mode="vision", width=10, height=10, png_b64="iVBORw0KGgo=")
        url = cu_tool._capture_response(cap)["content"][1]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")

    def test_explicit_mime_type_wins(self):
        cap = CaptureResult(
            mode="som", width=10, height=10, png_b64="abc", image_mime_type="image/webp",
        )
        url = cu_tool._capture_response(cap)["content"][1]["image_url"]["url"]
        assert url == "data:image/webp;base64,abc"


# ---------------------------------------------------------------------------
# _text_response / _maybe_follow_capture
# ---------------------------------------------------------------------------

class TestTextResponse:
    def test_full_shape(self):
        res = ActionResult(ok=True, action="click", message="clicked", meta={"x": 1})
        assert json.loads(cu_tool._text_response(res)) == {
            "ok": True, "action": "click", "message": "clicked", "meta": {"x": 1},
        }

    def test_omits_empty_message_and_meta(self):
        res = ActionResult(ok=True, action="wait")
        parsed = json.loads(cu_tool._text_response(res))
        assert parsed == {"ok": True, "action": "wait"}
        assert "message" not in parsed
        assert "meta" not in parsed


class TestMaybeFollowCapture:
    def test_capture_after_on_success_attaches_capture(self, noop_backend):
        res = ActionResult(ok=True, action="click", message="ok")
        parsed = json.loads(cu_tool._maybe_follow_capture(noop_backend, res, capture_after=True))
        assert "capture" in parsed
        assert parsed["capture"]["mode"] == "som"  # backend.capture() 默认模式

    def test_capture_after_skipped_on_failure(self, noop_backend):
        res = ActionResult(ok=False, action="click", message="element not found")
        parsed = json.loads(cu_tool._maybe_follow_capture(noop_backend, res, capture_after=True))
        assert "capture" not in parsed

    def test_no_capture_after_flag(self, noop_backend):
        res = ActionResult(ok=True, action="click")
        parsed = json.loads(cu_tool._maybe_follow_capture(noop_backend, res, capture_after=False))
        assert "capture" not in parsed

    def test_message_and_meta_preserved(self, noop_backend):
        res = ActionResult(ok=True, action="type", message="typed", meta={"n": 2})
        parsed = json.loads(cu_tool._maybe_follow_capture(noop_backend, res, capture_after=False))
        assert parsed["message"] == "typed"
        assert parsed["meta"] == {"n": 2}
