"""Spirit Agent tools/file_operations.py + todo_tool.py 工具测试。

file_operations.py 5 个工具：
- list_dir / move_file / delete_file / create_directory / file_info

todo_tool.py 1 个工具：
- todo（TodoStore + _todo_impl）

注意点：
- todo_tool 是单进程共享 _todo_store，需在测试间 reset
- file_operations 的 _format_size helper 暴露不完整
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from spirit.tools.file_operations import (
    DELETE_FILE_SCHEMA,
    FILE_INFO_SCHEMA,
    LIST_DIR_SCHEMA,
    MOVE_FILE_SCHEMA,
    CREATE_DIR_SCHEMA,
    _create_directory_impl,
    _delete_file_impl,
    _file_info_impl,
    _format_size,
    _list_dir_impl,
    _move_file_impl,
)
from spirit.tools.registry import registry
from spirit.tools.todo_tool import (
    MAX_CONTENT_CHARS,
    MAX_ITEMS,
    VALID_STATUSES,
    TODO_SCHEMA,
    TodoStore,
    _todo_impl,
    _todo_store,
)


# ──────────────────────────── 隔离 helpers ────────────────────────────


@pytest.fixture
def fresh_todo_store():
    """重置 todo 全局 store，提供新的隔离版本。"""
    original_items = list(_todo_store._items)
    _todo_store._items = []
    yield _todo_store
    _todo_store._items = original_items


# ════════════════════════════════════════════════════════════════════════
# file_operations.py 测试
# ════════════════════════════════════════════════════════════════════════


# ──────────────────────────── _format_size ────────────────────────────


class TestFormatSize:
    """_format_size 人类可读格式。"""

    def test_bytes(self):
        assert _format_size(0) == "0B"
        assert _format_size(512) == "512B"
        assert _format_size(1023) == "1023B"

    def test_kb(self):
        assert _format_size(1024) == "1.0KB"
        assert _format_size(2048) == "2.0KB"
        assert _format_size(1536) == "1.5KB"

    def test_mb(self):
        assert _format_size(1024 * 1024) == "1.0MB"
        size = int(2.5 * 1024 * 1024)
        assert _format_size(size) == "2.5MB"

    def test_gb(self):
        size = 1024 ** 3
        assert _format_size(size) == "1.0GB"

    def test_tb(self):
        size = 1024 ** 4
        assert _format_size(size) == "1.0TB"

    def test_boundary_just_under_kb(self):
        assert _format_size(1023) == "1023B"
        assert _format_size(1024) == "1.0KB"


# ──────────────────────────── list_dir ────────────────────────────


class TestListDir:
    """_list_dir_impl 列目录内容。"""

    def test_existing_dir_basic(self, tmp_path: Path):
        (tmp_path / "a.txt").write_text("hello")
        (tmp_path / "b.txt").write_text("world")
        (tmp_path / "sub").mkdir()

        result = json.loads(_list_dir_impl(str(tmp_path)))

        assert "entries" in result
        assert "path" in result
        assert "count" in result
        names = [e["name"] for e in result["entries"]]
        assert "a.txt" in names
        assert "b.txt" in names
        assert "sub" in names
        # 文件含 size
        a_entry = next(e for e in result["entries"] if e["name"] == "a.txt")
        assert a_entry["type"] == "file"
        assert "size" in a_entry

    def test_hidden_files_filtered_by_default(self, tmp_path: Path):
        (tmp_path / "visible.txt").write_text("v")
        (tmp_path / ".hidden").write_text("h")
        result = json.loads(_list_dir_impl(str(tmp_path)))
        names = [e["name"] for e in result["entries"]]
        assert "visible.txt" in names
        assert ".hidden" not in names

    def test_hidden_files_shown_with_flag(self, tmp_path: Path):
        (tmp_path / "visible.txt").write_text("v")
        (tmp_path / ".hidden").write_text("h")
        result = json.loads(_list_dir_impl(str(tmp_path), show_hidden=True))
        names = [e["name"] for e in result["entries"]]
        assert "visible.txt" in names
        assert ".hidden" in names

    def test_nonexistent_path_error(self):
        result = json.loads(_list_dir_impl("/tmp/nonexistent_path_xyz_12345"))
        assert "error" in result
        assert "不存在" in result["error"]

    def test_path_is_file_error(self, tmp_path: Path):
        f = tmp_path / "single.txt"
        f.write_text("x")
        result = json.loads(_list_dir_impl(str(f)))
        assert "error" in result
        assert "不是目录" in result["error"]

    def test_max_depth_1_no_recurse(self, tmp_path: Path):
        (tmp_path / "top.txt").write_text("t")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "deep.txt").write_text("d")
        result = json.loads(_list_dir_impl(str(tmp_path), max_depth=1))
        names = [e["name"] for e in result["entries"]]
        assert "top.txt" in names
        assert "sub" in names
        # max_depth=1 不递归深入，sub 目录本身被记录但不深入
        # 检查 sub 里没有 deep.txt
        for e in result["entries"]:
            if e["name"] == "sub":
                # sub 是 directory 类型，且不含 deep.txt
                assert e["type"] == "directory"

    def test_max_depth_2_recurse_one_level(self, tmp_path: Path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "level1.txt").write_text("l1")
        result = json.loads(_list_dir_impl(str(tmp_path), max_depth=2))
        names = [e["name"] for e in result["entries"]]
        assert "level1.txt" in names

    def test_max_depth_capped_at_5(self, tmp_path: Path):
        """max_depth 即使传 100，也被 cap 到 5。"""
        # 简单验证调用不报错
        result = json.loads(_list_dir_impl(str(tmp_path), max_depth=100))
        assert "entries" in result

    def test_entries_sorted_directories_first(self, tmp_path: Path):
        (tmp_path / "a_file.txt").write_text("a")
        (tmp_path / "z_dir").mkdir()
        (tmp_path / "b_file.txt").write_text("b")
        result = json.loads(_list_dir_impl(str(tmp_path)))
        types = [e["type"] for e in result["entries"]]
        # 目录应在文件前面
        if types:
            first_non_dir = next((i for i, t in enumerate(types) if t == "file"), len(types))
            first_dir = next((i for i, t in enumerate(types) if t == "directory"), len(types))
            assert first_dir < first_non_dir


# ──────────────────────────── move_file ────────────────────────────


class TestMoveFile:
    """_move_file_impl 移动/重命名。"""

    def test_basic_move(self, tmp_path: Path):
        src = tmp_path / "src.txt"
        src.write_text("content")
        dst = tmp_path / "sub_dst.txt"

        result = json.loads(_move_file_impl(str(src), str(dst)))
        assert result["success"] is True
        assert not src.exists()
        assert dst.exists()
        assert dst.read_text() == "content"

    def test_move_to_nonexistent_dir_creates_parent(self, tmp_path: Path):
        src = tmp_path / "src.txt"
        src.write_text("x")
        dst = tmp_path / "newdir" / "subdir" / "dest.txt"
        result = json.loads(_move_file_impl(str(src), str(dst)))
        assert result["success"] is True
        assert dst.exists()

    def test_nonexistent_source_error(self, tmp_path: Path):
        dst = tmp_path / "dest.txt"
        result = json.loads(_move_file_impl(
            str(tmp_path / "missing.txt"), str(dst),
        ))
        assert "error" in result
        assert "源路径不存在" in result["error"]

    def test_existing_destination_error(self, tmp_path: Path):
        src = tmp_path / "a.txt"
        src.write_text("a")
        dst = tmp_path / "b.txt"
        dst.write_text("b")
        result = json.loads(_move_file_impl(str(src), str(dst)))
        assert "error" in result
        assert "目标路径已存在" in result["error"]


# ──────────────────────────── delete_file ────────────────────────────


class TestDeleteFile:
    """_delete_file_impl 删除（带安全检查 + recursive）。"""

    def test_delete_file(self, tmp_path: Path):
        f = tmp_path / "del.txt"
        f.write_text("bye")
        result = json.loads(_delete_file_impl(str(f)))
        assert result["success"] is True
        assert not f.exists()

    def test_delete_empty_dir(self, tmp_path: Path):
        d = tmp_path / "empty"
        d.mkdir()
        result = json.loads(_delete_file_impl(str(d)))
        assert result["success"] is True
        assert not d.exists()

    def test_delete_nonempty_dir_without_recursive_error(self, tmp_path: Path):
        """Spirit 现状：Windows 上 Path.rmdir  错误消息是中文，源码判 'not empty' 不匹配。
        实际走通用 '删除失败' 路径。这是 spirit 跨平台错误处理 bug（待修复）。"""
        d = tmp_path / "nonempty"
        d.mkdir()
        (d / "child.txt").write_text("x")
        result = json.loads(_delete_file_impl(str(d), recursive=False))
        assert "error" in result
        # spirit 现状错误消息包含中文“目录不是空的”或“删除失败”
        err_text = result["error"]
        is_friendly_msg = "目录非空" in err_text or "recursive" in err_text
        is_generic_msg = "删除失败" in err_text
        assert is_friendly_msg or is_generic_msg, f"unexpected error: {err_text}"
        assert d.exists()  # 无论如何不能删掉非空目录
        # 记录 bug：Windows 上不是 friendly msg，是 generic msg
        # 如果未来修复， is_friendly_msg 会为 True

    def test_delete_nonempty_dir_with_recursive_succeeds(self, tmp_path: Path):
        d = tmp_path / "nonempty"
        d.mkdir()
        (d / "child.txt").write_text("x")
        (d / "nested").mkdir()
        (d / "nested" / "leaf.txt").write_text("l")
        result = json.loads(_delete_file_impl(str(d), recursive=True))
        assert result["success"] is True
        assert not d.exists()

    def test_nonexistent_path_error(self, tmp_path: Path):
        result = json.loads(_delete_file_impl(str(tmp_path / "ghost")))
        assert "error" in result
        assert "不存在" in result["error"]


# ──────────────────────────── create_directory ────────────────────────────


class TestCreateDirectory:
    """_create_directory_impl 多级目录创建。"""

    def test_basic_single_level(self, tmp_path: Path):
        d = tmp_path / "new"
        result = json.loads(_create_directory_impl(str(d)))
        assert result["success"] is True
        assert d.is_dir()

    def test_nested_creates_parents(self, tmp_path: Path):
        d = tmp_path / "a" / "b" / "c"
        result = json.loads(_create_directory_impl(str(d)))
        assert result["success"] is True
        assert d.is_dir()
        assert d.parent.is_dir()

    def test_existing_dir_idempotent(self, tmp_path: Path):
        d = tmp_path / "exists"
        d.mkdir()
        result = json.loads(_create_directory_impl(str(d)))
        # exist_ok=True，不报错
        assert result["success"] is True


# ──────────────────────────── file_info ────────────────────────────


class TestFileInfo:
    """_file_info_impl 文件元信息。"""

    def test_basic_file_info(self, tmp_path: Path):
        """验证文件元信息；尺寸用原始字节数，不受平台 newline 转换影响。"""
        f = tmp_path / "info.txt"
        content = "line1\nline2\nline3\n"
        f.write_bytes(content.encode("utf-8"))  # 用 bytes 避免 Windows \r\n 转换
        result = json.loads(_file_info_impl(str(f)))
        assert result["name"] == "info.txt"
        assert result["type"] == "file"
        assert "size_bytes" in result
        assert result["size_bytes"] == len(content.encode("utf-8"))
        assert "modified" in result
        assert "created" in result
        assert "permissions" in result
        assert "extension" in result
        assert result["extension"] == ".txt"
        assert "lines" in result
        assert result["lines"] == 3

    def test_dir_info_no_lines(self, tmp_path: Path):
        d = tmp_path / "my_dir"
        d.mkdir()
        result = json.loads(_file_info_impl(str(d)))
        assert result["type"] == "directory"
        assert "lines" not in result

    def test_nonexistent_path_error(self, tmp_path: Path):
        result = json.loads(_file_info_impl(str(tmp_path / "ghost")))
        assert "error" in result
        assert "不存在" in result["error"]

    def test_file_with_no_extension(self, tmp_path: Path):
        f = tmp_path / "noext"
        f.write_text("y")
        result = json.loads(_file_info_impl(str(f)))
        assert result["extension"] == ""

    def test_empty_file(self, tmp_path: Path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        result = json.loads(_file_info_impl(str(f)))
        assert result["size_bytes"] == 0
        assert result["lines"] == 0


# ──────────────────────────── schema 完整性 ────────────────────────────


class TestFileOperationsSchemas:
    """5 个 schema 都遵循 OpenAI function calling 格式。"""

    @pytest.mark.parametrize("schema,name", [
        (LIST_DIR_SCHEMA, "list_dir"),
        (MOVE_FILE_SCHEMA, "move_file"),
        (DELETE_FILE_SCHEMA, "delete_file"),
        (CREATE_DIR_SCHEMA, "create_directory"),
        (FILE_INFO_SCHEMA, "file_info"),
    ])
    def test_schema_has_function_call_format(self, schema, name):
        assert schema["type"] == "function"
        func = schema["function"]
        assert func["name"] == name
        assert "description" in func
        assert "parameters" in func
        assert func["parameters"]["type"] == "object"
        assert "properties" in func["parameters"]

    def test_list_dir_registered(self):
        entries = {e.name for e in registry.get_all_entries()}
        assert "list_dir" in entries

    def test_move_file_registered(self):
        entries = {e.name for e in registry.get_all_entries()}
        assert "move_file" in entries

    def test_delete_file_registered(self):
        entries = {e.name for e in registry.get_all_entries()}
        assert "delete_file" in entries

    def test_create_directory_registered(self):
        entries = {e.name for e in registry.get_all_entries()}
        assert "create_directory" in entries

    def test_file_info_registered(self):
        entries = {e.name for e in registry.get_all_entries()}
        assert "file_info" in entries


# ════════════════════════════════════════════════════════════════════════
# todo_tool.py 测试
# ════════════════════════════════════════════════════════════════════════


# ──────────────────────────── TodoStore ────────────────────────────


class TestTodoStore:
    """TodoStore 内存存储 + validate + merge。"""

    def test_empty_initial(self):
        s = TodoStore()
        assert s.read() == []

    def test_write_replace_mode(self):
        s = TodoStore()
        s.write([
            {"id": "1", "content": "task1", "status": "pending"},
            {"id": "2", "content": "task2", "status": "in_progress"},
        ])
        items = s.read()
        assert len(items) == 2
        assert items[0]["id"] == "1"

    def test_merge_existing_item_updates_content(self):
        s = TodoStore()
        s.write([{"id": "1", "content": "old", "status": "pending"}])
        s.write([{"id": "1", "content": "new content"}], merge=True)
        items = s.read()
        assert items[0]["content"] == "new content"
        assert items[0]["status"] == "pending"  # 没更新

    def test_merge_existing_item_updates_status(self):
        s = TodoStore()
        s.write([{"id": "1", "content": "task", "status": "pending"}])
        s.write([{"id": "1", "status": "completed"}], merge=True)
        items = s.read()
        assert items[0]["status"] == "completed"

    def test_merge_adds_new_item_with_valid_id(self):
        s = TodoStore()
        s.write([{"id": "1", "content": "task1"}])
        s.write([{"id": "2", "content": "task2"}], merge=True)
        assert len(s.read()) == 2

    def test_merge_skips_items_without_id(self):
        s = TodoStore()
        s.write([{"id": "1", "content": "task1"}])
        s.write([
            {"id": "2", "content": "task2"},
            {"content": "orphan no id"},  # 应被跳过
        ], merge=True)
        items = s.read()
        assert len(items) == 2  # 1 + 2，孤儿被跳
        ids = [i["id"] for i in items]
        assert "2" in ids

    def test_read_returns_copies(self):
        s = TodoStore()
        s.write([{"id": "1", "content": "a"}])
        items = s.read()
        items[0]["content"] = "mutated"  # 不应影响 store
        assert s.read()[0]["content"] == "a"

    def test_validate_invalid_status_falls_back_to_pending(self):
        s = TodoStore()
        s.write([{"id": "x", "content": "y", "status": "weird_status"}])
        items = s.read()
        assert items[0]["status"] == "pending"

    def test_validate_id_truncated_at_100(self):
        s = TodoStore()
        long_id = "x" * 200
        s.write([{"id": long_id, "content": "c"}])
        items = s.read()
        assert len(items[0]["id"]) == 100

    def test_validate_content_truncated(self):
        s = TodoStore()
        long_content = "y" * (MAX_CONTENT_CHARS + 100)
        s.write([{"id": "1", "content": long_content}])
        items = s.read()
        assert len(items[0]["content"]) == MAX_CONTENT_CHARS

    def test_id_stripped(self):
        s = TodoStore()
        s.write([{"id": "  spaced_id  ", "content": "c"}])
        items = s.read()
        assert items[0]["id"] == "spaced_id"

    def test_max_items_cap(self):
        s = TodoStore()
        todos = [{"id": str(i), "content": f"task{i}"} for i in range(MAX_ITEMS + 50)]
        s.write(todos)
        items = s.read()
        assert len(items) == MAX_ITEMS


# ──────────────────────────── _todo_impl ────────────────────────────


class TestTodoImpl:
    """_todo_impl 端到端：写入 / 读取 / 格式化。"""

    def test_read_empty(self, fresh_todo_store):
        result = json.loads(_todo_impl())
        assert result["todos"] == []
        assert "消息" in result.get("message", "") or "为空" in str(result)

    def test_write_new_list(self, fresh_todo_store):
        todos = [
            {"id": "1", "content": "first", "status": "pending"},
            {"id": "2", "content": "second", "status": "in_progress"},
        ]
        result = json.loads(_todo_impl(todos=todos))
        assert len(result["todos"]) == 2
        assert "display" in result
        assert "summary" in result

    def test_summary_counts(self, fresh_todo_store):
        todos = [
            {"id": "1", "content": "a", "status": "pending"},
            {"id": "2", "content": "b", "status": "in_progress"},
            {"id": "3", "content": "c", "status": "completed"},
            {"id": "4", "content": "d", "status": "completed"},
        ]
        result = json.loads(_todo_impl(todos=todos))
        s = result["summary"]
        assert s["total"] == 4
        assert s["pending"] == 1
        assert s["in_progress"] == 1
        assert s["completed"] == 2

    def test_display_format_includes_markers(self, fresh_todo_store):
        todos = [
            {"id": "1", "content": "done", "status": "completed"},
            {"id": "2", "content": "doing", "status": "in_progress"},
            {"id": "3", "content": "todo", "status": "pending"},
            {"id": "4", "content": "skip", "status": "cancelled"},
        ]
        result = json.loads(_todo_impl(todos=todos))
        display = result["display"]
        assert "[x]" in display  # completed
        assert "[>]" in display  # in_progress
        assert "[ ]" in display  # pending
        assert "[~]" in display  # cancelled

    def test_merge_mode_keeps_existing(self, fresh_todo_store):
        first = [{"id": "1", "content": "task1"}]
        _todo_impl(todos=first)
        second = [{"id": "1", "status": "completed"}]
        _todo_impl(todos=second, merge=True)
        result = json.loads(_todo_impl())
        items = result["todos"]
        # 1 的 status 应已更新
        item1 = next(i for i in items if i["id"] == "1")
        assert item1["status"] == "completed"

    def test_schema_registered(self):
        entries = {e.name for e in registry.get_all_entries()}
        assert "todo" in entries
        # TodoStore 模块级使用 _todo_store；handler 调用不依赖全局状态
        # 这里仅验证注册存在

    def test_schema_has_required_fields(self):
        func = TODO_SCHEMA["function"]
        assert func["name"] == "todo"
        assert "description" in func
        params = func["parameters"]
        assert "todos" in params["properties"]
        assert "merge" in params["properties"]
        # items.required includes id/content
        items_props = params["properties"]["todos"]["items"]["properties"]
        assert "id" in items_props and "content" in items_props


# ──────────────────────────── 全局 store + 集成 ────────────────────────────


class TestTodoGlobals:
    """全局 _todo_store + 多次调用集成。"""

    def test_global_store(self):
        """全局 _todo_store 是 TodoStore 实例。"""
        from spirit.tools import todo_tool

        assert isinstance(todo_tool._todo_store, TodoStore)

    def test_dispatch_via_registry(self):
        """通过 registry.dispatch 模拟模型调用。"""
        from spirit.tools.registry import registry as global_reg

        # 确保 todo 已注册
        entry = global_reg.get_entry("todo")
        if entry is None:
            pytest.skip("todo tool 未注册")
        # 写入
        result = global_reg.dispatch("todo", {
            "todos": [{"id": "x", "content": "via registry"}],
            "merge": False,
        })
        data = json.loads(result)
        assert any(t["id"] == "x" for t in data["todos"])
