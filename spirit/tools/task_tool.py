"""任务管理工具 — task_manage（持久化任务系统）。

参考 Hermes 的 kanban + todo 设计：
- 连接持久化 TaskManager（SQLite），而非内存 TodoStore
- 支持任务创建、查询、更新、执行、状态查看
- 支持任务分解（自动拆分为子任务 DAG）
- 支持预检（执行前检查依赖/环境）

与 todo 工具的关系：
- todo: 轻量内存追踪（快速笔记式任务列表）
- task_manage: 重量持久化管理（完整任务生命周期）
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 全局组件引用（由 agent_init 注入）
# ---------------------------------------------------------------------------

_task_manager = None
_task_checker = None
_task_planner = None
_task_executor = None


def configure(
    task_manager=None,
    task_checker=None,
    task_planner=None,
    task_executor=None,
) -> None:
    """注入任务系统组件（由 agent_init 调用）。"""
    global _task_manager, _task_checker, _task_planner, _task_executor
    _task_manager = task_manager
    _task_checker = task_checker
    _task_planner = task_planner
    _task_executor = task_executor


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

TASK_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "task_manage",
        "description": (
            "管理持久化任务 — 创建/查询/更新/执行/分解任务。\n\n"
            "用法：\n"
            "- action=create: 创建新任务\n"
            "- action=list: 列出任务（支持按状态过滤）\n"
            "- action=status: 查看任务状态 + 子任务进度\n"
            "- action=update: 更新任务状态\n"
            "- action=decompose: 分解任务为子任务\n\n"
            "与 todo 工具不同，本工具任务持久化到 SQLite，支持任务依赖和自动分解。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "操作类型: create/list/status/update/decompose",
                    "enum": ["create", "list", "status", "update", "decompose"],
                },
                "task_id": {
                    "type": "string",
                    "description": "任务 ID（status/update/decompose 时必填）",
                },
                "title": {
                    "type": "string",
                    "description": "任务标题（create 时必填）",
                },
                "description": {
                    "type": "string",
                    "description": "任务描述",
                },
                "parent_id": {
                    "type": "string",
                    "description": "父任务 ID（创建子任务时）",
                },
                "status": {
                    "type": "string",
                    "description": "目标状态: pending/running/completed/failed/cancelled",
                    "enum": ["pending", "running", "completed", "failed", "cancelled"],
                },
                "tags": {
                    "type": "array",
                    "description": "任务标签",
                    "items": {"type": "string"},
                },
                "filter_status": {
                    "type": "string",
                    "description": "list 时按状态过滤",
                },
                "result": {
                    "type": "string",
                    "description": "任务结果（update 完成时）",
                },
                "error": {
                    "type": "string",
                    "description": "错误信息（update 失败时）",
                },
            },
            "required": ["action"],
        },
    },
}


# ---------------------------------------------------------------------------
# 实现
# ---------------------------------------------------------------------------

def _task_manage_impl(
    action: str = "list",
    task_id: str = "",
    title: str = "",
    description: str = "",
    parent_id: str = "",
    status: str = "",
    tags: List[str] = None,
    filter_status: str = "",
    result: str = "",
    error: str = "",
    **kwargs,
) -> str:
    """任务管理工具实现。"""

    if not _task_manager:
        return json.dumps({"error": "任务系统未初始化"}, ensure_ascii=False)

    try:
        if action == "create":
            return _handle_create(title, description, parent_id, tags)
        elif action == "list":
            return _handle_list(filter_status)
        elif action == "status":
            return _handle_status(task_id)
        elif action == "update":
            return _handle_update(task_id, status, result, error)
        elif action == "decompose":
            return _handle_decompose(task_id)
        else:
            return json.dumps(
                {"error": f"未知操作: {action}"},
                ensure_ascii=False,
            )
    except Exception as e:
        logger.error("task_manage 失败: %s", e)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _handle_create(
    title: str,
    description: str,
    parent_id: str,
    tags: List[str],
) -> str:
    """创建任务。"""
    if not title:
        return json.dumps({"error": "创建任务需要 title 参数"}, ensure_ascii=False)

    from spirit.task.models import TaskType

    task = _task_manager.create(
        title=title,
        description=description,
        task_type=TaskType.SUBTASK if parent_id else TaskType.TASK,
        parent_id=parent_id,
        tags=tags or [],
    )

    return json.dumps({
        "action": "create",
        "task": task.to_dict(),
        "message": f"任务已创建: [{task.id}] {task.title}",
    }, ensure_ascii=False, indent=2)


def _handle_list(filter_status: str) -> str:
    """列出任务。"""
    from spirit.task.models import TaskStatus

    status_filter = None
    if filter_status:
        try:
            status_filter = TaskStatus(filter_status)
        except ValueError:
            return json.dumps(
                {"error": f"无效状态: {filter_status}"},
                ensure_ascii=False,
            )

    tasks = _task_manager.list(status=status_filter, limit=50)
    task_dicts = [t.to_dict() for t in tasks]

    # 统计
    stats = _task_manager.get_stats()

    return json.dumps({
        "action": "list",
        "tasks": task_dicts,
        "count": len(task_dicts),
        "stats": stats,
    }, ensure_ascii=False, indent=2)


def _handle_status(task_id: str) -> str:
    """查看任务状态 + 子任务进度。"""
    if not task_id:
        return json.dumps({"error": "需要 task_id"}, ensure_ascii=False)

    task = _task_manager.get(task_id)
    if not task:
        return json.dumps({"error": f"任务不存在: {task_id}"}, ensure_ascii=False)

    # 获取子任务
    subtasks = _task_manager.get_subtasks(task_id)
    subtask_summary = []
    for st in subtasks:
        subtask_summary.append({
            "id": st.id,
            "title": st.title,
            "status": st.status.value,
        })

    # 计算子任务进度
    completed_count = len([s for s in subtasks if s.status.value == "completed"])
    total_count = len(subtasks)
    progress = completed_count / total_count if total_count > 0 else task.progress

    return json.dumps({
        "action": "status",
        "task": task.to_dict(),
        "subtasks": subtask_summary,
        "subtask_progress": {
            "completed": completed_count,
            "total": total_count,
            "progress": round(progress, 2),
        },
    }, ensure_ascii=False, indent=2)


def _handle_update(
    task_id: str,
    status: str,
    result: str,
    error: str,
) -> str:
    """更新任务状态。"""
    if not task_id:
        return json.dumps({"error": "需要 task_id"}, ensure_ascii=False)
    if not status:
        return json.dumps({"error": "需要 status"}, ensure_ascii=False)

    from spirit.task.models import TaskStatus

    try:
        target_status = TaskStatus(status)
    except ValueError:
        return json.dumps(
            {"error": f"无效状态: {status}"},
            ensure_ascii=False,
        )

    success = _task_manager.update_status(
        task_id, target_status, result=result, error=error,
    )

    if success:
        task = _task_manager.get(task_id)
        return json.dumps({
            "action": "update",
            "task": task.to_dict() if task else None,
            "message": f"任务已更新: [{task_id}] -> {status}",
        }, ensure_ascii=False, indent=2)
    else:
        return json.dumps(
            {"error": f"更新失败: 任务 {task_id} 不存在"},
            ensure_ascii=False,
        )


def _handle_decompose(task_id: str) -> str:
    """分解任务为子任务。"""
    if not task_id:
        return json.dumps({"error": "需要 task_id"}, ensure_ascii=False)

    if not _task_planner:
        return json.dumps(
            {"error": "任务分解器未初始化"},
            ensure_ascii=False,
        )

    task = _task_manager.get(task_id)
    if not task:
        return json.dumps({"error": f"任务不存在: {task_id}"}, ensure_ascii=False)

    result = _task_planner.decompose(task)
    if not result.success:
        return json.dumps({
            "action": "decompose",
            "success": False,
            "message": "无法分解: 未匹配到分解策略",
            "task_id": task_id,
        }, ensure_ascii=False)

    # 实际创建子任务
    created = _task_planner.create_subtasks(task, _task_manager)

    # 触发分解钩子
    if _task_manager.hook_manager:
        _task_manager.hook_manager.emit(
            "on_task_decompose",
            task_id=task_id,
            subtask_count=len(created),
            strategy=result.strategy,
        )

    subtask_list = [
        {"id": t.id, "title": t.title, "status": t.status.value}
        for t in created
    ]

    return json.dumps({
        "action": "decompose",
        "success": True,
        "task_id": task_id,
        "strategy": result.strategy,
        "subtask_count": len(created),
        "subtasks": subtask_list,
        "dependency_graph": result.dependency_graph,
    }, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

registry.register(
    name="task_manage",
    toolset="planning",
    schema=TASK_TOOL_SCHEMA,
    handler=_task_manage_impl,
    description="持久化任务管理",
    emoji="📋",
)


__all__ = ["configure", "TASK_TOOL_SCHEMA"]
