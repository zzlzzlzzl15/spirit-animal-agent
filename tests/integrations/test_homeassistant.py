"""Home Assistant 集成测试 —— 安全护栏 / 过滤 / 服务调用（FakeTransport）。"""

from .conftest import FakeTransport, json_response

from spirit.integrations.homeassistant import (
    HomeAssistantIntegration,
    is_blocked_domain,
    validate_entity_id,
    validate_service_name,
)


def _ha(env, responses=None, base_url=None):
    t = FakeTransport(responses)
    return HomeAssistantIntegration(transport=t, env=env, base_url=base_url), t


# --------------------------------------------------------------------------
# 纯函数护栏
# --------------------------------------------------------------------------

def test_validate_entity_id():
    assert validate_entity_id("light.living_room") is True
    assert validate_entity_id("sensor.temp_1") is True
    assert validate_entity_id("") is False
    assert validate_entity_id("light") is False           # 无 .name
    assert validate_entity_id("light..x") is False
    assert validate_entity_id("Light.x") is False          # 大写非法
    assert validate_entity_id("light.x; rm -rf") is False  # 注入


def test_validate_service_name():
    assert validate_service_name("turn_on") is True
    assert validate_service_name("light") is True
    assert validate_service_name("") is False
    assert validate_service_name("1abc") is False  # 不能数字开头
    assert validate_service_name("turn-on") is False


def test_is_blocked_domain():
    for blocked in ("shell_command", "python_script", "hassio", "pyscript",
                    "command_line", "rest_command"):
        assert is_blocked_domain(blocked) is True
    assert is_blocked_domain("light") is False
    assert is_blocked_domain("switch") is False


# --------------------------------------------------------------------------
# base_url 解析
# --------------------------------------------------------------------------

def test_base_url_override():
    integ, _ = _ha({"HASS_TOKEN": "t"}, base_url="http://override:1234/")
    assert integ.base_url == "http://override:1234"


def test_base_url_from_env():
    integ, _ = _ha({"HASS_TOKEN": "t", "HASS_URL": "http://env:8123/"})
    assert integ.base_url == "http://env:8123"


def test_base_url_default():
    integ, _ = _ha({"HASS_TOKEN": "t"})
    assert integ.base_url.endswith("8123") or "homeassistant.local" in integ.base_url


# --------------------------------------------------------------------------
# 未配置降级
# --------------------------------------------------------------------------

def test_not_configured_no_network():
    integ, t = _ha({})
    assert integ.list_entities().ok is False
    assert integ.get_state("light.x").ok is False
    assert integ.call_service("light", "turn_on").ok is False
    assert t.calls == []


# --------------------------------------------------------------------------
# list_entities + 过滤
# --------------------------------------------------------------------------

STATES = [
    {"entity_id": "light.kitchen", "state": "on",
     "attributes": {"friendly_name": "Kitchen Light"}},
    {"entity_id": "light.bedroom", "state": "off",
     "attributes": {"friendly_name": "Bedroom Lamp"}},
    {"entity_id": "sensor.temp", "state": "21",
     "attributes": {"friendly_name": "Kitchen Temp"}},
]


def test_list_entities_all():
    integ, _ = _ha({"HASS_TOKEN": "t"}, [json_response(STATES)])
    res = integ.list_entities()
    assert res.ok is True
    assert res.data["count"] == 3


def test_list_entities_filter_by_domain():
    integ, _ = _ha({"HASS_TOKEN": "t"}, [json_response(STATES)])
    res = integ.list_entities(domain="light")
    assert res.data["count"] == 2


def test_list_entities_filter_by_area():
    integ, _ = _ha({"HASS_TOKEN": "t"}, [json_response(STATES)])
    res = integ.list_entities(area="kitchen")
    ids = [e["entity_id"] for e in res.data["entities"]]
    assert "light.kitchen" in ids and "sensor.temp" in ids
    assert "light.bedroom" not in ids


def test_filter_states_is_pure():
    out = HomeAssistantIntegration._filter_states(STATES, "sensor", "")
    assert len(out) == 1 and out[0]["entity_id"] == "sensor.temp"


def test_list_entities_auth_header_and_url():
    integ, t = _ha({"HASS_TOKEN": "tk"}, [json_response([])], base_url="http://h:1")
    integ.list_entities()
    assert t.last_call["headers"]["Authorization"] == "Bearer tk"
    assert t.last_call["url"] == "http://h:1/api/states"


# --------------------------------------------------------------------------
# get_state
# --------------------------------------------------------------------------

def test_get_state_rejects_invalid_entity():
    integ, t = _ha({"HASS_TOKEN": "t"})
    res = integ.get_state("bad entity; rm")
    assert res.ok is False
    assert t.calls == []  # 校验失败不发请求


def test_get_state_ok():
    integ, t = _ha({"HASS_TOKEN": "t"}, [json_response({"entity_id": "light.x"})])
    res = integ.get_state("light.x")
    assert res.ok is True
    assert t.last_call["url"].endswith("/api/states/light.x")


# --------------------------------------------------------------------------
# call_service + 护栏
# --------------------------------------------------------------------------

def test_call_service_blocks_dangerous_domain():
    integ, t = _ha({"HASS_TOKEN": "t"})
    res = integ.call_service("shell_command", "reboot")
    assert res.ok is False
    assert "安全策略" in res.error
    assert t.calls == []  # 高危域绝不发请求


def test_call_service_rejects_bad_name():
    integ, _ = _ha({"HASS_TOKEN": "t"})
    assert integ.call_service("light", "turn-on").ok is False
    assert integ.call_service("1light", "turn_on").ok is False


def test_call_service_ok_with_entity_and_data():
    integ, t = _ha({"HASS_TOKEN": "t"}, [json_response([{"ok": 1}])])
    res = integ.call_service("light", "turn_on", entity_id="light.x",
                             data={"brightness": 128})
    assert res.ok is True
    call = t.last_call
    assert call["url"].endswith("/api/services/light/turn_on")
    assert call["json_body"] == {"brightness": 128, "entity_id": "light.x"}


def test_call_service_error_status():
    integ, _ = _ha({"HASS_TOKEN": "t"}, [json_response({}, status=500)])
    assert integ.call_service("light", "turn_on").ok is False
