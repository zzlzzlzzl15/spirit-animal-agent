"""交互式提问工具 — clarify（向用户请求澄清）。

参考 Hermes 的 tools/clarify_tool.py 设计：
- 支持选择题（最多 4 个选项）和开放式提问
- 通过回调函数实现平台无关
- VSCode 扩展中弹出 QuickPick / InputBox
"""

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)

MAX_CHOICES = 4

# 平台回调（由 CLI / API / VSCode 注入）
_clarify_callback: Optional[Callable] = None


def set_clarify_callback(fn: Optional[Callable]) -> None:
    """设置平台层的提问回调。"""
    global _clarify_callback
    _clarify_callback = fn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

CLARIFY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "clarify",
        "description": (
            "向用户提问以获取澄清。\n\n"
            "当你不确定用户的需求、偏好或具体细节时使用。\n"
            "支持选择题（最多 4 个选项）和开放式提问。\n\n"
            "示例：\n"
            '- 不确定用哪个框架 → 提供选项\n'
            '- 需求模糊 → 开放式提问\n'
            '- 确认破坏性操作 → 是/否选择'
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "要问用户的问题",
                },
                "choices": {
                    "type": "array",
                    "description": "可选答案列表（最多 4 个，省略则为开放式提问）",
                    "items": {"type": "string"},
                },
            },
            "required": ["question"],
        },
    },
}


def _clarify_impl(question: str, choices: List[str] = None) -> str:
    """向用户提问。"""
    question = question.strip()
    if not question:
        return json.dumps({"error": "问题内容不能为空"})

    # 验证选项
    clean_choices = None
    if choices:
        clean_choices = []
        for c in choices[:MAX_CHOICES]:
            if isinstance(c, str) and c.strip():
                clean_choices.append(c.strip())
            elif isinstance(c, dict):
                # LLM 有时会传 dict
                for key in ("label", "description", "text", "title"):
                    v = c.get(key, "")
                    if isinstance(v, str) and v.strip():
                        clean_choices.append(v.strip())
                        break

    # 通过回调获取用户回答
    if _clarify_callback:
        try:
            answer = _clarify_callback(question, clean_choices)
            return json.dumps({
                "question": question,
                "choices": clean_choices,
                "answer": answer,
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"提问失败: {e}"})

    # 无回调时（API 模式），返回问题让前端展示
    return json.dumps({
        "question": question,
        "choices": clean_choices,
        "waiting_for_answer": True,
        "message": "等待用户回答",
    }, ensure_ascii=False)


registry.register(
    name="clarify",
    toolset="interaction",
    schema=CLARIFY_SCHEMA,
    handler=_clarify_impl,
    description="向用户提问",
    emoji="❓",
)
