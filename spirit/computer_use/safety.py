"""Computer Use 安全原语 — Spirit Agent（Phase 4.5）。

对标 Hermes ``tools/computer_use/tool.py`` 顶部的「Approval & safety」段：动作分级
（只读 vs 改变用户可见状态）、硬阻止的破坏性组合键、危险输入文本模式、组合键归一化。
抽成独立模块，便于 :mod:`spirit.computer_use.tool` 复用且可单独单测。

安全哲学（对齐 Hermes）：

- **只读动作**（capture / wait / list_apps / list_windows）永远放行，不经审批。
- **改变状态的动作**（click / type / key / drag / scroll / set_value / focus_app）经审批门。
- **硬阻止**：某些组合键（清空回收站 / 锁屏 / 注销 / Win+L / Alt+F4 …）无论审批级别一律
  拒绝——它们会杀掉 Spirit 赖以运行的会话，属于不可逆破坏。
- **危险输入**：``type`` 动作里的 ``curl … | bash`` / ``sudo rm -rf`` / fork 炸弹等模式
  在审批前就被拦截。
"""

from __future__ import annotations

import re
from typing import Optional

# 只读、无副作用的动作——永远放行。
SAFE_ACTIONS = frozenset({"capture", "wait", "list_apps", "list_windows"})

# 改变用户可见状态的动作——经审批门。
DESTRUCTIVE_ACTIONS = frozenset({
    "click", "double_click", "right_click", "middle_click",
    "drag", "scroll", "type", "key", "set_value", "focus_app",
})

# 硬阻止的组合键（对标 Hermes，含 macOS / Windows 破坏性会话快捷键）。
# 归一化后（alt→option、windows/super/meta→win）以子集匹配。
BLOCKED_KEY_COMBOS = frozenset({
    frozenset({"cmd", "shift", "backspace"}),   # 清空回收站
    frozenset({"cmd", "option", "backspace"}),  # 强制删除
    frozenset({"cmd", "ctrl", "q"}),            # 锁屏
    frozenset({"cmd", "shift", "q"}),           # 注销
    frozenset({"cmd", "option", "shift", "q"}),  # 强制注销
    frozenset({"win", "l"}),                    # Windows 锁屏
    frozenset({"ctrl", "option", "delete"}),    # Windows 安全选项
    frozenset({"ctrl", "option", "del"}),
    frozenset({"option", "f4"}),                # Alt+F4 关窗
})

# 组合键别名归一化表（对标 Hermes _KEY_ALIASES）。
KEY_ALIASES = {
    "command": "cmd", "control": "ctrl", "alt": "option", "⌘": "cmd", "⌥": "option",
    "windows": "win", "super": "win", "meta": "win",
}

# ``type`` 动作的危险文本模式（对标 Hermes _BLOCKED_TYPE_PATTERNS）。
BLOCKED_TYPE_PATTERNS = [
    re.compile(r"curl\s+[^|]*\|\s*bash", re.IGNORECASE),
    re.compile(r"curl\s+[^|]*\|\s*sh", re.IGNORECASE),
    re.compile(r"wget\s+[^|]*\|\s*bash", re.IGNORECASE),
    re.compile(r"\bsudo\s+rm\s+-[rf]", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\s+/\s*$", re.IGNORECASE),
    re.compile(r":\s*\(\)\s*\{\s*:\|:\s*&\s*\}", re.IGNORECASE),  # fork 炸弹
]


def canon_key_combo(keys: str) -> frozenset:
    """把 ``'cmd+shift+q'`` 归一化为规范键集合（小写、别名折叠、去空）。"""
    parts = [p.strip().lower() for p in re.split(r"\s*\+\s*", keys or "") if p.strip()]
    return frozenset(KEY_ALIASES.get(p, p) for p in parts)


def blocked_key_combo(keys: str) -> Optional[frozenset]:
    """若 ``keys`` 命中任一硬阻止组合键则返回该组合，否则 None。

    子集匹配：输入组合键包含某个被阻止组合的全部键即视为命中（``len(blocked) <= len(combo)``
    防止误伤更短的合法组合，但被阻止组合是输入的超集时不算——与 Hermes 一致）。
    """
    combo = canon_key_combo(keys)
    for blocked in BLOCKED_KEY_COMBOS:
        if blocked.issubset(combo) and len(blocked) <= len(combo):
            return blocked
    return None


def blocked_type_pattern(text: str) -> Optional[str]:
    """若 ``text`` 命中任一危险输入模式则返回该正则串，否则 None。"""
    for pat in BLOCKED_TYPE_PATTERNS:
        if pat.search(text or ""):
            return pat.pattern
    return None


__all__ = [
    "SAFE_ACTIONS",
    "DESTRUCTIVE_ACTIONS",
    "BLOCKED_KEY_COMBOS",
    "KEY_ALIASES",
    "BLOCKED_TYPE_PATTERNS",
    "canon_key_combo",
    "blocked_key_combo",
    "blocked_type_pattern",
]
