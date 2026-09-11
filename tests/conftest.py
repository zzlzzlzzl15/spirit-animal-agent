"""Spirit Agent 测试套件 — 共享 fixtures。"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# 临时目录
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_dir(tmp_path):
    """临时目录 fixture。"""
    return tmp_path


@pytest.fixture
def tmp_db(tmp_path):
    """临时 SQLite 数据库路径。"""
    return str(tmp_path / "test.db")


# ---------------------------------------------------------------------------
# Agent fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def agent_config():
    """测试用 Agent 配置。"""
    from spirit.agent.agent import AgentConfig
    return AgentConfig(
        model="gpt-4o-mini",
        api_key="test-key-12345",
        base_url="https://api.openai.com/v1",
        provider="openai",
        max_iterations=5,
        compression_enabled=False,
        streaming_enabled=False,
    )


@pytest.fixture
def agent(agent_config):
    """测试用 SpiritAgent 实例（不连接真实 API）。"""
    from spirit.agent.agent import SpiritAgent
    return SpiritAgent(config=agent_config)


# ---------------------------------------------------------------------------
# Hook fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def hook_manager():
    """测试用 HookManager。"""
    from spirit.hooks.hook_manager import HookManager
    return HookManager()


# ---------------------------------------------------------------------------
# Task fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def task_manager(tmp_db):
    """测试用 TaskManager。"""
    from spirit.task.manager import TaskManager
    mgr = TaskManager(db_path=tmp_db)
    yield mgr
    mgr.close()


@pytest.fixture
def task_executor(task_manager):
    """测试用 TaskExecutor。"""
    from spirit.task.executor import TaskExecutor
    executor = TaskExecutor(task_manager=task_manager, max_workers=2)
    yield executor
    executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Storage fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def session_db(tmp_db):
    """测试用 SessionDB。"""
    from spirit.storage.session_db import SessionDB
    db = SessionDB(db_path=Path(tmp_db))
    yield db
    db.close()


# ---------------------------------------------------------------------------
# Memory fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def memory_manager(tmp_db):
    """测试用 MemoryManager。"""
    from spirit.agent.memory_manager import MemoryManager
    mgr = MemoryManager(db_path=tmp_db)
    yield mgr
    mgr.close()


# ---------------------------------------------------------------------------
# Credential fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def credential_pool(tmp_path):
    """测试用 CredentialPool。"""
    from spirit.agent.credential_pool import CredentialPool
    pool = CredentialPool(storage_path=str(tmp_path / "creds.json"))
    return pool
