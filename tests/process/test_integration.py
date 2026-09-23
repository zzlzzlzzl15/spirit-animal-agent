"""4.7 三处接线的联动集成测试。

单测（registry/notifications/watch/process_tool）已覆盖各构件的内部逻辑，
本文件专测**跨模块接线**——即 Phase 4.7 消除的三处硬缺口是否真的接通：

1. goals wait 屏障：``judge._session_waiting`` / ``gather_background_processes``
   经真实注册表（而非旧的 ``return False`` 桩）。
2. terminal 后台派生：``terminal_tool._spawn_background(background=True)``
   真的把命令交给注册表并回传 session_id。
3. conversation_loop 通知回灌：``_inject_process_notifications`` 从完成队列
   drain 出事件并以 ``add_message("user", text)`` 注入对话。

三处都在函数体内 ``from spirit.process import process_registry``，故用
``patch("spirit.process.process_registry", reg)`` 换掉模块级单例即可隔离。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from spirit.process.registry import ProcessRegistry

from .conftest import _make_session, _wait_until


@pytest.fixture()
def wired_registry():
    """把 spirit.process.process_registry 单例换成受控实例（三处接线共用）。"""
    reg = ProcessRegistry(rehydrate_delegations=False)
    with patch("spirit.process.process_registry", reg):
        yield reg


def _python() -> str:
    return sys.executable


# =========================================================================
# 接线 1：goals wait 屏障
# =========================================================================

class TestGoalsBarrier:
    def test_session_waiting_true_for_running_watch(self, wired_registry):
        """运行中且 watch 未命中的会话 → 屏障应继续泊车（True）。"""
        from spirit.goals import judge

        s = _make_session(sid="g1", task_id="t1", watch_patterns=["ready"], pid=111)
        wired_registry._running[s.id] = s
        assert judge._session_waiting("g1") is True

    def test_session_waiting_false_after_watch_hit(self, wired_registry):
        """watch 命中一次后 → 屏障解锁（False），即便进程仍在运行。"""
        from spirit.goals import judge

        s = _make_session(sid="g2", task_id="t1", watch_patterns=["ready"], pid=222)
        wired_registry._running[s.id] = s
        wired_registry._check_watch_patterns(s, "server ready now")  # 命中
        assert s._watch_hits > 0
        assert judge._session_waiting("g2") is False

    def test_session_waiting_false_when_exited(self, wired_registry):
        from spirit.goals import judge

        s = _make_session(sid="g3", task_id="t1", exited=True, exit_code=0)
        wired_registry._finished[s.id] = s
        assert judge._session_waiting("g3") is False

    def test_session_waiting_empty_and_unknown(self, wired_registry):
        from spirit.goals import judge

        assert judge._session_waiting("") is False
        assert judge._session_waiting("does-not-exist") is False

    def test_gather_background_processes_filters_by_task(self, wired_registry):
        from spirit.goals import judge

        a = _make_session(sid="ga", task_id="tA", pid=1, command="npm run dev")
        b = _make_session(sid="gb", task_id="tB", pid=2, command="pytest")
        wired_registry._running[a.id] = a
        wired_registry._running[b.id] = b

        rows = judge.gather_background_processes(task_id="tA")
        ids = {r["session_id"] for r in rows}
        assert ids == {"ga"}
        assert rows[0]["status"] == "running"
        assert rows[0]["pid"] == 1

    def test_gather_background_processes_empty_registry(self, wired_registry):
        from spirit.goals import judge

        assert judge.gather_background_processes(task_id="nope") == []

    def test_render_background_block_consumes_gathered(self, wired_registry):
        """gather → render 链路：运行中进程被渲染进 judge prompt 块。"""
        from spirit.goals import judge

        s = _make_session(
            sid="gr", task_id="t1", pid=99, command="http.server",
            watch_patterns=["Serving"],
        )
        wired_registry._running[s.id] = s
        rows = judge.gather_background_processes(task_id="t1")
        block = judge._render_background_block(rows)
        assert "pid 99" in block
        assert "http.server" in block
        assert "watch_patterns" in block


# =========================================================================
# 接线 2：terminal 后台派生
# =========================================================================

class TestTerminalBackgroundSpawn:
    def test_spawn_background_registers_and_returns_session(self, wired_registry, tmp_path):
        """background=True 真的派生进程并回传可跟进的 session_id。"""
        from spirit.tools import terminal_tool

        script = tmp_path / "bg.py"
        script.write_text("import time\nprint('started', flush=True)\ntime.sleep(5)\n",
                          encoding="utf-8")

        with patch("spirit.tools.approval.get_current_session_key", return_value="sk1"):
            raw = terminal_tool._spawn_background(
                f'"{_python()}" bg.py', Path(str(tmp_path)),
                notify_on_complete=True,
            )
        payload = json.loads(raw)
        assert payload["background"] is True
        assert payload["status"] == "running"
        sid = payload["session_id"]
        assert sid in wired_registry._running

        # 后台进程真的在跑并产出输出
        assert _wait_until(
            lambda: "started" in wired_registry.poll(sid).get("output_preview", ""), 10
        )
        wired_registry.kill_process(sid)  # 清理

    def test_handle_terminal_background_flag_routes_to_spawn(self, wired_registry, tmp_path):
        """_handle_terminal(background=True) 走后台派生分支（不阻塞等输出）。"""
        from spirit.tools import terminal_tool

        script = tmp_path / "bg2.py"
        script.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")

        with patch("spirit.tools.approval.get_current_session_key", return_value="sk2"), \
             patch.object(terminal_tool, "request_command_approval",
                          return_value={"approved": True}):
            raw = terminal_tool._handle_terminal(
                f'"{_python()}" bg2.py', cwd=str(tmp_path), background=True,
            )
        payload = json.loads(raw)
        assert payload["background"] is True
        sid = payload["session_id"]
        assert sid in wired_registry._running
        wired_registry.kill_process(sid)

    def test_spawn_background_parses_watch_patterns_csv(self, wired_registry, tmp_path):
        """模型把 watch_patterns 发成逗号分隔字符串时容错解析为列表。"""
        from spirit.tools import terminal_tool

        script = tmp_path / "bg3.py"
        script.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
        with patch("spirit.tools.approval.get_current_session_key", return_value="sk3"):
            raw = terminal_tool._spawn_background(
                f'"{_python()}" bg3.py', Path(str(tmp_path)),
                watch_patterns="ready, listening",
            )
        payload = json.loads(raw)
        assert payload["watch_patterns"] == ["ready", "listening"]
        wired_registry.kill_process(payload["session_id"])


# =========================================================================
# 接线 3：conversation_loop 通知回灌
# =========================================================================

class _FakeAgent:
    def __init__(self):
        self.messages = []

    def add_message(self, role, content):
        self.messages.append((role, content))


class TestConversationLoopInjection:
    def test_inject_drains_completion_into_conversation(self, wired_registry):
        from spirit.agent.conversation_loop import _inject_process_notifications

        agent = _FakeAgent()
        wired_registry.completion_queue.put({
            "type": "completion", "session_id": "n1", "command": "npm test",
            "exit_code": 0, "output": "all green",
        })
        with patch("spirit.tools.approval.get_current_session_key", return_value=""):
            n = _inject_process_notifications(agent)
        assert n == 1
        assert len(agent.messages) == 1
        role, text = agent.messages[0]
        assert role == "user"
        assert "npm test" in text and "all green" in text

    def test_inject_returns_zero_on_empty_queue(self, wired_registry):
        from spirit.agent.conversation_loop import _inject_process_notifications

        agent = _FakeAgent()
        with patch("spirit.tools.approval.get_current_session_key", return_value=""):
            assert _inject_process_notifications(agent) == 0
        assert agent.messages == []

    def test_inject_respects_per_turn_limit_and_requeues(self, wired_registry):
        """超过 max_notifications_per_turn 的事件被重新入队，不静默丢弃。"""
        from spirit.agent.conversation_loop import _inject_process_notifications

        agent = _FakeAgent()
        for i in range(4):
            wired_registry.completion_queue.put({
                "type": "completion", "session_id": f"m{i}", "command": f"c{i}",
                "exit_code": 0, "output": "",
            })
        with patch("spirit.tools.approval.get_current_session_key", return_value=""), \
             patch("spirit.config.get_config_value",
                   side_effect=lambda k, d=None: 2 if k == "process.max_notifications_per_turn" else d):
            n = _inject_process_notifications(agent)
        assert n == 2
        # 剩下 2 条重新入队，仍可在下一轮取到
        assert wired_registry.completion_queue.qsize() == 2

    def test_inject_skips_events_owned_by_other_session(self, wired_registry):
        """带 session_key 的事件不匹配当前会话 → 重新入队，不注入。"""
        from spirit.agent.conversation_loop import _inject_process_notifications

        agent = _FakeAgent()
        wired_registry.completion_queue.put({
            "type": "completion", "session_id": "o1", "command": "x",
            "exit_code": 0, "output": "", "session_key": "OTHER",
        })
        with patch("spirit.tools.approval.get_current_session_key", return_value="MINE"):
            n = _inject_process_notifications(agent)
        assert n == 0
        assert agent.messages == []
        assert wired_registry.completion_queue.qsize() == 1  # 留给它的主人
