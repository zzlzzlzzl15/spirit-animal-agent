"""ProxyConfig 解析测试 —— env > config.yaml > 默认。"""

from spirit.proxy import config as cfg_mod


def test_defaults(monkeypatch):
    cfg = cfg_mod.load_proxy_config()
    assert cfg.enabled is False
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8642
    assert cfg.model == "spirit-agent"
    assert cfg.requires_auth() is False
    assert cfg.is_local_only() is True


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("SPIRIT_PROXY_ENABLED", "true")
    monkeypatch.setenv("SPIRIT_PROXY_HOST", "0.0.0.0")
    monkeypatch.setenv("SPIRIT_PROXY_PORT", "9999")
    monkeypatch.setenv("SPIRIT_PROXY_KEY", "abc")
    monkeypatch.setenv("SPIRIT_PROXY_MODEL", "custom-model")
    cfg = cfg_mod.load_proxy_config()
    assert cfg.enabled is True
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9999
    assert cfg.api_key == "abc"
    assert cfg.model == "custom-model"
    assert cfg.requires_auth() is True
    assert cfg.is_local_only() is False


def test_bool_env_variants(monkeypatch):
    for truthy in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("SPIRIT_PROXY_ENABLED", truthy)
        assert cfg_mod.load_proxy_config().enabled is True
    for falsy in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("SPIRIT_PROXY_ENABLED", falsy)
        assert cfg_mod.load_proxy_config().enabled is False


def test_bad_port_falls_back(monkeypatch):
    monkeypatch.setenv("SPIRIT_PROXY_PORT", "not-a-number")
    cfg = cfg_mod.load_proxy_config()
    assert cfg.port == 8642


def test_model_override_flag(monkeypatch):
    monkeypatch.setenv("SPIRIT_PROXY_MODEL_OVERRIDE", "true")
    assert cfg_mod.load_proxy_config().allow_model_override is True
