"""Phase 6.A 自进化子系统测试隔离与打桩工具。

沿用全仓 Spirit 子系统约定：
- 文件系统隔离：monkeypatch ``config.SPIRIT_HOME`` 到 tmp_path（对标 profile/moa）。
- LLM 打桩：``FakeCaller`` 复用 ``goals.judge.LLMCaller`` 签名
  ``(messages, temperature, max_tokens, timeout) -> str``。
"""

import json

import pytest

from spirit import config
from spirit.evolution import reset_memory


class FakeCaller:
    """可注入的假 llm_caller：回放固定回复或回复队列，并记录调用。"""

    def __init__(self, response="", responses=None):
        self.response = response
        self.responses = list(responses) if responses else None
        self.calls = []

    def __call__(self, messages, temperature, max_tokens, timeout):
        self.calls.append({
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": timeout,
        })
        if self.responses:
            return self.responses.pop(0)
        return self.response

    @property
    def last_call(self):
        return self.calls[-1] if self.calls else None


class RaisingCaller:
    """总是抛异常的 caller（验证 Role.invoke 的 fail-soft 捕获）。"""

    def __call__(self, messages, temperature, max_tokens, timeout):
        raise RuntimeError("llm boom")


def json_response(**kwargs):
    """把关键字参数序列化为 JSON 字符串（模拟角色结构化输出）。"""
    return json.dumps(kwargs, ensure_ascii=False)


@pytest.fixture
def evo_home(tmp_path, monkeypatch):
    """把 SPIRIT_HOME 指到 tmp_path，并重置记忆单例。"""
    monkeypatch.setattr(config, "SPIRIT_HOME", tmp_path, raising=False)
    reset_memory()
    yield tmp_path
    reset_memory()
