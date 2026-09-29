"""记忆哈希 + 冻结快照测试（Phase 6.D）：确定性哈希 / 冻结 / 校验 / 只读复用。"""

import json

from spirit.evolution.memory import EvolutionMemory
from spirit.evolution.memory_hash import (
    FrozenMemory,
    MemorySnapshot,
    compute_hash,
    freeze,
    list_snapshots,
    load_snapshot,
)


# --------------------------------------------------------------------------
# 哈希
# --------------------------------------------------------------------------

def test_hash_is_deterministic(evo_home):
    mem = EvolutionMemory()
    mem.add_insight("A", confidence=0.5, source_task_id="t1")
    h1 = compute_hash(mem)
    h2 = compute_hash(mem)
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_hash_changes_when_memory_changes(evo_home):
    mem = EvolutionMemory()
    h0 = compute_hash(mem)
    mem.add_insight("新经验", confidence=0.5, source_task_id="t1")
    h1 = compute_hash(mem)
    assert h0 != h1


def test_empty_memory_hash_stable(evo_home):
    mem = EvolutionMemory()
    assert compute_hash(mem) == compute_hash(EvolutionMemory())


# --------------------------------------------------------------------------
# 冻结 + 校验
# --------------------------------------------------------------------------

def test_freeze_creates_verifiable_snapshot(evo_home):
    mem = EvolutionMemory()
    mem.add_insight("冻结前的经验", confidence=0.8, source_task_id="t1")
    snap = freeze(mem, label="wave-1")
    assert snap.manifest_path.is_file()
    assert snap.verify() is True
    assert snap.counts["insights_active"] == 1
    assert snap.label == "wave-1"


def test_snapshot_verify_detects_tamper(evo_home):
    mem = EvolutionMemory()
    mem.add_insight("原始", confidence=0.5, source_task_id="t1")
    snap = freeze(mem)
    # 篡改快照内的 insights.json
    ins_path = snap.root / "insights.json"
    data = json.loads(ins_path.read_text(encoding="utf-8"))
    data.append({"id": "ins_fake", "text": "被篡改", "confidence": 1.0})
    ins_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert snap.verify() is False


def test_snapshot_hash_matches_source_at_freeze_time(evo_home):
    mem = EvolutionMemory()
    mem.add_insight("X", confidence=0.5, source_task_id="t1")
    src_hash = compute_hash(mem)
    snap = freeze(mem)
    assert snap.hash == src_hash


def test_snapshot_isolated_from_later_memory_writes(evo_home):
    mem = EvolutionMemory()
    mem.add_insight("快照时", confidence=0.5, source_task_id="t1")
    snap = freeze(mem)
    # 冻结后继续写主记忆，不应影响快照校验
    mem.add_insight("快照后新增", confidence=0.5, source_task_id="t2")
    assert snap.verify() is True
    assert compute_hash(mem) != snap.hash


# --------------------------------------------------------------------------
# 只读复用（Phase 3 · D4）
# --------------------------------------------------------------------------

def test_open_readonly_reads_frozen_insights(evo_home):
    mem = EvolutionMemory()
    mem.add_insight("冻结经验", confidence=0.9, source_task_id="t1")
    snap = freeze(mem)
    frozen = snap.open_readonly()
    assert isinstance(frozen, FrozenMemory)
    texts = [i.text for i in frozen.insights()]
    assert "冻结经验" in texts


def test_frozen_memory_rejects_writes(evo_home):
    mem = EvolutionMemory()
    mem.add_insight("x", confidence=0.5, source_task_id="t1")
    frozen = freeze(mem).open_readonly()
    for op in (
        lambda: frozen.add_trajectory("t", "c"),
        lambda: frozen.add_insight("y"),
        lambda: frozen.add_skill("n", "t", "p"),
        lambda: frozen.deprecate_insight("ins_1"),
        lambda: frozen.decay_insights(),
        lambda: frozen.clear(),
    ):
        try:
            op()
            assert False, "冻结记忆应拒绝写入"
        except RuntimeError:
            pass


# --------------------------------------------------------------------------
# 列举 / 加载
# --------------------------------------------------------------------------

def test_list_and_load_snapshots(evo_home):
    mem = EvolutionMemory()
    mem.add_insight("a", confidence=0.5, source_task_id="t1")
    s1 = freeze(mem, label="one")
    mem.add_insight("b", confidence=0.5, source_task_id="t2")
    s2 = freeze(mem, label="two")
    snaps = list_snapshots(mem)
    assert len(snaps) == 2
    assert [s.label for s in snaps] == ["one", "two"]  # 按 created_at 升序
    loaded = load_snapshot(s2.snapshot_id, mem)
    assert loaded is not None
    assert loaded.label == "two"


def test_load_missing_snapshot_returns_none(evo_home):
    mem = EvolutionMemory()
    assert load_snapshot("snap_nope", mem) is None


def test_list_snapshots_empty(evo_home):
    mem = EvolutionMemory()
    assert list_snapshots(mem) == []
