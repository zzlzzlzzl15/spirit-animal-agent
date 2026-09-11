"""子 Agent 委派工具 — delegate_task（并行子任务）。

参考 Hermes 的 tools/delegate_tool.py 设计：
- 生成子 Agent 处理独立子任务
- 支持并行执行多个子任务
- 子 Agent 有独立上下文，结果汇总回父 Agent
"""

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Dict, List, Optional

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)

from spirit.config import get_config_value

# 子 Agent 不允许使用的工具
BLOCKED_TOOLS_FOR_CHILDREN = frozenset([
    "delegate_task",   # 禁止递归委派
    "clarify",         # 子 Agent 不能与用户交互
    "memory",          # 子 Agent 不写共享记忆
])

DEFAULT_TIMEOUT = get_config_value("timeouts.delegate_default", 300)  # 5 分钟


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

DELEGATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": (
            "委派子任务给独立的子 Agent 执行。\n\n"
            "适用场景：\n"
            "- 多个独立任务可并行处理\n"
            "- 需要隔离上下文避免干扰\n"
            "- 复杂任务分解为子任务\n\n"
            "子 Agent 拥有与父 Agent 相同的工具集（除委派/交互/记忆外）。\n"
            "父 Agent 的上下文只看到委派请求和汇总结果。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "子任务列表",
                    "items": {
                        "type": "object",
                        "properties": {
                            "goal": {"type": "string", "description": "任务目标描述"},
                            "context": {"type": "string", "description": "相关上下文"},
                        },
                        "required": ["goal"],
                    },
                },
                "timeout": {
                    "type": "integer",
                    "description": f"每个子任务超时秒数（默认 {DEFAULT_TIMEOUT}）",
                },
            },
            "required": ["tasks"],
        },
    },
}


def _delegate_task_impl(
    tasks: List[Dict[str, str]],
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """委派子任务。"""
    if not tasks:
        return json.dumps({"error": "至少需要一个子任务"})

    if len(tasks) > 5:
        return json.dumps({"error": "最多同时委派 5 个子任务"})

    timeout = min(max(timeout, 30), 600)
    results = []

    def _run_child(task: Dict) -> Dict:
        """运行单个子任务。"""
        child_id = str(uuid.uuid4())[:8]
        goal = task.get("goal", "")
        context = task.get("context", "")

        try:
            from spirit.agent.agent import AgentConfig, SpiritAgent

            # 创建子 Agent（独立会话）
            config = AgentConfig(
                session_id=f"child-{child_id}",
                platform="delegate",
                max_iterations=30,  # 子 Agent 迭代次数限制
            )
            child = SpiritAgent(config)

            # 构建任务提示
            prompt = goal
            if context:
                prompt = f"上下文:\n{context}\n\n任务: {goal}"

            # 执行
            start_time = time.time()
            result = child.run_conversation(prompt)
            duration = time.time() - start_time

            return {
                "task_id": child_id,
                "goal": goal,
                "success": True,
                "response": result.response[:2000],  # 截断
                "iterations": result.iterations,
                "duration": round(duration, 1),
            }

        except Exception as e:
            return {
                "task_id": child_id,
                "goal": goal,
                "success": False,
                "error": str(e),
            }

    # 并行执行
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {executor.submit(_run_child, task): i for i, task in enumerate(tasks)}

        for future in futures:
            try:
                result = future.result(timeout=timeout)
                results.append(result)
            except FuturesTimeoutError:
                idx = futures[future]
                results.append({
                    "task_id": "unknown",
                    "goal": tasks[idx].get("goal", ""),
                    "success": False,
                    "error": f"超时（{timeout}秒）",
                })
            except Exception as e:
                idx = futures[future]
                results.append({
                    "task_id": "unknown",
                    "goal": tasks[idx].get("goal", ""),
                    "success": False,
                    "error": str(e),
                })

    # 汇总
    summary = {
        "total": len(tasks),
        "succeeded": len([r for r in results if r.get("success")]),
        "failed": len([r for r in results if not r.get("success")]),
        "results": results,
    }

    return json.dumps(summary, ensure_ascii=False, indent=2)


registry.register(
    name="delegate_task",
    toolset="delegation",
    schema=DELEGATE_SCHEMA,
    handler=_delegate_task_impl,
    description="委派子任务",
    emoji="🔀",
    max_result_size_chars=get_config_value("limits.delegate_max_chars", 20_000),
)
