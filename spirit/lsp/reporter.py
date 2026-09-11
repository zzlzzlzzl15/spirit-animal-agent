"""LSP 诊断格式化 — 将诊断信息呈现给模型。

模型看到的是紧凑的、按严重度过滤的、行号有界的诊断摘要。
格式: ``<diagnostics>`` 块，1-based 行号/列号，
每个文件最多 MAX_PER_FILE 条，总计不超过 MAX_TOTAL_CHARS。
"""
from __future__ import annotations

import html
from typing import Any, Dict, List, Optional

# 严重度名称
SEVERITY_NAMES = {1: "ERROR", 2: "WARN", 3: "INFO", 4: "HINT"}
DEFAULT_SEVERITIES = frozenset({1})  # 仅 ERROR

# 输出限制
MAX_PER_FILE = 20
MAX_TOTAL_CHARS = 4000

# 单字段长度限制（防止恶意诊断注入）
MAX_MESSAGE_CHARS = 300
MAX_CODE_CHARS = 80
MAX_SOURCE_CHARS = 80


def _sanitize_field(value: Any, *, limit: int) -> str:
    """清理诊断字段，防止注入。

    - 折叠换行
    - 移除控制字符
    - 截断长度
    - HTML 转义
    """
    if value is None:
        return ""
    raw = str(value)
    raw = raw.replace("\r", " ").replace("\n", " ")
    # 移除控制字符（保留空格）
    raw = "".join(
        ch for ch in raw
        if ch == " " or (ord(ch) >= 32 and ord(ch) != 127)
    )
    if len(raw) > limit:
        raw = raw[:limit] + "…"
    return html.escape(raw)


def format_diagnostics(
    file_path: str,
    diagnostics: List[dict],
    *,
    severities: Optional[frozenset] = None,
    max_per_file: int = MAX_PER_FILE,
) -> str:
    """格式化单个文件的诊断。

    Args:
        file_path: 文件路径（用于显示）
        diagnostics: LSP 诊断列表
        severities: 要包含的严重度集合（默认仅 ERROR）
        max_per_file: 每个文件最多显示几条

    Returns:
        格式化的诊断字符串，空串表示无诊断。
    """
    if severities is None:
        severities = DEFAULT_SEVERITIES

    filtered = [
        d for d in diagnostics
        if d.get("severity", 1) in severities
    ]

    if not filtered:
        return ""

    lines = [f"<diagnostics file=\"{_sanitize_field(file_path, limit=200)}\">"]

    for d in filtered[:max_per_file]:
        range_info = d.get("range", {})
        start = range_info.get("start", {})
        line = start.get("line", 0) + 1  # 1-based
        col = start.get("character", 0) + 1

        severity = SEVERITY_NAMES.get(d.get("severity", 1), "ERROR")
        message = _sanitize_field(d.get("message", ""), limit=MAX_MESSAGE_CHARS)
        source = _sanitize_field(d.get("source", ""), limit=MAX_SOURCE_CHARS)
        code = _sanitize_field(d.get("code", ""), limit=MAX_CODE_CHARS)

        parts = [f"  L{line}:{col} {severity}"]
        if source:
            parts.append(f"[{source}]")
        if code:
            parts.append(f"({code})")
        parts.append(message)

        lines.append(" ".join(parts))

    if len(filtered) > max_per_file:
        lines.append(f"  ... 还有 {len(filtered) - max_per_file} 条诊断未显示")

    lines.append("</diagnostics>")
    return "\n".join(lines)


def format_multi_file_diagnostics(
    diag_map: Dict[str, List[dict]],
    *,
    severities: Optional[frozenset] = None,
    max_total_chars: int = MAX_TOTAL_CHARS,
) -> str:
    """格式化多文件诊断。

    Args:
        diag_map: {文件路径: 诊断列表}
        severities: 严重度过滤
        max_total_chars: 总输出字符上限

    Returns:
        合并的诊断字符串。
    """
    if severities is None:
        severities = DEFAULT_SEVERITIES

    parts = []
    total_chars = 0

    for file_path, diagnostics in sorted(diag_map.items()):
        formatted = format_diagnostics(file_path, diagnostics, severities=severities)
        if not formatted:
            continue

        if total_chars + len(formatted) > max_total_chars:
            parts.append(f"\n... 输出已达 {max_total_chars} 字符上限，剩余文件省略")
            break

        parts.append(formatted)
        total_chars += len(formatted)

    return "\n".join(parts) if parts else ""


def count_errors(diagnostics: List[dict]) -> int:
    """统计 ERROR 级别诊断数量。"""
    return sum(1 for d in diagnostics if d.get("severity", 1) == 1)


def compute_delta(baseline: List[dict], current: List[dict]) -> List[dict]:
    """计算新增诊断（当前 - 基线）。

    比较逻辑: 按 (line, character, severity, message) 去重。
    """
    def _key(d: dict) -> tuple:
        r = d.get("range", {})
        s = r.get("start", {})
        return (s.get("line", 0), s.get("character", 0),
                d.get("severity", 1), d.get("message", ""))

    baseline_keys = {_key(d) for d in baseline}
    return [d for d in current if _key(d) not in baseline_keys]


__all__ = [
    "format_diagnostics",
    "format_multi_file_diagnostics",
    "count_errors",
    "compute_delta",
    "DEFAULT_SEVERITIES",
    "MAX_PER_FILE",
    "MAX_TOTAL_CHARS",
]
