"""任务钩子 — 任务创建、执行、完成的事件处理。

增强版：
- 任务创建 → 状态广播
- 任务开始 → 触发预检
- 任务进度 → 定期上报
- 任务完成 → 自动启动就绪的子任务
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from spirit.hooks.hook_manager import HookManager, HookEvent

logger = logging.getLogger(__name__)


def register_task_hooks(hook_manager: HookManager, agent: Any) -> None:
    """注册默认任务钩子处理器。

    Args:
        hook_manager: 钩子管理器
        agent: SpiritAgent 实例（用于获取 task_manager / status_callback 等）
    """

    # 获取 agent 上的组件（安全访问）
    task_manager = getattr(agent, "task_manager", None)
    status_callback = getattr(agent, "status_callback", None)

    # ── 任务创建 ──

    def _on_task_create(
        task_id: str = "",
        title: str = "",
        task_type: str = "task",
        **kwargs,
    ):
        logger.info("📝 任务创建: [%s] %s (%s)", task_id, title, task_type)
        # 广播状态到 Gateway
        if status_callback:
            try:
                status_callback(
                    "task_created",
                    task_id=task_id,
                    title=title,
                    task_type=task_type,
                )
            except Exception as e:
                logger.debug("状态回调失败: %s", e)

    hook_manager.register(HookEvent.ON_TASK_CREATE, _on_task_create)

    # ── 任务预检 ──

    def _on_task_check(
        task_id: str = "",
        passed: bool = True,
        failures: list = None,
        **kwargs,
    ):
        if passed:
            logger.info("✅ 任务预检通过: %s", task_id)
        else:
            failure_msg = "; ".join(failures or ["未知"])
            logger.warning("❌ 任务预检失败: %s -> %s", task_id, failure_msg)
            if status_callback:
                try:
                    status_callback(
                        "task_check_failed",
                        task_id=task_id,
                        failures=failures,
                    )
                except Exception as e:
                    logger.debug("状态回调失败: %s", e)

    hook_manager.register(HookEvent.ON_TASK_CHECK, _on_task_check)

    # ── 任务开始 ──

    def _on_task_start(task_id: str = "", **kwargs):
        logger.info("🚀 任务开始: %s", task_id)
        if status_callback:
            try:
                status_callback("task_started", task_id=task_id)
            except Exception as e:
                logger.debug("状态回调失败: %s", e)

    hook_manager.register(HookEvent.ON_TASK_START, _on_task_start)

    # ── 任务进度 ──

    def _on_task_progress(
        task_id: str = "",
        progress: float = 0.0,
        message: str = "",
        **kwargs,
    ):
        logger.debug("📊 任务进度: %s %.0f%% %s", task_id, progress * 100, message)
        if status_callback:
            try:
                status_callback(
                    "task_progress",
                    task_id=task_id,
                    progress=progress,
                    message=message,
                )
            except Exception as e:
                logger.debug("状态回调失败: %s", e)

    hook_manager.register(HookEvent.ON_TASK_PROGRESS, _on_task_progress)

    # ── 辅助：自动启动就绪子任务 ──

    def _auto_start_ready_subtasks(parent_id: str) -> None:
        """检查并自动启动依赖已满足的 pending 子任务。"""
        if not task_manager:
            return
        try:
            from spirit.task.models import TaskStatus
            subtasks = task_manager.get_subtasks(parent_id)
            for st in subtasks:
                if st.status != TaskStatus.PENDING:
                    continue
                # 检查 depends_on 是否全部完成
                depends_on = st.metadata.get("depends_on", [])
                if isinstance(depends_on, str):
                    depends_on = [depends_on]
                all_deps_met = True
                for dep_id in depends_on:
                    dep_task = task_manager.get(dep_id)
                    if not dep_task or dep_task.status != TaskStatus.COMPLETED:
                        all_deps_met = False
                        break
                if all_deps_met:
                    logger.info(
                        "🚀 自动启动就绪子任务: %s (依赖已满足)", st.id,
                    )
                    task_manager.update_status(st.id, TaskStatus.RUNNING)
        except Exception as e:
            logger.debug("自动启动子任务失败: %s", e)

    # ── 任务完成 ──

    def _on_task_complete(
        task_id: str = "",
        result: str = "",
        **kwargs,
    ):
        logger.info("✅ 任务完成: %s", task_id)
        if status_callback:
            try:
                status_callback(
                    "task_completed",
                    task_id=task_id,
                    result=result[:200] if result else "",
                )
            except Exception as e:
                logger.debug("状态回调失败: %s", e)

        # 检查是否有等待此任务的子任务可以自动启动
        _auto_start_ready_subtasks(task_id)

    hook_manager.register(HookEvent.ON_TASK_COMPLETE, _on_task_complete)

    # ── 任务失败 ──

    def _on_task_fail(
        task_id: str = "",
        error: str = "",
        **kwargs,
    ):
        logger.warning("❌ 任务失败: %s -> %s", task_id, error)
        if status_callback:
            try:
                status_callback(
                    "task_failed",
                    task_id=task_id,
                    error=error[:200] if error else "",
                )
            except Exception as e:
                logger.debug("状态回调失败: %s", e)

    hook_manager.register(HookEvent.ON_TASK_FAIL, _on_task_fail)

    # ── 任务分解 ──

    def _on_task_decompose(
        task_id: str = "",
        subtask_count: int = 0,
        strategy: str = "",
        **kwargs,
    ):
        logger.info(
            "🔀 任务分解: %s -> %d 个子任务 (策略: %s)",
            task_id, subtask_count, strategy,
        )
        if status_callback:
            try:
                status_callback(
                    "task_decomposed",
                    task_id=task_id,
                    subtask_count=subtask_count,
                    strategy=strategy,
                )
            except Exception as e:
                logger.debug("状态回调失败: %s", e)

    hook_manager.register(HookEvent.ON_TASK_DECOMPOSE, _on_task_decompose)

    # ── 子任务创建 ──

    def _on_subtask_create(
        parent_id: str = "",
        subtask_id: str = "",
        title: str = "",
        **kwargs,
    ):
        logger.info("📎 子任务创建: %s -> %s [%s]", parent_id, title, subtask_id)

    hook_manager.register(HookEvent.ON_SUBTASK_CREATE, _on_subtask_create)

    # ── 子任务完成 ──

    def _on_subtask_complete(
        parent_id: str = "",
        subtask_id: str = "",
        **kwargs,
    ):
        logger.info("✅ 子任务完成: %s (父: %s)", subtask_id, parent_id)
    hook_manager.register(HookEvent.ON_SUBTASK_COMPLETE, _on_subtask_complete)

    # ── 子任务失败 ──

    def _on_subtask_fail(
        parent_id: str = "",
        subtask_id: str = "",
        error: str = "",
        **kwargs,
    ):
        logger.warning("❌ 子任务失败: %s -> %s", subtask_id, error)

    hook_manager.register(HookEvent.ON_SUBTASK_FAIL, _on_subtask_fail)


__all__ = ["register_task_hooks"]
