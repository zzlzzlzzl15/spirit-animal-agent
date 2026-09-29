"""代理测试共享 fixture —— 隔离 env + fake 补全 seam。"""

import pytest

from spirit.proxy.config import ProxyConfig
from spirit.proxy.handler import ProxyHandler


@pytest.fixture(autouse=True)
def _clear_proxy_env(monkeypatch):
    """清除所有 SPIRIT_PROXY_* 环境变量，避免真实环境污染配置解析测试。"""
    for name in (
        "SPIRIT_PROXY_ENABLED", "SPIRIT_PROXY_HOST", "SPIRIT_PROXY_PORT",
        "SPIRIT_PROXY_KEY", "SPIRIT_PROXY_MODEL", "SPIRIT_PROXY_MODEL_OVERRIDE",
        "SPIRIT_PROXY_MAX_CONCURRENT",
    ):
        monkeypatch.delenv(name, raising=False)


def echo_completion(messages, *, model=None):
    """一个确定性 fake 补全：回显最后一条 user 文本。"""
    from spirit.proxy.completion import messages_to_user_prompt
    return f"echo:{messages_to_user_prompt(messages)}"


@pytest.fixture
def fake_completion():
    return echo_completion


@pytest.fixture
def config():
    """一个无鉴权、模型固定的测试配置。"""
    return ProxyConfig(enabled=True, host="127.0.0.1", port=0, model="spirit-agent")


@pytest.fixture
def auth_config():
    """一个要求 Bearer key 的测试配置。"""
    return ProxyConfig(
        enabled=True, host="127.0.0.1", port=0,
        model="spirit-agent", api_key="secret-key",
    )


@pytest.fixture
def handler(config, fake_completion):
    return ProxyHandler(config=config, completion_fn=fake_completion)


@pytest.fixture
def auth_handler(auth_config, fake_completion):
    return ProxyHandler(config=auth_config, completion_fn=fake_completion)
