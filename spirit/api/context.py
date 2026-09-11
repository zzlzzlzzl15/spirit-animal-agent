"""工作区上下文 — 来自 VSCode 扩展的编辑器状态快照。

每个 Agent 会话持有一份 WorkspaceContext，由 VSCode 扩展通过
WebSocket 持续推送。工具调用时可读取此上下文来感知用户正在
编辑什么文件、选了哪些代码、工作区结构是什么。

设计原则：
- 只读数据容器，不持有任何行为
- 每次推送覆盖更新（非增量合并）
- 工具层通过 agent.workspace_context 访问
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CursorPosition:
    """光标位置。"""
    line: int = 0
    character: int = 0


@dataclass
class SelectionRange:
    """选区范围。"""
    start_line: int = 0
    end_line: int = 0


@dataclass
class WorkspaceContext:
    """VSCode 工作区上下文快照。

    由扩展侧 WorkspaceContextCollector 收集，通过 WebSocket 推送。
    Agent 工具（如 code_intelligence）可直接读取这些字段。
    """

    # 工作区
    workspace_root: Optional[str] = None

    # 活动文件
    active_file: Optional[str] = None
    language_id: Optional[str] = None

    # 选区
    selection: Optional[str] = None
    selection_range: Optional[SelectionRange] = None

    # 光标
    cursor_position: Optional[CursorPosition] = None
    current_line: Optional[str] = None

    # 打开的标签页
    open_tabs: List[str] = field(default_factory=list)

    # ── 工厂方法 ────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> WorkspaceContext:
        """从 WebSocket JSON 消息构建上下文。"""
        if not data:
            return cls()

        # 选区范围
        sel_range = None
        raw_range = data.get("selectionRange")
        if raw_range:
            sel_range = SelectionRange(
                start_line=raw_range.get("startLine", 0),
                end_line=raw_range.get("endLine", 0),
            )

        # 光标位置
        cursor = None
        raw_cursor = data.get("cursorPosition")
        if raw_cursor:
            cursor = CursorPosition(
                line=raw_cursor.get("line", 0),
                character=raw_cursor.get("character", 0),
            )

        return cls(
            workspace_root=data.get("workspaceRoot"),
            active_file=data.get("activeFile"),
            language_id=data.get("languageId"),
            selection=data.get("selection"),
            selection_range=sel_range,
            cursor_position=cursor,
            current_line=data.get("currentLine"),
            open_tabs=data.get("openTabs", []),
        )

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典（用于日志/调试）。"""
        result: Dict[str, Any] = {}
        if self.workspace_root:
            result["workspaceRoot"] = self.workspace_root
        if self.active_file:
            result["activeFile"] = self.active_file
        if self.language_id:
            result["languageId"] = self.language_id
        if self.selection:
            result["selection"] = self.selection[:200]  # 截断
        if self.selection_range:
            result["selectionRange"] = {
                "startLine": self.selection_range.start_line,
                "endLine": self.selection_range.end_line,
            }
        if self.cursor_position:
            result["cursorPosition"] = {
                "line": self.cursor_position.line,
                "character": self.cursor_position.character,
            }
        if self.current_line:
            result["currentLine"] = self.current_line
        if self.open_tabs:
            result["openTabs"] = self.open_tabs[:20]  # 限制数量
        return result

    @property
    def has_active_file(self) -> bool:
        return self.active_file is not None

    @property
    def has_selection(self) -> bool:
        return self.selection is not None and len(self.selection) > 0

    def summary(self) -> str:
        """生成人类可读的上下文摘要（注入系统提示词用）。"""
        parts = []
        if self.workspace_root:
            parts.append(f"工作区: {self.workspace_root}")
        if self.active_file:
            parts.append(f"当前文件: {self.active_file}")
            if self.language_id:
                parts.append(f"语言: {self.language_id}")
        if self.has_selection:
            lines = self.selection_range
            if lines:
                parts.append(
                    f"选中: 第 {lines.start_line + 1}-{lines.end_line + 1} 行"
                )
            else:
                parts.append("选中: 有代码被选中")
        if self.cursor_position:
            parts.append(
                f"光标: 第 {self.cursor_position.line + 1} 行"
            )
        if self.open_tabs:
            import os
            tab_names = [os.path.basename(t) for t in self.open_tabs[:5]]
            parts.append(f"打开文件: {', '.join(tab_names)}")
        return " | ".join(parts) if parts else "无编辑器上下文"


__all__ = [
    "WorkspaceContext",
    "CursorPosition",
    "SelectionRange",
]
