"""``spirit.computer_use.backend`` + ``noop_backend`` 单元测试（Phase 4.5）。

对标 Hermes ``tests/tools/test_computer_use.py::TestSessionLifecycle`` /
``TestElementTokenAttachment`` 的抽象层子集：验证 dataclass 形状（UIElement.center /
默认值 / 不共享可变默认）、ABC 的具体默认方法（list_windows / wait 钳制），以及
NoopBackend 作为一等测试 seam 的调用记录、生命周期与脚本化返回。
"""

from __future__ import annotations

import time

import pytest

from spirit.computer_use.backend import (
    ActionResult,
    CaptureResult,
    ComputerUseBackend,
    UIElement,
)
from spirit.computer_use.noop_backend import NoopBackend


# ---------------------------------------------------------------------------
# UIElement
# ---------------------------------------------------------------------------

class TestUIElement:
    def test_center_computes_midpoint_of_bounds(self):
        el = UIElement(index=1, role="AXButton", bounds=(10, 20, 30, 40))
        # x + w//2, y + h//2 = 10+15, 20+20
        assert el.center() == (25, 40)

    def test_center_with_zero_bounds(self):
        el = UIElement(index=1, role="AXButton")
        assert el.center() == (0, 0)

    def test_defaults(self):
        el = UIElement(index=3, role="AXTextField")
        assert el.label == ""
        assert el.bounds == (0, 0, 0, 0)
        assert el.app == ""
        assert el.pid == 0
        assert el.window_id == 0
        assert el.element_token is None

    def test_attributes_default_factory_not_shared(self):
        a = UIElement(index=1, role="AXButton")
        b = UIElement(index=2, role="AXButton")
        a.attributes["k"] = "v"
        assert b.attributes == {}
        assert a.attributes is not b.attributes

    def test_element_token_roundtrip(self):
        el = UIElement(index=1, role="AXButton", element_token="tok-abc")
        assert el.element_token == "tok-abc"


# ---------------------------------------------------------------------------
# CaptureResult / ActionResult
# ---------------------------------------------------------------------------

class TestResultDataclasses:
    def test_capture_result_defaults(self):
        cap = CaptureResult(mode="ax", width=800, height=600)
        assert cap.png_b64 is None
        assert cap.elements == []
        assert cap.app == ""
        assert cap.window_title == ""
        assert cap.png_bytes_len == 0
        assert cap.image_mime_type is None

    def test_capture_result_elements_not_shared(self):
        a = CaptureResult(mode="som", width=1, height=1)
        b = CaptureResult(mode="som", width=1, height=1)
        a.elements.append(UIElement(index=1, role="AXButton"))
        assert b.elements == []

    def test_action_result_defaults(self):
        res = ActionResult(ok=True, action="click")
        assert res.message == ""
        assert res.capture is None
        assert res.meta == {}

    def test_action_result_meta_not_shared(self):
        a = ActionResult(ok=True, action="click")
        b = ActionResult(ok=True, action="click")
        a.meta["x"] = 1
        assert b.meta == {}


# ---------------------------------------------------------------------------
# ComputerUseBackend ABC
# ---------------------------------------------------------------------------

class TestBackendABC:
    def test_cannot_instantiate_abstract_base(self):
        with pytest.raises(TypeError):
            ComputerUseBackend()  # type: abstract

    def test_noop_is_subclass(self):
        assert issubclass(NoopBackend, ComputerUseBackend)

    def test_list_windows_abc_default_empty(self):
        # ABC 的 list_windows 默认返回 []（早于窗口发现能力的后端仍可实例化）。
        # NoopBackend 覆盖了它，故直接调未绑定的 ABC 方法验证默认本身。
        assert ComputerUseBackend.list_windows(NoopBackend()) == []

    def test_wait_clamps_to_config_max(self, monkeypatch):
        slept = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
        backend = NoopBackend()
        backend.wait(1000)  # 远超上限
        assert slept == [30.0]  # computer_use.wait_max_seconds 默认 30

    def test_wait_clamps_negative_to_zero(self, monkeypatch):
        slept = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
        backend = NoopBackend()
        backend.wait(-5)
        assert slept == [0.0]

    def test_wait_passes_through_in_range(self, monkeypatch):
        slept = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
        backend = NoopBackend()
        res = backend.wait(2.5)
        assert slept == [2.5]
        assert res.ok is True
        assert res.action == "wait"


# ---------------------------------------------------------------------------
# NoopBackend — 一等测试 seam
# ---------------------------------------------------------------------------

class TestNoopBackend:
    def test_lifecycle_start_stop(self):
        backend = NoopBackend()
        assert backend.started is False
        backend.start()
        assert backend.started is True
        backend.stop()
        assert backend.started is False

    def test_is_available_default_true(self):
        assert NoopBackend().is_available() is True

    def test_is_available_false_when_scripted(self):
        assert NoopBackend(available=False).is_available() is False

    def test_records_calls_and_call_names(self):
        backend = NoopBackend()
        backend.click(element=1)
        backend.type_text("hi")
        assert backend.call_names() == ["click", "type"]

    def test_last_call_none_when_empty(self):
        assert NoopBackend().last_call() is None

    def test_last_call_returns_name_and_kwargs(self):
        backend = NoopBackend()
        backend.type_text("hello")
        name, kw = backend.last_call()
        assert name == "type"
        assert kw == {"text": "hello"}

    def test_capture_records_and_returns_shape(self):
        backend = NoopBackend(width=640, height=480)
        cap = backend.capture(mode="ax", app="Safari")
        name, kw = backend.last_call()
        assert name == "capture"
        assert kw == {"mode": "ax", "app": "Safari", "pid": None, "window_id": None}
        assert cap.mode == "ax"
        assert cap.width == 640
        assert cap.height == 480
        assert cap.png_b64 is None  # noop 恒无像素
        assert cap.app == "Safari"

    def test_capture_vision_mode_returns_no_elements(self):
        elements = [UIElement(index=1, role="AXButton")]
        backend = NoopBackend(elements=elements)
        cap = backend.capture(mode="vision")
        assert cap.elements == []

    def test_capture_som_mode_returns_scripted_elements(self):
        elements = [UIElement(index=1, role="AXButton"), UIElement(index=2, role="AXLink")]
        backend = NoopBackend(elements=elements)
        cap = backend.capture(mode="som")
        assert len(cap.elements) == 2
        assert cap.elements[0].index == 1

    def test_capture_elements_are_copy_not_alias(self):
        elements = [UIElement(index=1, role="AXButton")]
        backend = NoopBackend(elements=elements)
        cap = backend.capture(mode="som")
        cap.elements.append(UIElement(index=2, role="AXLink"))
        # 再次捕获仍返回脚本的原始 1 个元素（未被上次返回的 list 污染）
        cap2 = backend.capture(mode="som")
        assert len(cap2.elements) == 1

    def test_list_apps_returns_scripted(self):
        apps = [{"name": "Safari", "pid": 100}]
        backend = NoopBackend(apps=apps)
        assert backend.list_apps() == apps
        assert backend.call_names() == ["list_apps"]

    def test_list_windows_returns_scripted(self):
        windows = [{"id": 1, "title": "Win"}]
        backend = NoopBackend(windows=windows)
        assert backend.list_windows() == windows

    def test_pointer_actions_return_ok_and_record(self):
        backend = NoopBackend()
        assert backend.click(element=5).ok is True
        assert backend.drag(from_element=1, to_element=2).ok is True
        assert backend.scroll(direction="down", amount=3).ok is True
        assert backend.call_names() == ["click", "drag", "scroll"]

    def test_click_message_includes_element(self):
        backend = NoopBackend()
        res = backend.click(element=7)
        assert res.action == "click"
        assert "7" in res.message

    def test_keyboard_actions_record(self):
        backend = NoopBackend()
        assert backend.type_text("abc").ok is True
        assert backend.key("cmd+s").ok is True
        assert backend.call_names() == ["type", "key"]
        assert backend.calls[0][1] == {"text": "abc"}
        assert backend.calls[1][1] == {"keys": "cmd+s"}

    def test_focus_app_records_raise_flag(self):
        backend = NoopBackend()
        assert backend.focus_app("Safari", raise_window=True).ok is True
        assert backend.last_call()[1] == {"app": "Safari", "raise": True}

    def test_set_value_records_value_and_element(self):
        backend = NoopBackend()
        assert backend.set_value("Blue", element=3).ok is True
        assert backend.last_call()[1] == {"value": "Blue", "element": 3}
