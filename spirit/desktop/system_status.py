"""系统状态采集 — CPU/RAM/Disk + Agent 运行指标。

为前端 StatusPopup / SystemPanel 提供数据源。
所有采集函数返回纯 dict，便于 JSON 序列化。
"""

from __future__ import annotations

import logging
import os
import platform
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 系统资源
# ---------------------------------------------------------------------------

def get_cpu_info() -> Dict[str, Any]:
    """采集 CPU 使用率。"""
    try:
        import psutil
        percent = psutil.cpu_percent(interval=0.1)
        per_cpu = psutil.cpu_percent(interval=0, percpu=True)
        freq = psutil.cpu_freq()
        return {
            "percent": percent,
            "per_cpu": per_cpu,
            "count": psutil.cpu_count(logical=True),
            "count_physical": psutil.cpu_count(logical=False),
            "freq_mhz": round(freq.current, 0) if freq else 0,
        }
    except ImportError:
        logger.debug("psutil 未安装，CPU 信息不可用")
        return {"percent": -1, "count": os.cpu_count() or 0}
    except Exception as exc:
        logger.debug("CPU 采集失败: %s", exc)
        return {"percent": -1}


def get_memory_info() -> Dict[str, Any]:
    """采集内存使用情况。"""
    try:
        import psutil
        vm = psutil.virtual_memory()
        return {
            "total_gb": round(vm.total / (1024 ** 3), 2),
            "used_gb": round(vm.used / (1024 ** 3), 2),
            "available_gb": round(vm.available / (1024 ** 3), 2),
            "percent": vm.percent,
        }
    except ImportError:
        return {"total_gb": -1, "used_gb": -1, "percent": -1}
    except Exception as exc:
        logger.debug("内存采集失败: %s", exc)
        return {"percent": -1}


def get_disk_info() -> Dict[str, Any]:
    """采集磁盘使用情况。"""
    try:
        import psutil
        # 获取主要分区
        partitions = psutil.disk_partitions(all=False)
        result = []
        for p in partitions:
            try:
                usage = psutil.disk_usage(p.mountpoint)
                result.append({
                    "mountpoint": p.mountpoint,
                    "device": p.device,
                    "fstype": p.fstype,
                    "total_gb": round(usage.total / (1024 ** 3), 1),
                    "used_gb": round(usage.used / (1024 ** 3), 1),
                    "percent": usage.percent,
                })
            except (PermissionError, OSError):
                continue
        return {"partitions": result}
    except ImportError:
        return {"partitions": []}
    except Exception as exc:
        logger.debug("磁盘采集失败: %s", exc)
        return {"partitions": []}


def get_system_info() -> Dict[str, Any]:
    """采集系统基本信息。"""
    return {
        "platform": platform.system(),
        "platform_release": platform.release(),
        "platform_version": platform.version(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "hostname": platform.node(),
        "uptime_seconds": _get_uptime(),
    }


def _get_uptime() -> float:
    """获取系统运行时间（秒）。"""
    try:
        import psutil
        boot = psutil.boot_time()
        return time.time() - boot
    except (ImportError, Exception):
        return -1


# ---------------------------------------------------------------------------
# Agent 状态
# ---------------------------------------------------------------------------

def get_agent_status(agent=None) -> Dict[str, Any]:
    """采集 Agent 运行状态。

    Args:
        agent: SpiritAgent 实例（可选）。如果为 None，返回占位数据。
    """
    if agent is None:
        return {
            "running": False,
            "model": "",
            "provider": "",
            "session_id": "",
            "message_count": 0,
            "tool_count": 0,
            "api_call_count": 0,
        }

    try:
        status = agent.get_status()
        return {
            "running": True,
            "model": status.get("model", ""),
            "provider": status.get("provider", ""),
            "session_id": status.get("session_id", "")[:8],
            "message_count": status.get("message_count", 0),
            "tool_count": status.get("tool_count", 0),
            "api_call_count": status.get("api_call_count", 0),
            "compression_enabled": status.get("compression_enabled", False),
            "platform": status.get("platform", ""),
        }
    except Exception as exc:
        logger.debug("Agent 状态采集失败: %s", exc)
        return {"running": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 综合快照
# ---------------------------------------------------------------------------

def get_full_status(agent=None) -> Dict[str, Any]:
    """获取完整系统状态快照。

    合并 CPU/RAM/Disk + Agent 状态，一次调用获取所有数据。
    """
    return {
        "system": get_system_info(),
        "cpu": get_cpu_info(),
        "memory": get_memory_info(),
        "disk": get_disk_info(),
        "agent": get_agent_status(agent),
        "timestamp": time.time(),
    }


__all__ = [
    "get_cpu_info",
    "get_memory_info",
    "get_disk_info",
    "get_system_info",
    "get_agent_status",
    "get_full_status",
]
