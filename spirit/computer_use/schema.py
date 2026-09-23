"""通用 ``computer_use`` 工具的 Schema — Spirit Agent（Phase 4.5）。

对标 Hermes ``tools/computer_use/schema.py``：模型无关（任何 tool-calling 模型都能驱动），
但适配 Spirit ``tools.registry`` 的 OpenAI function-calling 包裹形式
（``{"type": "function", "function": {...}}``）。

视觉模型应优先 ``capture(mode='som')`` 再 ``click(element=N)``——比像素坐标可靠得多；
像素坐标仍为受过相应训练的模型保留。单个工具用 ``action`` 判别式收敛，schema 紧凑、
每轮 token 成本低。
"""

from __future__ import annotations

from typing import Any, Dict

# 动作判别式的合法取值（与 tool._dispatch 的分支一一对应）。
COMPUTER_USE_ACTIONS = (
    "capture", "click", "double_click", "right_click", "middle_click",
    "drag", "scroll", "type", "key", "set_value", "wait",
    "list_apps", "list_windows", "focus_app",
)

COMPUTER_USE_PARAMETERS: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(COMPUTER_USE_ACTIONS),
            "description": (
                "要执行的动作。capture 无副作用（只读）；其余动作会改变用户可见状态，"
                "除非已自动批准，否则需经审批。set_value 用于 select/popup 与滑块——"
                "直接选中匹配项，不打开原生菜单（不抢焦点）。"
            ),
        },
        # ── capture ────────────────────────────────────────────
        "mode": {
            "type": "string",
            "enum": ["som", "vision", "ax"],
            "description": (
                "捕获模式。som（默认）是带编号覆盖层的截图 + AX 树——最适合视觉模型，"
                "可按元素索引点击。vision 是纯截图。ax 仅无障碍树（无图，适合纯文本模型）。"
            ),
        },
        "app": {
            "type": "string",
            "description": (
                "可选。把捕获/动作限定到某应用（按名，如 'Safari'，或 bundle ID）。"
                "省略时操作最前台应用的窗口。传 app='screen'（或 'desktop'）捕获桌面/外壳。"
            ),
        },
        "pid": {"type": "integer", "description": "可选的精确进程目标（capture）。"},
        "window_id": {"type": "integer", "description": "可选的精确原生窗口目标（capture）。"},
        "max_elements": {
            "type": "integer",
            "description": (
                "capture 返回的 AX elements 数组上限。默认 100，硬上限 1000。稠密 UI"
                "（Electron/IDE）可发布 500+ 节点，封顶防止单次捕获撑爆上下文。"
            ),
            "default": 100, "minimum": 1, "maximum": 1000,
        },
        # ── click / drag / scroll 定位 ─────────────────────────
        "element": {
            "type": "integer",
            "description": "上次 capture(mode='som') 返回的 1-based SOM 索引。强烈优先于裸坐标。",
        },
        "coordinate": {
            "type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2,
            "description": "相对截图左上角的像素坐标 [x, y]。仅在没有元素索引时使用。",
        },
        "button": {
            "type": "string", "enum": ["left", "right", "middle"],
            "description": "鼠标按键，默认 left。",
        },
        "modifiers": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["cmd", "shift", "option", "alt", "ctrl", "fn",
                         "win", "windows", "super", "meta"],
            },
            "description": "动作期间按住的修饰键。",
        },
        # ── drag ───────────────────────────────────────────────
        "from_element": {"type": "integer", "description": "拖拽源元素索引。"},
        "to_element": {"type": "integer", "description": "拖拽目标元素索引。"},
        "from_coordinate": {
            "type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2,
            "description": "拖拽源 [x,y]（无元素时使用）。",
        },
        "to_coordinate": {
            "type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2,
            "description": "拖拽目标 [x,y]（无元素时使用）。",
        },
        # ── scroll ─────────────────────────────────────────────
        "direction": {
            "type": "string", "enum": ["up", "down", "left", "right"],
            "description": "滚动方向。",
        },
        "amount": {"type": "integer", "description": "滚轮刻度数，默认 3。"},
        # ── set_value ──────────────────────────────────────────
        "value": {
            "type": "string",
            "description": "action='set_value' 时要设的值。下拉传选项显示标签（如 'Blue'）。",
        },
        # ── type / key / wait ──────────────────────────────────
        "text": {"type": "string", "description": "要输入的文本（遵循当前键盘布局）。"},
        "keys": {
            "type": "string",
            "description": "组合键，如 'cmd+s' / 'ctrl+alt+t' / 'return' / 'escape'。用 '+' 组合。",
        },
        "seconds": {"type": "number", "description": "等待秒数，最大 30。"},
        # ── focus_app ──────────────────────────────────────────
        "raise_window": {
            "type": "boolean",
            "description": "仅 action='focus_app'。true 会把窗口前置（打扰用户）；默认 false。",
        },
        # ── 返回形状 ───────────────────────────────────────────
        "capture_after": {
            "type": "boolean",
            "description": "true 时在动作后追加一次捕获并包含在响应里（省一次往返）。",
        },
    },
    "required": ["action"],
}

COMPUTER_USE_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "computer_use",
        "description": (
            "在后台驱动桌面——截图、鼠标、键盘、滚动、拖拽——不抢占用户的光标或键盘焦点。"
            "推荐流程：先 action='capture'（mode='som' 给编号元素覆盖层），再按 element 索引点击"
            "（更可靠）。像素坐标为受过相应训练的模型保留。需要已安装桌面控制后端；"
            "无后端时以 noop 后端安全降级（动作只记录、不生效）。"
        ),
        "parameters": COMPUTER_USE_PARAMETERS,
    },
}


def get_computer_use_schema() -> Dict[str, Any]:
    """返回通用 OpenAI function-calling schema（Spirit 包裹形式）。"""
    return COMPUTER_USE_SCHEMA


__all__ = [
    "COMPUTER_USE_ACTIONS",
    "COMPUTER_USE_PARAMETERS",
    "COMPUTER_USE_SCHEMA",
    "get_computer_use_schema",
]
