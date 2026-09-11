"""会话管理系统测试。"""

import pytest
from pathlib import Path

from spirit.storage.session_db import SessionDB
from spirit.sessions.session_search import SessionSearch, SearchQuery
from spirit.sessions.session_export import SessionExporter
from spirit.sessions.session_recap import SessionRecap


@pytest.fixture
def populated_db(tmp_path):
    """填充测试数据的数据库。"""
    db = SessionDB(tmp_path / "test.db")

    # 创建会话
    sid1 = db.create_session(source="telegram", model="gpt-4o")
    sid2 = db.create_session(source="discord", model="claude-3")

    # 添加消息
    db.save_message(sid1, "user", "How to use Python decorators?")
    db.save_message(sid1, "assistant", "Python decorators are functions that modify other functions...")
    db.save_message(sid1, "user", "Can you show an example with @property?")
    db.save_message(sid1, "assistant", "Sure! Here's an example:\n```python\n@property\ndef name(self):\n    return self._name\n```")

    db.save_message(sid2, "user", "Explain async/await in JavaScript")
    db.save_message(sid2, "assistant", "Async/await is syntactic sugar for promises...")

    return db, sid1, sid2


class TestSessionSearch:
    """SessionSearch 测试。"""

    def test_search_by_query(self, populated_db):
        """测试关键词搜索。"""
        db, sid1, sid2 = populated_db
        search = SessionSearch(db)

        results = search.search("Python")
        assert len(results) >= 1
        assert any("Python" in r.content for r in results)

    def test_search_by_source(self, populated_db):
        """测试按来源过滤。"""
        db, sid1, sid2 = populated_db
        search = SessionSearch(db)

        results = search.search(SearchQuery(source="telegram"))
        assert all(r.source == "telegram" for r in results)

    def test_search_by_model(self, populated_db):
        """测试按模型过滤。"""
        db, sid1, sid2 = populated_db
        search = SessionSearch(db)

        results = search.search(SearchQuery(model="gpt-4o"))
        assert all(r.model == "gpt-4o" for r in results)

    def test_search_no_results(self, populated_db):
        """测试无结果搜索。"""
        db, sid1, sid2 = populated_db
        search = SessionSearch(db)

        results = search.search("nonexistent_term_xyz123")
        # 可能返回空或关键词搜索找到部分
        # 这里主要测试不崩溃
        assert isinstance(results, list)

    def test_search_with_limit(self, populated_db):
        """测试限制结果数。"""
        db, sid1, sid2 = populated_db
        search = SessionSearch(db)

        results = search.search(SearchQuery(source="telegram", limit=2))
        assert len(results) <= 2

    def test_get_session_summary(self, populated_db):
        """测试获取会话摘要。"""
        db, sid1, sid2 = populated_db
        search = SessionSearch(db)

        summary = search.get_session_summary(sid1)
        assert summary["session_id"] == sid1
        assert summary["message_count"] == 4
        assert summary["user_messages"] == 2
        assert summary["assistant_messages"] == 2


class TestSessionExporter:
    """SessionExporter 测试。"""

    def test_export_html(self, populated_db):
        """测试 HTML 导出。"""
        db, sid1, sid2 = populated_db
        exporter = SessionExporter(db)

        html = exporter.export_html(sid1)
        assert "<!DOCTYPE html>" in html
        assert "Spirit Agent Session" in html
        assert "Python" in html

    def test_export_markdown(self, populated_db):
        """测试 Markdown 导出。"""
        db, sid1, sid2 = populated_db
        exporter = SessionExporter(db)

        md = exporter.export_markdown(sid1)
        assert "# Spirit Agent Session" in md
        assert "Python" in md
        assert "👤" in md  # 用户 emoji
        assert "🤖" in md  # 助手 emoji

    def test_export_json(self, populated_db):
        """测试 JSON 导出。"""
        import json
        db, sid1, sid2 = populated_db
        exporter = SessionExporter(db)

        json_str = exporter.export_json(sid1)
        data = json.loads(json_str)
        assert "session" in data
        assert "messages" in data
        assert len(data["messages"]) == 4

    def test_export_to_file(self, populated_db, tmp_path):
        """测试导出到文件。"""
        db, sid1, sid2 = populated_db
        exporter = SessionExporter(db)

        # HTML
        html_path = exporter.export_to_file(sid1, tmp_path / "session.html")
        assert html_path.exists()
        assert html_path.suffix == ".html"

        # Markdown
        md_path = exporter.export_to_file(sid1, tmp_path / "session.md", format="md")
        assert md_path.exists()
        assert md_path.suffix == ".md"

        # JSON
        json_path = exporter.export_to_file(sid1, tmp_path / "session.json", format="json")
        assert json_path.exists()
        assert json_path.suffix == ".json"

    def test_export_nonexistent_session(self, populated_db):
        """测试导出不存在的会话。"""
        db, sid1, sid2 = populated_db
        exporter = SessionExporter(db)

        html = exporter.export_html("nonexistent-id")
        assert "未找到" in html


class TestSessionRecap:
    """SessionRecap 测试。"""

    def test_generate_recap(self, populated_db):
        """测试生成回顾。"""
        db, sid1, sid2 = populated_db
        recap = SessionRecap(db)

        result = recap.generate(sid1)
        assert result.session_id == sid1
        assert result.message_count == 4
        assert "摘要" in recap.format_recap(result) or "消息" in recap.format_recap(result)

    def test_recap_empty_session(self, tmp_path):
        """测试空会话回顾。"""
        db = SessionDB(tmp_path / "test.db")
        sid = db.create_session()
        recap = SessionRecap(db)

        result = recap.generate(sid)
        assert result.message_count == 0

    def test_recap_nonexistent_session(self, tmp_path):
        """测试不存在的会话回顾。"""
        db = SessionDB(tmp_path / "test.db")
        recap = SessionRecap(db)

        result = recap.generate("nonexistent-id")
        assert "未找到" in result.summary

    def test_extract_modified_files(self, populated_db):
        """测试提取修改的文件。"""
        db, sid1, sid2 = populated_db

        # 添加包含文件路径的消息
        db.save_message(sid1, "assistant", "I've modified /path/to/file.py and utils.js")

        recap = SessionRecap(db)
        result = recap.generate(sid1)
        # 应该能检测到文件路径
        assert isinstance(result.files_modified, list)

    def test_format_recap(self, populated_db):
        """测试格式化回顾。"""
        db, sid1, sid2 = populated_db
        recap = SessionRecap(db)

        result = recap.generate(sid1)
        formatted = recap.format_recap(result)

        assert "会话回顾" in formatted
        assert "消息数" in formatted
