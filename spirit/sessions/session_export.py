"""会话导出 — 导出为 HTML/Markdown/JSON。

参考 Hermes 的 session_export_html.py 和 session_export_md.py：
- HTML 导出（带样式）
- Markdown 导出
- JSON 导出
- 支持会话元数据
"""

import html
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from spirit.storage.session_db import SessionDB

logger = logging.getLogger(__name__)


class SessionExporter:
    """会话导出器。

    支持格式：
    - HTML: 带样式的可读页面
    - Markdown: 纯文本对话记录
    - JSON: 结构化数据

    Usage:
        exporter = SessionExporter(session_db)
        html_content = exporter.export_html(session_id)
        exporter.export_to_file(session_id, "output.html")
    """

    def __init__(self, session_db: SessionDB):
        self.db = session_db

    # ------------------------------------------------------------------
    # HTML 导出
    # ------------------------------------------------------------------

    def export_html(self, session_id: str) -> str:
        """导出会话为 HTML。

        Args:
            session_id: 会话 ID

        Returns:
            HTML 字符串
        """
        session = self.db.get_session(session_id)
        if not session:
            return f"<p>会话 {session_id} 未找到</p>"

        messages = self.db.get_messages(session_id)

        # 构建 HTML
        html_parts = [
            "<!DOCTYPE html>",
            "<html lang='zh-CN'>",
            "<head>",
            "<meta charset='UTF-8'>",
            f"<title>Spirit Session - {session_id[:8]}</title>",
            "<style>",
            self._get_html_styles(),
            "</style>",
            "</head>",
            "<body>",
            "<div class='container'>",
            self._render_session_header(session),
            "<div class='messages'>",
        ]

        for msg in messages:
            html_parts.append(self._render_message_html(msg))

        html_parts.extend([
            "</div>",
            self._render_session_footer(session, messages),
            "</div>",
            "</body>",
            "</html>",
        ])

        return "\n".join(html_parts)

    def _render_session_header(self, session: Dict) -> str:
        """渲染会话头部。"""
        return f"""
        <div class='session-header'>
            <h1>Spirit Agent Session</h1>
            <div class='meta'>
                <span class='session-id'>ID: {session.get('id', '')[:8]}...</span>
                <span class='model'>Model: {session.get('model', 'unknown')}</span>
                <span class='source'>Source: {session.get('source', 'unknown')}</span>
                <span class='time'>Started: {session.get('started_at', '')}</span>
            </div>
        </div>
        """

    def _render_session_footer(self, session: Dict, messages: List[Dict]) -> str:
        """渲染会话尾部。"""
        user_count = sum(1 for m in messages if m["role"] == "user")
        assistant_count = sum(1 for m in messages if m["role"] == "assistant")
        return f"""
        <div class='session-footer'>
            <p>Total messages: {len(messages)} (User: {user_count}, Assistant: {assistant_count})</p>
            <p>Ended: {session.get('ended_at', 'Active')}</p>
        </div>
        """

    def _render_message_html(self, msg: Dict) -> str:
        """渲染单条消息。"""
        role = msg.get("role", "unknown")
        content = html.escape(msg.get("content", ""))
        timestamp = msg.get("created_at", "")

        role_class = f"message-{role}"
        role_label = {
            "user": "👤 User",
            "assistant": "🤖 Assistant",
            "system": "⚙️ System",
        }.get(role, role)

        # 处理代码块
        content = self._format_code_blocks(content)

        return f"""
        <div class='message {role_class}'>
            <div class='message-header'>
                <span class='role'>{role_label}</span>
                <span class='timestamp'>{timestamp}</span>
            </div>
            <div class='message-content'>{content}</div>
        </div>
        """

    def _format_code_blocks(self, text: str) -> str:
        """格式化代码块。"""
        import re
        # 处理 ```code``` 块
        def replace_code(match):
            lang = match.group(1) or ""
            code = html.escape(match.group(2))
            return f"<pre><code class='language-{lang}'>{code}</code></pre>"

        text = re.sub(r"```(\w*)\n?(.*?)```", replace_code, text, flags=re.DOTALL)

        # 处理 `inline code`
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)

        # 处理换行
        text = text.replace("\n", "<br>")

        return text

    def _get_html_styles(self) -> str:
        """获取 HTML 样式。"""
        return """
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f5f5;
            color: #333;
            line-height: 1.6;
        }
        .container {
            max-width: 900px;
            margin: 0 auto;
            padding: 20px;
        }
        .session-header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 12px;
            margin-bottom: 20px;
        }
        .session-header h1 { font-size: 24px; margin-bottom: 10px; }
        .meta { display: flex; gap: 20px; flex-wrap: wrap; font-size: 14px; opacity: 0.9; }
        .messages { display: flex; flex-direction: column; gap: 15px; }
        .message {
            background: white;
            border-radius: 12px;
            padding: 15px 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
        }
        .message-user { border-left: 4px solid #667eea; }
        .message-assistant { border-left: 4px solid #48bb78; }
        .message-system { border-left: 4px solid #ed8936; background: #fffaf0; }
        .message-header {
            display: flex;
            justify-content: space-between;
            margin-bottom: 10px;
            font-size: 13px;
            color: #666;
        }
        .role { font-weight: 600; }
        .message-content { white-space: pre-wrap; word-break: break-word; }
        .message-content pre {
            background: #2d3748;
            color: #e2e8f0;
            padding: 12px;
            border-radius: 6px;
            overflow-x: auto;
            margin: 10px 0;
        }
        .message-content code {
            background: #edf2f7;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 0.9em;
        }
        .message-content pre code { background: transparent; padding: 0; }
        .session-footer {
            margin-top: 20px;
            padding: 20px;
            background: white;
            border-radius: 12px;
            text-align: center;
            color: #666;
            font-size: 14px;
        }
        """

    # ------------------------------------------------------------------
    # Markdown 导出
    # ------------------------------------------------------------------

    def export_markdown(self, session_id: str) -> str:
        """导出会话为 Markdown。

        Args:
            session_id: 会话 ID

        Returns:
            Markdown 字符串
        """
        session = self.db.get_session(session_id)
        if not session:
            return f"# Session {session_id} not found"

        messages = self.db.get_messages(session_id)

        parts = [
            f"# Spirit Agent Session",
            "",
            f"- **Session ID**: `{session_id}`",
            f"- **Model**: {session.get('model', 'unknown')}",
            f"- **Source**: {session.get('source', 'unknown')}",
            f"- **Started**: {session.get('started_at', '')}",
            f"- **Ended**: {session.get('ended_at', 'Active')}",
            "",
            "---",
            "",
        ]

        for msg in messages:
            parts.append(self._render_message_markdown(msg))
            parts.append("")

        # 统计
        user_count = sum(1 for m in messages if m["role"] == "user")
        assistant_count = sum(1 for m in messages if m["role"] == "assistant")
        parts.extend([
            "---",
            "",
            f"*Total: {len(messages)} messages (User: {user_count}, Assistant: {assistant_count})*",
        ])

        return "\n".join(parts)

    def _render_message_markdown(self, msg: Dict) -> str:
        """渲染单条消息为 Markdown。"""
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        timestamp = msg.get("created_at", "")

        role_emoji = {
            "user": "👤",
            "assistant": "🤖",
            "system": "⚙️",
        }.get(role, "❓")

        return f"### {role_emoji} {role.capitalize()} `{timestamp}`\n\n{content}"

    # ------------------------------------------------------------------
    # JSON 导出
    # ------------------------------------------------------------------

    def export_json(self, session_id: str) -> str:
        """导出会话为 JSON。

        Args:
            session_id: 会话 ID

        Returns:
            JSON 字符串
        """
        session = self.db.get_session(session_id)
        if not session:
            return json.dumps({"error": f"Session {session_id} not found"})

        messages = self.db.get_messages(session_id)

        data = {
            "session": session,
            "messages": messages,
            "metadata": {
                "exported_at": datetime.now().isoformat(),
                "message_count": len(messages),
                "user_messages": sum(1 for m in messages if m["role"] == "user"),
                "assistant_messages": sum(1 for m in messages if m["role"] == "assistant"),
            },
        }

        return json.dumps(data, indent=2, ensure_ascii=False, default=str)

    # ------------------------------------------------------------------
    # 文件导出
    # ------------------------------------------------------------------

    def export_to_file(
        self,
        session_id: str,
        output_path: str | Path,
        format: str = "html",
    ) -> Path:
        """导出会话到文件。

        Args:
            session_id: 会话 ID
            output_path: 输出文件路径
            format: 导出格式 (html/md/json)

        Returns:
            输出文件路径
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if format == "html":
            content = self.export_html(session_id)
            if not output_path.suffix:
                output_path = output_path.with_suffix(".html")
        elif format in ("md", "markdown"):
            content = self.export_markdown(session_id)
            if not output_path.suffix:
                output_path = output_path.with_suffix(".md")
        elif format == "json":
            content = self.export_json(session_id)
            if not output_path.suffix:
                output_path = output_path.with_suffix(".json")
        else:
            raise ValueError(f"不支持的格式: {format}")

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info("会话已导出: %s -> %s", session_id[:8], output_path)
        return output_path
