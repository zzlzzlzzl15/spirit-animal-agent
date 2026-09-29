"""Autonomy 部署集成测试 —— Spirit Autonomy Phase 7.D。

离线验证：build_autonomy 组装桥接+循环（server=None）、start_autonomy 注册 WS 命令、
make_llm_caller/make_execute 无 api_key 时 fail-soft 返回 None、make_memory 持久记忆、
DRS 三角色按 caller 接线、execute 在沙箱工作目录内驱动 Agent 并还原 cwd。
"""

import os
from pathlib import Path

from spirit.autonomy.integration import (
    build_autonomy,
    make_execute,
    make_llm_caller,
    make_memory,
    make_proposer,
    resolve_interval_seconds,
    resolve_stop_policy,
    resolve_max_rounds,
    DEFAULT_RESIDENT_INTERVAL_SECONDS,
    start_autonomy,
)
from spirit.autonomy.bridge import CMD_ANSWER, CMD_START, CMD_STATUS, CMD_STOP
from spirit.autonomy.sandbox import Sandbox
from spirit.evolution.loop import (
    AttemptResult, STOP_VERIFIER_PASS, STOP_CURRICULUM_REVIEW,
)
from spirit.evolution.memory import EvolutionMemory


class FakeRegistry:
    def __init__(self):
        self.registered = {}

    def register(self, action, handler):
        self.registered[action] = handler


class FakeServer:
    def __init__(self):
        self.commands = FakeRegistry()

    async def broadcast(self, event_type, data):
        return None


def test_build_autonomy_offline():
    # 关掉 agent execute → 不依赖开发机真实配置，纯离线确定性
    bridge, autonomy = build_autonomy(enable_agent_execute=False)
    assert bridge.controller is autonomy
    assert autonomy.hitl.ask.__self__ is bridge   # HITL 询问桥接到桌宠桥接器
    assert autonomy.sandbox is not None
    assert autonomy.memory is not None            # 持久记忆已接上
    # 无 caller → DRS 三角色不接线（fail-soft 退化为安全默认）
    assert autonomy._evolution.actor.has_caller() is False


def test_build_autonomy_wires_drs_roles(tmp_path):
    # 有 caller 时：Actor/Verifier/Curriculum 用同一 caller 接线，闭环真正推理
    def fake_caller(messages, temperature=0.7, max_tokens=300, timeout=30.0):
        return ""

    bridge, autonomy = build_autonomy(
        caller=fake_caller,
        memory=EvolutionMemory(root=tmp_path / "mem"),
        sandbox=Sandbox(root=tmp_path / "sbx"),
        enable_agent_execute=False,
    )
    evo = autonomy._evolution
    assert evo.actor.has_caller() is True          # Actor 接线
    assert evo.curriculum is not None              # Curriculum 接线
    assert autonomy.hitl.memory is autonomy.memory  # HITL 回灌同一记忆


def test_start_autonomy_registers_commands():
    server = FakeServer()
    autonomy = start_autonomy(server, loop=None, autostart=False, enable_agent_execute=False)
    assert autonomy is not None
    assert set(server.commands.registered) >= {CMD_ANSWER, CMD_STATUS, CMD_START, CMD_STOP}
    assert autonomy.running is True          # 未 start 线程但 stop 事件未置位
    autonomy.stop()


def test_start_autonomy_failsoft_on_bad_server():
    # server 缺少 commands 属性 → attach 抛异常 → start_autonomy 返回 None（不崩）
    class Bad:
        pass

    assert start_autonomy(Bad(), loop=None, autostart=False, enable_agent_execute=False) is None


def test_make_llm_caller_no_api_key(monkeypatch):
    import spirit.config as cfg
    monkeypatch.setattr(cfg, "load_config", lambda *a, **k: {"llm": {}})
    assert make_llm_caller() is None


def test_make_memory_isolated(tmp_path):
    mem = make_memory()
    assert mem is None or isinstance(mem, EvolutionMemory)


def test_make_execute_none_without_api_key(tmp_path, monkeypatch):
    import spirit.config as cfg
    monkeypatch.setattr(cfg, "load_config", lambda *a, **k: {"llm": {}})
    assert make_execute(Sandbox(root=tmp_path / "sbx")) is None


def test_make_execute_runs_agent_in_sandbox(tmp_path, monkeypatch):
    import spirit.config as cfg
    monkeypatch.setattr(
        cfg, "load_config",
        lambda *a, **k: {"llm": {"api_key": "k", "model": "m", "provider": "openai"}},
    )
    sbx = Sandbox(root=tmp_path / "sbx")
    captured = {}

    class FakeAgent:
        def __init__(self, config):
            self.config = config

        def chat(self, message):
            captured["cwd"] = os.getcwd()
            captured["prompt"] = self.config.system_prompt
            captured["message"] = message
            return {"response": "已完成探索", "tool_calls": [{"name": "write_file"}],
                    "iterations": 3}

    execute = make_execute(sbx, agent_factory=FakeAgent)
    assert execute is not None
    prev = os.getcwd()
    result = execute("探索任务X")

    # Agent 在沙箱工作目录内执行，且注入 workspace 系统提示
    assert Path(captured["cwd"]) == sbx.root
    assert "自主探索模式" in captured["prompt"]
    assert captured["message"] == "探索任务X"
    # 产物封装为 AttemptResult（observation=回复，evidence=工具日志）
    assert isinstance(result, AttemptResult)
    assert result.observation == "已完成探索"
    assert "write_file" in result.evidence.logs
    # 执行后工作目录已还原
    assert os.getcwd() == prev


def test_make_execute_failsoft_on_agent_error(tmp_path, monkeypatch):
    import spirit.config as cfg
    monkeypatch.setattr(
        cfg, "load_config",
        lambda *a, **k: {"llm": {"api_key": "k", "model": "m"}},
    )
    sbx = Sandbox(root=tmp_path / "sbx")
    prev = os.getcwd()

    class BoomAgent:
        def __init__(self, config):
            pass

        def chat(self, message):
            raise RuntimeError("boom")

    execute = make_execute(sbx, agent_factory=BoomAgent)
    result = execute("会炸的任务")
    assert isinstance(result, AttemptResult)      # 异常被吞为 AttemptResult，不抛出
    assert "执行异常" in result.observation
    assert os.getcwd() == prev                    # 异常路径仍还原 cwd


# --- 5 小时定时（对齐 API token 刷新）------------------------------------

def test_resolve_interval_default_5h(monkeypatch):
    # 无显式/无配置 → 缺省 5 小时
    import spirit.config as cfg
    monkeypatch.setattr(cfg, "get_config_value", lambda key, default=None: default)
    assert DEFAULT_RESIDENT_INTERVAL_SECONDS == 5 * 3600.0
    assert resolve_interval_seconds() == 5 * 3600.0


def test_resolve_interval_explicit_wins():
    assert resolve_interval_seconds(600) == 600.0


def test_resolve_interval_from_config_hours(monkeypatch):
    import spirit.config as cfg
    monkeypatch.setattr(cfg, "get_config_value",
                        lambda key, default=None: 2 if key == "autonomy.interval_hours" else default)
    assert resolve_interval_seconds() == 2 * 3600.0


def test_resolve_interval_from_config_seconds(monkeypatch):
    import spirit.config as cfg
    monkeypatch.setattr(cfg, "get_config_value",
                        lambda key, default=None: 1800 if key == "autonomy.interval_seconds" else default)
    assert resolve_interval_seconds() == 1800.0


def test_build_autonomy_uses_resolved_interval(tmp_path, monkeypatch):
    # 缺省 → AutonomyLoop.interval_seconds == 5 小时，Scheduler interval 任务同步
    import spirit.config as cfg
    monkeypatch.setattr(cfg, "get_config_value", lambda key, default=None: default)
    bridge, autonomy = build_autonomy(
        sandbox=Sandbox(root=tmp_path / "sbx"), enable_agent_execute=False,
    )
    assert autonomy.interval_seconds == 5 * 3600.0
    task = autonomy.scheduler.get_task("autonomy_explore")
    assert task is not None and task.every_seconds == 5 * 3600.0


# --- 产物快照→丰富 Evidence（修复 STALLED）---------------------------

def test_make_execute_snapshots_products(tmp_path, monkeypatch):
    import spirit.config as cfg
    monkeypatch.setattr(
        cfg, "load_config",
        lambda *a, **k: {"llm": {"api_key": "k", "model": "m", "provider": "openai"}},
    )
    sbx = Sandbox(root=tmp_path / "sbx")

    class WriterAgent:
        def __init__(self, config):
            self.config = config

        def chat(self, message):
            # cwd 已被 execute 锁定到沙箱根 → 相对路径写入落在沙箱内
            Path("report.md").write_text("# 回测报告\n夏普=1.5", encoding="utf-8")
            Path("metrics.json").write_text('{"sharpe": 1.5}', encoding="utf-8")
            return {"response": "完成", "tool_calls": [{"name": "write_file"}],
                    "iterations": 2}

    execute = make_execute(sbx, agent_factory=WriterAgent)
    ev = execute("做回测").evidence
    # 本轮新建产物被快照进 Evidence.files（Verifier 的客观证据）
    assert "report.md" in ev.files
    assert "夏普=1.5" in ev.files["report.md"]
    assert "metrics.json" in ev.files
    # 改动文件清单进 env_state
    assert "report.md" in ev.env_state["sandbox_files_changed"]
    assert ev.env_state["sandbox_file_count"] >= 2


def test_collect_products_only_changed_and_text(tmp_path):
    # 已存在的旧文件不计入本轮产物；二进制/超大文件不回读
    from spirit.autonomy.integration import _collect_products, _sandbox_snapshot
    sbx = Sandbox(root=tmp_path / "sbx")
    sbx.write_text("old.md", "旧文件")
    before = _sandbox_snapshot(sbx)
    sbx.write_text("new.md", "新产物")
    (sbx.root / "big.bin").write_bytes(b"\x00" * 99999)
    changed, files = _collect_products(sbx, before)
    assert "new.md" in changed and "new.md" in files
    assert "old.md" not in changed              # 未改动→不计入
    assert "big.bin" in changed                 # 新建→列入改动清单
    assert "big.bin" not in files               # 非文本扩展名→不回读内容


# --- 架构型自提议（做完→自提新问题→继续）-------------------------

def test_make_proposer_none_without_caller():
    assert make_proposer(None) is None


def test_make_proposer_returns_architecture_questions():
    captured = {}

    def fake_caller(messages, temperature=0.7, max_tokens=300, timeout=30.0):
        captured["prompt"] = messages[0]["content"]
        return "1. 如何划分模块边界\n2. 缓存层选型\n3. 接口契约设计\n"

    propose = make_proposer(fake_caller, domain="金融")
    candidates = propose(None)
    assert len(candidates) == 3
    assert candidates[0] == "如何划分模块边界"      # 编号被 strip
    assert "架构/设计型" in captured["prompt"]
    assert "金融" in captured["prompt"]


def test_make_proposer_failsoft_on_caller_error():
    def boom(messages, temperature=0.7, max_tokens=300, timeout=30.0):
        raise RuntimeError("api down")

    propose = make_proposer(boom)
    assert propose(None) == []                    # 异常→空候选（fail-soft）


def test_build_autonomy_wires_proposer_and_policy(tmp_path, monkeypatch):
    import spirit.config as cfg
    monkeypatch.setattr(cfg, "load_config", lambda *a, **k: {})

    def fake_caller(messages, temperature=0.7, max_tokens=300, timeout=30.0):
        return "架构问题A\n"

    bridge, autonomy = build_autonomy(
        caller=fake_caller,
        memory=EvolutionMemory(root=tmp_path / "mem"),
        sandbox=Sandbox(root=tmp_path / "sbx"),
        enable_agent_execute=False,
        domain="金融",
    )
    # 架构型自提议已接上（propose seam 非默认）
    assert autonomy._propose is not None
    assert autonomy.propose_next() == "架构问题A"
    # 缺省停止策略 = curriculum_review（PASS 后仍可迭代打磨）+ 多轮次（对任务自动迭代）
    assert autonomy.stop_policy == STOP_CURRICULUM_REVIEW
    assert autonomy._evolution.stop_policy == STOP_CURRICULUM_REVIEW
    assert autonomy.max_rounds == 5
    assert autonomy._evolution.max_rounds == 5


# --- 停止策略/轮次解析（对任务自动迭代，可配置控 token）------------

def test_resolve_stop_policy_default_curriculum_review(monkeypatch):
    import spirit.config as cfg
    monkeypatch.setattr(cfg, "load_config", lambda *a, **k: {})
    assert resolve_stop_policy() == STOP_CURRICULUM_REVIEW
    assert resolve_max_rounds() == 5


def test_resolve_stop_policy_explicit_wins():
    assert resolve_stop_policy(STOP_VERIFIER_PASS) == STOP_VERIFIER_PASS
    assert resolve_max_rounds(2) == 2


def test_resolve_stop_policy_from_config(monkeypatch):
    import spirit.config as cfg
    monkeypatch.setattr(
        cfg, "load_config",
        lambda *a, **k: {"autonomy": {"stop_policy": "verifier_pass", "max_rounds": 3}},
    )
    assert resolve_stop_policy() == STOP_VERIFIER_PASS
    assert resolve_max_rounds() == 3


def test_build_autonomy_honors_explicit_policy(tmp_path, monkeypatch):
    # 显式传入 verifier_pass 覆盖缺省（省 token 档）
    import spirit.config as cfg
    monkeypatch.setattr(cfg, "load_config", lambda *a, **k: {})
    bridge, autonomy = build_autonomy(
        sandbox=Sandbox(root=tmp_path / "sbx"), enable_agent_execute=False,
        stop_policy=STOP_VERIFIER_PASS, max_rounds=2,
    )
    assert autonomy.stop_policy == STOP_VERIFIER_PASS
    assert autonomy._evolution.max_rounds == 2
