"""CheckpointManager 测试 —— blob 去重 / CRUD / restore / diff / cleanup / gc / 容错。"""

from spirit.checkpoint import paths


def _write(p, text):
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# create / get / list / latest
# --------------------------------------------------------------------------

def test_create_single_file(manager, sample_file):
    res = manager.create([str(sample_file)], description="初始版本")
    assert res["success"] is True
    cp = res["checkpoint"]
    assert cp["file_count"] == 1
    assert cp["description"] == "初始版本"
    assert cp["scope"] == "global"
    assert cp["files"][0]["name"] == "a.py"
    assert cp["id"].startswith("cp_")


def test_create_empty_list_fails(manager):
    res = manager.create([])
    assert res["success"] is False
    assert "error" in res


def test_create_all_missing_fails(manager, workspace):
    res = manager.create([str(workspace / "nope.py")])
    assert res["success"] is False


def test_create_skips_missing_keeps_present(manager, sample_file, workspace):
    missing = workspace / "gone.py"
    res = manager.create([str(sample_file), str(missing)])
    assert res["success"] is True
    assert res["checkpoint"]["file_count"] == 1


def test_create_persists_to_index(manager, sample_file):
    manager.create([str(sample_file)])
    assert paths.index_path().exists()
    # 新 manager 应能从磁盘读回
    from spirit.checkpoint.manager import CheckpointManager
    fresh = CheckpointManager()
    assert len(fresh.list_checkpoints()) == 1


def test_get_and_list_and_latest(manager, workspace):
    f1 = _write(workspace / "f1.py", "1")
    f2 = _write(workspace / "f2.py", "2")
    r1 = manager.create([str(f1)], description="first")
    r2 = manager.create([str(f2)], description="second")
    assert manager.get(r1["checkpoint"]["id"])["description"] == "first"
    assert manager.get("nope") is None

    listed = manager.list_checkpoints()
    # 新→旧
    assert listed[0]["id"] == r2["checkpoint"]["id"]
    assert listed[1]["id"] == r1["checkpoint"]["id"]
    assert manager.latest()["id"] == r2["checkpoint"]["id"]


def test_list_scope_filter(manager, workspace):
    f1 = _write(workspace / "f1.py", "1")
    f2 = _write(workspace / "f2.py", "2")
    manager.create([str(f1)], scope="work")
    manager.create([str(f2)], scope="other")
    assert len(manager.list_checkpoints(scope="work")) == 1
    assert manager.latest(scope="work")["scope"] == "work"
    assert manager.latest(scope="other")["scope"] == "other"


def test_list_limit(manager, workspace):
    for i in range(5):
        f = _write(workspace / f"f{i}.py", str(i))
        manager.create([str(f)])
    assert len(manager.list_checkpoints(limit=2)) == 2
    assert len(manager.list_checkpoints()) == 5


# --------------------------------------------------------------------------
# blob 去重
# --------------------------------------------------------------------------

def test_blob_dedup_same_content(manager, workspace):
    """相同内容的两个文件应共享同一 blob。"""
    a = _write(workspace / "a.py", "same")
    b = _write(workspace / "b.py", "same")
    manager.create([str(a), str(b)])
    stats = manager.store_stats()
    assert stats["blob_count"] == 1  # 内容寻址去重


def test_blob_distinct_content(manager, workspace):
    a = _write(workspace / "a.py", "one")
    b = _write(workspace / "b.py", "two")
    manager.create([str(a), str(b)])
    assert manager.store_stats()["blob_count"] == 2


# --------------------------------------------------------------------------
# restore + 安全网
# --------------------------------------------------------------------------

def test_restore_rolls_back_content(manager, sample_file):
    cp = manager.create([str(sample_file)])["checkpoint"]
    _write(sample_file, "print('v2')\n")
    assert sample_file.read_text(encoding="utf-8") == "print('v2')\n"

    res = manager.restore(cp["id"])
    assert res["success"] is True
    assert res["count"] == 1
    assert sample_file.read_text(encoding="utf-8") == "print('v1')\n"


def test_restore_creates_pre_rollback_safety_net(manager, sample_file):
    cp = manager.create([str(sample_file)])["checkpoint"]
    _write(sample_file, "print('v2')\n")
    manager.restore(cp["id"])
    safety = sample_file.with_suffix(".py.pre-rollback")
    assert safety.exists()
    assert safety.read_text(encoding="utf-8") == "print('v2')\n"


def test_restore_recreates_deleted_file(manager, sample_file):
    cp = manager.create([str(sample_file)])["checkpoint"]
    sample_file.unlink()
    res = manager.restore(cp["id"])
    assert res["success"] is True
    assert sample_file.read_text(encoding="utf-8") == "print('v1')\n"


def test_restore_unknown_id(manager):
    res = manager.restore("cp_missing")
    assert res["success"] is False


# --------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------

def test_diff_unchanged(manager, sample_file):
    cp = manager.create([str(sample_file)])["checkpoint"]
    res = manager.diff(cp["id"])
    assert res["success"] is True
    assert res["changed"] is False
    assert len(res["unchanged"]) == 1


def test_diff_modified(manager, sample_file):
    cp = manager.create([str(sample_file)])["checkpoint"]
    _write(sample_file, "print('v2')\n")
    res = manager.diff(cp["id"])
    assert res["changed"] is True
    assert len(res["modified"]) == 1


def test_diff_missing(manager, sample_file):
    cp = manager.create([str(sample_file)])["checkpoint"]
    sample_file.unlink()
    res = manager.diff(cp["id"])
    assert res["changed"] is True
    assert len(res["missing"]) == 1


def test_diff_unknown_id(manager):
    assert manager.diff("nope")["success"] is False


# --------------------------------------------------------------------------
# delete / cleanup / gc
# --------------------------------------------------------------------------

def test_delete_removes_and_gcs_blob(manager, sample_file):
    cp = manager.create([str(sample_file)])["checkpoint"]
    assert manager.store_stats()["blob_count"] == 1
    res = manager.delete(cp["id"])
    assert res["success"] is True
    assert manager.get(cp["id"]) is None
    assert manager.store_stats()["blob_count"] == 0  # 孤儿 blob 已回收


def test_delete_keeps_shared_blob(manager, workspace):
    """两个快照引用同一 blob，删一个后 blob 仍在。"""
    a = _write(workspace / "a.py", "content")
    cp1 = manager.create([str(a)])["checkpoint"]
    cp2 = manager.create([str(a)])["checkpoint"]
    manager.delete(cp1["id"])
    assert manager.store_stats()["blob_count"] == 1
    assert manager.get(cp2["id"]) is not None


def test_delete_unknown(manager):
    assert manager.delete("nope")["success"] is False


def test_cleanup_keeps_recent(manager, workspace):
    ids = []
    for i in range(5):
        f = _write(workspace / f"f{i}.py", f"c{i}")
        ids.append(manager.create([str(f)])["checkpoint"]["id"])
    res = manager.cleanup(keep=2)
    assert res["removed"] == 3
    assert res["kept"] == 2
    remaining = [c["id"] for c in manager.list_checkpoints()]
    assert remaining == [ids[4], ids[3]]


def test_cleanup_noop_when_under_keep(manager, workspace):
    f = _write(workspace / "a.py", "x")
    manager.create([str(f)])
    res = manager.cleanup(keep=20)
    assert res["removed"] == 0


def test_cleanup_by_scope(manager, workspace):
    work_ids = []
    for i in range(4):
        f = _write(workspace / f"w{i}.py", f"w{i}")
        work_ids.append(manager.create([str(f)], scope="work")["checkpoint"]["id"])
    other = _write(workspace / "o.py", "o")
    other_id = manager.create([str(other)], scope="other")["checkpoint"]["id"]

    res = manager.cleanup(keep=1, scope="work")
    assert res["removed"] == 3
    # other scope 未受影响
    assert manager.get(other_id) is not None
    assert len(manager.list_checkpoints(scope="work")) == 1


# --------------------------------------------------------------------------
# 索引容错 / 缓存
# --------------------------------------------------------------------------

def test_corrupt_index_resets_empty(manager, spirit_home):
    paths.checkpoints_root().mkdir(parents=True, exist_ok=True)
    paths.index_path().write_text("{not valid json", encoding="utf-8")
    assert manager.list_checkpoints() == []


def test_invalidate_cache_rereads_disk(manager, sample_file):
    manager.create([str(sample_file)])
    from spirit.checkpoint.manager import CheckpointManager
    other = CheckpointManager()
    assert len(other.list_checkpoints()) == 1
    # other 写入后，manager 需 invalidate 才能看到
    f2 = sample_file.parent / "b.py"
    _write(f2, "b")
    other.create([str(f2)])
    assert len(manager.list_checkpoints()) == 1  # 缓存未失效
    manager.invalidate_cache()
    assert len(manager.list_checkpoints()) == 2


def test_store_stats_counts(manager, workspace):
    a = _write(workspace / "a.py", "hello")
    manager.create([str(a)])
    stats = manager.store_stats()
    assert stats["checkpoint_count"] == 1
    assert stats["blob_count"] == 1
    assert stats["total_bytes"] == len(b"hello")
