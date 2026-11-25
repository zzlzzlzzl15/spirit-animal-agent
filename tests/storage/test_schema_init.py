"""tests/storage/test_schema_init.py — 存储层 schema 初始化回归。

v0.3.4 曾把 ``# ...`` 注释写进 SQLite DDL（``executescript`` 不支持 ``#``，
只支持 ``--``），导致 ``SessionDB()`` 在 fresh 路径上必抛
``unrecognized token: "#"``，存储层整体不可用（会话/消息不落库）。本测试
锁死：全新数据库必须能初始化、能重新打开，且内置 schema 文本里不含
裸 ``#`` 注释。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from spirit.storage import schema as schema_mod
from spirit.storage.session_db import SessionDB


def test_full_schema_sql_has_no_hash_comments():
    """DDL 里不能出现行尾 ``#`` 注释（SQLite 只认 ``--``）。"""
    sql = schema_mod._FULL_SCHEMA_SQL
    offenders = []
    for lineno, line in enumerate(sql.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            offenders.append((lineno, line))
            continue
        # 行内 "#"（排除字符串字面量里的 # —— 当前 schema 没有字符串里带 # 的列）
        if "#" in line and "--" not in line.split("#", 1)[0]:
            offenders.append((lineno, line))
    assert not offenders, f"schema SQL 含非法 # 注释: {offenders[:5]}"


def test_executescript_on_inmemory_db():
    """直接对内存库执行完整 DDL 不应抛异常。"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(schema_mod._FULL_SCHEMA_SQL)
        conn.commit()
        # 关键表都应建出来
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for required in ("sessions", "messages", "schema_version"):
            assert required in names, f"缺少表 {required}"
    finally:
        conn.close()


def test_fresh_session_db_initializes(tmp_path: Path):
    """全新路径上 SessionDB 必须初始化成功（v0.3.4 回归点）。"""
    db_path = tmp_path / "fresh_schema_test.db"
    db = SessionDB(db_path=db_path)
    try:
        assert db.current_schema_version() == schema_mod.CURRENT_SCHEMA_VERSION
        health = db.health_check()
        assert health.get("ok", True) is not False
    finally:
        db.close()


def test_reopen_existing_db(tmp_path: Path):
    """已存在的库重新打开不应重放 DDL 失败。"""
    db_path = tmp_path / "reopen_test.db"
    db1 = SessionDB(db_path=db_path)
    db1.create_session(session_id="s1", source="test", model="m")
    db1.close()

    db2 = SessionDB(db_path=db_path)
    try:
        assert db2.get_session("s1") is not None
    finally:
        db2.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
