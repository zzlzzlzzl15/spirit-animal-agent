"""TaskChecker — 任务执行前预检。

参考 Hermes 的 dep_ensure.py + file_safety.py 设计：
- 依赖检查（父任务是否完成、depends_on 列表）
- 环境检查（所需环境变量、命令行工具）
- 资源检查（磁盘空间）
- 冲突检查（互斥任务是否在执行）

设计原则：
- 预检失败不抛异常，返回 CheckResult 让调用方决策
- 每个检查项独立，一个失败不影响其他
- 支持自定义检查器注册
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from spirit.task.models import Task, TaskStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class MissingItem:
    """缺失项。"""
    name: str
    kind: str  # "env" | "tool" | "disk" | "dependency" | "conflict"
    detail: str = ""


@dataclass
class CheckResult:
    """预检结果。"""
    passed: bool = True
    failures: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    missing: List[MissingItem] = field(default_factory=list)

    def add_failure(self, msg: str, item: MissingItem = None) -> None:
        self.failures.append(msg)
        self.passed = False
        if item:
            self.missing.append(item)

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def summary(self) -> str:
        if self.passed:
            warn = f" ({len(self.warnings)} warnings)" if self.warnings else ""
            return f"PRE-CHECK PASSED{warn}"
        return f"PRE-CHECK FAILED: {'; '.join(self.failures)}"


# ---------------------------------------------------------------------------
# 预检器
# ---------------------------------------------------------------------------

# 最低磁盘空间要求（字节）— 100 MB
MIN_DISK_FREE_BYTES = 100 * 1024 * 1024


class TaskChecker:
    """任务执行前预检器。

    Usage:
        checker = TaskChecker(task_manager)
        result = checker.check_prerequisites(task)
        if not result.passed:
            task_manager.fail(task.id, error=result.summary())
    """

    def __init__(self, task_manager: Any = None):
        """初始化预检器。

        Args:
            task_manager: TaskManager 实例（用于依赖检查）
        """
        self.task_manager = task_manager
        # 自定义检查器: name -> callable(task) -> CheckResult
        self._custom_checks: Dict[str, Callable] = {}

    # ------------------------------------------------------------------
    # 注册自定义检查器
    # ------------------------------------------------------------------

    def register_check(self, name: str, check_fn: Callable) -> None:
        """注册自定义预检函数。

        Args:
            name: 检查器名称
            check_fn: 函数签名 (task: Task) -> CheckResult
        """
        self._custom_checks[name] = check_fn

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def check_prerequisites(self, task: Task) -> CheckResult:
        """执行所有预检。

        检查顺序：依赖 → 环境 → 资源 → 冲突 → 自定义
        每项独立执行，一个失败不影响其他。

        Args:
            task: 待检查的任务

        Returns:
            CheckResult 汇总结果
        """
        result = CheckResult()

        # 1. 依赖检查
        dep_result = self.check_dependencies(task)
        result.failures.extend(dep_result.failures)
        result.warnings.extend(dep_result.warnings)
        result.missing.extend(dep_result.missing)
        if not dep_result.passed:
            result.passed = False

        # 2. 环境检查
        env_result = self.check_environment(task)
        result.failures.extend(env_result.failures)
        result.warnings.extend(env_result.warnings)
        result.missing.extend(env_result.missing)
        if not env_result.passed:
            result.passed = False

        # 3. 资源检查
        res_result = self.check_resources(task)
        result.failures.extend(res_result.failures)
        result.warnings.extend(res_result.warnings)
        if not res_result.passed:
            result.passed = False

        # 4. 冲突检查
        conflict_result = self.check_conflicts(task)
        result.failures.extend(conflict_result.failures)
        result.warnings.extend(conflict_result.warnings)
        if not conflict_result.passed:
            result.passed = False

        # 5. 自定义检查器
        for name, check_fn in self._custom_checks.items():
            try:
                custom_result = check_fn(task)
                if isinstance(custom_result, CheckResult):
                    result.failures.extend(custom_result.failures)
                    result.warnings.extend(custom_result.warnings)
                    result.missing.extend(custom_result.missing)
                    if not custom_result.passed:
                        result.passed = False
            except Exception as e:
                logger.debug("自定义检查器 %s 失败: %s", name, e)
                result.add_warning(f"检查器 {name} 异常: {e}")

        logger.debug("预检完成 [%s]: %s", task.id, result.summary())
        return result

    # ------------------------------------------------------------------
    # 依赖检查
    # ------------------------------------------------------------------

    def check_dependencies(self, task: Task) -> CheckResult:
        """检查任务依赖是否满足。

        检查项：
        - 父任务（parent_id）是否已完成
        - metadata.depends_on 列表中的任务是否已完成
        """
        result = CheckResult()

        if not self.task_manager:
            return result

        # 检查父任务
        if task.parent_id:
            parent = self.task_manager.get(task.parent_id)
            if parent and parent.status != TaskStatus.COMPLETED:
                result.add_failure(
                    f"父任务 [{parent.id}] 未完成 (当前: {parent.status.value})",
                    MissingItem(
                        name=parent.id,
                        kind="dependency",
                        detail=f"父任务状态: {parent.status.value}",
                    ),
                )

        # 检查 depends_on 列表
        depends_on = task.metadata.get("depends_on", [])
        if isinstance(depends_on, str):
            depends_on = [depends_on]

        for dep_id in depends_on:
            dep_task = self.task_manager.get(dep_id)
            if not dep_task:
                result.add_failure(
                    f"依赖任务 [{dep_id}] 不存在",
                    MissingItem(
                        name=dep_id,
                        kind="dependency",
                        detail="任务不存在",
                    ),
                )
            elif dep_task.status != TaskStatus.COMPLETED:
                result.add_failure(
                    f"依赖任务 [{dep_id}] 未完成 (当前: {dep_task.status.value})",
                    MissingItem(
                        name=dep_id,
                        kind="dependency",
                        detail=f"状态: {dep_task.status.value}",
                    ),
                )

        return result

    # ------------------------------------------------------------------
    # 环境检查
    # ------------------------------------------------------------------

    def check_environment(self, task: Task) -> CheckResult:
        """检查任务所需的环境变量和命令行工具。

        从 task.metadata 读取：
        - required_env: ["ENV_VAR_1", "ENV_VAR_2"]
        - required_tools: ["git", "python", "node"]
        """
        result = CheckResult()

        # 检查环境变量
        required_env = task.metadata.get("required_env", [])
        if isinstance(required_env, str):
            required_env = [required_env]

        for env_key in required_env:
            if not os.getenv(env_key):
                result.add_failure(
                    f"缺少环境变量: {env_key}",
                    MissingItem(
                        name=env_key,
                        kind="env",
                        detail=f"环境变量 {env_key} 未设置",
                    ),
                )

        # 检查命令行工具
        required_tools = task.metadata.get("required_tools", [])
        if isinstance(required_tools, str):
            required_tools = [required_tools]

        for tool_name in required_tools:
            if not shutil.which(tool_name):
                result.add_failure(
                    f"缺少命令行工具: {tool_name}",
                    MissingItem(
                        name=tool_name,
                        kind="tool",
                        detail=f"工具 {tool_name} 不在 PATH 中",
                    ),
                )

        return result

    # ------------------------------------------------------------------
    # 资源检查
    # ------------------------------------------------------------------

    def check_resources(self, task: Task) -> CheckResult:
        """检查系统资源。

        检查项：
        - 磁盘空间（最低 100 MB）
        - 可从 task.metadata 覆盖: min_disk_mb
        """
        result = CheckResult()

        min_disk = task.metadata.get("min_disk_mb", MIN_DISK_FREE_BYTES // (1024 * 1024))
        min_bytes = min_disk * 1024 * 1024

        try:
            usage = shutil.disk_usage("/")
            if usage.free < min_bytes:
                free_mb = usage.free // (1024 * 1024)
                result.add_failure(
                    f"磁盘空间不足: 需要 {min_disk}MB, 可用 {free_mb}MB",
                    MissingItem(
                        name="disk",
                        kind="disk",
                        detail=f"需要 {min_disk}MB, 可用 {free_mb}MB",
                    ),
                )
        except Exception as e:
            result.add_warning(f"无法检查磁盘空间: {e}")

        return result

    # ------------------------------------------------------------------
    # 冲突检查
    # ------------------------------------------------------------------

    def check_conflicts(self, task: Task) -> CheckResult:
        """检查是否有互斥任务正在执行。

        从 task.metadata 读取：
        - exclusive_group: 同组任务不能并行（如 "deploy"）
        """
        result = CheckResult()

        if not self.task_manager:
            return result

        exclusive_group = task.metadata.get("exclusive_group", "")
        if not exclusive_group:
            return result

        # 查找同组正在运行的任务
        running = self.task_manager.list(status=TaskStatus.RUNNING)
        for running_task in running:
            if running_task.id == task.id:
                continue
            running_group = running_task.metadata.get("exclusive_group", "")
            if running_group == exclusive_group:
                result.add_failure(
                    f"互斥任务 [{running_task.id}] 正在执行 "
                    f"(同组: {exclusive_group})",
                    MissingItem(
                        name=running_task.id,
                        kind="conflict",
                        detail=f"互斥组 {exclusive_group} 中已有任务运行",
                    ),
                )

        return result


__all__ = ["TaskChecker", "CheckResult", "MissingItem"]
