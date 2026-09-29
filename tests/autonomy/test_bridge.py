"""DesktopBridge 桌宠 WS 桥接测试 —— Spirit Autonomy Phase 7.C。

聚焦离线可测路径：无通道 fail-open、假发送器收集广播、答复回收唤醒 ask、
WS 命令 handler（answer/status/start/stop）、attach 注册。
"""

import asyncio
import threading
from types import SimpleNamespace

from spirit.autonomy.bridge import (
    CMD_ANSWER,
    CMD_START,
    CMD_STATUS,
    CMD_STOP,
    DesktopBridge,
    EVENT_DECISION,
)
from spirit.evolution.hitl import HumanQuery


class FakeSender:
    def __init__(self):
        self.events = []

    def __call__(self, event_type, data):
        self.events.append((event_type, data))


class FakeController:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def state(self):
        return {"cycles": []}


def _query():
    return HumanQuery(question="继续？", options=["a", "b"], is_fork=True)


def test_ask_no_channel_fail_open():
    bridge = DesktopBridge(timeout=0.1)
    assert bridge.ask(_query()) is None


def test_ask_broadcasts_decision():
    sender = FakeSender()
    bridge = DesktopBridge(send=sender, timeout=0.1)
    bridge.ask(_query())
    assert len(sender.events) == 1
    et, data = sender.events[0]
    assert et == EVENT_DECISION
    assert data["question"] == "继续？"
    assert data["options"] == ["a", "b"]


def test_ask_timeout_returns_none():
    sender = FakeSender()
    bridge = DesktopBridge(send=sender, timeout=0.1)
    assert bridge.ask(_query()) is None


def test_submit_answer_wakes_ask():
    sender = FakeSender()
    bridge = DesktopBridge(send=sender, timeout=2.0)
    result = {}

    def worker():
        result["text"] = bridge.ask(_query())

    t = threading.Thread(target=worker)
    t.start()
    # 等广播发出后回发答复
    for _ in range(50):
        if sender.events:
            break
        threading.Event().wait(0.01)
    bridge.submit_answer("a")
    t.join(timeout=3.0)
    assert result.get("text") == "a"


def test_submit_answer_empty_rejected():
    bridge = DesktopBridge(timeout=0.1)
    assert bridge.submit_answer("   ") is False


def test_handlers_answer_and_status():
    ctrl = FakeController()
    bridge = DesktopBridge(timeout=0.1, controller=ctrl)
    handlers = bridge.handlers()
    assert set(handlers) >= {CMD_ANSWER, CMD_STATUS, CMD_START, CMD_STOP}

    loop = asyncio.new_event_loop()
    try:
        ok = loop.run_until_complete(handlers[CMD_ANSWER](None, {"text": "b"}))
        assert ok == {"ok": True}
        st = loop.run_until_complete(handlers[CMD_STATUS](None, {}))
        assert st == {"state": {"cycles": []}}
        s = loop.run_until_complete(handlers[CMD_START](None, {}))
        assert s == {"ok": True}
        p = loop.run_until_complete(handlers[CMD_STOP](None, {}))
        assert p == {"ok": True}
    finally:
        loop.close()
    assert ctrl.started == 1
    assert ctrl.stopped == 1


def test_attach_registers_commands():
    class FakeRegistry:
        def __init__(self):
            self.registered = {}

        def register(self, action, handler):
            self.registered[action] = handler

    class FakeServer:
        def __init__(self):
            self.commands = FakeRegistry()

    bridge = DesktopBridge(timeout=0.1)
    server = FakeServer()
    bridge.attach(server)
    assert set(server.commands.registered) >= {CMD_ANSWER, CMD_STATUS, CMD_START, CMD_STOP}
