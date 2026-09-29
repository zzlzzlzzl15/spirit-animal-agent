"""AutonomyLoop 常驻自主循环测试 —— Spirit Autonomy Phase 7.B。

聚焦不依赖 EvolutionLoop.run 的路径：空转 step、目标提议（含 HITL 分叉）、
默认 LLM 提议、日报摘要、max_cycles 常驻退出。
"""

from types import SimpleNamespace

from spirit.autonomy.explorer import AutonomyLoop, IDLE
from spirit.autonomy.sandbox import Sandbox


class FakeHITL:
    def __init__(self, answer_text="b"):
        self.answer_text = answer_text
        self.queries = []

    def consult(self, query):
        self.queries.append(query)
        return SimpleNamespace(text=self.answer_text, from_user=True)


def test_step_idle_no_candidates(tmp_path):
    loop = AutonomyLoop(propose=lambda mem: [], sandbox=Sandbox(root=tmp_path / "sb"))
    cycle = loop.step()
    assert cycle.status == IDLE
    assert cycle.task == ""
    assert cycle.note_path == ""
    assert len(loop.cycles) == 1


def test_propose_next_single_candidate(tmp_path):
    loop = AutonomyLoop(propose=lambda mem: ["only"], sandbox=Sandbox(root=tmp_path / "sb"))
    assert loop.propose_next() == "only"


def test_propose_next_fork_uses_hitl(tmp_path):
    hitl = FakeHITL(answer_text="b")
    loop = AutonomyLoop(
        propose=lambda mem: ["a", "b"], hitl=hitl, sandbox=Sandbox(root=tmp_path / "sb")
    )
    assert loop.propose_next() == "b"
    assert len(hitl.queries) == 1
    assert hitl.queries[0].options == ["a", "b"]
    assert hitl.queries[0].is_fork is True


def test_default_propose_with_caller(tmp_path):
    caller = lambda msgs, temp, maxtok, timeout: "solo direction\n"
    loop = AutonomyLoop(caller=caller, sandbox=Sandbox(root=tmp_path / "sb"))
    assert loop.propose_next() == "solo direction"


def test_summary_counts_cycles(tmp_path):
    loop = AutonomyLoop(propose=lambda mem: [], sandbox=Sandbox(root=tmp_path / "sb"))
    loop.step()
    loop.step()
    text = loop.summary()
    assert "共 2 轮" in text


def test_run_forever_max_cycles(tmp_path):
    loop = AutonomyLoop(
        propose=lambda mem: [],
        sandbox=Sandbox(root=tmp_path / "sb"),
        interval_seconds=0.0,
        max_cycles=2,
    )
    loop.run_forever(poll=0.0)
    assert len(loop.cycles) >= 2
    assert loop.state()["cycle_no"] >= 2
