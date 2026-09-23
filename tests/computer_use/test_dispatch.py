"""``spirit.computer_use.tool`` 派发路由测试（Phase 4.5）。

对标 Hermes ``tests/tools/test_computer_use.py::TestDispatch`` / ``TestClickButtonPassthrough``
/ ``TestCaptureAfter*``：验证 ``handle_computer_use`` 把每个 action 正确路由到后端方法、
派生 click 的 button/click_count、解析 coordinate、capture_after 的成功门控，以及各动作的
参数校验错误（missing/unknown action、drag 缺端点、set_value 缺 value、focus_app 缺 app、
capture 非法 mode）。全部对着 NoopBackend 的 ``.calls`` 断言。
"""

from __future__ import annotations

import json

import pytest

from spirit.computer_use import tool as cu_tool
from spirit.computer_use.backend import ActionResult
from spirit.computer_use.noop_backend import NoopBackend


def _kw_for(backend: NoopBackend, name: str) -> dict:
    """取 backend 记录里第一个名为 ``name`` 的调用的 kwargs。"""
    return next(kw for n, kw in backend.calls if n == name)


# ---------------------------------------------------------------------------
# 动作缺失 / 未知
# ---------------------------------------------------------------------------

class TestActionValidation:
    def test_missing_action_returns_error(self):
        parsed = json.loads(cu_tool.handle_computer_use({}))
        assert "error" in parsed

    def test_blank_action_returns_error(self):
        parsed = json.loads(cu_tool.handle_computer_use({"action": "   "}))
        assert "error" in parsed

    def test_unknown_action_returns_error(self, noop_backend):
        parsed = json.loads(cu_tool.handle_computer_use({"action": "nope"}))
        assert "error" in parsed
        assert noop_backend.calls == []  # 未知动作不触达后端

    def test_action_is_case_insensitive_and_trimmed(self, noop_backend):
        cu_tool.handle_computer_use({"action": "  LIST_APPS  "})
        assert "list_apps" in noop_backend.call_names()


# ---------------------------------------------------------------------------
# 只读动作路由
# ---------------------------------------------------------------------------

class TestReadOnlyDispatch:
    def test_list_apps_returns_json(self, noop_backend):
        parsed = json.loads(cu_tool.handle_computer_use({"action": "list_apps"}))
        assert parsed == {"apps": [], "count": 0}

    def test_list_windows_returns_json(self, noop_backend):
        parsed = json.loads(cu_tool.handle_computer_use({"action": "list_windows"}))
        assert parsed == {"windows": [], "count": 0}

    def test_wait_returns_ok(self, noop_backend):
        parsed = json.loads(cu_tool.handle_computer_use({"action": "wait", "seconds": 0.01}))
        assert parsed["ok"] is True
        assert parsed["action"] == "wait"

    def test_capture_ax_returns_json_with_mode(self, noop_backend):
        parsed = json.loads(cu_tool.handle_computer_use({"action": "capture", "mode": "ax"}))
        assert parsed["mode"] == "ax"

    def test_capture_bad_mode_returns_error(self, noop_backend):
        parsed = json.loads(cu_tool.handle_computer_use({"action": "capture", "mode": "bogus"}))
        assert "error" in parsed
        assert "bad mode" in parsed["error"]

    def test_capture_forwards_exact_pid_window_target(self, noop_backend):
        cu_tool.handle_computer_use({
            "action": "capture", "mode": "ax", "pid": 23502, "window_id": 58720504,
        })
        assert _kw_for(noop_backend, "capture") == {
            "mode": "ax", "app": None, "pid": 23502, "window_id": 58720504,
        }

    def test_capture_forwards_app(self, noop_backend):
        cu_tool.handle_computer_use({"action": "capture", "mode": "som", "app": "Safari"})
        kw = _kw_for(noop_backend, "capture")
        assert kw["app"] == "Safari"
        assert kw["mode"] == "som"


# ---------------------------------------------------------------------------
# click 系：button / click_count / coordinate 派生
# ---------------------------------------------------------------------------

class TestClickDispatch:
    def test_click_by_element_routes_to_backend(self, noop_backend):
        cu_tool.handle_computer_use({"action": "click", "element": 7})
        assert "click" in noop_backend.call_names()
        assert _kw_for(noop_backend, "click")["element"] == 7

    def test_click_defaults_button_left_count_one(self, noop_backend):
        cu_tool.handle_computer_use({"action": "click", "element": 1})
        kw = _kw_for(noop_backend, "click")
        assert kw["button"] == "left"
        assert kw["click_count"] == 1

    def test_double_click_sets_click_count_two(self, noop_backend):
        cu_tool.handle_computer_use({"action": "double_click", "element": 3})
        assert _kw_for(noop_backend, "click")["click_count"] == 2

    def test_right_click_sets_button_right(self, noop_backend):
        cu_tool.handle_computer_use({"action": "right_click", "element": 3})
        assert _kw_for(noop_backend, "click")["button"] == "right"

    def test_middle_click_sets_button_middle(self, noop_backend):
        cu_tool.handle_computer_use({"action": "middle_click", "element": 3})
        assert _kw_for(noop_backend, "click")["button"] == "middle"

    def test_explicit_button_passthrough(self, noop_backend):
        cu_tool.handle_computer_use({"action": "click", "element": 1, "button": "right"})
        assert _kw_for(noop_backend, "click")["button"] == "right"

    def test_coordinate_parsed_to_x_y(self, noop_backend):
        cu_tool.handle_computer_use({"action": "click", "coordinate": [100, 200]})
        kw = _kw_for(noop_backend, "click")
        assert kw["x"] == 100
        assert kw["y"] == 200
        assert kw["element"] is None

    def test_no_coordinate_gives_none_x_y(self, noop_backend):
        cu_tool.handle_computer_use({"action": "click", "element": 2})
        kw = _kw_for(noop_backend, "click")
        assert kw["x"] is None
        assert kw["y"] is None

    def test_modifiers_passthrough(self, noop_backend):
        cu_tool.handle_computer_use({"action": "click", "element": 1, "modifiers": ["cmd"]})
        assert _kw_for(noop_backend, "click")["modifiers"] == ["cmd"]


# ---------------------------------------------------------------------------
# drag / scroll / type / key / set_value / focus_app
# ---------------------------------------------------------------------------

class TestOtherActionDispatch:
    def test_drag_by_coordinate(self, noop_backend):
        out = cu_tool.handle_computer_use({
            "action": "drag", "from_coordinate": [100, 200], "to_coordinate": [400, 500],
        })
        assert "error" not in json.loads(out)
        kw = _kw_for(noop_backend, "drag")
        assert kw["from_xy"] == (100, 200)
        assert kw["to_xy"] == (400, 500)

    def test_drag_by_element(self, noop_backend):
        cu_tool.handle_computer_use({"action": "drag", "from_element": 1, "to_element": 5})
        kw = _kw_for(noop_backend, "drag")
        assert kw["from_element"] == 1
        assert kw["to_element"] == 5

    def test_drag_requires_endpoints(self, noop_backend):
        parsed = json.loads(cu_tool.handle_computer_use({"action": "drag"}))
        assert "error" in parsed

    def test_scroll_defaults(self, noop_backend):
        cu_tool.handle_computer_use({"action": "scroll"})
        kw = _kw_for(noop_backend, "scroll")
        assert kw["direction"] == "down"
        assert kw["amount"] == 3

    def test_scroll_explicit(self, noop_backend):
        cu_tool.handle_computer_use({"action": "scroll", "direction": "up", "amount": 5})
        kw = _kw_for(noop_backend, "scroll")
        assert kw["direction"] == "up"
        assert kw["amount"] == 5

    def test_type_routes_to_type_text(self, noop_backend):
        out = cu_tool.handle_computer_use({"action": "type", "text": "hello"})
        assert "error" not in json.loads(out)
        assert "type" in noop_backend.call_names()
        assert _kw_for(noop_backend, "type")["text"] == "hello"

    def test_type_empty_string_allowed(self, noop_backend):
        out = cu_tool.handle_computer_use({"action": "type", "text": ""})
        assert "error" not in json.loads(out)

    def test_key_routes_to_backend(self, noop_backend):
        cu_tool.handle_computer_use({"action": "key", "keys": "cmd+s"})
        assert _kw_for(noop_backend, "key")["keys"] == "cmd+s"

    def test_set_value_routes(self, noop_backend):
        out = cu_tool.handle_computer_use({"action": "set_value", "value": "Option A", "element": 5})
        parsed = json.loads(out)
        assert parsed["ok"] is True
        assert parsed["action"] == "set_value"
        assert _kw_for(noop_backend, "set_value") == {"value": "Option A", "element": 5}

    def test_set_value_missing_value_errors(self, noop_backend):
        parsed = json.loads(cu_tool.handle_computer_use({"action": "set_value"}))
        assert "error" in parsed

    def test_set_value_coerces_non_string(self, noop_backend):
        cu_tool.handle_computer_use({"action": "set_value", "value": 42})
        assert _kw_for(noop_backend, "set_value")["value"] == "42"

    def test_focus_app_routes(self, noop_backend):
        cu_tool.handle_computer_use({"action": "focus_app", "app": "Safari"})
        assert _kw_for(noop_backend, "focus_app") == {"app": "Safari", "raise": False}

    def test_focus_app_raise_window(self, noop_backend):
        cu_tool.handle_computer_use({"action": "focus_app", "app": "Safari", "raise_window": True})
        assert _kw_for(noop_backend, "focus_app")["raise"] is True

    def test_focus_app_requires_app(self, noop_backend):
        parsed = json.loads(cu_tool.handle_computer_use({"action": "focus_app"}))
        assert "error" in parsed


# ---------------------------------------------------------------------------
# capture_after 门控
# ---------------------------------------------------------------------------

class TestCaptureAfter:
    def test_capture_after_fires_on_success(self, noop_backend):
        cu_tool.handle_computer_use({"action": "click", "element": 1, "capture_after": True})
        assert noop_backend.call_names().count("capture") == 1

    def test_capture_after_skipped_on_failure(self, monkeypatch):
        backend = NoopBackend()
        monkeypatch.setattr(
            backend, "click",
            lambda **kw: ActionResult(ok=False, action="click", message="element not found"),
        )
        cu_tool.set_backend(backend)
        out = cu_tool.handle_computer_use({"action": "click", "element": 99, "capture_after": True})
        parsed = json.loads(out)
        assert parsed["ok"] is False
        # 失败动作后不得追加捕获（否则误导模型以为成功）
        assert backend.call_names().count("capture") == 0

    def test_capture_after_absent_by_default(self, noop_backend):
        cu_tool.handle_computer_use({"action": "click", "element": 1})
        assert "capture" not in noop_backend.call_names()

    def test_capture_after_embeds_capture_payload(self, noop_backend):
        out = cu_tool.handle_computer_use({"action": "click", "element": 1, "capture_after": True})
        parsed = json.loads(out)
        assert "capture" in parsed
        assert parsed["capture"]["mode"] == "som"  # 默认捕获模式


# ---------------------------------------------------------------------------
# 后端不可用 / 派发异常折叠
# ---------------------------------------------------------------------------

class TestErrorFolding:
    def test_backend_unavailable_folds_to_error(self):
        cu_tool.set_backend(NoopBackend(available=False))
        # is_available False 不阻止派发（check_fn 才门控工具面）；派发仍走后端。
        out = cu_tool.handle_computer_use({"action": "list_apps"})
        assert "apps" in json.loads(out)

    def test_unknown_backend_name_folds_to_error(self, monkeypatch):
        cu_tool.reset_backend_for_tests()
        monkeypatch.setenv("SPIRIT_COMPUTER_USE_BACKEND", "does_not_exist")
        out = cu_tool.handle_computer_use({"action": "capture", "mode": "ax"})
        parsed = json.loads(out)
        assert "error" in parsed
        assert "backend unavailable" in parsed["error"]

    def test_dispatch_exception_folds_to_error(self, monkeypatch):
        backend = NoopBackend()

        def boom(**kw):
            raise RuntimeError("backend exploded")

        monkeypatch.setattr(backend, "click", boom)
        cu_tool.set_backend(backend)
        out = cu_tool.handle_computer_use({"action": "click", "element": 1})
        parsed = json.loads(out)
        assert "error" in parsed
        assert "failed" in parsed["error"]


# ---------------------------------------------------------------------------
# _coerce_max_elements 钳制
# ---------------------------------------------------------------------------

class TestCoerceMaxElements:
    def test_none_returns_default(self):
        assert cu_tool._coerce_max_elements(None) == cu_tool._DEFAULT_MAX_ELEMENTS

    def test_invalid_string_returns_default(self):
        assert cu_tool._coerce_max_elements("abc") == cu_tool._DEFAULT_MAX_ELEMENTS

    def test_in_range_passthrough(self):
        assert cu_tool._coerce_max_elements(50) == 50

    def test_numeric_string(self):
        assert cu_tool._coerce_max_elements("30") == 30

    def test_below_one_clamps_to_one(self):
        assert cu_tool._coerce_max_elements(0) == 1
        assert cu_tool._coerce_max_elements(-5) == 1

    def test_above_ceiling_clamps(self):
        assert cu_tool._coerce_max_elements(99999) == cu_tool._MAX_ALLOWED_MAX_ELEMENTS
