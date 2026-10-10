"""spirit/api/context.py + spirit/api/events.py 回归测试。

context: WorkspaceContext 数据类 + from_dict/to_dict/summary
events: 9 种流式事件 dataclass + event_to_dict 序列化
"""
import dataclasses
import json

import pytest


# ──────────────────────────────────────────────────────────────────────
# context.py — CursorPosition / SelectionRange
# ──────────────────────────────────────────────────────────────────────

class TestCursorPosition:
    def test_defaults(self):
        from spirit.api.context import CursorPosition
        c = CursorPosition()
        assert c.line == 0
        assert c.character == 0

    def test_explicit(self):
        from spirit.api.context import CursorPosition
        c = CursorPosition(line=10, character=42)
        assert c.line == 10
        assert c.character == 42

    def test_equality(self):
        from spirit.api.context import CursorPosition
        a = CursorPosition(line=1, character=2)
        b = CursorPosition(line=1, character=2)
        assert a == b


class TestSelectionRange:
    def test_defaults(self):
        from spirit.api.context import SelectionRange
        r = SelectionRange()
        assert r.start_line == 0
        assert r.end_line == 0

    def test_explicit(self):
        from spirit.api.context import SelectionRange
        r = SelectionRange(start_line=5, end_line=20)
        assert r.start_line == 5
        assert r.end_line == 20


# ──────────────────────────────────────────────────────────────────────
# context.py — WorkspaceContext
# ──────────────────────────────────────────────────────────────────────

class TestWorkspaceContextDefaults:
    def test_default_construction(self):
        from spirit.api.context import WorkspaceContext
        c = WorkspaceContext()
        assert c.workspace_root is None
        assert c.active_file is None
        assert c.language_id is None
        assert c.selection is None
        assert c.selection_range is None
        assert c.cursor_position is None
        assert c.current_line is None
        assert c.open_tabs == []

    def test_has_active_file_false(self):
        from spirit.api.context import WorkspaceContext
        assert WorkspaceContext().has_active_file is False

    def test_has_active_file_true(self):
        from spirit.api.context import WorkspaceContext
        c = WorkspaceContext(active_file="foo.py")
        assert c.has_active_file is True

    def test_has_selection_false_none(self):
        from spirit.api.context import WorkspaceContext
        assert WorkspaceContext().has_selection is False

    def test_has_selection_false_empty(self):
        from spirit.api.context import WorkspaceContext
        c = WorkspaceContext(selection="")
        assert c.has_selection is False

    def test_has_selection_true(self):
        from spirit.api.context import WorkspaceContext
        c = WorkspaceContext(selection="some text")
        assert c.has_selection is True


class TestFromDict:
    def test_none_data(self):
        from spirit.api.context import WorkspaceContext
        c = WorkspaceContext.from_dict(None)
        assert c.workspace_root is None
        assert c.open_tabs == []

    def test_empty_data(self):
        from spirit.api.context import WorkspaceContext
        c = WorkspaceContext.from_dict({})
        assert c.workspace_root is None
        assert c.active_file is None
        assert c.selection_range is None
        assert c.cursor_position is None
        assert c.open_tabs == []

    def test_full_data(self):
        from spirit.api.context import WorkspaceContext
        data = {
            "workspaceRoot": "/home/proj",
            "activeFile": "src/main.py",
            "languageId": "python",
            "selection": "selected code",
            "selectionRange": {"startLine": 10, "endLine": 20},
            "cursorPosition": {"line": 15, "character": 5},
            "currentLine": "def foo():",
            "openTabs": ["a.py", "b.py", "c.py"],
        }
        c = WorkspaceContext.from_dict(data)
        assert c.workspace_root == "/home/proj"
        assert c.active_file == "src/main.py"
        assert c.language_id == "python"
        assert c.selection == "selected code"
        assert c.selection_range.start_line == 10
        assert c.selection_range.end_line == 20
        assert c.cursor_position.line == 15
        assert c.cursor_position.character == 5
        assert c.current_line == "def foo():"
        assert c.open_tabs == ["a.py", "b.py", "c.py"]

    def test_partial_data(self):
        from spirit.api.context import WorkspaceContext
        c = WorkspaceContext.from_dict({"activeFile": "only.py"})
        assert c.active_file == "only.py"
        assert c.workspace_root is None
        assert c.selection_range is None
        assert c.cursor_position is None

    def test_selection_range_missing(self):
        from spirit.api.context import WorkspaceContext
        c = WorkspaceContext.from_dict({"selectionRange": None})
        assert c.selection_range is None

    def test_cursor_position_missing(self):
        from spirit.api.context import WorkspaceContext
        c = WorkspaceContext.from_dict({"cursorPosition": None})
        assert c.cursor_position is None

    def test_open_tabs_default(self):
        """openTabs 缺省时为 []。"""
        from spirit.api.context import WorkspaceContext
        c = WorkspaceContext.from_dict({})
        assert c.open_tabs == []

    def test_selection_range_start_line_default(self):
        """startLine 缺省时为 0。"""
        from spirit.api.context import WorkspaceContext
        c = WorkspaceContext.from_dict({"selectionRange": {"endLine": 5}})
        assert c.selection_range.start_line == 0
        assert c.selection_range.end_line == 5

    def test_cursor_position_character_default(self):
        """character 缺省时为 0。"""
        from spirit.api.context import WorkspaceContext
        c = WorkspaceContext.from_dict({"cursorPosition": {"line": 3}})
        assert c.cursor_position.line == 3
        assert c.cursor_position.character == 0


class TestToDict:
    def test_empty(self):
        from spirit.api.context import WorkspaceContext
        d = WorkspaceContext().to_dict()
        assert d == {}

    def test_all_fields(self):
        from spirit.api.context import (
            WorkspaceContext, CursorPosition, SelectionRange,
        )
        c = WorkspaceContext(
            workspace_root="/p",
            active_file="f.py",
            language_id="py",
            selection="x",
            selection_range=SelectionRange(start_line=1, end_line=2),
            cursor_position=CursorPosition(line=3, character=4),
            current_line="line",
            open_tabs=["a", "b"],
        )
        d = c.to_dict()
        assert d["workspaceRoot"] == "/p"
        assert d["activeFile"] == "f.py"
        assert d["languageId"] == "py"
        assert d["selection"] == "x"
        assert d["selectionRange"] == {"startLine": 1, "endLine": 2}
        assert d["cursorPosition"] == {"line": 3, "character": 4}
        assert d["currentLine"] == "line"
        assert d["openTabs"] == ["a", "b"]

    def test_selection_truncated_200(self):
        from spirit.api.context import WorkspaceContext
        big = "x" * 500
        c = WorkspaceContext(selection=big)
        d = c.to_dict()
        assert len(d["selection"]) == 200

    def test_open_tabs_capped_20(self):
        from spirit.api.context import WorkspaceContext
        tabs = [f"t{i}.py" for i in range(50)]
        c = WorkspaceContext(open_tabs=tabs)
        d = c.to_dict()
        assert len(d["openTabs"]) == 20

    def test_open_tabs_empty_excluded(self):
        """open_tabs 为空时不写入。"""
        from spirit.api.context import WorkspaceContext
        d = WorkspaceContext(open_tabs=[]).to_dict()
        assert "openTabs" not in d

    def test_none_fields_excluded(self):
        """None 字段不写入。"""
        from spirit.api.context import WorkspaceContext
        d = WorkspaceContext().to_dict()
        assert "workspaceRoot" not in d
        assert "activeFile" not in d


class TestSummary:
    def test_empty(self):
        from spirit.api.context import WorkspaceContext
        s = WorkspaceContext().summary()
        assert s == "无编辑器上下文"

    def test_workspace_root_only(self):
        from spirit.api.context import WorkspaceContext
        s = WorkspaceContext(workspace_root="/p").summary()
        assert "/p" in s
        assert "工作区" in s

    def test_active_file_with_language(self):
        from spirit.api.context import WorkspaceContext
        s = WorkspaceContext(
            active_file="f.py", language_id="python"
        ).summary()
        assert "f.py" in s
        assert "python" in s
        assert "语言" in s

    def test_active_file_no_language(self):
        from spirit.api.context import WorkspaceContext
        s = WorkspaceContext(active_file="f.py").summary()
        assert "f.py" in s
        assert "语言" not in s

    def test_selection_with_range(self):
        from spirit.api.context import WorkspaceContext, SelectionRange
        s = WorkspaceContext(
            selection="code",
            selection_range=SelectionRange(start_line=2, end_line=5),
        ).summary()
        assert "选中" in s
        # +1 because source uses 1-based display
        assert "3" in s and "6" in s

    def test_selection_no_range(self):
        from spirit.api.context import WorkspaceContext
        s = WorkspaceContext(selection="code").summary()
        assert "选中" in s
        assert "有代码被选中" in s

    def test_cursor_position(self):
        from spirit.api.context import WorkspaceContext, CursorPosition
        s = WorkspaceContext(
            cursor_position=CursorPosition(line=10, character=0)
        ).summary()
        assert "光标" in s
        assert "11" in s  # +1 for 1-based

    def test_open_tabs_basename_only(self):
        from spirit.api.context import WorkspaceContext
        s = WorkspaceContext(open_tabs=[
            "/long/path/a.py", "/long/path/b.py", "/long/path/c.py",
            "/long/path/d.py", "/long/path/e.py", "/long/path/f.py",
        ]).summary()
        assert "打开文件" in s
        # 只取前 5 个 basename
        for name in ["a.py", "b.py", "c.py", "d.py", "e.py"]:
            assert name in s

    def test_joined_by_pipe(self):
        from spirit.api.context import WorkspaceContext
        s = WorkspaceContext(
            workspace_root="/p",
            active_file="f.py",
        ).summary()
        assert " | " in s


# ──────────────────────────────────────────────────────────────────────
# events.py — 数据类 frozen / 字段
# ──────────────────────────────────────────────────────────────────────

class TestMessageChunk:
    def test_basic(self):
        from spirit.api.events import MessageChunk
        e = MessageChunk(text="hi")
        assert e.text == "hi"

    def test_frozen(self):
        from spirit.api.events import MessageChunk
        e = MessageChunk(text="x")
        with pytest.raises(dataclasses.FrozenInstanceError):
            e.text = "y"

    def test_equality(self):
        from spirit.api.events import MessageChunk
        assert MessageChunk(text="x") == MessageChunk(text="x")
        assert MessageChunk(text="x") != MessageChunk(text="y")

    def test_hashable(self):
        from spirit.api.events import MessageChunk
        s = {MessageChunk(text="a"), MessageChunk(text="a"), MessageChunk(text="b")}
        assert len(s) == 2


class TestMessageStop:
    def test_default_not_final(self):
        from spirit.api.events import MessageStop
        e = MessageStop()
        assert e.final is False

    def test_final_true(self):
        from spirit.api.events import MessageStop
        assert MessageStop(final=True).final is True

    def test_frozen(self):
        from spirit.api.events import MessageStop
        with pytest.raises(dataclasses.FrozenInstanceError):
            MessageStop().final = True


class TestCommentary:
    def test_basic(self):
        from spirit.api.events import Commentary
        e = Commentary(text="let me check")
        assert e.text == "let me check"


class TestToolCallStart:
    def test_required_only(self):
        from spirit.api.events import ToolCallStart
        e = ToolCallStart(tool_name="read_file", tool_call_id="t1")
        assert e.tool_name == "read_file"
        assert e.tool_call_id == "t1"
        assert e.args is None
        assert e.index == 0

    def test_with_args(self):
        from spirit.api.events import ToolCallStart
        e = ToolCallStart(
            tool_name="x", tool_call_id="t1", args={"p": 1}, index=3,
        )
        assert e.args == {"p": 1}
        assert e.index == 3


class TestToolCallResult:
    def test_defaults(self):
        from spirit.api.events import ToolCallResult
        e = ToolCallResult(tool_name="x", tool_call_id="t1", result="r")
        assert e.duration == 0.0
        assert e.ok is True
        assert e.index == 0

    def test_with_all(self):
        from spirit.api.events import ToolCallResult
        e = ToolCallResult(
            tool_name="x", tool_call_id="t1", result="r",
            duration=1.5, ok=False, index=2,
        )
        assert e.duration == 1.5
        assert e.ok is False
        assert e.index == 2


class TestStatusUpdate:
    def test_status_only(self):
        from spirit.api.events import StatusUpdate
        e = StatusUpdate(status="thinking")
        assert e.status == "thinking"
        assert e.message == ""

    def test_with_message(self):
        from spirit.api.events import StatusUpdate
        e = StatusUpdate(status="idle", message="done")
        assert e.status == "idle"
        assert e.message == "done"

    def test_various_statuses(self):
        """源码文档说明 status 是 'thinking' | 'tool_executing' | 'idle' | 'error'。"""
        from spirit.api.events import StatusUpdate
        for s in ("thinking", "tool_executing", "idle", "error"):
            assert StatusUpdate(status=s).status == s


class TestErrorEvent:
    def test_required(self):
        from spirit.api.events import ErrorEvent
        e = ErrorEvent(code="E001", message="oops")
        assert e.code == "E001"
        assert e.message == "oops"
        assert e.details is None

    def test_with_details(self):
        from spirit.api.events import ErrorEvent
        e = ErrorEvent(code="E", message="m", details={"k": "v"})
        assert e.details == {"k": "v"}


class TestEditProposal:
    def test_required_only(self):
        from spirit.api.events import EditProposal
        e = EditProposal(
            file_path="f.py", original_content="a", new_content="b",
        )
        assert e.file_path == "f.py"
        assert e.original_content == "a"
        assert e.new_content == "b"
        assert e.description == ""
        assert e.edit_id == ""

    def test_with_optional(self):
        from spirit.api.events import EditProposal
        e = EditProposal(
            file_path="f", original_content="o", new_content="n",
            description="refactor", edit_id="e1",
        )
        assert e.description == "refactor"
        assert e.edit_id == "e1"


class TestEditResult:
    def test_required(self):
        from spirit.api.events import EditResult
        e = EditResult(edit_id="e1", accepted=True)
        assert e.edit_id == "e1"
        assert e.accepted is True
        assert e.message == ""

    def test_rejected(self):
        from spirit.api.events import EditResult
        e = EditResult(edit_id="e1", accepted=False, message="no")
        assert e.accepted is False
        assert e.message == "no"


# ──────────────────────────────────────────────────────────────────────
# events.py — event_to_dict 序列化
# ──────────────────────────────────────────────────────────────────────

class TestEventToDict:
    def test_message_chunk(self):
        from spirit.api.events import MessageChunk, event_to_dict
        d = event_to_dict(MessageChunk(text="hi"))
        assert d == {"type": "message_chunk", "text": "hi"}

    def test_message_stop(self):
        from spirit.api.events import MessageStop, event_to_dict
        d = event_to_dict(MessageStop(final=True))
        assert d == {"type": "message_stop", "final": True}

    def test_message_stop_not_final(self):
        from spirit.api.events import MessageStop, event_to_dict
        d = event_to_dict(MessageStop(final=False))
        assert d == {"type": "message_stop", "final": False}

    def test_commentary(self):
        from spirit.api.events import Commentary, event_to_dict
        d = event_to_dict(Commentary(text="think"))
        assert d == {"type": "commentary", "text": "think"}

    def test_tool_call_start(self):
        from spirit.api.events import ToolCallStart, event_to_dict
        d = event_to_dict(ToolCallStart(
            tool_name="read", tool_call_id="t1", args={"p": 1}, index=2,
        ))
        assert d == {
            "type": "tool_call_start",
            "tool_name": "read",
            "tool_call_id": "t1",
            "args": {"p": 1},
            "index": 2,
        }

    def test_tool_call_result_truncated_500(self):
        from spirit.api.events import ToolCallResult, event_to_dict
        big = "x" * 1000
        d = event_to_dict(ToolCallResult(
            tool_name="x", tool_call_id="t1", result=big,
        ))
        assert len(d["result"]) == 500

    def test_tool_call_result_short(self):
        from spirit.api.events import ToolCallResult, event_to_dict
        d = event_to_dict(ToolCallResult(
            tool_name="x", tool_call_id="t1", result="ok",
            duration=0.5, ok=True, index=1,
        ))
        assert d == {
            "type": "tool_call_result",
            "tool_name": "x",
            "tool_call_id": "t1",
            "result": "ok",
            "duration": 0.5,
            "ok": True,
            "index": 1,
        }

    def test_status_update(self):
        from spirit.api.events import StatusUpdate, event_to_dict
        d = event_to_dict(StatusUpdate(status="thinking", message="busy"))
        assert d == {"type": "status", "status": "thinking", "message": "busy"}

    def test_error_event(self):
        from spirit.api.events import ErrorEvent, event_to_dict
        d = event_to_dict(ErrorEvent(code="E", message="m"))
        assert d == {
            "type": "error", "code": "E", "message": "m", "details": None,
        }

    def test_error_event_with_details(self):
        from spirit.api.events import ErrorEvent, event_to_dict
        d = event_to_dict(ErrorEvent(
            code="E", message="m", details={"k": "v"},
        ))
        assert d["details"] == {"k": "v"}

    def test_edit_proposal(self):
        from spirit.api.events import EditProposal, event_to_dict
        d = event_to_dict(EditProposal(
            file_path="f", original_content="o", new_content="n",
            description="d", edit_id="e1",
        ))
        assert d == {
            "type": "edit_proposal",
            "file_path": "f",
            "original_content": "o",
            "new_content": "n",
            "description": "d",
            "edit_id": "e1",
        }

    def test_edit_result(self):
        from spirit.api.events import EditResult, event_to_dict
        d = event_to_dict(EditResult(edit_id="e1", accepted=True, message="ok"))
        assert d == {
            "type": "edit_result",
            "edit_id": "e1",
            "accepted": True,
            "message": "ok",
        }

    def test_unknown_event_returns_unknown(self):
        """非 StreamEvent 实例 → {"type": "unknown"}。"""
        from spirit.api.events import event_to_dict
        d = event_to_dict("not an event")
        assert d == {"type": "unknown"}


class TestEventJsonSerializable:
    """event_to_dict 的输出应能直接 JSON 序列化。"""

    def test_message_chunk_json(self):
        from spirit.api.events import MessageChunk, event_to_dict
        d = event_to_dict(MessageChunk(text="hello 你好"))
        s = json.dumps(d, ensure_ascii=False)
        assert "hello 你好" in s

    def test_tool_call_result_json(self):
        from spirit.api.events import ToolCallResult, event_to_dict
        d = event_to_dict(ToolCallResult(
            tool_name="x", tool_call_id="t", result="中文",
        ))
        s = json.dumps(d, ensure_ascii=False)
        assert "中文" in s
