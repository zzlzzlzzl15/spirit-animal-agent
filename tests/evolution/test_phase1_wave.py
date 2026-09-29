"""BRS 广度并行 + wave memory barrier 测试（Phase 6.D）。"""

from spirit.evolution.memory import EvolutionMemory
from spirit.evolution.phase1_wave import BranchOutcome, Phase1Wave, WaveResult, run_wave
from spirit.evolution.protocol import TargetVerdict
from spirit.evolution.roles import ActorRole
from spirit.evolution.verifier import Evidence, ExitCodeVerifier, Verifier

from .conftest import FakeCaller, json_response


def _verifier():
    return Verifier(domain_verifiers=[ExitCodeVerifier(0)])


def make_execute(mapping):
    """按 task 关键字回放退出码；未命中默认失败码 1。"""

    def execute(task):
        for key, code in mapping.items():
            if key in task:
                return Evidence(task=task, exit_code=code, logs=f"{task} exit {code}")
        return Evidence(task=task, exit_code=1, logs=f"{task} exit 1")

    return execute


PROJECTS = [
    {"task_id": "p1", "task": "因子A 回测"},
    {"task_id": "p2", "task": "因子B 回测"},
    {"task_id": "p3", "task": "因子C 回测"},
]


# --------------------------------------------------------------------------
# 全通过 → 合并记忆
# --------------------------------------------------------------------------

def test_all_pass_merges_memory(evo_home):
    mem = EvolutionMemory()
    actor = ActorRole(FakeCaller(response=json_response(memory="波次经验", diagnosis="诊断")))
    wave = Phase1Wave(
        execute=make_execute({"因子A": 0, "因子B": 0, "因子C": 0}),
        verifier=_verifier(),
        actor=actor,
        memory=mem,
        parallel=False,
    )
    result = wave.run(PROJECTS)
    assert result.success is True
    assert result.merged is True
    assert mem.stats()["trajectories"] == 3
    assert mem.stats()["insights_active"] == 3
    assert result.blocked_ids == []


def test_post_hash_changes_after_merge(evo_home):
    mem = EvolutionMemory()
    wave = Phase1Wave(
        execute=make_execute({"因子": 0}),
        verifier=_verifier(),
        actor=ActorRole(FakeCaller(response=json_response(memory="m", diagnosis="d"))),
        memory=mem,
        parallel=False,
    )
    result = wave.run(PROJECTS)
    assert result.merged is True
    assert result.pre_wave_hash != result.post_wave_hash


# --------------------------------------------------------------------------
# wave memory barrier：任一失败 → 整 wave 不合并
# --------------------------------------------------------------------------

def test_one_failure_blocks_whole_wave(evo_home):
    mem = EvolutionMemory()
    actor = ActorRole(FakeCaller(response=json_response(memory="m", diagnosis="d")))
    wave = Phase1Wave(
        execute=make_execute({"因子A": 0, "因子B": 1, "因子C": 0}),  # B 失败
        verifier=_verifier(),
        actor=actor,
        memory=mem,
        parallel=False,
    )
    result = wave.run(PROJECTS)
    assert result.success is False
    assert result.merged is False
    assert result.blocked_ids == ["p2"]
    assert result.passed_ids == ["p1", "p3"]
    # barrier：整 wave 记忆一律不合并
    assert mem.stats()["trajectories"] == 0
    assert mem.stats()["insights_active"] == 0
    assert result.pre_wave_hash == result.post_wave_hash


def test_unverified_branch_blocks_wave(evo_home):
    mem = EvolutionMemory()
    wave = Phase1Wave(
        execute=make_execute({"因子A": 0, "因子C": 0}),  # B 无映射 → 默认失败
        verifier=_verifier(),
        memory=mem,
        parallel=False,
    )
    result = wave.run(PROJECTS)
    assert result.merged is False
    assert "p2" in result.blocked_ids


def test_execute_exception_blocks_wave(evo_home):
    mem = EvolutionMemory()

    def boom(task):
        raise RuntimeError("炸")

    wave = Phase1Wave(execute=boom, verifier=_verifier(), memory=mem, parallel=False)
    result = wave.run(PROJECTS)
    # 异常 → 空证据 → UNVERIFIED → 阻塞
    assert result.merged is False
    assert all(o.verdict.verdict is TargetVerdict.UNVERIFIED for o in result.outcomes)


# --------------------------------------------------------------------------
# 并行 vs 串行一致性 / 边界
# --------------------------------------------------------------------------

def test_parallel_and_serial_same_success(evo_home):
    mem1 = EvolutionMemory(root=evo_home / "m1")
    mem2 = EvolutionMemory(root=evo_home / "m2")
    ok = make_execute({"因子": 0})
    r_serial = Phase1Wave(
        execute=ok, verifier=_verifier(),
        actor=ActorRole(FakeCaller(response=json_response(memory="m", diagnosis="d"))),
        memory=mem1, parallel=False,
    ).run(PROJECTS)
    r_parallel = Phase1Wave(
        execute=ok, verifier=_verifier(),
        actor=ActorRole(FakeCaller(response=json_response(memory="m", diagnosis="d"))),
        memory=mem2, parallel=True, max_workers=3,
    ).run(PROJECTS)
    assert r_serial.success == r_parallel.success is True
    assert r_serial.passed_ids == r_parallel.passed_ids


def test_empty_wave(evo_home):
    result = Phase1Wave(verifier=_verifier()).run([])
    assert result.success is False
    assert result.merged is False
    assert result.outcomes == []
    assert "空 wave" in result.reason


def test_projects_accept_plain_strings(evo_home):
    mem = EvolutionMemory()
    wave = Phase1Wave(
        execute=make_execute({"任务": 0}),
        verifier=_verifier(),
        actor=ActorRole(FakeCaller(response=json_response(memory="m", diagnosis="d"))),
        memory=mem,
        parallel=False,
    )
    result = wave.run(["任务1", "任务2"])
    assert result.success is True
    assert [o.task_id for o in result.outcomes] == ["wave0", "wave1"]


def test_commit_memory_disabled(evo_home):
    mem = EvolutionMemory()
    wave = Phase1Wave(
        execute=make_execute({"因子": 0}),
        verifier=_verifier(),
        actor=ActorRole(FakeCaller(response=json_response(memory="m", diagnosis="d"))),
        memory=mem,
        parallel=False,
        commit_memory=False,
    )
    result = wave.run(PROJECTS)
    assert result.success is True
    assert result.merged is False
    assert mem.stats()["trajectories"] == 0


def test_to_dict_shape(evo_home):
    wave = Phase1Wave(
        execute=make_execute({"因子": 0}),
        verifier=_verifier(),
        actor=ActorRole(FakeCaller(response=json_response(memory="m", diagnosis="d"))),
        memory=EvolutionMemory(),
        parallel=False,
    )
    d = wave.run(PROJECTS).to_dict()
    assert d["success"] is True
    assert len(d["branches"]) == 3
    assert d["branches"][0]["verdict"] == "PASS"


def test_run_wave_entry(evo_home):
    mem = EvolutionMemory()
    result = run_wave(
        PROJECTS,
        execute=make_execute({"因子": 0}),
        verifier=_verifier(),
        actor=ActorRole(FakeCaller(response=json_response(memory="m", diagnosis="d"))),
        memory=mem,
        parallel=False,
    )
    assert isinstance(result, WaveResult)
    assert result.success is True
