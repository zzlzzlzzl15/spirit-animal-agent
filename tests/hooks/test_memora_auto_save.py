"""Memora 自动保存钩子测试。

测试文件过滤逻辑、路径提取、钩子注册。
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from spirit.hooks.memora_auto_save import (
    _should_save_file,
    _extract_file_path,
    _generate_title,
    _generate_tags,
    on_tool_complete,
    register_auto_save_hook,
    clear_saved_paths,
    SAVABLE_EXTENSIONS,
    TARGET_TOOLS,
    MIN_FILE_SIZE,
)


# ---------------------------------------------------------------------------
# 文件过滤
# ---------------------------------------------------------------------------

class TestShouldSaveFile:
    def setup_method(self):
        clear_saved_paths()

    def test_empty_path(self):
        assert _should_save_file("") is False
        assert _should_save_file(None) is False

    def test_unsupported_extension(self, tmp_path):
        f = tmp_path / "test.exe"
        f.write_bytes(b"x" * 200)
        assert _should_save_file(str(f)) is False

    def test_supported_extension(self, tmp_path):
        f = tmp_path / "notes.md"
        f.write_text("# 笔记\n" * 50, encoding="utf-8")
        assert _should_save_file(str(f)) is True

    def test_too_small_file(self, tmp_path):
        f = tmp_path / "tiny.txt"
        f.write_text("hi", encoding="utf-8")
        assert _should_save_file(str(f)) is False

    def test_excluded_directory(self, tmp_path):
        cache_dir = tmp_path / ".cache"
        cache_dir.mkdir()
        f = cache_dir / "cached.md"
        f.write_text("# 缓存\n" * 50, encoding="utf-8")
        assert _should_save_file(str(f)) is False

    def test_nonexistent_file(self):
        assert _should_save_file("/nonexistent/file.md") is False

    def test_deduplication(self, tmp_path):
        f = tmp_path / "dedup.md"
        f.write_text("# 去重\n" * 50, encoding="utf-8")
        assert _should_save_file(str(f)) is True
        # 第二次应该返回 False
        assert _should_save_file(str(f)) is False

    def test_all_savable_extensions(self, tmp_path):
        for ext in SAVABLE_EXTENSIONS:
            f = tmp_path / f"test{ext}"
            f.write_text("x" * 200, encoding="utf-8")
            clear_saved_paths()
            assert _should_save_file(str(f)) is True, f"Extension {ext} should be savable"


# ---------------------------------------------------------------------------
# 路径提取
# ---------------------------------------------------------------------------

class TestExtractFilePath:
    def test_from_args_file_path(self):
        result = _extract_file_path("write_file", {"file_path": "/tmp/test.md"}, "")
        assert result == "/tmp/test.md"

    def test_from_args_path(self):
        result = _extract_file_path("create_file", {"path": "/tmp/test.txt"}, "")
        assert result == "/tmp/test.txt"

    def test_from_args_output_path(self):
        result = _extract_file_path("export", {"output_path": "/tmp/out.html"}, "")
        assert result == "/tmp/out.html"

    def test_from_result_json(self):
        result_json = json.dumps({"file_path": "/tmp/result.json"})
        result = _extract_file_path("some_tool", {}, result_json)
        assert result == "/tmp/result.json"

    def test_from_result_text_pattern(self):
        result = _extract_file_path(
            "some_tool", {},
            "文件已保存到 /home/user/notes.md 成功"
        )
        assert result == "/home/user/notes.md"

    def test_no_path_found(self):
        result = _extract_file_path("some_tool", {}, "没有路径信息")
        assert result is None


# ---------------------------------------------------------------------------
# 标题和标签生成
# ---------------------------------------------------------------------------

class TestTitleAndTags:
    def test_generate_title(self):
        title = _generate_title("/home/user/my-notes.md")
        assert title == "[Auto] my-notes"

    def test_generate_tags_md(self):
        tags = _generate_tags("/path/to/file.md")
        assert "spirit-agent" in tags
        assert "auto-save" in tags
        assert "markdown" in tags

    def test_generate_tags_json(self):
        tags = _generate_tags("/path/to/data.json")
        assert "data" in tags

    def test_generate_tags_html(self):
        tags = _generate_tags("/path/to/page.html")
        assert "html" in tags

    def test_generate_tags_yaml(self):
        tags = _generate_tags("/path/to/config.yaml")
        assert "config" in tags


# ---------------------------------------------------------------------------
# 钩子处理器
# ---------------------------------------------------------------------------

class TestOnToolComplete:
    def setup_method(self):
        clear_saved_paths()

    def test_non_target_tool_ignored(self):
        # 非目标工具不应触发任何操作
        on_tool_complete(tool_name="read_file", args={}, result="")

    def test_target_tool_no_path(self):
        # 目标工具但没有路径，不应崩溃
        on_tool_complete(tool_name="write_file", args={}, result="")

    def test_target_tool_with_nonexistent_path(self):
        # 路径不存在，不应崩溃
        on_tool_complete(
            tool_name="write_file",
            args={"file_path": "/nonexistent/file.md"},
            result="",
        )


# ---------------------------------------------------------------------------
# 钩子注册
# ---------------------------------------------------------------------------

class TestHookRegistration:
    def test_register_auto_save_hook(self):
        from spirit.hooks.hook_manager import HookManager, HookEvent

        hm = HookManager()
        register_auto_save_hook(hm)

        assert hm.has_handlers(HookEvent.AFTER_TOOL_EXECUTE)
        handlers = hm.list_handlers(HookEvent.AFTER_TOOL_EXECUTE)
        assert "on_tool_complete" in handlers
