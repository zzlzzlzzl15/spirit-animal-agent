"""SKILL.md 预处理 — Spirit Agent（Phase 4.6）。

对标 Hermes ``agent/skill_preprocessing.py``：技能正文在注入对话前可经两类预处理——

1. **模板变量替换**：``${SPIRIT_SKILL_DIR}`` / ``${SPIRIT_SESSION_ID}``。只替换有具体值的
   token，未解析的 token 原样保留（方便作者调试）。默认开启（``skills.template_vars``）。
2. **内联 shell 展开**：``!`cmd``` 片段替换为其 stdout。以技能目录为 CWD 运行（相对路径
   按作者预期工作），输出封顶防止跑飞的命令撑爆上下文。**默认关闭**（``skills.inline_shell``），
   因为执行任意 shell 有安全成本，须用户显式开启。

与 Hermes 的差异：Hermes 用 ``bash -c`` + ``hermes_cli._subprocess_compat``；Spirit 复用
``process.registry`` 同款的跨平台 shell 选择（Windows 走 ``cmd /c`` + ``CREATE_NO_WINDOW``，
POSIX 走 ``$SHELL``/``bash -c``），失败一律返回 ``[inline-shell error: ...]`` 标记而非抛异常，
保证一个坏片段不会毁掉整条技能消息。
"""

from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from spirit.skills_hub import discovery

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"

# SKILL.md 中的 ${SPIRIT_SKILL_DIR} / ${SPIRIT_SESSION_ID} token。
_SKILL_TEMPLATE_RE = re.compile(r"\$\{(SPIRIT_SKILL_DIR|SPIRIT_SESSION_ID)\}")

# 内联 shell 片段：!`date +%Y-%m-%d`（非贪婪、单行——反引号内不含换行）。
_INLINE_SHELL_RE = re.compile(r"!`([^`\n]+)`")

# 内联 shell 输出封顶，防止跑飞的命令撑爆上下文。
_INLINE_SHELL_MAX_OUTPUT = 4000


def load_skills_config() -> Dict[str, Any]:
    """``skills`` 配置节（转发 :func:`discovery.load_skills_config`，作 monkeypatch seam）。"""
    return discovery.load_skills_config()


def _windows_hide_flags() -> int:
    flags = 0
    for name in ("CREATE_NO_WINDOW", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, name, 0)
    return flags


def _shell_argv(command: str):
    """返回执行内联命令的 argv（Windows: cmd /c；POSIX: $SHELL/bash -c）。"""
    if _IS_WINDOWS:
        return ["cmd", "/c", command]
    shell = os.environ.get("SHELL") or "/bin/bash"
    return [shell, "-c", command]


def substitute_template_vars(
    content: str,
    skill_dir: Optional[Path],
    session_id: Optional[str],
) -> str:
    """替换 ``${SPIRIT_SKILL_DIR}`` / ``${SPIRIT_SESSION_ID}``（仅替换有值的 token）。"""
    if not content:
        return content
    skill_dir_str = str(skill_dir) if skill_dir else None

    def _replace(match: re.Match) -> str:
        token = match.group(1)
        if token == "SPIRIT_SKILL_DIR" and skill_dir_str:
            return skill_dir_str
        if token == "SPIRIT_SESSION_ID" and session_id:
            return str(session_id)
        return match.group(0)

    return _SKILL_TEMPLATE_RE.sub(_replace, content)


def run_inline_shell(command: str, cwd: Optional[Path], timeout: int) -> str:
    """执行单个内联 shell 片段，返回其 stdout（去尾换行、封顶）。

    失败返回简短 ``[inline-shell error: ...]`` / ``[inline-shell timeout ...]`` 标记而非
    抛异常，故一个坏片段不会毁掉整条技能消息。
    """
    popen_kwargs: Dict[str, Any] = {"creationflags": _windows_hide_flags()} if _IS_WINDOWS else {}
    try:
        completed = subprocess.run(
            _shell_argv(command),
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout)),
            check=False,
            stdin=subprocess.DEVNULL,
            **popen_kwargs,
        )
    except subprocess.TimeoutExpired:
        return f"[inline-shell timeout after {timeout}s: {command}]"
    except FileNotFoundError:
        return "[inline-shell error: shell not found]"
    except Exception as exc:
        return f"[inline-shell error: {exc}]"

    output = (completed.stdout or "").rstrip("\n")
    if not output and completed.stderr:
        output = completed.stderr.rstrip("\n")
    if len(output) > _INLINE_SHELL_MAX_OUTPUT:
        output = output[:_INLINE_SHELL_MAX_OUTPUT] + "...[truncated]"
    return output


def expand_inline_shell(content: str, skill_dir: Optional[Path], timeout: int) -> str:
    """把 ``content`` 中每个 ``!`cmd``` 片段替换为其 stdout（以技能目录为 CWD）。"""
    if "!`" not in content:
        return content

    def _replace(match: re.Match) -> str:
        cmd = match.group(1).strip()
        if not cmd:
            return ""
        return run_inline_shell(cmd, skill_dir, timeout)

    return _INLINE_SHELL_RE.sub(_replace, content)


def preprocess_skill_content(
    content: str,
    skill_dir: Optional[Path],
    session_id: Optional[str] = None,
    skills_cfg: Optional[Dict[str, Any]] = None,
) -> str:
    """按配置对 SKILL.md 正文施加模板变量替换 + 内联 shell 展开。

    ``skills_cfg`` 为 None 时读 ``load_skills_config()``。``template_vars`` 默认开启，
    ``inline_shell`` 默认关闭（安全成本，须显式开启）。
    """
    if not content:
        return content
    cfg = skills_cfg if isinstance(skills_cfg, dict) else load_skills_config()
    if cfg.get("template_vars", True):
        content = substitute_template_vars(content, skill_dir, session_id)
    if cfg.get("inline_shell", False):
        timeout = int(cfg.get("inline_shell_timeout", 10) or 10)
        content = expand_inline_shell(content, skill_dir, timeout)
    return content


__all__ = [
    "load_skills_config",
    "substitute_template_vars",
    "run_inline_shell",
    "expand_inline_shell",
    "preprocess_skill_content",
]
