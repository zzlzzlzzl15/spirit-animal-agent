"""技能来源追踪 — Spirit Agent（Phase 4.6）。

对标 Hermes ``tools/skill_provenance.py``：用一个 ``ContextVar`` 区分「后台自我改进 fork
自动沉淀的技能写入」与「前台用户显式指示的技能写入」。Curator 只应整合 / 修剪它自己在
后台评审 fork 里创建的技能；用户让前台 agent 写的技能属于用户，绝不可被自动 curate。

信号约定（对齐 Hermes）：

- ``"foreground"``（默认）——常规（非评审）agent 从 CLI / 网关 / 定时任务 / 子 agent 发起的
  任何工具调用。
- ``"background_review"``——自我改进评审 fork；只有此来源下创建的技能才应标记为
  agent-created 交给 Curator 管理。

另附一个轻量的**安装来源记录器**（``record_install`` / ``get_install``），把市场安装的技能
来源（source / identifier / trust_level / source_url / installed_at）落到
``<hub>/provenance.json``，与 ``lock.json`` 互补：lock 面向 uninstall 的路径事实源，
provenance 面向「这技能从哪来」的人类可读溯源（``/skill info`` 展示）。
"""

from __future__ import annotations

import contextvars
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from spirit.skills_hub import paths

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 写入来源 ContextVar（对标 Hermes skill_provenance）
# ---------------------------------------------------------------------------

_write_origin: contextvars.ContextVar[str] = contextvars.ContextVar(
    "skill_write_origin",
    default="foreground",
)

# 后台评审 fork 使用的哨兵值（对标 Hermes BACKGROUND_REVIEW）。
BACKGROUND_REVIEW = "background_review"
FOREGROUND = "foreground"


def set_current_write_origin(origin: str) -> contextvars.Token:
    """把活动写入来源绑定到当前上下文，返回须在 ``finally`` 里 reset 的 Token。"""
    return _write_origin.set(origin or FOREGROUND)


def reset_current_write_origin(token: contextvars.Token) -> None:
    """恢复先前的写入来源上下文。"""
    _write_origin.reset(token)


def get_current_write_origin() -> str:
    """返回活动写入来源（默认 ``"foreground"``）。"""
    return _write_origin.get()


def is_background_review() -> bool:
    """便捷判定：当前写入来源是否为后台评审 fork。"""
    return get_current_write_origin() == BACKGROUND_REVIEW


# ---------------------------------------------------------------------------
# 安装来源记录（<hub>/provenance.json）
# ---------------------------------------------------------------------------

def _read_provenance() -> Dict[str, Any]:
    path = paths.provenance_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_provenance(data: Dict[str, Any]) -> None:
    path = paths.provenance_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.debug("无法写入技能来源记录: %s", exc)


def record_install(
    skill_name: str,
    *,
    source: str = "",
    identifier: str = "",
    trust_level: str = "community",
    source_url: str = "",
    content_hash: str = "",
) -> None:
    """记录一次市场安装的来源（幂等覆盖同名技能）。"""
    name = (skill_name or "").strip()
    if not name:
        return
    data = _read_provenance()
    data[name] = {
        "source": source,
        "identifier": identifier,
        "trust_level": trust_level,
        "source_url": source_url,
        "content_hash": content_hash,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_provenance(data)


def clear_install(skill_name: str) -> None:
    """移除某技能的来源记录（uninstall 时调用）。"""
    name = (skill_name or "").strip()
    if not name:
        return
    data = _read_provenance()
    if data.pop(name, None) is not None:
        _write_provenance(data)


def get_install(skill_name: str) -> Optional[Dict[str, Any]]:
    """返回某技能的安装来源记录（无则 None）。"""
    return _read_provenance().get((skill_name or "").strip())


def all_installs() -> Dict[str, Any]:
    """返回全部安装来源记录。"""
    return _read_provenance()


__all__ = [
    "BACKGROUND_REVIEW",
    "FOREGROUND",
    "set_current_write_origin",
    "reset_current_write_origin",
    "get_current_write_origin",
    "is_background_review",
    "record_install",
    "clear_install",
    "get_install",
    "all_installs",
]
