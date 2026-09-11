"""系统提示词构建 — 动态组装身份、工具指导、环境提示。

参考 Hermes 的 prompt_builder.py (2044行) + system_prompt.py (593行)。
三层提示词架构：
- stable: 身份 + 工具指导 + 技能指导 + 环境提示（跨轮不变）
- context: 上下文文件（AGENTS.md 等）+ 用户自定义系统消息
- volatile: 记忆快照 + 时间戳 + 会话信息（每轮可能变）

安全特性：
- 上下文文件扫描（防止提示注入）
- 环境自动检测（OS、shell、git、工作目录）
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# =========================================================================
# 核心身份
# =========================================================================

DEFAULT_AGENT_IDENTITY = (
    "你是 Spirit Agent，一个强大的 AI 编程助手。"
    "你帮助用户编写和调试代码、分析信息、执行操作。"
    "你知识丰富、直接高效，优先提供真正有用的帮助。"
    "回答要简洁准确，不确定时坦诚说明。"
    "积极探索和调查，善用工具完成任务。"
)

# =========================================================================
# 工具使用指导
# =========================================================================

TOOL_USE_GUIDANCE = (
    "你拥有一系列强大的工具来完成任务。优先使用工具而不是纯文本回答。\n"
    "对于文件操作使用 read_file/write_file/patch_file；"
    "对于命令执行使用 terminal；对于代码测试使用 execute_code。\n"
    "当需要搜索时使用 search_files/find_files/web_search；"
    "当需要追踪任务进度时使用 todo。\n"
    "尽量并行调用独立的工具以提高效率。\n\n"
    "## 工具调用格式说明\n"
    "当你需要使用工具时，请在响应中输出 XML 格式的工具调用标记：\n"
    "<tool_call>\n"
    '{"id": "call_1", "type": "function", "function": {"name": "工具名称", "arguments": {"参数名": "参数值"}}}\n'
    "</tool_call>\n\n"
    "示例（搜索今天的新闻）：\n"
    "<tool_call>\n"
    '{"id": "call_1", "type": "function", "function": {"name": "web_search", "arguments": {"query": "today news"}}}\n'
    "</tool_call>\n\n"
    "重要：必须严格使用上述 XML 格式，不要发明其他格式（如 <invoke><parameter> 等）。"
)

PARALLEL_TOOL_GUIDANCE = (
    "当你有多个独立操作需要执行时（如读取多个文件、搜索多个位置），"
    "请在同一轮中并行发起多个工具调用，而不是串行等待。"
)

# =========================================================================
# 记忆指导
# =========================================================================

MEMORY_GUIDANCE = (
    "你有跨会话的持久记忆。使用 memory 工具保存持久事实：\n"
    "用户偏好、环境细节、工具特性、稳定约定。\n"
    "记忆会注入到每一轮中，所以保持紧凑。\n"
    "优先保存能减少用户未来重复提醒的事实。\n"
    "不要保存任务进度、已完成工作日志或临时状态到记忆中。"
    "记忆应写为声明性事实，而非指令。"
    "'用户偏好简洁回复' ✓ — '总是简洁回复' ✗"
)

# =========================================================================
# 环境提示构建
# =========================================================================

def build_environment_hints() -> str:
    """自动检测运行环境并生成提示。"""
    hints = ["## 运行环境"]

    # OS
    os_name = platform.system()
    os_version = platform.version()
    hints.append(f"- 操作系统: {os_name} {os_version}")

    # Python
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    hints.append(f"- Python: {py_version}")

    # Shell
    if os_name == "Windows":
        shell = os.environ.get("COMSPEC", "cmd.exe")
        shell_name = Path(shell).name if shell else "cmd.exe"
        hints.append(f"- Shell: {shell_name}")
    else:
        shell = os.environ.get("SHELL", "/bin/sh")
        hints.append(f"- Shell: {Path(shell).name}")

    # 工作目录
    cwd = os.getcwd()
    hints.append(f"- 工作目录: {cwd}")

    # Git 检测
    git_root = _find_git_root(Path(cwd))
    if git_root:
        hints.append(f"- Git 仓库: {git_root}")

    # 虚拟环境
    venv = os.environ.get("VIRTUAL_ENV") or os.environ.get("CONDA_PREFIX")
    if venv:
        hints.append(f"- 虚拟环境: {venv}")

    # 平台特定提示
    if os_name == "Windows":
        hints.append("- 注意: Windows 系统，路径使用反斜杠或正斜杠均可")
        hints.append("- 终端命令使用 PowerShell 语法")
    elif os_name == "Darwin":
        hints.append("- 注意: macOS 系统")
    else:
        hints.append("- 注意: Linux 系统")

    return "\n".join(hints)


def _find_git_root(start: Path) -> Optional[Path]:
    """向上查找 .git 目录。"""
    current = start.resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None


# =========================================================================
# 上下文文件加载
# =========================================================================

# 支持的上下文文件名（按优先级排序）
CONTEXT_FILE_NAMES = [
    "AGENTS.md",
    ".cursorrules",
    ".spiritrules",
    "SOUL.md",
    ".hermes.md",
    "HERMES.md",
]

# 提示注入检测模式
_INJECTION_PATTERNS = [
    "ignore previous instructions",
    "ignore all instructions",
    "disregard all previous",
    "forget your instructions",
    "you are now",
    "new role:",
    "system:",
    "<script",
    "javascript:",
]


def scan_context_content(content: str, filename: str) -> str:
    """扫描上下文文件内容是否有注入。返回清理后的内容。"""
    # 去除 UTF-8 BOM
    if content.startswith("\ufeff"):
        content = content[1:]

    lower = content.lower()
    for pattern in _INJECTION_PATTERNS:
        if pattern in lower:
            logger.warning("上下文文件 %s 被阻止: 检测到 '%s'", filename, pattern)
            return f"[已阻止: {filename} 包含潜在提示注入 ({pattern})，内容未加载]"

    return content


def load_context_files(cwd: Optional[Path] = None) -> str:
    """从工作目录加载上下文文件。"""
    if cwd is None:
        cwd = Path(os.getcwd())

    parts = []
    git_root = _find_git_root(cwd)

    for name in CONTEXT_FILE_NAMES:
        # 从 cwd 向上查找到 git root
        search_dirs = [cwd]
        if git_root:
            current = cwd.resolve()
            for parent in current.parents:
                if parent == git_root or parent == git_root.parent:
                    break
                search_dirs.append(parent)

        for directory in search_dirs:
            candidate = directory / name
            if candidate.is_file():
                try:
                    content = candidate.read_text(encoding="utf-8")
                    content = scan_context_content(content, str(candidate))
                    parts.append(f"## {name}\n\n{content}")
                    break  # 每个文件名只加载一个
                except Exception as exc:
                    logger.warning("加载上下文文件 %s 失败: %s", candidate, exc)

    return "\n\n---\n\n".join(parts)


# =========================================================================
# 系统提示词组装
# =========================================================================

def build_system_prompt(
    *,
    agent: Any = None,
    custom_system_message: Optional[str] = None,
    memory_content: Optional[str] = None,
    include_environment: bool = True,
    include_tools: bool = True,
    include_memory: bool = True,
    extra_context: Optional[str] = None,
) -> str:
    """组装完整系统提示词。

    三层结构：
    1. stable: 身份 + 工具指导 + 环境
    2. context: 上下文文件 + 自定义消息
    3. volatile: 记忆 + 时间戳

    Args:
        agent: SpiritAgent 实例（可选，用于读取配置）
        custom_system_message: 用户自定义系统消息
        memory_content: 持久记忆内容
        include_environment: 是否包含环境提示
        include_tools: 是否包含工具指导
        include_memory: 是否包含记忆
        extra_context: 额外上下文

    Returns:
        组装后的系统提示词
    """
    stable_parts: List[str] = []
    context_parts: List[str] = []
    volatile_parts: List[str] = []

    # ── Stable 层 ─────────────────────────────────────────────

    # 身份
    if agent and hasattr(agent, "config") and agent.config.system_prompt:
        stable_parts.append(agent.config.system_prompt)
    else:
        stable_parts.append(DEFAULT_AGENT_IDENTITY)

    # 工具指导
    if include_tools:
        stable_parts.append(TOOL_USE_GUIDANCE)
        stable_parts.append(PARALLEL_TOOL_GUIDANCE)

    # 记忆指导
    if include_memory:
        stable_parts.append(MEMORY_GUIDANCE)

    # 环境提示
    if include_environment:
        stable_parts.append(build_environment_hints())

    # ── Context 层 ────────────────────────────────────────────

    # 上下文文件
    context_content = load_context_files()
    if context_content:
        context_parts.append(context_content)

    # 自定义系统消息
    if custom_system_message:
        context_parts.append(custom_system_message)

    # 额外上下文
    if extra_context:
        context_parts.append(extra_context)

    # ── Volatile 层 ───────────────────────────────────────────

    # 持久记忆
    if include_memory and memory_content:
        volatile_parts.append(f"## 持久记忆\n\n{memory_content}")

    # 时间戳
    current_time = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    volatile_parts.append(f"当前时间: {current_time}")

    # ── 组装 ──────────────────────────────────────────────────

    sections = []

    stable_text = "\n\n".join(stable_parts)
    if stable_text:
        sections.append(stable_text)

    if context_parts:
        sections.append("\n\n".join(context_parts))

    if volatile_parts:
        sections.append("\n\n".join(volatile_parts))

    return "\n\n---\n\n".join(sections)


# =========================================================================
# 临时系统提示词（ephemeral）
# =========================================================================

class EphemeralSystemPrompt:
    """临时系统提示词管理器。

    临时提示词仅在 API 调用时注入，不持久化到会话数据库。
    用于 /steer 指令、运行时指导注入等。
    """

    def __init__(self):
        self._text: str = ""

    def set(self, text: str) -> None:
        self._text = text

    def append(self, text: str) -> None:
        if self._text:
            self._text += "\n\n" + text
        else:
            self._text = text

    def clear(self) -> None:
        self._text = ""

    @property
    def text(self) -> str:
        return self._text

    def __bool__(self) -> bool:
        return bool(self._text)


# =========================================================================
# 辅助函数
# =========================================================================

def format_memory_block(memories: List[Dict[str, Any]]) -> str:
    """格式化记忆列表为可注入的文本块。"""
    if not memories:
        return ""

    lines = ["## 持久记忆"]
    for mem in memories:
        category = mem.get("category", "")
        content = mem.get("content", "")
        if content:
            prefix = f"[{category}] " if category else ""
            lines.append(f"- {prefix}{content}")

    return "\n".join(lines)


def format_tool_guidance_for_model(tools: List[dict]) -> str:
    """为特定工具集生成简要指导。"""
    if not tools:
        return ""

    tool_names = []
    for t in tools:
        if isinstance(t, dict) and "function" in t:
            name = t["function"].get("name", "")
            desc = t["function"].get("description", "")
            if name:
                tool_names.append(f"- {name}: {desc[:80]}")

    if not tool_names:
        return ""

    return "## 可用工具\n\n" + "\n".join(tool_names[:30])  # 最多列 30 个


__all__ = [
    "DEFAULT_AGENT_IDENTITY",
    "TOOL_USE_GUIDANCE",
    "PARALLEL_TOOL_GUIDANCE",
    "MEMORY_GUIDANCE",
    "build_environment_hints",
    "load_context_files",
    "scan_context_content",
    "build_system_prompt",
    "EphemeralSystemPrompt",
    "format_memory_block",
    "format_tool_guidance_for_model",
]
