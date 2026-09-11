"""tests/agent/test_memory_manager.py — 记忆管理器测试。"""

import pytest
import time
from spirit.agent.memory_manager import MemoryManager, Memory


class TestMemory:
    """Memory 数据模型测试。"""

    def test_create_memory(self):
        mem = Memory(content="test fact", category="fact")
        assert mem.id
        assert mem.content == "test fact"
        assert mem.category == "fact"
        assert mem.created_at > 0

    def test_to_dict_and_back(self):
        mem = Memory(content="test", tags=["a", "b"], metadata={"k": "v"})
        data = mem.to_dict()
        restored = Memory.from_dict(data)
        assert restored.content == "test"
        assert restored.tags == ["a", "b"]


class TestMemoryManager:
    """MemoryManager 管理器测试。"""

    def test_remember(self, memory_manager):
        mem = memory_manager.remember("Python is great", category="fact")
        assert mem.content == "Python is great"
        assert memory_manager.count() == 1

    def test_remember_with_tags(self, memory_manager):
        memory_manager.remember("API key", tags=["secret", "config"])
        results = memory_manager.recall(tags=["secret"])
        assert len(results) >= 1

    def test_recall_by_query(self, memory_manager):
        memory_manager.remember("Python is great for ML")
        memory_manager.remember("JavaScript is great for web")
        results = memory_manager.recall(query="Python")
        assert len(results) >= 1
        assert any("Python" in m.content for m in results)

    def test_recall_by_category(self, memory_manager):
        memory_manager.remember("fact 1", category="fact")
        memory_manager.remember("skill 1", category="skill")
        facts = memory_manager.recall(category="fact")
        assert len(facts) == 1
        assert facts[0].category == "fact"

    def test_recall_recent(self, memory_manager):
        memory_manager.remember("old fact")
        memory_manager.remember("new fact")
        recent = memory_manager.recall_recent(limit=1)
        assert len(recent) == 1

    def test_update_memory(self, memory_manager):
        mem = memory_manager.remember("original")
        assert memory_manager.update(mem.id, content="updated") is True
        results = memory_manager.recall(query="updated")
        assert any(m.content == "updated" for m in results)

    def test_forget_memory(self, memory_manager):
        mem = memory_manager.remember("forgettable")
        assert memory_manager.count() == 1
        assert memory_manager.forget(mem.id) is True
        assert memory_manager.count() == 0

    def test_capacity_enforcement(self, tmp_db):
        mgr = MemoryManager(db_path=tmp_db, max_memories=5)
        for i in range(10):
            mgr.remember(f"memory {i}")
        assert mgr.count() <= 5
        mgr.close()

    def test_importance_filtering(self, memory_manager):
        memory_manager.remember("low importance", importance=0.1)
        memory_manager.remember("high importance", importance=0.9)
        results = memory_manager.recall(min_importance=0.5)
        assert len(results) == 1
        assert results[0].content == "high importance"

    def test_get_stats(self, memory_manager):
        memory_manager.remember("a", category="fact")
        memory_manager.remember("b", category="skill")
        stats = memory_manager.get_stats()
        assert stats["total"] == 2
        assert "by_category" in stats
