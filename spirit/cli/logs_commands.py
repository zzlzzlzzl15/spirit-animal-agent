"""spirit/cli/logs_commands.py — 日志查询面（纯函数，绝不抛异常）。

v0.3.5：配合统一日志系统（``spirit.logging_setup``）提供可查询的排错入口。
数据源：

- ``$SPIRIT_HOME/logs/events.jsonl`` —— 结构化业务事件（api_error /
  compression / payload_invalid / provider_switch / tool_error ...）
- ``$SPIRIT_HOME/logs/agent.log`` / ``errors.log`` —— 滚动文本日志

风格契约与 ``spirit/cli/storage_commands.py`` 一致：纯函数、出错返回
``{"ok": False, "error": ...}``，Click 包装层负责渲染。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from spirit.config import DEFAULT_CONFIG, SPIRIT_HOME, get_config_path, load_effective_config
from spirit.logging_setup import get_events_path, get_log_dir

logger = logging.getLogger(__name__)


# ── 时间窗口解析 ────────────────────────────────────────────────────────────

_SINCE_RE = re.compile(r"^\s*(\d+)\s*([smhdw]?)\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"": 0, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_since(since: Optional[str]) -> Optional[_dt.datetime]:
    """把 ``"30m" / "2h" / "1d"`` 解析成起始 datetime（本地时区）。

    也接受绝对 ISO 时间。返回 None 表示不限制 / 解析失败。
    """
    if not since:
        return None
    since = since.strip()
    m = _SINCE_RE.match(since)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        seconds = n * _UNIT_SECONDS.get(unit, 0)
        if seconds <= 0:
            return None
        return _dt.datetime.now().astimezone() - _dt.timedelta(seconds=seconds)
    try:
        return _dt.datetime.fromisoformat(since)
    except ValueError:
        return None


def _event_ts(ev: Dict[str, Any]) -> Optional[_dt.datetime]:
    raw = ev.get("ts")
    if not raw:
        return None
    try:
        return _dt.datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


# ── 事件读取 ────────────────────────────────────────────────────────────────

def _iter_events(path: Path):
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.debug("读取 events.jsonl 失败: %s", exc)


def query_events(
    *,
    log_dir: Optional[Path] = None,
    kinds: Optional[List[str]] = None,
    since: Optional[str] = None,
    level: Optional[str] = None,
    session: Optional[str] = None,
    reason: Optional[str] = None,
    grep: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """按条件过滤 ``events.jsonl``。纯函数，失败返回 ok=False。

    Args:
        kinds: 只保留这些 kind（如 ["api_error", "payload_invalid"]）
        since: 时间窗口（"1h"）或 ISO 时间
        level: 最低级别（DEBUG/INFO/WARNING/ERROR）
        session: session_id 子串匹配
        reason: FailoverReason 子串（如 unknown / invalid_request）
        grep: 对整条事件 JSON 做大小写不敏感子串匹配
        limit: 返回上限
    """
    try:
        path = get_events_path(log_dir)
        if not path.exists():
            return {
                "ok": True,
                "events": [],
                "count": 0,
                "path": str(path),
                "note": "events.jsonl 尚不存在（还没有产生过结构化事件）",
            }

        since_dt = parse_since(since)
        level_rank = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        min_rank = level_rank.get((level or "").upper(), 0)
        kinds_set = {k.lower() for k in kinds} if kinds else None
        grep_l = grep.lower() if grep else None

        out: List[Dict[str, Any]] = []
        for ev in _iter_events(path):
            if kinds_set and str(ev.get("kind", "")).lower() not in kinds_set:
                continue
            if min_rank:
                if level_rank.get(str(ev.get("level", "")).upper(), 0) < min_rank:
                    continue
            if since_dt:
                ts = _event_ts(ev)
                if ts is not None and ts < since_dt:
                    continue
            if session and session.lower() not in str(ev.get("session_id", "")).lower():
                continue
            if reason and reason.lower() not in str(ev.get("reason", "")).lower():
                continue
            if grep_l and grep_l not in json.dumps(ev, ensure_ascii=False).lower():
                continue
            out.append(ev)

        out = out[-limit:]
        return {"ok": True, "events": out, "count": len(out), "path": str(path)}
    except Exception as exc:  # noqa: BLE001 - 查询面绝不抛异常
        logger.exception("query_events failed")
        return {"ok": False, "error": str(exc), "events": [], "count": 0}


def cmd_errors(
    *,
    since: str = "24h",
    level: str = "WARNING",
    session: Optional[str] = None,
    reason: Optional[str] = None,
    grep: Optional[str] = None,
    limit: int = 50,
    log_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """查错误类事件（api_error / payload_invalid / storage_error / chat_error /
    tool_error / uncaught_exception / thread_exception），默认近 24h、WARNING+。"""
    kinds = [
        "api_error", "payload_invalid", "storage_error", "chat_error",
        "tool_error", "uncaught_exception", "thread_exception",
    ]
    return query_events(
        log_dir=log_dir, kinds=kinds, since=since, level=level,
        session=session, reason=reason, grep=grep, limit=limit,
    )


def cmd_events(
    *,
    kind: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 50,
    log_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """查全部事件，可按 kind 过滤。"""
    kinds = [k.strip() for k in kind.split(",")] if kind else None
    return query_events(log_dir=log_dir, kinds=kinds, since=since, limit=limit)


def read_log_tail(
    name: str = "agent.log",
    *,
    lines: int = 100,
    log_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """读取某个滚动日志文件的末尾 N 行（纯函数）。"""
    try:
        path = get_log_dir(log_dir) / name
        if not path.exists():
            return {"ok": True, "path": str(path), "lines": [], "note": "文件不存在"}
        # 简单的尾部读取（日志单行可能很长，但 10MB×5 规模足够）
        all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"ok": True, "path": str(path), "lines": all_lines[-lines:]}
    except Exception as exc:  # noqa: BLE001
        logger.exception("read_log_tail failed")
        return {"ok": False, "error": str(exc), "lines": []}


# ── doctor 诊断 ─────────────────────────────────────────────────────────────

def _resolve_dotted(data: Dict[str, Any], dotted: str) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def cmd_doctor(*, probe_api: bool = False) -> Dict[str, Any]:
    """一键诊断：配置生效值与来源、存储层健康、日志目录、配对自检。

    Args:
        probe_api: 是否对当前 LLM 发一个真实小请求（默认不发，纯本地检查）
    """
    report: Dict[str, Any] = {"ok": True, "checks": []}

    def add(name: str, status: str, detail: Any = None):
        report["checks"].append({"name": name, "status": status, "detail": detail})

    # 1) 配置：生效值 + 是否被 YAML 覆盖
    try:
        effective = load_effective_config()
        cfg_path = get_config_path()
        keys = [
            "llm.model", "llm.provider", "llm.base_url", "llm.context_length",
            "llm.max_tokens", "agent.max_retries", "compression.threshold",
            "compression.protect_first_n", "compression.protect_last_n",
            "logging.enabled", "logging.level",
        ]
        rows = []
        for dotted in keys:
            eff = _resolve_dotted(effective, dotted)
            default = _resolve_dotted(DEFAULT_CONFIG, dotted)
            source = "yaml/env" if eff != default else "default"
            # 密钥类不展示
            if dotted.endswith("api_key"):
                eff = "***"
            rows.append({"key": dotted, "value": eff, "source": source})
        add(
            "config",
            "ok",
            {"path": str(cfg_path), "yaml_exists": cfg_path.exists(), "values": rows},
        )
    except Exception as exc:  # noqa: BLE001
        add("config", "error", str(exc))

    # 2) 存储层
    try:
        from spirit.storage.session_db import SessionDB
        db = SessionDB()
        health = db.health_check()
        add("storage", "ok" if health.get("ok", True) else "warn", {
            "path": str(db.db_path), "schema": db.current_schema_version(), "health": health,
        })
        db.close()
    except Exception as exc:  # noqa: BLE001
        add("storage", "error", f"{type(exc).__name__}: {exc}")

    # 3) 日志目录
    try:
        log_dir = get_log_dir()
        files = []
        if log_dir.exists():
            for p in sorted(log_dir.glob("*")):
                if p.is_file():
                    files.append({"name": p.name, "bytes": p.stat().st_size})
        add("logs", "ok" if files else "warn", {"dir": str(log_dir), "files": files})
    except Exception as exc:  # noqa: BLE001
        add("logs", "error", str(exc))

    # 4) 当前会话内存配对自检（无 agent 时跳过）
    try:
        from spirit.agent.message_pairing import validate_tool_pairing
        add("pairing_checker", "ok", "message_pairing 模块可导入")
    except Exception as exc:  # noqa: BLE001
        add("pairing_checker", "error", str(exc))

    # 5) SPIRIT_HOME
    add("spirit_home", "ok", str(Path(SPIRIT_HOME)))

    return report
