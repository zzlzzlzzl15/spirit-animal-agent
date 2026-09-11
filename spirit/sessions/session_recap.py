"""会话回顾/摘要。

参考 Hermes 的 hermes_cli/session_recap.py：
- 生成会话摘要
- 提取关键决策
- 生成待办事项
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from spirit.storage.session_db import SessionDB

logger = logging.getLogger(__name__)


@dataclass
class SessionRecapResult:
    """会话回顾结果。"""
    session_id: str
    summary: str
    key_points: List[str] = field(default_factory=list)
    decisions: List[str] = field(default_factory=list)
    action_items: List[str] = field(default_factory=list)
    files_modified: List[str] = field(default_factory=list)
    tools_used: List[str] = field(default_factory=list)
    duration_minutes: float = 0.0
    message_count: int = 0


class SessionRecap:
    """会话回顾生成器。

    功能：
    - 生成会话摘要
    - 提取关键信息点
    - 识别修改的文件
    - 统计工具使用情况

    Usage:
        recap = SessionRecap(session_db)
        result = recap.generate(session_id)
        print(result.summary)
    """

    def __init__(self, session_db: SessionDB):
        self.db = session_db

    def generate(self, session_id: str) -> SessionRecapResult:
        """生成会话回顾。

        Args:
            session_id: 会话 ID

        Returns:
            SessionRecapResult
        """
        session = self.db.get_session(session_id)
        if not session:
            return SessionRecapResult(
                session_id=session_id,
                summary="会话未找到",
            )

        messages = self.db.get_messages(session_id)

        # 生成摘要
        summary = self._generate_summary(messages)

        # 提取关键点
        key_points = self._extract_key_points(messages)

        # 识别决策
        decisions = self._extract_decisions(messages)

        # 识别修改的文件
        files_modified = self._extract_modified_files(messages)

        # 统计工具使用
        tools_used = self._extract_tools_used(messages)

        # 计算时长
        duration = self._calculate_duration(session)

        return SessionRecapResult(
            session_id=session_id,
            summary=summary,
            key_points=key_points,
            decisions=decisions,
            action_items=[],  # 需要 LLM 生成
            files_modified=files_modified,
            tools_used=tools_used,
            duration_minutes=duration,
            message_count=len(messages),
        )

    def _generate_summary(self, messages: List[Dict]) -> str:
        """生成会话摘要。

        简单版本：提取最后几条 assistant 消息作为摘要。
        完整版本需要 LLM 参与。
        """
        if not messages:
            return "空会话"

        # 统计
        user_msgs = [m for m in messages if m["role"] == "user"]
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]

        # 提取第一条用户消息作为主题线索
        first_user_msg = ""
        if user_msgs:
            first_user_msg = user_msgs[0].get("content", "")[:200]

        # 提取最后一条 assistant 消息作为结论
        last_assistant = ""
        if assistant_msgs:
            last_assistant = assistant_msgs[-1].get("content", "")[:300]

        parts = [
            f"共 {len(messages)} 条消息",
            f"({len(user_msgs)} 用户, {len(assistant_msgs)} 助手)",
        ]

        if first_user_msg:
            parts.append(f"\n开始话题: {first_user_msg}...")

        if last_assistant:
            parts.append(f"\n最后回复: {last_assistant}...")

        return "\n".join(parts)

    def _extract_key_points(self, messages: List[Dict]) -> List[str]:
        """提取关键点。

        简单启发式：提取包含特定关键词的消息。
        """
        key_points = []
        keywords = ["重要", "关键", "注意", "记住", "总结", "important", "key", "note", "summary"]

        for msg in messages:
            content = msg.get("content", "").lower()
            for kw in keywords:
                if kw in content:
                    # 提取包含关键词的句子
                    for line in content.split("\n"):
                        if kw in line.lower() and len(line) > 10:
                            key_points.append(line[:200])
                            break
                    break

        return key_points[:10]  # 最多 10 个关键点

    def _extract_decisions(self, messages: List[Dict]) -> List[str]:
        """提取决策。

        简单启发式：提取包含决策相关词汇的内容。
        """
        decisions = []
        decision_keywords = ["决定", "选择", "采用", "使用", "改为", "decided", "choose", "will use"]

        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", "").lower()
            for kw in decision_keywords:
                if kw in content:
                    # 提取包含决策词的句子
                    for line in content.split("\n"):
                        if kw in line.lower() and len(line) > 10:
                            decisions.append(line[:200])
                            break
                    break

        return decisions[:10]

    def _extract_modified_files(self, messages: List[Dict]) -> List[str]:
        """提取修改的文件列表。

        从工具调用结果中提取文件路径。
        """
        files = set()
        file_patterns = [".py", ".js", ".ts", ".json", ".yaml", ".md", ".txt", ".html", ".css"]

        for msg in messages:
            content = msg.get("content", "")

            # 查找文件路径模式
            import re
            # 匹配常见文件路径
            paths = re.findall(r'[/\w.-]+\.\w{1,5}', content)
            for path in paths:
                if any(path.endswith(ext) for ext in file_patterns):
                    # 排除常见非文件路径
                    if not any(x in path.lower() for x in ["http", "www", "node_modules", ".git"]):
                        files.add(path)

        return list(files)[:20]

    def _extract_tools_used(self, messages: List[Dict]) -> List[str]:
        """提取使用的工具列表。"""
        tools = set()

        for msg in messages:
            # 检查 tool_calls
            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                if isinstance(tc, dict):
                    func = tc.get("function", {})
                    name = func.get("name", "")
                    if name:
                        tools.add(name)

            # 检查 tool_call_id（工具结果）
            if msg.get("role") == "tool":
                name = msg.get("name", "")
                if name:
                    tools.add(name)

        return list(tools)

    def _calculate_duration(self, session: Dict) -> float:
        """计算会话时长（分钟）。"""
        from datetime import datetime

        started = session.get("started_at")
        ended = session.get("ended_at")

        if not started:
            return 0.0

        try:
            start_time = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            if ended:
                end_time = datetime.fromisoformat(str(ended).replace("Z", "+00:00"))
            else:
                end_time = datetime.now()

            delta = end_time - start_time
            return delta.total_seconds() / 60.0
        except Exception:
            return 0.0

    def format_recap(self, result: SessionRecapResult) -> str:
        """格式化回顾结果为可读文本。"""
        parts = [
            f"## 会话回顾: {result.session_id[:8]}...",
            "",
            f"**时长**: {result.duration_minutes:.1f} 分钟",
            f"**消息数**: {result.message_count}",
            "",
            "### 摘要",
            result.summary,
            "",
        ]

        if result.key_points:
            parts.append("### 关键点")
            for point in result.key_points:
                parts.append(f"- {point}")
            parts.append("")

        if result.decisions:
            parts.append("### 决策")
            for decision in result.decisions:
                parts.append(f"- {decision}")
            parts.append("")

        if result.files_modified:
            parts.append("### 修改的文件")
            for f in result.files_modified:
                parts.append(f"- `{f}`")
            parts.append("")

        if result.tools_used:
            parts.append("### 使用的工具")
            parts.append(", ".join(result.tools_used))
            parts.append("")

        return "\n".join(parts)
