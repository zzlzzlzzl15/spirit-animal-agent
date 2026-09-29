"""Sandbox 沙箱写入护栏测试 —— Spirit Autonomy Phase 7.A。"""

import pytest

from spirit.autonomy.sandbox import Sandbox, SandboxViolation


def test_write_read_roundtrip(tmp_path):
    sb = Sandbox(root=tmp_path / "sandbox")
    target = sb.write_text("notes/hello.md", "# hi\n")
    assert target.name == "hello.md"
    assert sb.exists("notes/hello.md")
    assert sb.read_text("notes/hello.md") == "# hi\n"


def test_nested_dirs_created(tmp_path):
    sb = Sandbox(root=tmp_path / "sandbox")
    sb.write_text("a/b/c/deep.txt", "deep")
    assert sb.read_text("a/b/c/deep.txt") == "deep"
    names = sb.list("a/b")
    assert "a/b/c/deep.txt" in names


def test_path_traversal_blocked(tmp_path):
    sb = Sandbox(root=tmp_path / "sandbox")
    with pytest.raises(SandboxViolation):
        sb.write_text("../escape.txt", "nope")
    with pytest.raises(SandboxViolation):
        sb.read_text("../../etc/passwd")


def test_absolute_path_outside_blocked(tmp_path):
    sb = Sandbox(root=tmp_path / "sandbox")
    outside = tmp_path / "outside.txt"
    with pytest.raises(SandboxViolation):
        sb.write_text(str(outside), "nope")


def test_list_and_exists(tmp_path):
    sb = Sandbox(root=tmp_path / "sandbox")
    sb.write_text("x/one.txt", "1")
    sb.write_text("x/two.txt", "2")
    names = sb.list("x")
    assert set(names) >= {"x/one.txt", "x/two.txt"}
    assert sb.exists("x/one.txt")
    assert not sb.exists("x/missing.txt")
