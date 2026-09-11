"""tests/task/test_planner.py — 任务分解器测试。"""

import pytest
from unittest.mock import MagicMock

from spirit.task.models import Task, TaskStatus, TaskType
from spirit.task.planner import TaskPlanner, DecomposeResult, SubtaskSpec


# ---------------------------------------------------------------------------
# SubtaskSpec / DecomposeResult 测试
# ---------------------------------------------------------------------------

class TestSubtaskSpec:

    def test_defaults(self):
        st = SubtaskSpec(title="test")
        assert st.title == "test"
        assert st.depends_on == []
        assert st.estimated_seconds == 0.0


class TestDecomposeResult:

    def test_empty_graph(self):
        r = DecomposeResult()
        assert r.dependency_graph == {}

    def test_dependency_graph(self):
        r = DecomposeResult(subtasks=[
            SubtaskSpec(title="A"),
            SubtaskSpec(title="B", depends_on=["A"]),
        ])
        graph = r.dependency_graph
        assert graph["A"] == []
        assert graph["B"] == ["A"]

    def test_topological_order_linear(self):
        r = DecomposeResult(subtasks=[
            SubtaskSpec(title="C", depends_on=["B"]),
            SubtaskSpec(title="A"),
            SubtaskSpec(title="B", depends_on=["A"]),
        ])
        order = r.topological_order()
        titles = [st.title for st in order]
        assert titles.index("A") < titles.index("B")
        assert titles.index("B") < titles.index("C")

    def test_topological_order_parallel(self):
        r = DecomposeResult(subtasks=[
            SubtaskSpec(title="A"),
            SubtaskSpec(title="B"),
            SubtaskSpec(title="C", depends_on=["A", "B"]),
        ])
        order = r.topological_order()
        titles = [st.title for st in order]
        assert titles.index("A") < titles.index("C")
        assert titles.index("B") < titles.index("C")

    def test_topological_order_empty(self):
        r = DecomposeResult()
        assert r.topological_order() == []


# ---------------------------------------------------------------------------
# 模板匹配
# ---------------------------------------------------------------------------

class TestTemplateDecompose:

    def test_feature_implementation(self):
        planner = TaskPlanner()
        task = Task(title="实现用户登录功能")
        result = planner.decompose(task)
        assert result.success is True
        assert "template" in result.strategy
        titles = [st.title for st in result.subtasks]
        assert "设计" in titles
        assert "编码" in titles
        assert "测试" in titles

    def test_bug_fix(self):
        planner = TaskPlanner()
        task = Task(title="修复登录页面 bug")
        result = planner.decompose(task)
        assert result.success is True
        titles = [st.title for st in result.subtasks]
        assert "复现" in titles
        assert "修复" in titles
        assert "验证" in titles

    def test_refactor(self):
        planner = TaskPlanner()
        task = Task(title="重构数据库模块代码")
        result = planner.decompose(task)
        assert result.success is True
        titles = [st.title for st in result.subtasks]
        assert "分析" in titles
        assert "重构实施" in titles

    def test_deploy(self):
        planner = TaskPlanner()
        task = Task(title="部署新版本到生产环境")
        result = planner.decompose(task)
        assert result.success is True
        titles = [st.title for st in result.subtasks]
        assert "构建" in titles
        assert "部署" in titles

    def test_research(self):
        planner = TaskPlanner()
        task = Task(title="调研新的前端框架")
        result = planner.decompose(task)
        assert result.success is True
        titles = [st.title for st in result.subtasks]
        assert "信息收集" in titles
        assert "对比分析" in titles

    def test_no_match(self):
        planner = TaskPlanner()
        task = Task(title="随便做点什么")
        result = planner.decompose(task)
        assert result.success is False
        assert result.strategy == "none"

    def test_template_has_dependencies(self):
        planner = TaskPlanner()
        task = Task(title="实现新 API 接口")
        result = planner.decompose(task)
        assert result.success is True
        # "编码" should depend on "设计"
        coding = [st for st in result.subtasks if st.title == "编码"][0]
        assert "设计" in coding.depends_on


# ---------------------------------------------------------------------------
# metadata 分解
# ---------------------------------------------------------------------------

class TestMetadataDecompose:

    def test_from_subtasks_metadata(self):
        planner = TaskPlanner()
        task = Task(
            title="custom task",
            metadata={
                "subtasks": [
                    {"title": "Step 1", "description": "First step"},
                    {"title": "Step 2", "depends_on": ["Step 1"]},
                    {"title": "Step 3", "depends_on": ["Step 1", "Step 2"]},
                ],
            },
        )
        result = planner.decompose(task)
        assert result.success is True
        assert result.strategy == "metadata:subtasks"
        assert len(result.subtasks) == 3

    def test_from_decompose_steps(self):
        planner = TaskPlanner()
        task = Task(
            title="sequential task",
            metadata={"decompose_steps": ["准备", "执行", "验证"]},
        )
        result = planner.decompose(task)
        assert result.success is True
        assert result.strategy == "metadata:steps"
        assert len(result.subtasks) == 3
        # 顺序依赖
        assert result.subtasks[0].depends_on == []
        assert result.subtasks[1].depends_on == ["准备"]
        assert result.subtasks[2].depends_on == ["执行"]

    def test_empty_metadata(self):
        planner = TaskPlanner()
        task = Task(title="no metadata", metadata={})
        result = planner.decompose(task)
        assert result.success is False


# ---------------------------------------------------------------------------
# 自定义分解器
# ---------------------------------------------------------------------------

class TestCustomDecomposer:

    def test_custom_decomposer(self):
        planner = TaskPlanner()

        def my_decomposer(task):
            return DecomposeResult(
                subtasks=[
                    SubtaskSpec(title="Custom A"),
                    SubtaskSpec(title="Custom B", depends_on=["Custom A"]),
                ],
                strategy="custom:my_decomposer",
                success=True,
            )

        planner.register_decomposer("my_decomposer", my_decomposer)
        task = Task(title="anything")
        result = planner.decompose(task)
        assert result.success is True
        assert result.strategy == "custom:my_decomposer"
        assert len(result.subtasks) == 2

    def test_custom_decomposer_returns_none(self):
        planner = TaskPlanner()

        def skip_decomposer(task):
            return None  # 不适用

        planner.register_decomposer("skip", skip_decomposer)
        task = Task(title="实现新功能")
        result = planner.decompose(task)
        # Should fall through to template matching
        assert result.success is True
        assert "template" in result.strategy

    def test_custom_decomposer_exception(self):
        planner = TaskPlanner()

        def bad_decomposer(task):
            raise RuntimeError("boom")

        planner.register_decomposer("bad", bad_decomposer)
        task = Task(title="实现新功能")
        result = planner.decompose(task)
        # Should fall through to template matching
        assert result.success is True


# ---------------------------------------------------------------------------
# add_template
# ---------------------------------------------------------------------------

class TestAddTemplate:

    def test_add_custom_template(self):
        planner = TaskPlanner()
        planner.add_template(
            r"迁移.*数据库",
            [
                SubtaskSpec(title="备份"),
                SubtaskSpec(title="迁移", depends_on=["备份"]),
                SubtaskSpec(title="验证", depends_on=["迁移"]),
            ],
        )
        task = Task(title="迁移生产数据库")
        result = planner.decompose(task)
        assert result.success is True
        assert len(result.subtasks) == 3


# ---------------------------------------------------------------------------
# create_subtasks (集成 TaskManager)
# ---------------------------------------------------------------------------

class TestCreateSubtasks:

    def test_create_subtasks_with_mock_manager(self):
        planner = TaskPlanner()
        task = Task(id="parent1", title="实现用户注册功能")

        # Mock TaskManager
        tm = MagicMock()
        created_tasks = []

        def mock_create(**kwargs):
            t = Task(
                title=kwargs.get("title", ""),
                task_type=TaskType.SUBTASK,
                parent_id=kwargs.get("parent_id", ""),
                metadata=kwargs.get("metadata", {}),
            )
            created_tasks.append(t)
            return t

        tm.create.side_effect = mock_create
        tm.hook_manager = None

        result = planner.create_subtasks(task, tm)
        assert len(result) > 0
        assert tm.create.call_count > 0
        # 验证触发了 on_subtask_create 钩子 (hook_manager is None, so no emit)

    def test_create_subtasks_no_decompose(self):
        planner = TaskPlanner()
        task = Task(id="p1", title="无法匹配的任务名称")
        tm = MagicMock()
        tm.hook_manager = None

        result = planner.create_subtasks(task, tm)
        assert result == []


# ---------------------------------------------------------------------------
# create_subtask_graph
# ---------------------------------------------------------------------------

class TestSubtaskGraph:

    def test_graph_from_template(self):
        planner = TaskPlanner()
        task = Task(title="修复登录 bug")
        graph = planner.create_subtask_graph(task)
        assert isinstance(graph, dict)
        # "修复" depends on "复现"
        if "修复" in graph:
            assert "复现" in graph["修复"]

    def test_graph_no_match(self):
        planner = TaskPlanner()
        task = Task(title="随便")
        graph = planner.create_subtask_graph(task)
        assert graph == {}
