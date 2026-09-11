"""TaskExecutor — 任务执行器。

功能：
- 任务执行（同步/异步）
- 子代理委托
- 进度追踪
- 超时控制
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from spirit.task.models import Task, TaskStatus, TaskType
from spirit.task.manager import TaskManager

logger = logging.getLogger(__name__)


class PreCheckError(Exception):
    """预检失败异常。"""

    def __init__(self, failures: list):
        self.failures = failures
        super().__init__(f"预检失败: {'; '.join(failures)}")


class TaskExecutor:
    """任务执行器 — 管理任务执行和委托。"""

    def __init__(
        self,
        task_manager: TaskManager,
        max_workers: int = 4,
        agent_factory: Callable = None,
        checker: Any = None,
        hook_manager: Any = None,
    ):
        """初始化任务执行器。

        Args:
            task_manager: 任务管理器
            max_workers: 最大工作线程数
            agent_factory: Agent 工厂函数（用于创建子代理）
            checker: TaskChecker 实例（可选，启用预检）
            hook_manager: HookManager 实例（可选，启用钩子）
        """
        self.task_manager = task_manager
        self.max_workers = max_workers
        self.agent_factory = agent_factory
        self.checker = checker
        self.hook_manager = hook_manager
        self._executor: Optional[ThreadPoolExecutor] = None
        self._running_tasks: Dict[str, Future] = {}
        self._lock = threading.Lock()

    def _get_executor(self) -> ThreadPoolExecutor:
        """获取线程池（延迟初始化）。"""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix="spirit-task",
            )
        return self._executor

    # ------------------------------------------------------------------
    # 同步执行
    # ------------------------------------------------------------------

    def execute(
        self,
        task_id: str,
        handler: Callable[[Task], str],
        timeout: float = 300.0,
    ) -> str:
        """同步执行任务。

        增强流程：
        1. 预检（如果配置了 checker）
        2. 触发预检钩子
        3. 标记 running → 执行
        4. 触发进度钩子
        5. 完成/失败

        Args:
            task_id: 任务 ID
            handler: 处理函数 (task) -> result
            timeout: 超时时间（秒）

        Returns:
            执行结果

        Raises:
            PreCheckError: 预检失败
            TimeoutError: 执行超时
            RuntimeError: 执行失败
        """
        task = self.task_manager.get(task_id)
        if not task:
            raise ValueError(f"任务不存在: {task_id}")

        if task.is_terminal:
            return task.result or ""

        # ── 新增：预检 ──
        if self.checker:
            try:
                check_result = self.checker.check_prerequisites(task)
                if not check_result.passed:
                    self.task_manager.fail(
                        task_id, error=f"预检失败: {check_result.summary()}"
                    )
                    # 触发预检失败钩子
                    if self.hook_manager:
                        self.hook_manager.emit(
                            "on_task_check",
                            task_id=task_id,
                            passed=False,
                            failures=check_result.failures,
                        )
                    raise PreCheckError(check_result.failures)
            except PreCheckError:
                raise
            except Exception as e:
                logger.debug("预检异常: %s", e)

        # 触发预检通过钩子
        if self.hook_manager:
            self.hook_manager.emit("on_task_check", task_id=task_id, passed=True)

        # 标记为执行中
        self.task_manager.update_status(task_id, TaskStatus.RUNNING)

        start_time = time.time()
        try:
            # 触发进度钩子: 开始
            if self.hook_manager:
                self.hook_manager.emit(
                    "on_task_progress", task_id=task_id, progress=0.0, message="开始执行",
                )

            result = handler(task)
            elapsed = time.time() - start_time

            # 触发进度钩子: 完成
            if self.hook_manager:
                self.hook_manager.emit(
                    "on_task_progress", task_id=task_id, progress=1.0, message="执行完成",
                )

            # 标记完成
            self.task_manager.complete(task_id, result=result)
            logger.info("任务完成: [%s] %.1fs", task_id, elapsed)
            return result

        except Exception as e:
            elapsed = time.time() - start_time
            error_msg = str(e)
            self.task_manager.fail(task_id, error=error_msg)
            logger.error("任务失败: [%s] %.1fs - %s", task_id, elapsed, error_msg)
            raise

    # ------------------------------------------------------------------
    # 异步执行
    # ------------------------------------------------------------------

    def execute_async(
        self,
        task_id: str,
        handler: Callable[[Task], str],
        timeout: float = 300.0,
        callback: Callable[[str, str, bool], None] = None,
    ) -> Future:
        """异步执行任务。

        Args:
            task_id: 任务 ID
            handler: 处理函数 (task) -> result
            timeout: 超时时间（秒）
            callback: 完成回调 (task_id, result, success)

        Returns:
            Future 对象
        """
        executor = self._get_executor()

        def _run():
            try:
                result = self.execute(task_id, handler, timeout)
                if callback:
                    callback(task_id, result, True)
                return result
            except Exception as e:
                if callback:
                    callback(task_id, str(e), False)
                raise

        future = executor.submit(_run)

        with self._lock:
            self._running_tasks[task_id] = future

        # 清理完成的任务
        def _cleanup(f):
            with self._lock:
                self._running_tasks.pop(task_id, None)

        future.add_done_callback(_cleanup)

        return future

    # ------------------------------------------------------------------
    # 委托执行
    # ------------------------------------------------------------------

    def delegate(
        self,
        task_id: str,
        message: str,
        timeout: float = 600.0,
    ) -> Future:
        """委托任务给子代理。

        Args:
            task_id: 任务 ID
            message: 委托消息
            timeout: 超时时间（秒）

        Returns:
            Future 对象
        """
        if not self.agent_factory:
            raise RuntimeError("未配置 agent_factory，无法委托任务")

        def _delegate_handler(task: Task) -> str:
            # 创建子代理
            sub_agent = self.agent_factory()

            # 更新任务类型为委托
            self.task_manager._update_task(task_id, {"task_type": TaskType.DELEGATED.value})

            # 执行委托
            result = sub_agent.chat(message)
            return result

        return self.execute_async(task_id, _delegate_handler, timeout)

    # ------------------------------------------------------------------
    # 控制
    # ------------------------------------------------------------------

    def cancel_task(self, task_id: str) -> bool:
        """取消正在执行的任务。"""
        with self._lock:
            future = self._running_tasks.get(task_id)

        if future:
            cancelled = future.cancel()
            if cancelled:
                self.task_manager.cancel(task_id)
            return cancelled

        # 任务不在运行中，直接标记取消
        return self.task_manager.cancel(task_id)

    def wait(self, task_id: str, timeout: float = None) -> Optional[str]:
        """等待任务完成。

        Args:
            task_id: 任务 ID
            timeout: 超时时间（秒）

        Returns:
            任务结果（如果完成）
        """
        with self._lock:
            future = self._running_tasks.get(task_id)

        if future:
            try:
                return future.result(timeout=timeout)
            except Exception as e:
                return f"[执行失败: {e}]"

        # 任务不在运行中，检查状态
        task = self.task_manager.get(task_id)
        if task and task.is_terminal:
            return task.result
        return None

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    def is_running(self, task_id: str) -> bool:
        """检查任务是否正在执行。"""
        with self._lock:
            return task_id in self._running_tasks

    def get_running_tasks(self) -> List[str]:
        """获取所有正在执行的任务 ID。"""
        with self._lock:
            return list(self._running_tasks.keys())

    def get_status(self, task_id: str) -> Dict[str, Any]:
        """获取任务状态摘要。"""
        task = self.task_manager.get(task_id)
        if not task:
            return {"error": "任务不存在"}

        return {
            "id": task.id,
            "title": task.title,
            "status": task.status.value,
            "is_running": self.is_running(task_id),
            "duration": task.duration,
            "progress": task.progress,
        }

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------

    def shutdown(self, wait: bool = True) -> None:
        """关闭执行器。"""
        if self._executor:
            self._executor.shutdown(wait=wait)
            self._executor = None

        with self._lock:
            self._running_tasks.clear()

        logger.info("任务执行器已关闭")


__all__ = ["TaskExecutor", "PreCheckError"]
