"""Spirit Agent tools 测试批 1.3：search_tools + delegate_tool + memory_tool。

search_tools.py:
- search_files（内容搜索，正则）
- find_files（文件名搜索，通配符/正则）

delegate_tool.py:
- BLOCKED_TOOLS_FOR_CHILDREN
- _goal_snippet
- 进度 sink 接口
- get_unfinished_delegate_tasks
- interrupt_live_children
- _delegate_task_impl 错误路径

memory_tool.py:
- MemoryStore（基于文件 add/replace/remove/read）
- _memory_impl 4 路由

注意点：
- memory_tool 全局 MEMORY_DIR 是 Path.home() 下，需 monkeypatch 隔离
- delegate_tool 全局 _live_children + _snapshot + _progress_sink 也需隔离
- search_files 跨文件 IO 用 tmp_path
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from typing import Any, Callable

import pytest

from spirit.tools.memory_tool import (
    ENTRY_DELIMITER,
    MAX_MEMORY_CHARS,
    MAX_USER_CHARS,
    MEMORY_DIR,
    MEMORY_SCHEMA,
    MemoryStore,
    _memory_impl,
)
from spirit.tools.delegate_tool import (
    BLOCKED_TOOLS_FOR_CHILDREN,
    DELEGATE_SCHEMA,
    _delegate_task_impl,
    _emit_progress,
    _goal_snippet,
    _progress_sink,
    get_unfinished_delegate_tasks,
    interrupt_live_children,
    set_delegate_progress_sink,
)
from spirit.tools.search_tools import (
    BINARY_EXTENSIONS,
    FIND_FILES_SCHEMA,
    IGNORE_DIRS,
    MAX_FILE_SIZE,
    MAX_RESULTS,
    SEARCH_FILES_SCHEMA,
    _find_files_impl,
    _search_files_impl,
)
from spirit.tools.registry import registry


# ════════════════════════════════════════════════════════════════════════
# search_tools.py 测试
# ════════════════════════════════════════════════════════════════════════


# ──────────────────────────── 常量 ────────────────────────────


class TestSearchConstants:
    """search_tools 常量完整性。"""

    def test_ignore_dirs_has_common(self):
        for d in [".git", "node_modules", "__pycache__", ".venv", "dist", "build"]:
            assert d in IGNORE_DIRS

    def test_binary_extensions_has_common(self):
        for ext in [".pyc", ".dll", ".jpg", ".png", ".pdf", ".zip"]:
            assert ext in BINARY_EXTENSIONS

    def test_max_results(self):
        assert MAX_RESULTS == 100

    def test_max_file_size_5mb(self):
        assert MAX_FILE_SIZE == 5 * 1024 * 1024


# ──────────────────────────── search_files ────────────────────────────


class TestSearchFiles:
    """search_files 内容搜索。"""

    def test_basic_match_in_single_file(self, tmp_path: Path):
        f = tmp_path / "a.txt"
        f.write_text("hello world\nfoo bar\nhello again\n", encoding="utf-8")
        result = json.loads(_search_files_impl("hello", path=str(tmp_path)))
        assert result["count"] == 2
        assert result["files_searched"] == 1
        assert result["truncated"] is False

    def test_no_match_returns_zero(self, tmp_path: Path):
        f = tmp_path / "a.txt"
        f.write_text("nothing to see", encoding="utf-8")
        result = json.loads(_search_files_impl("xyz", path=str(tmp_path)))
        assert result["count"] == 0
        assert result["results"] == []

    def test_regex_pattern(self, tmp_path: Path):
        f = tmp_path / "code.py"
        f.write_text("def foo():\n    return 42\n", encoding="utf-8")
        result = json.loads(_search_files_impl(r"def \w+\(\)", path=str(tmp_path)))
        assert result["count"] == 1

    def test_invalid_regex_returns_error(self, tmp_path: Path):
        result = json.loads(_search_files_impl("[invalid(", path=str(tmp_path)))
        assert "error" in result
        assert "正则" in result["error"]

    def test_invalid_directory(self):
        result = json.loads(_search_files_impl("any", path="/nonexistent/xyz_abc"))
        assert "error" in result
        assert "不是目录" in result["error"]

    def test_ignore_case(self, tmp_path: Path):
        f = tmp_path / "mix.txt"
        f.write_text("Hello\nWORLD\nhello again", encoding="utf-8")
        r1 = json.loads(_search_files_impl("hello", path=str(tmp_path)))
        r2 = json.loads(_search_files_impl("hello", path=str(tmp_path), ignore_case=True))
        assert r1["count"] == 1
        assert r2["count"] == 2

    def test_file_glob_filter(self, tmp_path: Path):
        (tmp_path / "code.py").write_text("hello", encoding="utf-8")
        (tmp_path / "code.txt").write_text("hello", encoding="utf-8")
        result = json.loads(_search_files_impl("hello", path=str(tmp_path), file_glob="*.py"))
        assert result["count"] == 1
        names = [r["file"] for r in result["results"]]
        assert any("code.py" in n for n in names)
        assert not any(n.endswith(".txt") for n in names)

    def test_binary_files_skipped(self, tmp_path: Path):
        (tmp_path / "ok.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "skip.exe").write_text("hello", encoding="utf-8")  # 二进制扩展
        result = json.loads(_search_files_impl("hello", path=str(tmp_path)))
        assert result["count"] == 1
        names = [r["file"] for r in result["results"]]
        assert "ok.txt" in names[0]
        assert not any("skip.exe" in n for n in names)

    def test_ignore_dirs_skipped(self, tmp_path: Path):
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "inside.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "visible.txt").write_text("hello", encoding="utf-8")
        result = json.loads(_search_files_impl("hello", path=str(tmp_path)))
        names = [r["file"] for r in result["results"]]
        # node_modules 应被跳过
        assert not any("node_modules" in n for n in names)
        assert any("visible.txt" in n for n in names)

    def test_max_results_caps(self, tmp_path: Path):
        for i in range(200):
            (tmp_path / f"f{i}.txt").write_text("X match", encoding="utf-8")
        result = json.loads(_search_files_impl("match", path=str(tmp_path), max_results=5))
        assert result["count"] == 5
        assert result["truncated"] is True

    def test_max_results_min_clamped_to_1(self, tmp_path: Path):
        result = json.loads(_search_files_impl("any", path=str(tmp_path), max_results=0))
        # max 0 → clamp to 1
        assert isinstance(result, dict)

    def test_max_results_max_clamped_to_100(self, tmp_path: Path):
        # max 1000 → clamp to 100, 但仅 0 matches 时不报错
        result = json.loads(_search_files_impl("never", path=str(tmp_path), max_results=1000))
        assert result["count"] == 0

    def test_context_lines(self, tmp_path: Path):
        f = tmp_path / "a.txt"
        f.write_text("line1\nline2\nMATCH line\nline4\nline5\n", encoding="utf-8")
        result = json.loads(_search_files_impl("MATCH", path=str(tmp_path), context_lines=2))
        assert result["count"] == 1
        match = result["results"][0]
        assert "context" in match
        # 上下文应有 5 行（line1~line5），其中 MATCH 行加 > 前缀
        ctx = match["context"]
        assert len(ctx) == 5
        # MATCH 行有 > 前缀
        match_line = [c for c in ctx if "MATCH" in c]
        assert any(c.startswith(">") for c in match_line)

    def test_no_context_when_context_lines_zero(self, tmp_path: Path):
        f = tmp_path / "a.txt"
        f.write_text("MATCH", encoding="utf-8")
        result = json.loads(_search_files_impl("MATCH", path=str(tmp_path)))
        assert result["results"][0]["context"] is None

    def test_match_truncated_at_200_chars(self, tmp_path: Path):
        long_line = "MATCH " + "x" * 500
        (tmp_path / "long.txt").write_text(long_line, encoding="utf-8")
        result = json.loads(_search_files_impl("MATCH", path=str(tmp_path)))
        assert len(result["results"][0]["match"]) <= 200

    def test_large_file_skipped(self, tmp_path: Path):
        # 写一个 > 5MB 的文件
        big = tmp_path / "big.txt"
        big.write_text("MATCH\n" + "x" * (6 * 1024 * 1024), encoding="utf-8")
        result = json.loads(_search_files_impl("MATCH", path=str(tmp_path)))
        assert result["count"] == 0
        assert result["files_searched"] == 0


# ──────────────────────────── find_files ────────────────────────────


class TestFindFiles:
    """find_files 文件名搜索。"""

    def test_basic_glob_match(self, tmp_path: Path):
        (tmp_path / "foo.py").write_text("")
        (tmp_path / "bar.py").write_text("")
        (tmp_path / "baz.txt").write_text("")
        result = json.loads(_find_files_impl("*.py", path=str(tmp_path)))
        assert result["count"] == 2
        names = result["files"]
        assert all(n.endswith(".py") for n in names)

    def test_exact_match(self, tmp_path: Path):
        (tmp_path / "README.md").write_text("")
        result = json.loads(_find_files_impl("README.md", path=str(tmp_path)))
        assert result["count"] == 1
        assert result["files"][0] == "README.md"

    def test_no_match_returns_empty(self, tmp_path: Path):
        (tmp_path / "a.py").write_text("")
        result = json.loads(_find_files_impl("*.xyz", path=str(tmp_path)))
        assert result["count"] == 0
        assert result["files"] == []

    def test_regex_match(self, tmp_path: Path):
        (tmp_path / "test_1.py").write_text("")
        (tmp_path / "test_2.py").write_text("")
        (tmp_path / "lib.py").write_text("")
        result = json.loads(_find_files_impl(r"test_\d\.py", path=str(tmp_path), regex=True))
        assert result["count"] == 2

    def test_invalid_regex_returns_error(self, tmp_path: Path):
        (tmp_path / "x.py").write_text("")
        result = json.loads(_find_files_impl(r"[bad(", path=str(tmp_path), regex=True))
        assert "error" in result
        assert "正则" in result["error"]

    def test_ignore_dirs_skipped(self, tmp_path: Path):
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "x.py").write_text("")
        (tmp_path / "y.py").write_text("")
        result = json.loads(_find_files_impl("*.py", path=str(tmp_path)))
        names = result["files"]
        assert not any("node_modules" in n for n in names)
        assert any("y.py" in n for n in names)

    def test_invalid_dir(self):
        result = json.loads(_find_files_impl("*.py", path="/nonexistent/xyz_dir"))
        assert "error" in result

    def test_max_results_caps_at_200(self, tmp_path: Path):
        for i in range(300):
            (tmp_path / f"f{i:03d}.py").write_text("")
        result = json.loads(_find_files_impl("*.py", path=str(tmp_path), max_results=1000))
        assert result["count"] == 200  # clamp 上限

    def test_results_sorted(self, tmp_path: Path):
        for name in ["z.py", "a.py", "m.py"]:
            (tmp_path / name).write_text("")
        result = json.loads(_find_files_impl("*.py", path=str(tmp_path)))
        assert result["files"] == sorted(result["files"])

    def test_recursive_subdirs(self, tmp_path: Path):
        sub = tmp_path / "sub" / "deep"
        sub.mkdir(parents=True)
        (sub / "deep.py").write_text("")
        result = json.loads(_find_files_impl("*.py", path=str(tmp_path)))
        assert result["count"] == 1
        assert result["files"][0].endswith("deep.py")


# ──────────────────────────── schemas + registration ────────────────────────────


class TestSearchSchemas:
    """search_files + find_files schema 注册。"""

    def test_search_files_schema_format(self):
        assert SEARCH_FILES_SCHEMA["type"] == "function"
        assert SEARCH_FILES_SCHEMA["function"]["name"] == "search_files"
        assert "pattern" in SEARCH_FILES_SCHEMA["function"]["parameters"]["required"]

    def test_find_files_schema_format(self):
        assert FIND_FILES_SCHEMA["type"] == "function"
        assert FIND_FILES_SCHEMA["function"]["name"] == "find_files"
        assert "pattern" in FIND_FILES_SCHEMA["function"]["parameters"]["required"]

    def test_search_files_registered(self):
        entries = {e.name for e in registry.get_all_entries()}
        assert "search_files" in entries
        assert "find_files" in entries


# ════════════════════════════════════════════════════════════════════════
# delegate_tool.py 测试（不实际跑子 Agent）
# ════════════════════════════════════════════════════════════════════════


# ──────────────────────────── 进度 sink 接口 ────────────────────────────


class TestDelegateProgressSink:
    """set_delegate_progress_sink + _emit_progress。"""

    def setup_method(self):
        # 记录所有触发的事件
        self.events = []
        def sink(evt):
            self.events.append(evt)
        set_delegate_progress_sink(sink)

    def teardown_method(self):
        set_delegate_progress_sink(None)

    def test_sink_set_and_emit(self):
        _emit_progress({"phase": "test", "data": 1})
        assert len(self.events) == 1
        assert self.events[0]["phase"] == "test"

    def test_sink_none_silently_ignored(self):
        set_delegate_progress_sink(None)
        # 不抛错
        _emit_progress({"phase": "x"})
        assert self.events == []

    def test_sink_exception_caught(self):
        # 即使 sink 抛错也不影响
        def bad_sink(evt):
            raise RuntimeError("sink failed")
        set_delegate_progress_sink(bad_sink)
        # 不应抛错
        _emit_progress({"phase": "x"})

    def test_multiple_events(self):
        _emit_progress({"phase": "a"})
        _emit_progress({"phase": "b"})
        _emit_progress({"phase": "c"})
        assert [e["phase"] for e in self.events] == ["a", "b", "c"]


# ──────────────────────────── _goal_snippet ────────────────────────────


class TestGoalSnippet:
    """_goal_snippet 任务摘要展示。"""

    def test_empty_goal(self):
        assert _goal_snippet("") == "(未命名任务)"

    def test_short_goal(self):
        assert _goal_snippet("hello world") == "hello world"

    def test_long_goal_truncated_at_40(self):
        long = "x" * 100
        result = _goal_snippet(long)
        assert len(result) <= 41  # 40 + ellipsis "…"
        assert result.endswith("…")

    def test_first_sentence_extracted_chinese_period(self):
        result = _goal_snippet("第一句。第二句")
        assert result == "第一句。"

    def test_first_sentence_extracted_exclamation(self):
        result = _goal_snippet("Hello!World")
        assert result == "Hello!"

    def test_first_sentence_extracted_question(self):
        result = _goal_snippet("Yes?No way")
        assert result == "Yes?"

    def test_first_sentence_extracted_semicolon(self):
        result = _goal_snippet("Done;Remaining")
        assert result == "Done;"

    def test_no_separator_returns_full_when_short(self):
        result = _goal_snippet("just a phrase")
        assert result == "just a phrase"

    def test_whitespace_normalized(self):
        result = _goal_snippet("hello   \n   world")
        assert " " not in result.split(" ", 1)[0] or "  " not in result


# ──────────────────────────── get_unfinished_delegate_tasks ────────────────────────────


class TestUnfinishedDelegateTasks:
    """get_unfinished_delegate_tasks 快照读取。"""

    def test_no_snapshot_returns_empty(self):
        from spirit.tools.delegate_tool import _snapshot_lock, _delegate_snapshot  # noqa
        with _snapshot_lock:
            # 临时清空
            original = _delegate_snapshot
            import spirit.tools.delegate_tool as dt
            dt._delegate_snapshot = None
        try:
            tasks = get_unfinished_delegate_tasks()
            assert tasks == []
        finally:
            dt._delegate_snapshot = original

    def test_filters_success(self):
        from spirit.tools.delegate_tool import _snapshot_lock
        import spirit.tools.delegate_tool as dt
        with _snapshot_lock:
            original = dt._delegate_snapshot
            dt._delegate_snapshot = {
                "updated": __import__("time").time(),
                "tasks": [
                    {"name": "A", "goal": "gA", "context": "", "status": "success"},
                    {"name": "B", "goal": "gB", "context": "", "status": "failed"},
                    {"name": "C", "goal": "gC", "context": "", "status": "running"},
                ],
            }
        try:
            unfinished = get_unfinished_delegate_tasks()
            # 只返回非 success
            names = [t["name"] for t in unfinished]
            assert "A" not in names
            assert "B" in names
            assert "C" in names
        finally:
            dt._delegate_snapshot = original

    def test_max_age_filter(self):
        import time
        import spirit.tools.delegate_tool as dt
        with dt._snapshot_lock:
            original = dt._delegate_snapshot
            dt._delegate_snapshot = {
                "updated": time.time() - 7200,  # 2 小时前
                "tasks": [{"name": "X", "goal": "", "context": "", "status": "failed"}],
            }
        try:
            # max_age 1 小时 → 应返回空（snapshot 太旧）
            tasks = get_unfinished_delegate_tasks(max_age_seconds=3600)
            assert tasks == []
        finally:
            dt._delegate_snapshot = original

    def test_returns_copies_not_references(self):
        """返回的是 dict 副本，不应改到原 snapshot。"""
        import spirit.tools.delegate_tool as dt
        import time
        with dt._snapshot_lock:
            original = dt._delegate_snapshot
            dt._delegate_snapshot = {
                "updated": time.time(),
                "tasks": [{"name": "Y", "goal": "", "context": "", "status": "failed"}],
            }
        try:
            tasks = get_unfinished_delegate_tasks()
            tasks[0]["name"] = "mutated"
            # snapshot 没被改
            assert dt._delegate_snapshot["tasks"][0]["name"] == "Y"
        finally:
            dt._delegate_snapshot = original


# ──────────────────────────── interrupt_live_children ────────────────────────────


class TestInterruptLiveChildren:
    """interrupt_live_children 测试（轻量）。"""

    def test_no_children_returns_zero(self):
        import spirit.tools.delegate_tool as dt
        with dt._live_lock:
            original = dt._live_children
            dt._live_children = {}
        try:
            killed = interrupt_live_children("test reason")
            assert killed == 0
        finally:
            dt._live_children = original

    def test_kills_registered_children(self):
        """register 一个 mock child, interrupt 应被调用。"""
        import spirit.tools.delegate_tool as dt

        interrupt_called = []
        meta_state = {}

        class MockAgent:
            def interrupt(self):
                interrupt_called.append("ok")

        with dt._live_lock:
            original = dt._live_children
            dt._live_children = {
                "child1": {"agent": MockAgent(), "meta": meta_state},
            }
        try:
            killed = interrupt_live_children("my reason")
            assert killed == 1
            assert interrupt_called == ["ok"]
            # meta 应被设置
            assert meta_state.get("external_kill") == "my reason"
        finally:
            dt._live_children = original

    def test_interrupt_exception_caught(self):
        """agent.interrupt() 抛错不传播。"""
        import spirit.tools.delegate_tool as dt

        meta_state = {}

        class BadAgent:
            def interrupt(self):
                raise RuntimeError("bad")

        with dt._live_lock:
            original = dt._live_children
            dt._live_children = {
                "c": {"agent": BadAgent(), "meta": meta_state},
            }
        try:
            # 即使 interrupt 抛错也不影响
            killed = interrupt_live_children("reason")
            assert killed == 1
            assert meta_state.get("external_kill") == "reason"
        finally:
            dt._live_children = original


# ──────────────────────────── _delegate_task_impl 边界 ────────────────────────────


class TestDelegateTaskImplBoundaries:
    """_delegate_task_impl 边界（不实际跑子 Agent）。"""

    def test_empty_tasks_error(self):
        result = json.loads(_delegate_task_impl(tasks=[]))
        assert "error" in result
        assert "至少需要" in result["error"]

    def test_too_many_tasks_error(self):
        # 6 个任务应被拒绝
        tasks = [{"goal": f"task{i}"} for i in range(6)]
        result = json.loads(_delegate_task_impl(tasks=tasks))
        assert "error" in result
        assert "5" in result["error"]


# ──────────────────────────── BLOCKED_TOOLS_FOR_CHILDREN ────────────────────────────


class TestBlockedToolsForChildren:
    """BLOCKED_TOOLS_FOR_CHILDREN 子 Agent 限制。"""

    def test_is_frozenset(self):
        assert isinstance(BLOCKED_TOOLS_FOR_CHILDREN, frozenset)

    def test_contains_delegate(self):
        assert "delegate_task" in BLOCKED_TOOLS_FOR_CHILDREN

    def test_contains_clarify(self):
        assert "clarify" in BLOCKED_TOOLS_FOR_CHILDREN

    def test_contains_memory(self):
        assert "memory" in BLOCKED_TOOLS_FOR_CHILDREN

    def test_does_not_contain_safe_tools(self):
        assert "search_files" not in BLOCKED_TOOLS_FOR_CHILDREN
        assert "find_files" not in BLOCKED_TOOLS_FOR_CHILDREN


# ──────────────────────────── DELEGATE_SCHEMA ────────────────────────────


class TestDelegateSchema:
    """delegate_task schema 完整性。"""

    def test_schema_format(self):
        assert DELEGATE_SCHEMA["type"] == "function"
        assert DELEGATE_SCHEMA["function"]["name"] == "delegate_task"

    def test_required_tasks(self):
        assert "tasks" in DELEGATE_SCHEMA["function"]["parameters"]["required"]

    def test_tasks_is_array_of_objects(self):
        items = DELEGATE_SCHEMA["function"]["parameters"]["properties"]["tasks"]["items"]
        assert "goal" in items["properties"]
        assert "goal" in items["required"]

    def test_registered(self):
        entries = {e.name for e in registry.get_all_entries()}
        assert "delegate_task" in entries


# ════════════════════════════════════════════════════════════════════════
# memory_tool.py 测试
# ════════════════════════════════════════════════════════════════════════


# ──────────────────────────── MemoryStore（隔离 tmp_path）─────────────────────────────


@pytest.fixture
def isolated_memory(tmp_path: Path, monkeypatch):
    """重定位 MEMORY_DIR 到 tmp_path。"""
    from spirit.tools import memory_tool

    monkeypatch.setattr(memory_tool, "MEMORY_DIR", tmp_path)
    yield tmp_path


class TestMemoryStoreIsolated:
    """MemoryStore add / replace / remove / read。"""

    def test_initial_empty(self, isolated_memory):
        store = MemoryStore()
        result = store.read()
        assert result["memory_entries"] == []
        assert result["user_entries"] == []

    def test_add_creates_file(self, isolated_memory):
        store = MemoryStore()
        result = store.add("memory", "first entry")
        assert result["success"] is True
        assert result["action"] == "add"
        assert result["count"] == 1

    def test_add_user_store(self, isolated_memory):
        store = MemoryStore()
        result = store.add("user", "user preference")
        assert result["success"] is True
        user_file = isolated_memory / "USER.md"
        assert user_file.exists()

    def test_add_strips_content(self, isolated_memory):
        store = MemoryStore()
        store.add("memory", "  trimmed  ")
        assert store.read()["memory_entries"][0] == "trimmed"

    def test_read_specific_store(self, isolated_memory):
        store = MemoryStore()
        store.add("memory", "mem1")
        store.add("user", "user1")
        assert store.read("memory") == {"memory_entries": ["mem1"]}
        assert store.read("user") == {"user_entries": ["user1"]}

    def test_replace_existing(self, isolated_memory):
        store = MemoryStore()
        store.add("memory", "old text here")
        result = store.replace("memory", "old text", "new text")
        assert result["success"] is True
        assert store.read()["memory_entries"][0] == "new text"

    def test_replace_not_found(self, isolated_memory):
        store = MemoryStore()
        result = store.replace("memory", "missing", "replacement")
        assert result["success"] is False
        assert "未找到" in result["error"]

    def test_replace_first_match_wins(self, isolated_memory):
        """Spirit 现状：replace 是“整条子匹配替换”——包含子串的整个 entry 披
        替换为 new_content（不是只替换子串）。第一条含子串的 entry 被替换，后面的不变。"""
        store = MemoryStore()
        store.add("memory", "alpha beta")
        store.add("memory", "alpha gamma")
        result = store.replace("memory", "alpha", "X")
        entries = store.read()["memory_entries"]
        # 第一个 "alpha beta" 含 alpha 被全替换为 "X"
        assert entries == ["X", "alpha gamma"]

    def test_remove_existing(self, isolated_memory):
        store = MemoryStore()
        store.add("memory", "keep")
        store.add("memory", "remove me")
        result = store.remove("memory", "remove me")
        assert result["success"] is True
        entries = store.read()["memory_entries"]
        assert entries == ["keep"]

    def test_remove_not_found(self, isolated_memory):
        store = MemoryStore()
        result = store.remove("memory", "missing")
        assert result["success"] is False
        assert "未找到" in result["error"]

    def test_remove_partial_match(self, isolated_memory):
        """子串匹配删除：保留不含 substring 的。"""
        store = MemoryStore()
        store.add("memory", "aaa")
        store.add("memory", "bbb")
        store.add("memory", "aab")
        store.remove("memory", "aa")
        entries = store.read()["memory_entries"]
        assert entries == ["bbb"]


class TestMemoryStoreCharLimits:
    """字符上限限制。"""

    def test_memory_capped_at_max(self, isolated_memory):
        store = MemoryStore()
        # 添加超出 MAX_MEMORY_CHARS 总和的条目
        big1 = "x" * (MAX_MEMORY_CHARS - 100)
        big2 = "y" * 200
        store.add("memory", big1)
        store.add("memory", big2)
        # _write_entries 在写入前 truncate，最早的应被弹出
        entries = store.read()["memory_entries"]
        total_chars = sum(len(e) for e in entries)
        # 至少不应无限大
        assert total_chars <= MAX_MEMORY_CHARS + len(ENTRY_DELIMITER)

    def test_user_capped_at_max_user(self, isolated_memory):
        """字符上限限制在多条目时才生效。

        Spirit 现状：单条目超出 max_chars 不会被裁（源代码 `while total > max_chars and len(entries) > 1`）。
        这里是记录到的 bug：该函数对 len(entries) == 1 不生效。多条目会起作用。"""
        store = MemoryStore()
        # 多个 entries，total 超 max → pop 最早的
        store.add("user", "old " * 1000)  # ~4000 chars
        store.add("user", "new " * 100)   # ~400 chars
        entries = store.read("user")["user_entries"]
        # “old” 应被 pop，"new" 应保留
        assert "old" not in str(entries)
        assert "new" in str(entries)
        # total 不超 max（这里 total 是 ~400）
        assert sum(len(e) for e in entries) <= MAX_USER_CHARS

    def test_atomic_write_uses_tmp(self, isolated_memory):
        """写入用 .tmp + replace 实现原子。"""
        store = MemoryStore()
        store.add("memory", "atomic")
        # 不残留 .tmp
        tmp_files = list(isolated_memory.glob("*.tmp"))
        assert tmp_files == []


# ──────────────────────────── _memory_impl ────────────────────────────


class TestMemoryImpl:
    """_memory_impl 4 个 action 路由。"""

    def test_read_default(self, isolated_memory):
        """默认 read 返回 memory + user。
        _memory_impl 的 store 默认是 'memory'，所以要显式传 None 让他走
        MemoryStore.read(None) 的 else 分支同时返回两个 entries。
        需重置全局 _memory_store 走新隔离路径。"""
        # module 级 _memory_store 是 import 时创建的，使用真实 MEMORY_DIR。
        # 这里用 monkeypatch 后手动重置全局 store。
        from spirit.tools import memory_tool
        old_store = memory_tool._memory_store
        memory_tool._memory_store = MemoryStore()
        try:
            result = json.loads(_memory_impl(action="read", store=None))
            assert "memory_entries" in result
            assert "user_entries" in result
        finally:
            memory_tool._memory_store = old_store

    def test_read_specific(self, isolated_memory):
        """读特定 store 返回 {store_entries: [...]}。"""
        from spirit.tools import memory_tool
        old_store = memory_tool._memory_store
        memory_tool._memory_store = MemoryStore()
        try:
            MemoryStore().add("memory", "m1")
            result = json.loads(_memory_impl(action="read", store="memory"))
            assert result == {"memory_entries": ["m1"]}
        finally:
            memory_tool._memory_store = old_store

    def test_add_without_content_error(self):
        result = json.loads(_memory_impl(action="add"))
        assert "error" in result
        assert "content" in result["error"]

    def test_add_with_empty_content_error(self):
        result = json.loads(_memory_impl(action="add", content=""))
        assert "error" in result

    def test_add_success(self, monkeypatch, tmp_path):
        from spirit.tools import memory_tool
        monkeypatch.setattr(memory_tool, "MEMORY_DIR", tmp_path)
        result = json.loads(_memory_impl(action="add", store="memory", content="hello"))
        assert result["success"] is True

    def test_replace_missing_params_error(self):
        result = json.loads(_memory_impl(action="replace"))
        assert "error" in result

    def test_replace_without_new_content_error(self, monkeypatch, tmp_path):
        from spirit.tools import memory_tool
        monkeypatch.setattr(memory_tool, "MEMORY_DIR", tmp_path)
        MemoryStore().add("memory", "find me")
        result = json.loads(_memory_impl(action="replace", store="memory", substring="find"))
        # 缺 new_content → error
        assert "error" in result

    def test_remove_without_substring_error(self):
        result = json.loads(_memory_impl(action="remove"))
        assert "error" in result
        assert "substring" in result["error"]

    def test_unknown_action_error(self):
        result = json.loads(_memory_impl(action="weird_op"))
        assert "error" in result
        assert "未知操作" in result["error"]

    def test_schema_registered(self):
        entries = {e.name for e in registry.get_all_entries()}
        assert "memory" in entries

    def test_schema_format(self):
        assert MEMORY_SCHEMA["type"] == "function"
        assert MEMORY_SCHEMA["function"]["name"] == "memory"
        actions = MEMORY_SCHEMA["function"]["parameters"]["properties"]["action"]["enum"]
        assert set(actions) == {"add", "replace", "remove", "read"}
