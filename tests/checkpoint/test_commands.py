"""``/checkpoint`` 命令分发测试 —— verb 别名 / flag 解析 / fail-soft。"""

from spirit.checkpoint.commands import _parse_flags, handle_checkpoint_command


def _write(p, text):
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# _parse_flags
# --------------------------------------------------------------------------

def test_parse_flags_positional_and_value_flags():
    pos, flags = _parse_flags(["a.py", "b.py", "--desc", "hello", "--scope", "work"])
    assert pos == ["a.py", "b.py"]
    assert flags["desc"] == "hello"
    assert flags["scope"] == "work"


def test_parse_flags_int_flags():
    pos, flags = _parse_flags(["--keep", "5", "--limit", "3"])
    assert flags["keep"] == 5
    assert flags["limit"] == 3


def test_parse_flags_bad_int_becomes_none():
    _, flags = _parse_flags(["--keep", "abc"])
    assert flags["keep"] is None


def test_parse_flags_boolean_flag():
    _, flags = _parse_flags(["--force"])
    assert flags["force"] is True


def test_parse_flags_description_alias_normalized():
    _, flags = _parse_flags(["--description", "x"])
    assert flags["desc"] == "x"


def test_parse_flags_dangling_value_flag():
    pos, flags = _parse_flags(["a.py", "--desc"])
    assert pos == ["a.py"]
    assert "desc" not in flags


# --------------------------------------------------------------------------
# 顶层分发
# --------------------------------------------------------------------------

def test_empty_arg_returns_help(manager):
    res = handle_checkpoint_command("", manager)
    assert res["ok"] is False
    assert res["action"] == "help"


def test_unknown_verb(manager):
    res = handle_checkpoint_command("frobnicate", manager)
    assert res["ok"] is False
    assert res["action"] == "unknown"


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------

def test_cmd_snapshot(manager, sample_file):
    res = handle_checkpoint_command(
        f'snapshot "{sample_file}" --desc "v1" --scope work', manager
    )
    assert res["ok"] is True
    assert res["action"] == "snapshot"
    assert res["checkpoint"]["scope"] == "work"
    assert any("v1" in ln for ln in res["lines"])


def test_cmd_snapshot_alias_create(manager, sample_file):
    res = handle_checkpoint_command(f'create "{sample_file}"', manager)
    assert res["ok"] is True
    assert res["action"] == "snapshot"


def test_cmd_snapshot_no_files(manager):
    res = handle_checkpoint_command("snapshot", manager)
    assert res["ok"] is False


def test_cmd_snapshot_missing_file_fails(manager, workspace):
    res = handle_checkpoint_command(f'snapshot "{workspace / "gone.py"}"', manager)
    assert res["ok"] is False


# --------------------------------------------------------------------------
# list / latest / stats
# --------------------------------------------------------------------------

def test_cmd_list_empty(manager):
    res = handle_checkpoint_command("list", manager)
    assert res["ok"] is True
    assert res["message"] == "暂无快照"


def test_cmd_list_with_entries(manager, sample_file):
    handle_checkpoint_command(f'snapshot "{sample_file}"', manager)
    res = handle_checkpoint_command("ls", manager)
    assert res["ok"] is True
    assert len(res["checkpoints"]) == 1


def test_cmd_latest_empty(manager):
    res = handle_checkpoint_command("latest", manager)
    assert res["ok"] is True
    assert res["message"] == "暂无快照"


def test_cmd_latest(manager, sample_file):
    handle_checkpoint_command(f'snapshot "{sample_file}" --desc "d"', manager)
    res = handle_checkpoint_command("last", manager)
    assert res["ok"] is True
    assert res["checkpoint"]["description"] == "d"


def test_cmd_stats(manager, sample_file):
    handle_checkpoint_command(f'snapshot "{sample_file}"', manager)
    res = handle_checkpoint_command("stats", manager)
    assert res["ok"] is True
    assert res["stats"]["checkpoint_count"] == 1


# --------------------------------------------------------------------------
# restore / diff / delete
# --------------------------------------------------------------------------

def test_cmd_restore_roundtrip(manager, sample_file):
    snap = handle_checkpoint_command(f'snapshot "{sample_file}"', manager)
    cp_id = snap["checkpoint"]["id"]
    _write(sample_file, "changed\n")
    res = handle_checkpoint_command(f"restore {cp_id}", manager)
    assert res["ok"] is True
    assert sample_file.read_text(encoding="utf-8") == "print('v1')\n"


def test_cmd_restore_no_id(manager):
    assert handle_checkpoint_command("restore", manager)["ok"] is False


def test_cmd_restore_unknown_id(manager):
    assert handle_checkpoint_command("restore cp_nope", manager)["ok"] is False


def test_cmd_diff(manager, sample_file):
    snap = handle_checkpoint_command(f'snapshot "{sample_file}"', manager)
    cp_id = snap["checkpoint"]["id"]
    _write(sample_file, "changed\n")
    res = handle_checkpoint_command(f"diff {cp_id}", manager)
    assert res["ok"] is True
    assert res["message"] == "有变更"


def test_cmd_diff_no_id(manager):
    assert handle_checkpoint_command("diff", manager)["ok"] is False


def test_cmd_delete(manager, sample_file):
    snap = handle_checkpoint_command(f'snapshot "{sample_file}"', manager)
    cp_id = snap["checkpoint"]["id"]
    res = handle_checkpoint_command(f"rm {cp_id}", manager)
    assert res["ok"] is True
    assert manager.get(cp_id) is None


def test_cmd_delete_no_id(manager):
    assert handle_checkpoint_command("delete", manager)["ok"] is False


# --------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------

def test_cmd_cleanup_keep(manager, workspace):
    for i in range(4):
        f = _write(workspace / f"f{i}.py", f"c{i}")
        handle_checkpoint_command(f'snapshot "{f}"', manager)
    res = handle_checkpoint_command("cleanup --keep 2", manager)
    assert res["ok"] is True
    assert len(manager.list_checkpoints()) == 2


def test_cmd_cleanup_default_keep(manager, sample_file):
    handle_checkpoint_command(f'snapshot "{sample_file}"', manager)
    res = handle_checkpoint_command("clean", manager)
    assert res["ok"] is True


# --------------------------------------------------------------------------
# fail-soft：命令层绝不抛
# --------------------------------------------------------------------------

def test_command_never_raises_on_broken_manager():
    """即使 manager 内部抛异常，命令层也应返回结构化错误。"""

    class Boom:
        def list_checkpoints(self, *a, **k):
            raise RuntimeError("boom")

    res = handle_checkpoint_command("list", Boom())
    assert res["ok"] is False
    assert "执行失败" in res["message"]
