"""ProcessRegistry 查询/状态/终止/checkpoint 单测。

对标 Hermes ``tests/tools/test_process_registry.py`` 的：
TestGetAndPoll / TestReadLog / TestStdinHelpers / TestListSessions /
TestActiveQueries / TestPruning / TestCheckpoint / TestKillProcess /
TestOrphanedPipeReconciliation / TestPidReuseGuard /
TestTerminateHostPid{Windows,Posix}。

绝大多数用例直接往 ``registry._running`` / ``_finished`` 塞构造好的会话，
做纯状态逻辑的确定性断言；只有少数几处用真实子进程（标注 realproc）。
"""

from __future__ import annotations

import json
import signal
import time
from unittest.mock import MagicMock, patch

import pytest

from spirit.process import registry as registry_mod
from spirit.process.session import ProcessSession

from .conftest import _make_session, _spawn_python_sleep, _wait_until


# =========================================================================
# Get / Poll
# =========================================================================

class TestGetAndPoll:
    def test_get_not_found(self, registry):
        assert registry.get("nonexistent") is None

    def test_get_running(self, registry):
        s = _make_session()
        registry._running[s.id] = s
        assert registry.get(s.id) is s

    def test_get_finished(self, registry):
        s = _make_session(exited=True, exit_code=0)
        registry._finished[s.id] = s
        assert registry.get(s.id) is s

    def test_poll_not_found(self, registry):
        result = registry.poll("nonexistent")
        assert result["status"] == "not_found"

    def test_poll_running(self, registry):
        s = _make_session(output="some output here")
        registry._running[s.id] = s
        result = registry.poll(s.id)
        assert result["status"] == "running"
        assert "some output" in result["output_preview"]
        assert result["command"] == "echo hello"

    def test_poll_exited(self, registry):
        s = _make_session(exited=True, exit_code=0, output="done")
        registry._finished[s.id] = s
        result = registry.poll(s.id)
        assert result["status"] == "exited"
        assert result["exit_code"] == 0

    def test_poll_marks_poll_observed_not_consumed(self, registry):
        """poll 是只读查询：记入 _poll_observed，但绝不标记 _completion_consumed。"""
        s = _make_session(exited=True, exit_code=0)
        registry._finished[s.id] = s
        registry.poll(s.id)
        assert s.id in registry._poll_observed
        assert s.id not in registry._completion_consumed


# =========================================================================
# read_log
# =========================================================================

class TestReadLog:
    def test_not_found(self, registry):
        assert registry.read_log("nope")["status"] == "not_found"

    def test_default_returns_tail(self, registry):
        out = "\n".join(f"line{i}" for i in range(500))
        s = _make_session(output=out)
        registry._running[s.id] = s
        result = registry.read_log(s.id)  # offset=0, limit=200 → 最后 200 行
        assert result["total_lines"] == 500
        assert result["output"].splitlines()[-1] == "line499"
        assert len(result["output"].splitlines()) == 200

    def test_offset_pagination(self, registry):
        out = "\n".join(f"line{i}" for i in range(10))
        s = _make_session(output=out)
        registry._running[s.id] = s
        result = registry.read_log(s.id, offset=2, limit=3)
        assert result["output"].splitlines() == ["line2", "line3", "line4"]

    def test_reading_tail_marks_consumed_when_exited(self, registry):
        """读到收尾输出且已退出 → 标记 _completion_consumed（抑制重复通知）。"""
        s = _make_session(exited=True, exit_code=0, output="all done")
        registry._finished[s.id] = s
        registry.read_log(s.id)
        assert s.id in registry._completion_consumed

    def test_mid_pagination_does_not_mark_consumed(self, registry):
        """读中间分页（没看到结尾）不标记消费 —— Agent 还没看到收尾。"""
        out = "\n".join(f"line{i}" for i in range(10))
        s = _make_session(exited=True, exit_code=0, output=out)
        registry._finished[s.id] = s
        # offset>0 且 [offset, offset+limit) 未触及末尾 → 未观察到收尾输出
        registry.read_log(s.id, offset=2, limit=3)
        assert s.id not in registry._completion_consumed


# =========================================================================
# wait
# =========================================================================

class TestWait:
    def test_not_found(self, registry):
        assert registry.wait("nope", timeout=1)["status"] == "not_found"

    def test_already_exited_returns_immediately(self, registry):
        s = _make_session(exited=True, exit_code=0, output="finished")
        registry._finished[s.id] = s
        result = registry.wait(s.id, timeout=5)
        assert result["status"] == "exited"
        assert result["exit_code"] == 0
        # wait 消费了输出
        assert s.id in registry._completion_consumed

    def test_timeout_on_still_running(self, registry):
        """伪运行会话（无真实 process）→ 等到超时返回 timeout。"""
        s = _make_session(exited=False)
        registry._running[s.id] = s
        result = registry.wait(s.id, timeout=1)
        assert result["status"] == "timeout"
        assert "timeout_note" in result

    def test_timeout_clamped_to_configured_limit(self, registry, monkeypatch):
        """请求超过配置上限 → 钳制到上限并附 timeout_note（不静默截断）。"""
        import spirit.config as config

        monkeypatch.setitem(config.DEFAULT_CONFIG["process"], "wait_default_seconds", 1)
        s = _make_session(exited=False)
        registry._running[s.id] = s
        result = registry.wait(s.id, timeout=999)
        assert result["status"] == "timeout"
        assert "clamped" in result["timeout_note"]

    def test_interrupted(self, registry):
        """is_interrupted 为真 → 立即返回 interrupted。"""
        s = _make_session(exited=False)
        registry._running[s.id] = s
        with patch("spirit.tools.extra_tools.is_interrupted", return_value=True):
            result = registry.wait(s.id, timeout=5)
        assert result["status"] == "interrupted"


# =========================================================================
# kill_process
# =========================================================================

class TestKillProcess:
    def test_not_found(self, registry):
        assert registry.kill_process("nope")["status"] == "not_found"

    def test_already_exited_marks_consumed(self, registry):
        s = _make_session(exited=True, exit_code=0, output="done")
        registry._finished[s.id] = s
        result = registry.kill_process(s.id)  # consume_output=True 默认
        assert result["status"] == "already_exited"
        assert s.id in registry._completion_consumed

    def test_already_exited_no_consume(self, registry):
        s = _make_session(exited=True, exit_code=0)
        registry._finished[s.id] = s
        registry.kill_process(s.id, consume_output=False)
        assert s.id not in registry._completion_consumed

    def test_kill_running_local_process(self, registry):
        """有真实 process 句柄的运行会话 → 终止、标记 killed、移入 finished。"""
        s = _make_session(exited=False)
        fake_proc = MagicMock()
        fake_proc.pid = 424242
        s.process = fake_proc
        registry._running[s.id] = s
        with patch.object(registry, "_terminate_host_pid") as term:
            result = registry.kill_process(s.id, source="test.kill")
        term.assert_called_once()
        assert result["status"] == "killed"
        assert s.exited is True
        assert s.exit_code == -15
        assert s.completion_reason == "killed"
        assert s.termination_source == "test.kill"
        assert s.id in registry._finished
        assert s.id not in registry._running

    def test_kill_enqueues_completion_when_notify(self, registry):
        """kill 一个 notify_on_complete 的进程 → 入队一条完成通知。"""
        s = _make_session(exited=False, notify_on_complete=True)
        fake_proc = MagicMock()
        fake_proc.pid = 424243
        s.process = fake_proc
        registry._running[s.id] = s
        with patch.object(registry, "_terminate_host_pid"):
            registry.kill_process(s.id, consume_output=False)
        evt = registry.completion_queue.get_nowait()
        assert evt["type"] == "completion"
        assert evt["completion_reason"] == "killed"

    def test_kill_detached_without_handle_errors(self, registry):
        """恢复得到、无句柄、pid_scope 非 host → 明确错误而非抛异常。"""
        s = _make_session(exited=False)
        s.detached = True
        s.pid_scope = "sandbox"
        registry._running[s.id] = s
        result = registry.kill_process(s.id)
        assert result["status"] == "error"


# =========================================================================
# stdin 三件套
# =========================================================================

class TestStdinHelpers:
    def test_write_stdin_windows_pty_uses_str(self, registry, monkeypatch):
        written = []

        class _FakePty:
            def write(self, value):
                written.append(value)

        s = _make_session(sid="pty-win")
        s._pty = _FakePty()
        registry._running[s.id] = s
        monkeypatch.setattr(registry_mod, "_IS_WINDOWS", True)
        result = registry.write_stdin(s.id, "hello\n")
        assert result == {"status": "ok", "bytes_written": 6}
        assert written == ["hello\n"]
        assert isinstance(written[0], str)

    def test_write_stdin_posix_pty_uses_bytes(self, registry, monkeypatch):
        written = []

        class _FakePty:
            def write(self, value):
                written.append(value)

        s = _make_session(sid="pty-posix")
        s._pty = _FakePty()
        registry._running[s.id] = s
        monkeypatch.setattr(registry_mod, "_IS_WINDOWS", False)
        result = registry.write_stdin(s.id, "hello\n")
        assert result == {"status": "ok", "bytes_written": 6}
        assert written == [b"hello\n"]

    def test_submit_stdin_appends_newline(self, registry):
        s = _make_session(sid="submit")
        stdin = MagicMock()
        s.process = MagicMock()
        s.process.stdin = stdin
        registry._running[s.id] = s
        registry.submit_stdin(s.id, "yes")
        stdin.write.assert_called_once_with("yes\n")
        stdin.flush.assert_called_once()

    def test_write_stdin_popen_no_stdin_errors(self, registry):
        s = _make_session(sid="nostdin")
        s.process = MagicMock()
        s.process.stdin = None
        registry._running[s.id] = s
        result = registry.write_stdin(s.id, "x")
        assert result["status"] == "error"

    def test_write_stdin_not_found(self, registry):
        assert registry.write_stdin("nope", "x")["status"] == "not_found"

    def test_write_stdin_already_exited(self, registry):
        s = _make_session(exited=True)
        registry._finished[s.id] = s
        assert registry.write_stdin(s.id, "x")["status"] == "already_exited"

    def test_close_stdin_popen(self, registry):
        s = _make_session(sid="close")
        stdin = MagicMock()
        s.process = MagicMock()
        s.process.stdin = stdin
        registry._running[s.id] = s
        result = registry.close_stdin(s.id)
        assert result["status"] == "ok"
        stdin.close.assert_called_once()

    def test_close_stdin_pty(self, registry):
        s = _make_session(sid="close-pty")
        pty = MagicMock()
        s._pty = pty
        registry._running[s.id] = s
        result = registry.close_stdin(s.id)
        assert result["status"] == "ok"
        pty.sendeof.assert_called_once()


# =========================================================================
# list_sessions
# =========================================================================

class TestListSessions:
    def test_empty(self, registry):
        assert registry.list_sessions() == []

    def test_lists_running_and_finished(self, registry):
        r = _make_session(sid="run1", task_id="t1")
        f = _make_session(sid="fin1", task_id="t1", exited=True, exit_code=0)
        registry._running[r.id] = r
        registry._finished[f.id] = f
        result = registry.list_sessions(task_id="t1")
        ids = {e["session_id"] for e in result}
        assert ids == {"run1", "fin1"}

    def test_filter_by_task_id(self, registry):
        a = _make_session(sid="a", task_id="t1")
        b = _make_session(sid="b", task_id="t2")
        registry._running[a.id] = a
        registry._running[b.id] = b
        result = registry.list_sessions(task_id="t1")
        assert [e["session_id"] for e in result] == ["a"]

    def test_session_scoped_cross_task(self, registry):
        """共享 session_key 但属于别的 task → 浮出并标记 session_scoped。"""
        cur = _make_session(sid="cur", task_id="t1", session_key="s1")
        forgotten = _make_session(sid="forgotten", task_id="t2", session_key="s1")
        registry._running[cur.id] = cur
        registry._running[forgotten.id] = forgotten
        result = registry.list_sessions(task_id="t1", session_key="s1")
        by_id = {e["session_id"]: e for e in result}
        assert "forgotten" in by_id
        assert by_id["forgotten"].get("session_scoped") is True
        assert by_id["cur"].get("session_scoped") is None

    def test_watch_and_notify_metadata(self, registry):
        s = _make_session(sid="w", task_id="t1",
                          watch_patterns=["ready"], notify_on_complete=True)
        s._watch_hits = 1
        registry._running[s.id] = s
        entry = registry.list_sessions(task_id="t1")[0]
        assert entry["watch_patterns"] == ["ready"]
        assert entry["watch_hit"] is True
        assert entry["notify_on_complete"] is True

    def test_exited_includes_exit_code(self, registry):
        s = _make_session(sid="e", task_id="t1", exited=True, exit_code=3)
        registry._finished[s.id] = s
        entry = registry.list_sessions(task_id="t1")[0]
        assert entry["status"] == "exited"
        assert entry["exit_code"] == 3


# =========================================================================
# 活跃查询
# =========================================================================

class TestActiveQueries:
    def test_count_running(self, registry):
        registry._running["a"] = _make_session(sid="a")
        registry._running["b"] = _make_session(sid="b")
        assert registry.count_running() == 2

    def test_has_active_processes(self, registry):
        s = _make_session(sid="a", task_id="t1")
        registry._running[s.id] = s
        assert registry.has_active_processes("t1") is True
        assert registry.has_active_processes("t2") is False

    def test_has_active_processes_ignores_exited(self, registry):
        s = _make_session(sid="a", task_id="t1", exited=True)
        registry._running[s.id] = s
        assert registry.has_active_processes("t1") is False

    def test_has_active_for_session(self, registry):
        s = _make_session(sid="a", session_key="s1")
        registry._running[s.id] = s
        assert registry.has_active_for_session("s1") is True
        assert registry.has_active_for_session("s2") is False

    def test_has_active_for_session_max_age_ignores_stale(self, registry):
        """启动超过 max_active_age 的进程被视为陈旧，不阻塞会话空闲。"""
        old = _make_session(sid="old", session_key="s1",
                            started_at=time.time() - 1000)
        registry._running[old.id] = old
        assert registry.has_active_for_session("s1", max_active_age=10) is False
        assert registry.has_active_for_session("s1") is True

    def test_has_any_active(self, registry):
        assert registry.has_any_active() is False
        registry._running["a"] = _make_session(sid="a")
        assert registry.has_any_active() is True

    def test_kill_all_filters_by_task(self, registry):
        a = _make_session(sid="a", task_id="t1")
        a.process = MagicMock(pid=1)
        b = _make_session(sid="b", task_id="t2")
        b.process = MagicMock(pid=2)
        registry._running[a.id] = a
        registry._running[b.id] = b
        with patch.object(registry, "_terminate_host_pid"):
            killed = registry.kill_all(task_id="t1")
        assert killed == 1
        assert a.exited is True
        assert b.exited is False


# =========================================================================
# 孤儿管道对齐
# =========================================================================

class TestOrphanedPipeReconciliation:
    def test_reconcile_flips_exited_when_popen_done(self, registry):
        """直接子进程已退出（Popen.poll 返回码）但 reader 未翻转 → reconcile 对齐。"""
        s = _make_session(sid="orphan", exited=False)
        proc = MagicMock()
        proc.poll.return_value = 0
        s.process = proc
        registry._running[s.id] = s
        registry._reconcile_local_exit(s)
        assert s.exited is True
        assert s.exit_code == 0
        assert s.completion_reason == "exited"
        assert s.id in registry._finished

    def test_reconcile_noop_when_still_running(self, registry):
        s = _make_session(sid="live", exited=False)
        proc = MagicMock()
        proc.poll.return_value = None
        s.process = proc
        registry._running[s.id] = s
        registry._reconcile_local_exit(s)
        assert s.exited is False
        assert s.id in registry._running

    def test_reconcile_preserves_killed_reason(self, registry):
        """已被 kill（reason=killed）的会话，reconcile 不覆盖退出码/原因。"""
        s = _make_session(sid="killed", exited=False)
        s.completion_reason = "killed"
        proc = MagicMock()
        proc.poll.return_value = 0
        s.process = proc
        registry._running[s.id] = s
        registry._reconcile_local_exit(s)
        assert s.exited is True
        assert s.completion_reason == "killed"


# =========================================================================
# PID 复用防护
# =========================================================================

class TestPidReuseGuard:
    def test_is_ours_false_when_dead(self, registry):
        with patch.object(registry_mod.ProcessRegistry, "_is_host_pid_alive",
                          return_value=False):
            assert registry._host_pid_is_ours(999, 12345) is False

    def test_is_ours_true_without_baseline(self, registry):
        """无创建时间基线 → 降级为纯存活检查（存活即视为我们的）。"""
        with patch.object(registry_mod.ProcessRegistry, "_is_host_pid_alive",
                          return_value=True):
            assert registry._host_pid_is_ours(999, None) is True

    def test_is_ours_true_when_start_matches(self, registry):
        with patch.object(registry_mod.ProcessRegistry, "_is_host_pid_alive",
                          return_value=True), \
             patch.object(registry_mod.ProcessRegistry, "_safe_host_start_time",
                          return_value=12345):
            assert registry._host_pid_is_ours(999, 12345) is True

    def test_is_ours_false_when_reused(self, registry):
        """存活但创建时间不匹配 → PID 被复用到无关进程，判为不是我们的。"""
        with patch.object(registry_mod.ProcessRegistry, "_is_host_pid_alive",
                          return_value=True), \
             patch.object(registry_mod.ProcessRegistry, "_safe_host_start_time",
                          return_value=99999):
            assert registry._host_pid_is_ours(999, 12345) is False

    def test_refresh_detached_marks_exited_on_reuse(self, registry):
        """detached 会话的 PID 被复用 → 视为已退出，移入 finished。"""
        s = _make_session(sid="det", exited=False)
        s.detached = True
        s.pid = 999
        s.pid_scope = "host"
        s.host_start_time = 12345
        registry._running[s.id] = s
        with patch.object(registry, "_host_pid_is_ours", return_value=False):
            registry._refresh_detached_session(s)
        assert s.exited is True
        assert s.id in registry._finished

    def test_refresh_detached_keeps_when_still_ours(self, registry):
        s = _make_session(sid="det2", exited=False)
        s.detached = True
        s.pid = 999
        s.pid_scope = "host"
        registry._running[s.id] = s
        with patch.object(registry, "_host_pid_is_ours", return_value=True):
            registry._refresh_detached_session(s)
        assert s.exited is False
        assert s.id in registry._running


# =========================================================================
# 进程树终止（平台分叉）
# =========================================================================

class TestTerminateHostPidWindows:
    def test_uses_taskkill_tree(self, monkeypatch):
        monkeypatch.setattr(registry_mod, "_IS_WINDOWS", True)
        with patch.object(registry_mod.subprocess, "run") as run:
            registry_mod.ProcessRegistry._terminate_host_pid(4242)
        args = run.call_args[0][0]
        assert args[:3] == ["taskkill", "/PID", "4242"]
        assert "/T" in args and "/F" in args

    def test_refuses_when_pid_reused(self, monkeypatch):
        """创建时间不匹配 → 拒绝发任何信号（泄漏孤儿优于杀陌生人）。"""
        monkeypatch.setattr(registry_mod, "_IS_WINDOWS", True)
        with patch.object(registry_mod.ProcessRegistry, "_host_pid_is_ours",
                          return_value=False), \
             patch.object(registry_mod.subprocess, "run") as run:
            registry_mod.ProcessRegistry._terminate_host_pid(4242, expected_start=1)
        run.assert_not_called()

    def test_falls_back_to_sigterm_when_taskkill_missing(self, monkeypatch):
        monkeypatch.setattr(registry_mod, "_IS_WINDOWS", True)
        with patch.object(registry_mod.subprocess, "run",
                          side_effect=FileNotFoundError), \
             patch.object(registry_mod.os, "kill") as kill:
            registry_mod.ProcessRegistry._terminate_host_pid(4242)
        kill.assert_called_once_with(4242, signal.SIGTERM)


class TestTerminateHostPidPosix:
    def test_terminates_children_then_parent(self, monkeypatch):
        monkeypatch.setattr(registry_mod, "_IS_WINDOWS", False)
        parent = MagicMock()
        child = MagicMock()
        parent.children.return_value = [child]
        fake_psutil = MagicMock()
        fake_psutil.Process.return_value = parent
        fake_psutil.NoSuchProcess = Exception
        fake_psutil.AccessDenied = Exception
        fake_psutil.STATUS_ZOMBIE = "zombie"
        import sys as _sys
        with patch.dict(_sys.modules, {"psutil": fake_psutil}), \
             patch.object(registry_mod.ProcessRegistry, "_daemon_term_grace_seconds",
                          return_value=0.0):
            registry_mod.ProcessRegistry._terminate_host_pid(4242)
        child.terminate.assert_called_once()
        parent.terminate.assert_called_once()

    def test_escalates_to_sigkill_after_grace(self, monkeypatch):
        """无视 SIGTERM 的幸存者在宽限窗口后升级为 SIGKILL。"""
        monkeypatch.setattr(registry_mod, "_IS_WINDOWS", False)
        parent = MagicMock()
        parent.children.return_value = []
        fake_psutil = MagicMock()
        fake_psutil.Process.return_value = parent
        fake_psutil.NoSuchProcess = Exception
        fake_psutil.AccessDenied = Exception
        fake_psutil.STATUS_ZOMBIE = "zombie"
        import sys as _sys
        with patch.dict(_sys.modules, {"psutil": fake_psutil}), \
             patch.object(registry_mod.ProcessRegistry, "_daemon_term_grace_seconds",
                          return_value=0.15), \
             patch.object(registry_mod.ProcessRegistry, "_proc_alive",
                          return_value=True), \
             patch.object(registry_mod.time, "sleep"):
            registry_mod.ProcessRegistry._terminate_host_pid(4242)
        parent.kill.assert_called_once()


# =========================================================================
# Pruning
# =========================================================================

class TestPruning:
    def test_ttl_expiry_removes_old_finished(self, registry, monkeypatch):
        monkeypatch.setattr(registry_mod, "FINISHED_TTL_SECONDS", 10)
        old = _make_session(sid="old", exited=True, started_at=time.time() - 100)
        registry._finished[old.id] = old
        with registry._lock:
            registry._prune_if_needed()
        assert "old" not in registry._finished

    def test_max_processes_evicts_oldest_finished(self, registry, monkeypatch):
        monkeypatch.setattr(registry_mod, "MAX_PROCESSES", 3)
        monkeypatch.setattr(registry_mod, "FINISHED_TTL_SECONDS", 10_000)
        now = time.time()
        # 2 running + 2 finished = 4 ≥ 3 → 淘汰最旧的 finished
        registry._running["r1"] = _make_session(sid="r1", started_at=now)
        registry._running["r2"] = _make_session(sid="r2", started_at=now)
        registry._finished["f_old"] = _make_session(sid="f_old", exited=True,
                                                    started_at=now - 50)
        registry._finished["f_new"] = _make_session(sid="f_new", exited=True,
                                                    started_at=now)
        with registry._lock:
            registry._prune_if_needed()
        assert "f_old" not in registry._finished
        assert "f_new" in registry._finished

    def test_stale_consumed_sets_pruned(self, registry):
        """不再被追踪的 consumed/observed 条目被清理（防无限增长）。"""
        registry._completion_consumed.add("ghost1")
        registry._poll_observed.add("ghost2")
        with registry._lock:
            registry._prune_if_needed()
        assert "ghost1" not in registry._completion_consumed
        assert "ghost2" not in registry._poll_observed


# =========================================================================
# Checkpoint / Recover
# =========================================================================

class TestCheckpoint:
    def test_write_and_recover_roundtrip(self, registry, checkpoint_home):
        s = _make_session(sid="cp1", task_id="t1", session_key="s1",
                          exited=False, notify_on_complete=True)
        s.pid = 4242
        s.host_start_time = 12345
        registry._running[s.id] = s
        registry._write_checkpoint()

        cp_file = checkpoint_home / "processes.json"
        assert cp_file.exists()
        entries = json.loads(cp_file.read_text(encoding="utf-8"))
        assert len(entries) == 1
        assert entries[0]["session_id"] == "cp1"

        # 新注册表恢复：PID 仍是我们派生的那个 → 接管为 detached
        fresh = registry_mod.ProcessRegistry(rehydrate_delegations=False)
        with patch.object(fresh, "_host_pid_is_ours", return_value=True):
            recovered = fresh.recover_from_checkpoint()
        assert recovered == 1
        restored = fresh.get("cp1")
        assert restored is not None
        assert restored.detached is True
        assert restored.notify_on_complete is True

    def test_recover_skips_reused_pid(self, registry, checkpoint_home):
        s = _make_session(sid="cp2", exited=False)
        s.pid = 4242
        s.host_start_time = 12345
        registry._running[s.id] = s
        registry._write_checkpoint()

        fresh = registry_mod.ProcessRegistry(rehydrate_delegations=False)
        with patch.object(fresh, "_host_pid_is_ours", return_value=False):
            recovered = fresh.recover_from_checkpoint()
        assert recovered == 0
        assert fresh.get("cp2") is None

    def test_recover_no_pid_marks_lost(self, registry, checkpoint_home):
        s = _make_session(sid="cp3", exited=False)
        s.pid = None
        registry._running[s.id] = s
        registry._write_checkpoint()

        fresh = registry_mod.ProcessRegistry(rehydrate_delegations=False)
        recovered = fresh.recover_from_checkpoint()
        assert recovered == 0
        lost = fresh.get("cp3")
        assert lost is not None
        assert lost.exited is True
        assert lost.completion_reason == "lost"

    def test_recover_missing_file_returns_zero(self, registry, checkpoint_home):
        assert registry.recover_from_checkpoint() == 0

    def test_recover_corrupt_file_returns_zero(self, registry, checkpoint_home):
        (checkpoint_home / "processes.json").write_text("{not json", encoding="utf-8")
        assert registry.recover_from_checkpoint() == 0

    def test_checkpoint_disabled_skips_write(self, registry, checkpoint_home, monkeypatch):
        import spirit.config as config

        monkeypatch.setitem(config.DEFAULT_CONFIG["process"], "checkpoint_enabled", False)
        s = _make_session(sid="cp4", exited=False)
        registry._running[s.id] = s
        registry._write_checkpoint()
        assert not (checkpoint_home / "processes.json").exists()


# =========================================================================
# 真实子进程端到端（realproc）
# =========================================================================

class TestRealProcess:
    """真实子进程端到端。

    用临时脚本文件 + ``cwd`` 派生，而不是 ``python -c "..."``：Windows 上
    ``cmd /c`` 对内联双引号的解析会损坏命令（环境伪影，非注册表逻辑）。
    ``checkpoint_home`` 已把目录隔离到 ASCII 临时路径。
    """

    def _write_script(self, checkpoint_home, name: str, body: str) -> str:
        path = checkpoint_home / name
        path.write_text(body, encoding="utf-8")
        return path.name

    def test_spawn_wait_lifecycle(self, registry, checkpoint_home):
        """派生真实短命进程 → wait 自然退出（exit_code=0，输出可见）。"""
        self._write_script(
            checkpoint_home, "quick.py",
            "import time\nprint('hi', flush=True)\ntime.sleep(0.4)\n",
        )
        sess = registry.spawn_local(
            f'"{_python()}" quick.py', cwd=str(checkpoint_home),
            session_key="realproc", notify_on_complete=True,
        )
        assert sess.completion_reason != "failed_start"
        assert sess.pid
        result = registry.wait(sess.id, timeout=15)
        assert result["status"] == "exited"
        assert result["exit_code"] == 0
        assert "hi" in result["output"]

    def test_spawn_and_kill_running(self, registry, checkpoint_home):
        self._write_script(
            checkpoint_home, "sleep.py", "import time\ntime.sleep(30)\n",
        )
        sess = registry.spawn_local(
            f'"{_python()}" sleep.py', cwd=str(checkpoint_home),
            session_key="realproc",
        )
        assert _wait_until(lambda: registry.poll(sess.id)["status"] == "running", 10)
        assert registry.is_session_waiting(sess.id) is True
        result = registry.kill_process(sess.id)
        assert result["status"] == "killed"
        assert _wait_until(lambda: registry.get(sess.id).exited, 10)

    def test_incremental_output_visible_while_running(self, registry, checkpoint_home):
        """增量输出：进程还在跑时就能读到已产出的行（read1 不阻塞到 EOF）。"""
        self._write_script(
            checkpoint_home, "early.py",
            "import time\nprint('early', flush=True)\ntime.sleep(10)\n",
        )
        sess = registry.spawn_local(
            f'"{_python()}" early.py', cwd=str(checkpoint_home),
            session_key="realproc",
        )
        try:
            ok = _wait_until(
                lambda: "early" in registry.poll(sess.id).get("output_preview", ""), 10,
            )
            assert ok, "运行中的进程输出应增量可见"
            # 进程仍在跑（没因读完而退出）
            assert registry.poll(sess.id)["status"] == "running"
        finally:
            registry.kill_process(sess.id)

    def test_failed_start_is_captured_not_raised(self, registry, checkpoint_home):
        """派生不存在的命令 → 落一个 failed_start 会话而不是抛异常。"""
        sess = registry.spawn_local(
            "this_command_does_not_exist_xyz", cwd=str(checkpoint_home),
            session_key="realproc",
        )
        # shell 会启动但命令未找到：要么 failed_start，要么非零退出
        assert sess.id
        result = registry.wait(sess.id, timeout=10)
        assert result["status"] in {"exited", "timeout"}
        if result["status"] == "exited":
            assert result["exit_code"] != 0


def _python() -> str:
    import sys
    return sys.executable
