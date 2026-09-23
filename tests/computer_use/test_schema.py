"""``spirit.computer_use.schema`` 单元测试（Phase 4.5）。

对标 Hermes ``tests/tools/test_computer_use.py::TestSchema``，但适配 Spirit 的 OpenAI
function-calling **包裹形式**（``{"type":"function","function":{...}}``）：验证 schema 形状、
动作枚举、定位参数、capture 模式枚举、max_elements 的 default/maximum 与运行时钳制常量一致，
且不含 Anthropic 原生 ``computer_20251124`` 类型（模型无关）。
"""

from __future__ import annotations

import json

from spirit.computer_use.schema import (
    COMPUTER_USE_ACTIONS,
    COMPUTER_USE_PARAMETERS,
    COMPUTER_USE_SCHEMA,
    get_computer_use_schema,
)


def _fn() -> dict:
    return COMPUTER_USE_SCHEMA["function"]


def _props() -> dict:
    return COMPUTER_USE_SCHEMA["function"]["parameters"]["properties"]


class TestSchemaShape:
    def test_wrapped_openai_function_format(self):
        assert COMPUTER_USE_SCHEMA["type"] == "function"
        assert _fn()["name"] == "computer_use"
        assert "parameters" in _fn()

    def test_parameters_object_with_required_action(self):
        params = _fn()["parameters"]
        assert params["type"] == "object"
        assert "action" in params["properties"]
        assert params["required"] == ["action"]

    def test_parameters_alias_matches_module_constant(self):
        assert COMPUTER_USE_PARAMETERS is _fn()["parameters"]

    def test_get_schema_returns_same_object(self):
        assert get_computer_use_schema() is COMPUTER_USE_SCHEMA

    def test_description_non_empty(self):
        assert _fn()["description"].strip()


class TestSchemaIsModelAgnostic:
    def test_no_anthropic_native_type(self):
        assert COMPUTER_USE_SCHEMA.get("type") != "computer_20251124"
        dumped = json.dumps(COMPUTER_USE_SCHEMA, ensure_ascii=False)
        assert "computer_20251124" not in dumped


class TestSchemaActions:
    def test_action_enum_lists_all_expected_actions(self):
        actions = set(_props()["action"]["enum"])
        assert actions >= {
            "capture", "click", "double_click", "right_click", "middle_click",
            "drag", "scroll", "type", "key", "wait", "list_apps", "list_windows",
            "focus_app",
        }

    def test_actions_tuple_matches_enum(self):
        assert set(COMPUTER_USE_ACTIONS) == set(_props()["action"]["enum"])

    def test_set_value_action_present(self):
        assert "set_value" in set(_props()["action"]["enum"])


class TestSchemaTargeting:
    def test_element_and_coordinate_targeting(self):
        props = _props()
        assert "element" in props
        assert "coordinate" in props
        assert props["element"]["type"] == "integer"
        assert props["coordinate"]["type"] == "array"

    def test_exact_capture_targeting_pid_window(self):
        props = _props()
        assert props["pid"]["type"] == "integer"
        assert props["window_id"]["type"] == "integer"

    def test_drag_endpoints_present(self):
        props = _props()
        for key in ("from_element", "to_element", "from_coordinate", "to_coordinate"):
            assert key in props

    def test_capture_mode_enum_som_vision_ax(self):
        modes = set(_props()["mode"]["enum"])
        assert modes == {"som", "vision", "ax"}


class TestSchemaMaxElements:
    def test_max_elements_is_integer_with_bounds(self):
        prop = _props()["max_elements"]
        assert prop["type"] == "integer"
        assert prop.get("minimum", 1) >= 1

    def test_max_elements_default_and_max_match_runtime_constants(self):
        """Schema 描述必须与运行时钳制一致（对标 Hermes 同款回归）。"""
        from spirit.computer_use.tool import (
            _DEFAULT_MAX_ELEMENTS,
            _MAX_ALLOWED_MAX_ELEMENTS,
        )
        prop = _props()["max_elements"]
        assert prop.get("default") == _DEFAULT_MAX_ELEMENTS
        assert prop.get("maximum") == _MAX_ALLOWED_MAX_ELEMENTS


class TestSchemaValueAndModifiers:
    def test_value_text_keys_seconds_present(self):
        props = _props()
        for key in ("value", "text", "keys", "seconds"):
            assert key in props

    def test_button_enum(self):
        assert set(_props()["button"]["enum"]) == {"left", "right", "middle"}

    def test_direction_enum(self):
        assert set(_props()["direction"]["enum"]) == {"up", "down", "left", "right"}

    def test_capture_after_and_raise_window_boolean(self):
        props = _props()
        assert props["capture_after"]["type"] == "boolean"
        assert props["raise_window"]["type"] == "boolean"
