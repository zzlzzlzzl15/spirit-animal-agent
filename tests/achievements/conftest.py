"""成就系统测试共享 fixture —— 内存会话 DB + 隔离 SPIRIT_HOME。"""

import json
import sqlite3
import time

import pytest

from spirit import config


SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'cli',
    model TEXT NOT NULL,
    system_prompt TEXT,
    cwd TEXT,
    parent_session_id TEXT,
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    end_reason TEXT,
    metadata TEXT DEFAULT '{}'
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    created_at TIMESTAMP
);
"""


def make_ts(days_ago=0, hour=12, weekday_override=None):
    """返回距今 days_ago 天、指定小时的 SQLite 文本时间戳。"""
    t = time.time() - days_ago * 86400
    lt = time.localtime(t)
    return time.strftime("%Y-%m-%d ", lt) + f"{hour:02d}:30:00"


def ts_at(when: float, hour=12):
    """把一个 epoch 秒格式化为 SQLite 文本时间戳，并强制到指定小时。"""
    lt = time.localtime(when)
    return time.strftime("%Y-%m-%d ", lt) + f"{hour:02d}:00:00"


@pytest.fixture
def db():
    """内存会话 DB（含 Spirit schema）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


@pytest.fixture
def spirit_home(tmp_path, monkeypatch):
    """把 config.SPIRIT_HOME 指向 tmp，隔离成就 state 落盘。"""
    monkeypatch.setattr(config, "SPIRIT_HOME", tmp_path, raising=False)
    return tmp_path


# --- 造数据辅助 ---

def add_session(conn, sid, model="gpt-4", source="cli", started_at=None, metadata="{}"):
    conn.execute(
        "INSERT INTO sessions (id, source, model, started_at, metadata) "
        "VALUES (?, ?, ?, ?, ?)",
        (sid, source, model, started_at or make_ts(), metadata),
    )


def toolcall(names):
    """把工具名列表打包成 OpenAI 风格 tool_calls JSON。"""
    if isinstance(names, str):
        names = [names]
    return json.dumps([{"function": {"name": n}} for n in names])


def add_message(conn, sid, role="assistant", tool_calls=None):
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_calls) "
        "VALUES (?, ?, ?, ?)",
        (sid, role, "x", tool_calls),
    )
