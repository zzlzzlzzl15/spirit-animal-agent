"""基础设施工具集 — Spirit Agent 核心工具。

合并自 Hermes:
- ansi_strip.py: ANSI 转义序列清理
- binary_extensions.py: 二进制文件扩展名检测
- path_security.py: 路径安全验证
- url_safety.py: URL 安全检查（SSRF 防护）
- fuzzy_match.py: 模糊匹配（9 策略链）
"""

import ipaddress
import logging
import os
import re
import socket
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, List, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# ============================================================================
# ANSI 清理（来自 ansi_strip.py）
# ============================================================================

_ANSI_ESCAPE_RE = re.compile(
    r"\x1b"
    r"(?:"
    r"\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"
    r"|\][\s\S]*?(?:\x07|\x1b\\)"
    r"|[PX^_][\s\S]*?(?:\x1b\\)"
    r"|[\x20-\x2f]+[\x30-\x7e]"
    r"|[\x30-\x7e]"
    r")"
    r"|\x9b[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"
    r"|\x9d[\s\S]*?(?:\x07|\x9c)"
    r"|[\x80-\x9f]",
    re.DOTALL,
)

_HAS_ESCAPE = re.compile(r"[\x1b\x80-\x9f]")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_ansi(text: str) -> str:
    """移除 ANSI 转义序列。"""
    if not text or not _HAS_ESCAPE.search(text):
        return text
    return _ANSI_ESCAPE_RE.sub("", text)


def sanitize_display_text(text: str) -> str:
    """清理显示文本（移除 ANSI + 控制字符）。"""
    if not text or not re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", text):
        return text
    text = strip_ansi(text)
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_CHARS_RE.sub("", text)


# ============================================================================
# 二进制扩展名检测（来自 binary_extensions.py）
# ============================================================================

BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".tif",
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".flv", ".m4v", ".mpeg", ".mpg",
    ".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma", ".aiff", ".opus",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".xz", ".z", ".tgz", ".iso",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".obj", ".lib",
    ".app", ".msi", ".deb", ".rpm",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".pyc", ".pyo", ".class", ".jar", ".war", ".ear", ".node", ".wasm", ".rlib",
    ".sqlite", ".sqlite3", ".db", ".mdb", ".idx",
    ".psd", ".ai", ".eps", ".sketch", ".fig", ".xd", ".blend", ".3ds", ".max",
    ".swf", ".fla", ".lockb", ".dat", ".data",
})


def has_binary_extension(path: str) -> bool:
    """检查文件是否为二进制（通过扩展名）。"""
    dot = path.rfind(".")
    if dot == -1:
        return False
    return path[dot:].lower() in BINARY_EXTENSIONS


# ============================================================================
# 路径安全（来自 path_security.py）
# ============================================================================


def validate_within_dir(path: Path, root: Path) -> Optional[str]:
    """确保 path 在 root 目录内。返回错误消息或 None。"""
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
        resolved.relative_to(root_resolved)
    except (ValueError, OSError) as exc:
        return f"路径超出允许范围: {exc}"
    return None


def has_traversal_component(path_str: str) -> bool:
    """检查路径是否包含 .. 遍历。"""
    return ".." in Path(path_str).parts


# ============================================================================
# URL 安全（来自 url_safety.py，简化版）
# ============================================================================

_BLOCKED_HOSTNAMES = frozenset({"metadata.google.internal", "metadata.goog"})
_ALWAYS_BLOCKED_IPS = frozenset({
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("169.254.169.253"),
    ipaddress.ip_address("fd00:ec2::254"),
    ipaddress.ip_address("100.100.100.200"),
})
_ALWAYS_BLOCKED_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
)


def normalize_url_for_request(url: str) -> str:
    """标准化 URL（ASCII 安全）。"""
    if not isinstance(url, str):
        return url
    raw = url.strip()
    if not raw:
        return raw
    raw = re.sub(r"^([A-Za-z][A-Za-z0-9+.-]*://)\s+", r"\1", raw)
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw
    if parsed.scheme.lower() not in {"http", "https"}:
        return raw
    netloc = parsed.netloc
    hostname = parsed.hostname
    if hostname:
        try:
            ascii_host = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            ascii_host = hostname
        if ascii_host != hostname:
            netloc = netloc.replace(hostname, ascii_host, 1)
    from urllib.parse import quote, urlunsplit
    path = quote(parsed.path, safe="/%:@!$&'()*+,;=")
    query = quote(parsed.query, safe="/%:@!$&'()*+,;=?")
    fragment = quote(parsed.fragment, safe="/%:@!$&'()*+,;=?")
    return urlunsplit((parsed.scheme, netloc, path, query, fragment))


def is_safe_url(url: str) -> bool:
    """检查 URL 是否安全（非私有/内部地址）。"""
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").strip().lower().rstrip(".")
        scheme = (parsed.scheme or "").strip().lower()
        if scheme not in {"http", "https"}:
            return False
        if not hostname:
            return False
        if hostname in _BLOCKED_HOSTNAMES:
            logger.warning("阻止访问内部主机名: %s", hostname)
            return False
        try:
            addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            logger.warning("DNS 解析失败: %s", hostname)
            return False
        for family, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            if '%' in ip_str:
                ip_str = ip_str.split('%')[0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                return False
            if ip in _ALWAYS_BLOCKED_IPS or any(ip in net for net in _ALWAYS_BLOCKED_NETWORKS):
                logger.warning("阻止访问云元数据地址: %s -> %s", hostname, ip_str)
                return False
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                logger.warning("阻止访问私有地址: %s -> %s", hostname, ip_str)
                return False
        return True
    except Exception as exc:
        logger.warning("URL 安全检查失败: %s", exc)
        return False


# ============================================================================
# 模糊匹配（来自 fuzzy_match.py，简化版）
# ============================================================================

UNICODE_MAP = {
    "\u201c": '"', "\u201d": '"',
    "\u2018": "'", "\u2019": "'",
    "\u2014": "--", "\u2013": "-",
    "\u2026": "...", "\u00a0": " ",
}


def _unicode_normalize(text: str) -> str:
    for char, repl in UNICODE_MAP.items():
        text = text.replace(char, repl)
    return text


def fuzzy_find_and_replace(
    content: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> Tuple[str, int, Optional[str], Optional[str]]:
    """模糊查找并替换（9 策略链）。

    返回: (新内容, 匹配数, 策略名, 错误消息)
    """
    if not old_string:
        return content, 0, None, "old_string 不能为空"
    if old_string == new_string:
        return content, 0, None, "old_string 和 new_string 相同"

    strategies = [
        ("exact", lambda c, p: _strategy_exact(c, p)),
        ("line_trimmed", lambda c, p: _strategy_line_trimmed(c, p)),
        ("whitespace_normalized", lambda c, p: _strategy_whitespace_normalized(c, p)),
        ("indentation_flexible", lambda c, p: _strategy_indentation_flexible(c, p)),
        ("escape_normalized", lambda c, p: _strategy_escape_normalized(c, p)),
        ("unicode_normalized", lambda c, p: _strategy_unicode_normalized(c, p)),
    ]

    for strategy_name, strategy_fn in strategies:
        matches = strategy_fn(content, old_string)
        if matches:
            if len(matches) > 1 and not replace_all:
                return content, 0, None, (
                    f"找到 {len(matches)} 处匹配。请提供更多上下文或使用 replace_all=True。"
                )
            new_content = _apply_replacements(content, matches, new_string)
            return new_content, len(matches), strategy_name, None

    return content, 0, None, "未找到匹配"


def _strategy_exact(content: str, pattern: str) -> List[Tuple[int, int]]:
    matches = []
    start = 0
    while True:
        pos = content.find(pattern, start)
        if pos == -1:
            break
        matches.append((pos, pos + len(pattern)))
        start = pos + len(pattern)
    return matches


def _strategy_line_trimmed(content: str, pattern: str) -> List[Tuple[int, int]]:
    pattern_lines = [line.strip() for line in pattern.split('\n')]
    pattern_normalized = '\n'.join(pattern_lines)
    content_lines = content.split('\n')
    content_normalized_lines = [line.strip() for line in content_lines]
    return _find_normalized_matches(
        content, content_lines, content_normalized_lines,
        pattern, pattern_normalized
    )


def _strategy_whitespace_normalized(content: str, pattern: str) -> List[Tuple[int, int]]:
    def normalize(s):
        return re.sub(r'[ \t]+', ' ', s)
    pattern_normalized = normalize(pattern)
    content_normalized = normalize(content)
    matches = _strategy_exact(content_normalized, pattern_normalized)
    if not matches:
        return []
    return _map_normalized_positions(content, content_normalized, matches)


def _strategy_indentation_flexible(content: str, pattern: str) -> List[Tuple[int, int]]:
    content_lines = content.split('\n')
    content_stripped = [line.lstrip() for line in content_lines]
    pattern_lines = [line.lstrip() for line in pattern.split('\n')]
    return _find_normalized_matches(
        content, content_lines, content_stripped,
        pattern, '\n'.join(pattern_lines)
    )


def _strategy_escape_normalized(content: str, pattern: str) -> List[Tuple[int, int]]:
    def unescape(s):
        return s.replace('\\n', '\n').replace('\\t', '\t').replace('\\r', '\r')
    pattern_unescaped = unescape(pattern)
    if pattern_unescaped == pattern:
        return []
    return _strategy_exact(content, pattern_unescaped)


def _strategy_unicode_normalized(content: str, pattern: str) -> List[Tuple[int, int]]:
    norm_pattern = _unicode_normalize(pattern)
    norm_content = _unicode_normalize(content)
    if norm_content == content and norm_pattern == pattern:
        return []
    matches = _strategy_exact(norm_content, norm_pattern)
    if not matches:
        matches = _strategy_line_trimmed(norm_content, norm_pattern)
    return matches


def _find_normalized_matches(
    content, content_lines, content_normalized_lines,
    pattern, pattern_normalized
) -> List[Tuple[int, int]]:
    pattern_norm_lines = pattern_normalized.split('\n')
    num_pattern_lines = len(pattern_norm_lines)
    matches = []
    for i in range(len(content_normalized_lines) - num_pattern_lines + 1):
        block = '\n'.join(content_normalized_lines[i:i + num_pattern_lines])
        if block == pattern_normalized:
            start_pos = sum(len(line) + 1 for line in content_lines[:i])
            end_pos = sum(len(line) + 1 for line in content_lines[:i + num_pattern_lines]) - 1
            end_pos = min(len(content), end_pos)
            matches.append((start_pos, end_pos))
    return matches


def _map_normalized_positions(
    original: str, normalized: str, normalized_matches: List[Tuple[int, int]]
) -> List[Tuple[int, int]]:
    if not normalized_matches:
        return []
    orig_to_norm = []
    orig_idx = norm_idx = 0
    while orig_idx < len(original) and norm_idx < len(normalized):
        if original[orig_idx] == normalized[norm_idx]:
            orig_to_norm.append(norm_idx)
            orig_idx += 1
            norm_idx += 1
        elif original[orig_idx] in ' \t' and normalized[norm_idx] == ' ':
            orig_to_norm.append(norm_idx)
            orig_idx += 1
            if orig_idx < len(original) and original[orig_idx] not in ' \t':
                norm_idx += 1
        elif original[orig_idx] in ' \t':
            orig_to_norm.append(norm_idx)
            orig_idx += 1
        else:
            orig_to_norm.append(norm_idx)
            orig_idx += 1
    while orig_idx < len(original):
        orig_to_norm.append(len(normalized))
        orig_idx += 1
    norm_to_orig_start = {}
    norm_to_orig_end = {}
    for orig_pos, norm_pos in enumerate(orig_to_norm):
        if norm_pos not in norm_to_orig_start:
            norm_to_orig_start[norm_pos] = orig_pos
        norm_to_orig_end[norm_pos] = orig_pos
    original_matches = []
    for norm_start, norm_end in normalized_matches:
        if norm_start in norm_to_orig_start:
            orig_start = norm_to_orig_start[norm_start]
        else:
            orig_start = min(i for i, n in enumerate(orig_to_norm) if n >= norm_start)
        if norm_end - 1 in norm_to_orig_end:
            orig_end = norm_to_orig_end[norm_end - 1] + 1
        else:
            orig_end = orig_start + (norm_end - norm_start)
        original_matches.append((orig_start, min(orig_end, len(original))))
    return original_matches


def _apply_replacements(
    content: str, matches: List[Tuple[int, int]], new_string: str
) -> str:
    sorted_matches = sorted(matches, key=lambda x: x[0], reverse=True)
    result = content
    for start, end in sorted_matches:
        result = result[:start] + new_string + result[end:]
    return result
