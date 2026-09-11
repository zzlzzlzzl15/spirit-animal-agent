"""TaskPlanner — 任务分解器。

参考 Hermes 的 kanban_decompose.py 设计（简化版）：
- 基于模板的分解（预定义模式匹配）
- 基于规则的分解（文件操作 → 读/写/验证）
- 顺序分解 / 并行分解 / 模板匹配
- 返回子任务列表（带依赖关系）

设计原则：
- 分解是确定性的（不调用 LLM），LLM 辅助通过外部扩展实现
- 每种分解策略独立，可组合
- 支持自定义分解器注册
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from spirit.task.models import Task, TaskStatus, TaskType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class SubtaskSpec:
    """子任务规格 — 分解后的子任务描述。"""

    title: str
    description: str = ""
    depends_on: List[str] = field(default_factory=list)  # 依赖的 subtask title 列表
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    estimated_seconds: float = 0.0


@dataclass
class DecomposeResult:
    """分解结果。"""

    subtasks: List[SubtaskSpec] = field(default_factory=list)
    strategy: str = "none"  # 使用的分解策略名称
    success: bool = False

    @property
    def dependency_graph(self) -> Dict[str, List[str]]:
        """返回依赖图 {title: [depends_on_titles]}。"""
        return {st.title: st.depends_on for st in self.subtasks}

    def topological_order(self) -> List[SubtaskSpec]:
        """拓扑排序 — 返回执行顺序。"""
        if not self.subtasks:
            return []

        # 构建邻接表
        in_degree: Dict[str, int] = {}
        graph: Dict[str, List[str]] = {}
        title_map: Dict[str, SubtaskSpec] = {}

        for st in self.subtasks:
            title_map[st.title] = st
            in_degree.setdefault(st.title, 0)
            graph.setdefault(st.title, [])

        for st in self.subtasks:
            for dep in st.depends_on:
                if dep in title_map:
                    graph[dep].append(st.title)
                    in_degree[st.title] = in_degree.get(st.title, 0) + 1

        # BFS 拓扑排序
        queue = [t for t in in_degree if in_degree[t] == 0]
        order: List[str] = []

        while queue:
            node = queue.pop(0)
            order.append(node)
            for neighbor in graph.get(node, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        return [title_map[t] for t in order if t in title_map]


# ---------------------------------------------------------------------------
# 分解策略模板
# ---------------------------------------------------------------------------

# 预定义模板: (匹配正则, 子任务列表)
DECOMPOSE_TEMPLATES: List[Tuple[str, List[SubtaskSpec]]] = [
    # ── 功能实现 ──
    (
        r"(?:实现|开发|创建|添加|新增).*(?:功能|模块|组件|接口|API)",
        [
            SubtaskSpec(
                title="设计",
                description="分析需求，设计接口和数据结构",
                estimated_seconds=300,
            ),
            SubtaskSpec(
                title="编码",
                description="实现核心逻辑",
                depends_on=["设计"],
                estimated_seconds=600,
            ),
            SubtaskSpec(
                title="测试",
                description="编写单元测试和集成测试",
                depends_on=["编码"],
                estimated_seconds=300,
            ),
            SubtaskSpec(
                title="文档",
                description="更新文档和注释",
                depends_on=["编码"],
                estimated_seconds=120,
            ),
        ],
    ),
    # ── Bug 修复 ──
    (
        r"(?:修复|fix|解决|处理).*(?:bug|问题|错误|异常|故障)",
        [
            SubtaskSpec(
                title="复现",
                description="复现问题，确认根因",
                estimated_seconds=180,
            ),
            SubtaskSpec(
                title="修复",
                description="实施修复方案",
                depends_on=["复现"],
                estimated_seconds=300,
            ),
            SubtaskSpec(
                title="验证",
                description="验证修复效果，确保无回归",
                depends_on=["修复"],
                estimated_seconds=180,
            ),
        ],
    ),
    # ── 重构 ──
    (
        r"(?:重构|优化|改进|重写|refactor).*(?:代码|模块|架构|结构)",
        [
            SubtaskSpec(
                title="分析",
                description="分析现有代码，确定重构范围",
                estimated_seconds=240,
            ),
            SubtaskSpec(
                title="重构实施",
                description="执行重构",
                depends_on=["分析"],
                estimated_seconds=600,
            ),
            SubtaskSpec(
                title="测试验证",
                description="运行测试确保无回归",
                depends_on=["重构实施"],
                estimated_seconds=300,
            ),
        ],
    ),
    # ── 部署 ──
    (
        r"(?:部署|发布|上线|deploy|release)",
        [
            SubtaskSpec(
                title="构建",
                description="构建产物",
                estimated_seconds=180,
            ),
            SubtaskSpec(
                title="测试",
                description="部署前测试",
                depends_on=["构建"],
                estimated_seconds=300,
            ),
            SubtaskSpec(
                title="部署",
                description="执行部署",
                depends_on=["测试"],
                estimated_seconds=180,
            ),
            SubtaskSpec(
                title="验证",
                description="部署后验证",
                depends_on=["部署"],
                estimated_seconds=120,
            ),
        ],
    ),
    # ── 调研 ──
    (
        r"(?:调研|研究|分析|评估|对比).*(?:方案|技术|框架|工具|库)",
        [
            SubtaskSpec(
                title="信息收集",
                description="搜集相关资料和文档",
                estimated_seconds=300,
            ),
            SubtaskSpec(
                title="对比分析",
                description="对比各方案优劣",
                depends_on=["信息收集"],
                estimated_seconds=300,
            ),
            SubtaskSpec(
                title="结论报告",
                description="输出调研报告和建议",
                depends_on=["对比分析"],
                estimated_seconds=180,
            ),
        ],
    ),
]


# ---------------------------------------------------------------------------
# 分解器
# ---------------------------------------------------------------------------

class TaskPlanner:
    """任务分解器 — 将复杂任务拆分为子任务 DAG。

    Usage:
        planner = TaskPlanner()
        result = planner.decompose(task)
        if result.success:
            for spec in result.topological_order():
                # 按顺序创建子任务
                ...
    """

    def __init__(self):
        # 自定义分解器: name -> callable(task) -> DecomposeResult | None
        self._custom_decomposers: Dict[str, Callable] = {}
        # 内置模板（可追加）
        self._templates: List[Tuple[str, List[SubtaskSpec]]] = list(DECOMPOSE_TEMPLATES)

    # ------------------------------------------------------------------
    # 注册自定义分解器
    # ------------------------------------------------------------------

    def register_decomposer(self, name: str, decomposer_fn: Callable) -> None:
        """注册自定义分解器。

        Args:
            name: 分解器名称
            decomposer_fn: 函数签名 (task: Task) -> DecomposeResult | None
                           返回 None 表示该分解器不适用
        """
        self._custom_decomposers[name] = decomposer_fn

    def add_template(self, pattern: str, subtasks: List[SubtaskSpec]) -> None:
        """添加自定义模板。

        Args:
            pattern: 正则表达式（匹配任务标题/描述）
            subtasks: 子任务规格列表
        """
        self._templates.append((pattern, subtasks))

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def decompose(self, task: Task) -> DecomposeResult:
        """将任务分解为子任务列表。

        分解策略优先级：
        1. 自定义分解器（按注册顺序尝试）
        2. 模板匹配（正则匹配标题/描述）
        3. 基于 metadata.subtasks 的直接声明
        4. 基于 metadata.depends_on 的依赖图构建
        5. 回退：不分解

        Args:
            task: 待分解的任务

        Returns:
            DecomposeResult
        """
        # 1. 自定义分解器
        for name, decomposer_fn in self._custom_decomposers.items():
            try:
                result = decomposer_fn(task)
                if result is not None and isinstance(result, DecomposeResult):
                    result.success = True
                    result.strategy = f"custom:{name}"
                    logger.debug("任务 [%s] 使用自定义分解器: %s", task.id, name)
                    return result
            except Exception as e:
                logger.debug("自定义分解器 %s 失败: %s", name, e)

        # 2. 模板匹配
        template_result = self._match_template(task)
        if template_result:
            return template_result

        # 3. metadata.subtasks 直接声明
        declared_result = self._from_metadata(task)
        if declared_result:
            return declared_result

        # 4. 回退：不分解
        return DecomposeResult(success=False, strategy="none")

    # ------------------------------------------------------------------
    # 模板匹配
    # ------------------------------------------------------------------

    def _match_template(self, task: Task) -> Optional[DecomposeResult]:
        """尝试模板匹配。"""
        text = f"{task.title} {task.description}".strip()
        if not text:
            return None

        for pattern, subtask_specs in self._templates:
            if re.search(pattern, text, re.IGNORECASE):
                # 深拷贝子任务规格（避免模板污染）
                subtasks = [
                    SubtaskSpec(
                        title=st.title,
                        description=st.description,
                        depends_on=list(st.depends_on),
                        tags=list(st.tags),
                        metadata=dict(st.metadata),
                        estimated_seconds=st.estimated_seconds,
                    )
                    for st in subtask_specs
                ]
                logger.debug("任务 [%s] 匹配模板: %s", task.id, pattern)
                return DecomposeResult(
                    subtasks=subtasks,
                    strategy=f"template:{pattern[:30]}",
                    success=True,
                )

        return None

    # ------------------------------------------------------------------
    # 从 metadata 构建
    # ------------------------------------------------------------------

    def _from_metadata(self, task: Task) -> Optional[DecomposeResult]:
        """从 task.metadata 读取子任务声明。

        支持两种格式：
        1. metadata.subtasks: [{title, description, depends_on}, ...]
        2. metadata.decompose_steps: ["step1", "step2", ...] (顺序分解)
        """
        # 格式 1: 完整子任务声明
        subtasks_meta = task.metadata.get("subtasks", [])
        if subtasks_meta and isinstance(subtasks_meta, list):
            subtasks = []
            for item in subtasks_meta:
                if isinstance(item, dict):
                    st = SubtaskSpec(
                        title=item.get("title", ""),
                        description=item.get("description", ""),
                        depends_on=item.get("depends_on", []),
                        tags=item.get("tags", []),
                        metadata=item.get("metadata", {}),
                        estimated_seconds=item.get("estimated_seconds", 0),
                    )
                    if st.title:
                        subtasks.append(st)

            if subtasks:
                logger.debug("任务 [%s] 从 metadata.subtasks 构建 %d 个子任务", task.id, len(subtasks))
                return DecomposeResult(
                    subtasks=subtasks,
                    strategy="metadata:subtasks",
                    success=True,
                )

        # 格式 2: 顺序步骤
        steps = task.metadata.get("decompose_steps", [])
        if steps and isinstance(steps, list):
            subtasks = []
            prev_title = ""
            for i, step_title in enumerate(steps):
                if not isinstance(step_title, str) or not step_title.strip():
                    continue
                st = SubtaskSpec(
                    title=step_title.strip(),
                    description=f"步骤 {i + 1}: {step_title.strip()}",
                    depends_on=[prev_title] if prev_title else [],
                )
                subtasks.append(st)
                prev_title = st.title

            if subtasks:
                logger.debug("任务 [%s] 从 metadata.decompose_steps 构建 %d 个顺序子任务", task.id, len(subtasks))
                return DecomposeResult(
                    subtasks=subtasks,
                    strategy="metadata:steps",
                    success=True,
                )

        return None

    # ------------------------------------------------------------------
    # 依赖图
    # ------------------------------------------------------------------

    def create_subtask_graph(self, task: Task) -> Dict[str, List[str]]:
        """返回子任务依赖图 {title: [depends_on_titles]}。

        不实际创建子任务，仅分析依赖关系。
        """
        result = self.decompose(task)
        return result.dependency_graph

    # ------------------------------------------------------------------
    # 批量创建子任务（与 TaskManager 集成）
    # ------------------------------------------------------------------

    def create_subtasks(
        self,
        task: Task,
        task_manager: Any,
    ) -> List[Task]:
        """分解任务并在 TaskManager 中创建子任务。

        Args:
            task: 父任务
            task_manager: TaskManager 实例

        Returns:
            创建的子任务 Task 列表（按拓扑排序顺序）
        """
        result = self.decompose(task)
        if not result.success or not result.subtasks:
            return []

        created_tasks: List[Task] = []
        # title -> task_id 映射（用于解析依赖）
        title_to_id: Dict[str, str] = {}

        # 按拓扑排序顺序创建
        for spec in result.topological_order():
            # 解析 depends_on: title -> task_id
            dep_ids = []
            for dep_title in spec.depends_on:
                dep_id = title_to_id.get(dep_title, "")
                if dep_id:
                    dep_ids.append(dep_id)

            # 合并 depends_on 到 metadata
            meta = dict(spec.metadata)
            if dep_ids:
                meta["depends_on"] = dep_ids

            sub_task = task_manager.create(
                title=spec.title,
                description=spec.description,
                task_type=TaskType.SUBTASK,
                parent_id=task.id,
                tags=spec.tags,
                metadata=meta,
            )

            title_to_id[spec.title] = sub_task.id
            created_tasks.append(sub_task)

            # 触发子任务创建钩子
            if task_manager.hook_manager:
                task_manager.hook_manager.emit(
                    "on_subtask_create",
                    parent_id=task.id,
                    subtask_id=sub_task.id,
                    title=sub_task.title,
                )

        logger.info(
            "任务 [%s] 分解为 %d 个子任务 (策略: %s)",
            task.id, len(created_tasks), result.strategy,
        )
        return created_tasks


__all__ = ["TaskPlanner", "DecomposeResult", "SubtaskSpec"]
