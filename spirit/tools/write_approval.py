"""文件写入审批工具 — write_approval（安全审批机制）。

参考 Hermes 的 tools/write_approval.py 设计：
- 危险文件写入前需要用户审批
- 支持审批队列（暂存 → 审批 → 执行）
- 保护系统文件、配置文件、凭证文件
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 写入保护规则
# ---------------------------------------------------------------------------

# 受保护的路径模式（不允许写入）
DENIED_PATHS = {
    # 系统/凭证
    ".env", ".env.local", ".env.production",
    "id_rsa", "id_ed25519", ".ssh/config",
    ".gitconfig", ".npmrc", ".pypirc",
    # 包管理锁文件（不应手动修改）
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    # 系统文件
    "/etc/passwd", "/etc/shadow",
}

# 受保护的目录前缀
DENIED_PREFIXES = [
    ".ssh/", ".gnupg/", ".aws/credentials",
    "/etc/", "/proc/", "/sys/",
]

# 需要审批的操作（不拒绝，但需要确认）
APPROVAL_REQUIRED_PATTERNS = [
    "Dockerfile",
    "docker-compose.yml",
    ".github/workflows/",
    "Makefile",
    "pyproject.toml",
    "package.json",
]


def _is_denied_path(filepath: str) -> Optional[str]:
    """检查是否为禁止写入的路径。返回原因或 None。"""
    path = Path(filepath).resolve()
    name = path.name.lower()
    path_str = str(path)

    # 检查精确匹配
    for denied in DENIED_PATHS:
        if name == denied.lower() or path_str.endswith(denied):
            return f"禁止写入受保护的文件: {denied}"

    # 检查前缀
    for prefix in DENIED_PREFIXES:
        if path_str.startswith(prefix) or f"/{prefix}" in path_str:
            return f"禁止写入系统/凭证目录: {prefix}"

    return None


def _needs_approval(filepath: str) -> bool:
    """检查是否需要审批。"""
    path_str = str(Path(filepath).resolve())
    for pattern in APPROVAL_REQUIRED_PATTERNS:
        if pattern in path_str:
            return True
    return False


# ---------------------------------------------------------------------------
# 审批回调
# ---------------------------------------------------------------------------

_approval_callback: Optional[Callable] = None


def set_approval_callback(fn: Optional[Callable]) -> None:
    """设置审批回调。签名: callback(filepath, description) -> bool"""
    global _approval_callback
    _approval_callback = fn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

WRITE_APPROVAL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "check_write_safety",
        "description": (
            "检查文件写入是否安全（不会实际写入）。\n\n"
            "在执行 write_file / patch 之前调用，验证：\n"
            "- 目标路径是否受保护\n"
            "- 是否需要用户审批\n"
            "- 返回安全评估结果"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要写入的文件路径",
                },
                "description": {
                    "type": "string",
                    "description": "写入操作描述",
                },
            },
            "required": ["path"],
        },
    },
}


def _check_write_safety_impl(path: str, description: str = "") -> str:
    """检查写入安全性。"""
    # 1. 检查是否被禁止
    denied_reason = _is_denied_path(path)
    if denied_reason:
        return json.dumps({
            "allowed": False,
            "reason": "denied",
            "message": denied_reason,
            "path": path,
        })

    # 2. 检查是否需要审批
    needs_approval = _needs_approval(path)

    # 3. 检查文件是否存在（覆盖风险）
    exists = Path(path).exists()

    # 4. 检查文件大小（覆盖大文件风险）
    size_warning = False
    if exists:
        try:
            size = Path(path).stat().st_size
            if size > 100_000:  # > 100KB
                size_warning = True
        except OSError:
            pass

    result = {
        "allowed": True,
        "needs_approval": needs_approval,
        "path": path,
        "exists": exists,
        "size_warning": size_warning,
    }

    if needs_approval:
        result["message"] = "此文件为项目配置文件，建议确认后再修改"
    if size_warning:
        result["message"] = result.get("message", "") + "（目标文件较大，覆盖前请确认）"

    return json.dumps(result, ensure_ascii=False)


registry.register(
    name="check_write_safety",
    toolset="safety",
    schema=WRITE_APPROVAL_SCHEMA,
    handler=_check_write_safety_impl,
    description="检查文件写入安全性",
    emoji="🛡️",
)


# ---------------------------------------------------------------------------
# 审批队列（暂存待审批的操作）
# ---------------------------------------------------------------------------

_pending_approvals: List[Dict[str, Any]] = []


def stage_approval(filepath: str, description: str, content: str = "") -> str:
    """暂存一个待审批的写入操作。"""
    import uuid
    approval_id = str(uuid.uuid4())[:8]

    _pending_approvals.append({
        "id": approval_id,
        "path": filepath,
        "description": description,
        "content": content,
        "status": "pending",
    })

    return json.dumps({
        "staged": True,
        "approval_id": approval_id,
        "message": f"写入操作已暂存，等待审批 (ID: {approval_id})",
    })


def list_pending_approvals() -> str:
    """列出所有待审批的操作。"""
    pending = [a for a in _pending_approvals if a["status"] == "pending"]
    return json.dumps({
        "pending": pending,
        "count": len(pending),
    }, ensure_ascii=False, indent=2)


def approve_or_reject(approval_id: str, approved: bool) -> str:
    """审批或拒绝一个暂存的操作。"""
    for item in _pending_approvals:
        if item["id"] == approval_id and item["status"] == "pending":
            item["status"] = "approved" if approved else "rejected"
            return json.dumps({
                "id": approval_id,
                "status": item["status"],
                "path": item["path"],
            })
    return json.dumps({"error": f"未找到待审批项: {approval_id}"})
