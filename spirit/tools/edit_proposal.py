"""编辑提案管理 — VSCode 模式下的文件编辑协议。

当 Agent 运行在 VSCode 模式（platform == "vscode"）时，
文件修改工具（write_file / patch / insert_at）不直接写磁盘，
而是生成 EditProposal 事件，由 VSCode 扩展展示 diff 预览，
用户选择 Accept 或 Reject 后才真正应用。

设计：
- EditProposalManager: 全局单例，管理提案的发送和等待
- 通过 agent 上的回调发送提案到 VSCode
- 通过 threading.Event 同步等待用户响应
- CLI 模式下不设置回调，工具直接写磁盘（兼容现有行为）
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 挂起的编辑提案
# ---------------------------------------------------------------------------

@dataclass
class _PendingEdit:
    """等待用户响应的编辑提案。"""
    edit_id: str
    event: threading.Event = field(default_factory=threading.Event)
    accepted: bool = False
    message: str = ""


# ---------------------------------------------------------------------------
# EditProposalManager
# ---------------------------------------------------------------------------

class EditProposalManager:
    """编辑提案管理器。

    每个 Agent 会话持有一个实例（通过 agent._edit_proposal_manager）。
    工具调用时检查是否处于 VSCode 模式，如果是则发送提案等待确认。
    """

    def __init__(self, send_fn: Optional[Callable] = None):
        """
        Args:
            send_fn: 发送编辑提案到 VSCode 的回调。
                     签名: send_fn(proposal: dict) -> None
        """
        self._send_fn = send_fn
        self._pending: Dict[str, _PendingEdit] = {}
        self._lock = threading.Lock()

    @property
    def is_vscode_mode(self) -> bool:
        """是否处于 VSCode 模式（有发送回调）。"""
        return self._send_fn is not None

    def propose_edit(
        self,
        file_path: str,
        original_content: str,
        new_content: str,
        description: str = "",
        timeout: float = 120.0,
    ) -> Dict[str, Any]:
        """发送编辑提案并等待用户响应。

        Args:
            file_path: 文件路径
            original_content: 原始内容
            new_content: 新内容
            description: 修改说明
            timeout: 等待用户响应的超时（秒）

        Returns:
            结果字典: {"accepted": bool, "message": str}
        """
        if not self.is_vscode_mode:
            return {"accepted": False, "message": "非 VSCode 模式"}

        edit_id = str(uuid.uuid4())[:12]
        pending = _PendingEdit(edit_id=edit_id)

        with self._lock:
            self._pending[edit_id] = pending

        try:
            # 发送提案到 VSCode
            self._send_fn({
                "type": "edit_proposal",
                "edit_id": edit_id,
                "file_path": file_path,
                "original_content": original_content,
                "new_content": new_content,
                "description": description,
            })

            # 等待用户响应
            if not pending.event.wait(timeout=timeout):
                return {"accepted": False, "message": f"用户未在 {timeout}s 内响应"}

            if pending.accepted:
                return {"accepted": True, "message": pending.message or "用户已接受"}
            else:
                return {"accepted": False, "message": pending.message or "用户已拒绝"}

        except Exception as e:
            return {"accepted": False, "message": f"提案失败: {e}"}
        finally:
            with self._lock:
                self._pending.pop(edit_id, None)

    def handle_response(self, edit_id: str, accepted: bool, message: str = ""):
        """处理来自 VSCode 的用户响应。"""
        with self._lock:
            pending = self._pending.get(edit_id)

        if pending:
            pending.accepted = accepted
            pending.message = message
            pending.event.set()
            logger.info(
                "编辑提案 %s: %s (%s)",
                edit_id,
                "已接受" if accepted else "已拒绝",
                message or "无备注",
            )
        else:
            logger.warning("收到未知编辑提案的响应: %s", edit_id)


# ---------------------------------------------------------------------------
# 全局管理器注册表
# ---------------------------------------------------------------------------

_managers: Dict[str, EditProposalManager] = {}


def get_manager(session_id: str) -> Optional[EditProposalManager]:
    """获取指定会话的编辑提案管理器。"""
    return _managers.get(session_id)


def register_manager(session_id: str, send_fn: Callable) -> EditProposalManager:
    """注册新的编辑提案管理器。"""
    mgr = EditProposalManager(send_fn)
    _managers[session_id] = mgr
    return mgr


def unregister_manager(session_id: str):
    """移除编辑提案管理器。"""
    _managers.pop(session_id, None)


__all__ = [
    "EditProposalManager",
    "get_manager",
    "register_manager",
    "unregister_manager",
]
