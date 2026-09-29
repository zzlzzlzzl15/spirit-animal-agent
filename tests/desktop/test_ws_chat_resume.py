"""ws_server cmd_chat 超时自动续跑测试。

用户要求：超时不能就算了——得自动重启上一步的任务、继续完成未完成的部分。
cmd_chat 的续跑循环：超时 → interrupt 当前轮 → 等旧循环落定 → 注入续跑指令
重进对话循环，直到完成或续跑轮数封顶。本组测试锁定：
1. 超时后自动续跑且第二轮完成任务（续跑指令正确注入、通知可见、chat_complete 仅一次）；
2. 关闭 auto_resume 时保持旧行为（直接返回超时错误）；
3. 续跑轮数封顶后返回超时错误（防无限续跑烧 token）。
"""

import asyncio
import time

import spirit.config as config_mod
from spirit.desktop.pet_engine import PetEngine
from spirit.desktop.ws_server import WSServer, _RESUME_CONTINUATION_PROMPT


# =========================================================================
# 替身
# =========================================================================

class _FakeClient:
    def __init__(self):
        self.events = []

    async def send_event(self, name, data):
        self.events.append((name, data))


class _Cfg:
    streaming_enabled = False


class _FakeAgent:
    """假 agent：首轮 sleep 超过 chat_timeout 模拟长任务被超时，后续轮直接给结论。"""

    def __init__(self, slow_seconds=0.4):
        self.config = _Cfg()
        self.on_tool_start = None
        self.on_tool_complete = None
        self.on_stream_delta = None
        self.interrupted = False
        self.calls = []
        self._slow = slow_seconds

    def clear_interrupt(self):
        self.interrupted = False

    def interrupt(self):
        self.interrupted = True

    def chat(self, message, cb=None):
        self.calls.append(message)
        if len(self.calls) == 1:
            time.sleep(self._slow)  # 超过 chat_timeout → 触发超时续跑
            return {"response": "[已中断]", "tool_calls": []}
        return {"response": "最终结论：任务完成。", "tool_calls": []}


class _AlwaysSlowAgent(_FakeAgent):
    """每轮都超时的假 agent（用于续跑封顶测试）。"""

    def chat(self, message, cb=None):
        self.calls.append(message)
        time.sleep(self._slow)
        return {"response": "[已中断]", "tool_calls": []}


# =========================================================================
# 辅助
# =========================================================================

def _patch_timeouts(monkeypatch, **overrides):
    """把 cmd_chat 读的配置项打桩：超时 0.15s 让续跑路径秒级触发。"""
    table = {
        "timeouts.chat_request": 0.15,
        "chat.auto_resume_on_timeout": True,
        "chat.max_auto_resumes": 2,
        "chat.resume_grace_seconds": 5,
    }
    table.update(overrides)

    def fake_get(key, default=None):
        return table.get(key, default)

    monkeypatch.setattr(config_mod, "get_config_value", fake_get)


def _chat_handler(agent):
    server = WSServer(PetEngine(), host="127.0.0.1", port=0, agent=agent)
    return server.commands.get("chat")


# =========================================================================
# 1. 超时 → 自动续跑 → 第二轮完成
# =========================================================================

def test_timeout_auto_resumes_and_completes(monkeypatch):
    _patch_timeouts(monkeypatch)
    agent = _FakeAgent()
    client = _FakeClient()

    result = asyncio.run(_chat_handler(agent)(client, {"message": "长任务"}))

    # 首轮超时后自动续跑，第二轮带着续跑指令完成任务
    assert len(agent.calls) == 2
    assert agent.calls[1] == _RESUME_CONTINUATION_PROMPT
    assert result.get("response") == "最终结论：任务完成。"
    assert "error" not in result
    # 续跑通知对前端可见（stream_delta 事件携带"自动续跑"）
    notices = [
        d for n, d in client.events
        if n == "stream_delta" and "自动续跑" in (d.get("text") or "")
    ]
    assert notices
    # 续跑前清中断标志，不污染后续轮
    assert agent.interrupted is False
    # chat_complete 只在最终收尾发一次
    assert sum(1 for n, _ in client.events if n == "chat_complete") == 1


# =========================================================================
# 2. 关闭自动续跑 → 保持旧行为
# =========================================================================

def test_auto_resume_disabled_returns_timeout_error(monkeypatch):
    _patch_timeouts(monkeypatch, **{"chat.auto_resume_on_timeout": False})
    agent = _FakeAgent()

    result = asyncio.run(_chat_handler(agent)(_FakeClient(), {"message": "长任务"}))

    assert len(agent.calls) == 1
    assert result.get("timeout") is True
    assert "超时" in result["error"]


# =========================================================================
# 3. 续跑封顶 → 返回超时错误（防无限续跑）
# =========================================================================

def test_resume_cap_exhausted_returns_error(monkeypatch):
    _patch_timeouts(monkeypatch, **{"chat.max_auto_resumes": 1})
    agent = _AlwaysSlowAgent()

    result = asyncio.run(_chat_handler(agent)(_FakeClient(), {"message": "长任务"}))

    # 首轮超时 → 续跑第 1 轮 → 仍超时 → 封顶放弃
    assert len(agent.calls) == 2
    assert result.get("timeout") is True
    assert "自动续跑" in result["error"]
