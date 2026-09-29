"""A4 三层进化记忆测试：轨迹池 / 洞察库 / 技能库 + 落盘 + fail-soft。"""

import json

from spirit.evolution import get_memory
from spirit.evolution.memory import (
    INSIGHT_DEPRECATED,
    EvolutionMemory,
    evolution_memory_root,
    insights_path,
    trajectories_path,
)


# --------------------------------------------------------------------------
# 路径解析（按调用读 SPIRIT_HOME）
# --------------------------------------------------------------------------

def test_root_reflects_spirit_home(evo_home):
    assert evolution_memory_root() == evo_home / "evolution_memory"


def test_get_memory_singleton(evo_home):
    m1 = get_memory()
    m2 = get_memory()
    assert m1 is m2
    assert m1.root == evo_home / "evolution_memory"


def test_injected_root_isolation(tmp_path):
    mem = EvolutionMemory(root=tmp_path / "custom")
    mem.add_insight("x", confidence=0.9, source_task_id="t")
    assert (tmp_path / "custom" / "insights.json").exists()


# --------------------------------------------------------------------------
# 轨迹池
# --------------------------------------------------------------------------

def test_add_and_list_trajectories(tmp_path):
    mem = EvolutionMemory(root=tmp_path)
    mem.add_trajectory("t1", "did A -> saw B", outcome="PASS")
    mem.add_trajectory("t2", "did C")
    assert len(mem.trajectories()) == 2
    only_t1 = mem.trajectories(task_id="t1")
    assert len(only_t1) == 1 and only_t1[0].outcome == "PASS"


def test_trajectories_persist_across_instances(tmp_path):
    EvolutionMemory(root=tmp_path).add_trajectory("t1", "content")
    reloaded = EvolutionMemory(root=tmp_path).trajectories()
    assert len(reloaded) == 1 and reloaded[0].content == "content"


def test_trajectory_has_id_and_timestamp(tmp_path):
    mem = EvolutionMemory(root=tmp_path)
    traj = mem.add_trajectory("t1", "c")
    assert traj.id.startswith("traj_") and traj.timestamp > 0


# --------------------------------------------------------------------------
# 洞察库
# --------------------------------------------------------------------------

def test_add_insight_defaults_active(tmp_path):
    mem = EvolutionMemory(root=tmp_path)
    ins = mem.add_insight("回测夏普>1 才纳入", confidence=0.8, source_task_id="t1")
    assert ins.active is True
    assert ins.source_task_id == "t1"
    assert mem.insights()[0].text.startswith("回测")


def test_insight_confidence_clamped(tmp_path):
    mem = EvolutionMemory(root=tmp_path)
    assert mem.add_insight("a", confidence=5.0).confidence == 1.0
    assert mem.add_insight("b", confidence=-3.0).confidence == 0.0
    assert mem.add_insight("c", confidence="bad").confidence == 0.5


def test_insights_active_only_and_min_confidence(tmp_path):
    mem = EvolutionMemory(root=tmp_path)
    mem.add_insight("low", confidence=0.2, source_task_id="t")
    mem.add_insight("high", confidence=0.9, source_task_id="t")
    assert len(mem.insights(active_only=True)) == 2
    high = mem.insights(min_confidence=0.5)
    assert len(high) == 1 and high[0].text == "high"


def test_deprecate_insight_sinks_it(tmp_path):
    mem = EvolutionMemory(root=tmp_path)
    ins = mem.add_insight("will be falsified", confidence=0.7, source_task_id="t")
    assert mem.deprecate_insight(ins.id) is True
    assert mem.get_insight(ins.id).status == INSIGHT_DEPRECATED
    # active_only 不再包含它，但全量仍在（不物理删除以保审计）
    assert len(mem.insights(active_only=True)) == 0
    assert len(mem.insights(active_only=False)) == 1


def test_deprecate_unknown_returns_false(tmp_path):
    assert EvolutionMemory(root=tmp_path).deprecate_insight("nope") is False


def test_decay_insights_lowers_confidence(tmp_path):
    mem = EvolutionMemory(root=tmp_path)
    mem.add_insight("a", confidence=1.0, source_task_id="t")
    count = mem.decay_insights(factor=0.5)
    assert count == 1
    assert mem.insights()[0].confidence == 0.5


def test_decay_skips_deprecated(tmp_path):
    mem = EvolutionMemory(root=tmp_path)
    ins = mem.add_insight("a", confidence=1.0, source_task_id="t")
    mem.deprecate_insight(ins.id)
    assert mem.decay_insights(factor=0.5) == 0


# --------------------------------------------------------------------------
# 技能库
# --------------------------------------------------------------------------

def test_add_and_list_skills(tmp_path):
    mem = EvolutionMemory(root=tmp_path)
    skill = mem.add_skill("均值回归入场", "当价差>2σ", "步骤...", source_insight_ids=["ins_1"])
    skills = mem.skills()
    assert len(skills) == 1
    assert skills[0].name == "均值回归入场"
    assert skills[0].source_insight_ids == ["ins_1"]
    assert skill.id.startswith("skill_")


# --------------------------------------------------------------------------
# 汇总 / 清理
# --------------------------------------------------------------------------

def test_stats(tmp_path):
    mem = EvolutionMemory(root=tmp_path)
    mem.add_trajectory("t", "c")
    good = mem.add_insight("a", confidence=0.9, source_task_id="t")
    mem.add_insight("b", confidence=0.1, source_task_id="t")
    mem.deprecate_insight(good.id)
    mem.add_skill("n", "trig", "proc")
    stats = mem.stats()
    assert stats["trajectories"] == 1
    assert stats["insights_total"] == 2
    assert stats["insights_active"] == 1
    assert stats["skills"] == 1


def test_clear_removes_all(tmp_path):
    mem = EvolutionMemory(root=tmp_path)
    mem.add_trajectory("t", "c")
    mem.add_insight("a", confidence=0.5, source_task_id="t")
    mem.add_skill("n", "trig", "proc")
    mem.clear()
    assert mem.stats() == {
        "trajectories": 0, "insights_active": 0, "insights_total": 0, "skills": 0
    }


# --------------------------------------------------------------------------
# fail-soft：损坏文件
# --------------------------------------------------------------------------

def test_corrupt_json_is_treated_as_empty(tmp_path):
    mem = EvolutionMemory(root=tmp_path)
    mem.add_insight("a", confidence=0.5, source_task_id="t")
    # 写坏 insights.json
    with open(insights_path(tmp_path), "w", encoding="utf-8") as fh:
        fh.write("{not valid json")
    assert mem.insights() == []
    # 仍可继续写入（覆盖损坏文件）
    mem.add_insight("b", confidence=0.6, source_task_id="t2")
    assert len(mem.insights()) == 1


def test_missing_files_are_empty(tmp_path):
    mem = EvolutionMemory(root=tmp_path / "nonexistent")
    assert mem.trajectories() == [] and mem.insights() == [] and mem.skills() == []


def test_non_list_json_treated_as_empty(tmp_path):
    mem = EvolutionMemory(root=tmp_path)
    (tmp_path).mkdir(parents=True, exist_ok=True)
    with open(trajectories_path(tmp_path), "w", encoding="utf-8") as fh:
        json.dump({"unexpected": "object"}, fh)
    assert mem.trajectories() == []
