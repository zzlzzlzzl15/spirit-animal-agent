"""危险命令检测与审批系统 — Spirit Agent。

移植自 Hermes tools/approval.py（3,496 行）。

核心功能：
- HARDLINE_PATTERNS: 无条件阻止（rm -rf /, mkfs, dd 块设备, fork bomb, shutdown/reboot）
- DANGEROUS_PATTERNS: 需要审批的危险命令（递归删除, chmod 777, SQL DROP, curl|sh 等）
- 命令反混淆规范化（ANSI 剥离, Unicode 归一化, shell 转义折叠, $IFS 展开）
- 会话级审批状态管理
- YOLO 模式 / 永久白名单
- sudo stdin 密码猜测防护
- 敏感文件路径保护（/etc/, ~/.ssh, ~/.hermes/config.yaml, .env 等）
"""

import contextvars
import fnmatch
import functools
import logging
import os
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from spirit.config import SPIRIT_HOME

# ============================================================================
# 交互式上下文（ContextVar 线程安全）
# ============================================================================

_spirit_interactive_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "spirit_interactive", default=None,
)
_approval_session_key: contextvars.ContextVar[str] = contextvars.ContextVar(
    "spirit_approval_session", default="",
)


def set_interactive_context(interactive: bool) -> contextvars.Token:
    """绑定当前上下文的交互模式（线程/协程安全）。"""
    return _spirit_interactive_ctx.set("1" if interactive else "")


def reset_interactive_context(token: contextvars.Token) -> None:
    """恢复先前交互模式。"""
    _spirit_interactive_ctx.reset(token)


def set_current_session_key(session_key: str) -> contextvars.Token[str]:
    """绑定当前审批会话键。"""
    return _approval_session_key.set(session_key or "")


def reset_current_session_key(token: contextvars.Token[str]) -> None:
    """恢复先前会话键。"""
    _approval_session_key.reset(token)


def is_truthy_value(val: Any) -> bool:
    """判断值是否为 truthy 字符串。"""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("1", "true", "yes", "on")
    return bool(val)


def env_var_enabled(name: str, default: str = "") -> bool:
    """检查环境变量是否为 truthy。"""
    return is_truthy_value(os.environ.get(name, default))


def _is_interactive_session() -> bool:
    """当前是否在交互式会话中运行。"""
    ctx_val = _spirit_interactive_ctx.get()
    if ctx_val is not None:
        return is_truthy_value(ctx_val)
    return env_var_enabled("SPIRIT_INTERACTIVE")


def get_current_session_key(default: str = "default") -> str:
    """返回当前活跃会话键。"""
    session_key = _approval_session_key.get()
    if session_key:
        return session_key
    return os.environ.get("SPIRIT_SESSION_KEY", default)


# ============================================================================
# 敏感路径正则
# ============================================================================

_SSH_SENSITIVE_PATH = r'(?:~|\$home|\$\{home\})/\.ssh(?:/|$)'
_SPIRIT_ENV_PATH = (
    r'(?:~\/\.spirit/|'
    r'(?:\$home|\$\{home\})/\.spirit/|'
    r'(?:\$spirit_home|\$\{spirit_home\})/)'
    r'\.env\b'
)
_SPIRIT_CONFIG_PATH = (
    r'(?:~\/\.spirit/|'
    r'(?:\$home|\$\{home\})/\.spirit/|'
    r'(?:\$spirit_home|\$\{spirit_home\})/)'
    r'config\.yaml\b'
)
_PROJECT_ENV_PATH = r'(?:(?:/|\.{1,2}/)?(?:[^\s/"\'`]+/)*\.env(?:\.[^/\s"\'`]+)*)'
_PROJECT_CONFIG_PATH = r'(?:(?:/|\.{1,2}/)?(?:[^\s/"\'`]+/)*config\.yaml)'
_SHELL_RC_FILES = (
    r'(?:~|\$home|\$\{home\})/\.'
    r'(?:bashrc|zshrc|profile|bash_profile|zprofile)\b'
)
_CREDENTIAL_FILES = (
    r'(?:~|\$home|\$\{home\})/\.'
    r'(?:netrc|pgpass|npmrc|pypirc)\b'
)
_MACOS_PRIVATE_SYSTEM_PATH = r'/private/(?:etc|var|tmp|home)/'
_SYSTEM_CONFIG_PATH = rf'(?:/etc/|{_MACOS_PRIVATE_SYSTEM_PATH})'
_SENSITIVE_WRITE_TARGET = (
    rf'(?:{_SYSTEM_CONFIG_PATH}|/dev/sd|'
    rf'{_SSH_SENSITIVE_PATH}|'
    rf'{_SPIRIT_ENV_PATH}|'
    rf'{_SPIRIT_CONFIG_PATH}|'
    rf'{_SHELL_RC_FILES}|'
    rf'{_CREDENTIAL_FILES})'
)
_USER_SENSITIVE_WRITE_TARGET = (
    rf'(?:{_SSH_SENSITIVE_PATH}|'
    rf'{_SHELL_RC_FILES}|'
    rf'{_CREDENTIAL_FILES})'
)
_PROJECT_SENSITIVE_WRITE_TARGET = rf'(?:{_PROJECT_ENV_PATH}|{_PROJECT_CONFIG_PATH})'
_COMMAND_TAIL = r'(?:\s*(?:&&|\|\||;).*)?$'
_WRITE_TARGET_BOUNDARY = r'(?=[\s;&|<>"\']|$)'

# ============================================================================
# 命令位置锚点（防止 grep "rm -rf /" 误触发）
# ============================================================================

_CMDPOS = (
    r'(?:^|[\n`]|\$\()'
    r'\s*'
    r'(?:sudo\s+(?:-[^\s]+\s+)*)?'
    r'(?:env\s+(?:\w+=\S*\s+)*)?'
    r'(?:(?:exec|nohup|setsid|time)\s+)*'
    r'\s*'
)


def _hardline_rm_path(path_alt: str, tail: str = r'(?:\s|$|[)`;|&])') -> str:
    return rf'(?:["\'](?:{path_alt})["\']|(?:{path_alt}){tail})'


_HARDLINE_SYSTEM_DIRS = (
    r'/home|/home/\*|/root|/root/\*|/etc|/etc/\*|/usr|/usr/\*|'
    r'/var|/var/\*|/bin|/bin/\*|/sbin|/sbin/\*|/boot|/boot/\*|/lib|/lib/\*'
)

_RM_FLAG_PREFIX = _CMDPOS + r'rm\s+(-[^\s]*\s+)*'

# ============================================================================
# HARDLINE 模式 — 无条件阻止（即使 YOLO 也不能绕过）
# ============================================================================

HARDLINE_PATTERNS = [
    (_RM_FLAG_PREFIX + _hardline_rm_path(r'/(?:(?:\.\.?)?/)*(?:\.\.?)?\**|/ \*'),
     "recursive delete of root filesystem"),
    (_RM_FLAG_PREFIX + _hardline_rm_path(_HARDLINE_SYSTEM_DIRS),
     "recursive delete of system directory"),
    (_RM_FLAG_PREFIX + _hardline_rm_path(r'(?:~|\$\{?HOME\}?)(?:/?|/\*)?'),
     "recursive delete of home directory"),
    (r'\bmkfs(\.[a-z0-9]+)?\b', "format filesystem (mkfs)"),
    (r'\bdd\b[^\n]*\bof=/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*',
     "dd to raw block device"),
    (r'>\s*/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*\b',
     "redirect to raw block device"),
    (r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:', "fork bomb"),
    (r'\bkill\s+(-[^\s]+\s+)*-1\b', "kill all processes"),
    (_CMDPOS + r'(shutdown|reboot|halt|poweroff)\b', "system shutdown/reboot"),
    (_CMDPOS + r'init\s+[06]\b', "init 0/6 (shutdown/reboot)"),
    (_CMDPOS + r'systemctl\s+(poweroff|reboot|halt|kexec)\b',
     "systemctl poweroff/reboot"),
    (_CMDPOS + r'telinit\s+[06]\b', "telinit 0/6 (shutdown/reboot)"),
]

_RE_FLAGS = re.IGNORECASE | re.DOTALL
HARDLINE_PATTERNS_COMPILED = [
    (re.compile(pattern, _RE_FLAGS), description)
    for pattern, description in HARDLINE_PATTERNS
]

# ============================================================================
# sudo stdin 密码猜测防护
# ============================================================================

_SUDO_STDIN_RE = re.compile(
    r'(?:^|[;&|`\n]|&&|\|\||\$\()\s*sudo\s+-S\b',
    re.IGNORECASE,
)


def _check_sudo_stdin_guard(command: str) -> Tuple[bool, Optional[str]]:
    """检测 sudo -S（stdin 密码猜测）攻击向量。"""
    if "SUDO_PASSWORD" in os.environ:
        return (False, None)
    normalized = _normalize_command_for_detection(command).lower()
    if _SUDO_STDIN_RE.search(normalized):
        return (True, "sudo password guessing via stdin (sudo -S)")
    return (False, None)


# ============================================================================
# DANGEROUS 模式 — 需要审批
# ============================================================================

DANGEROUS_PATTERNS = [
    (r'\brm\s+(-[^\s]*\s+)*/', "delete in root path"),
    (r'\brm\s+-[^\s]*r', "recursive delete"),
    (r'\brm\s+--recursive\b', "recursive delete (long flag)"),
    (r'\bcmd(?:\.exe)?\s+/(?:c|k)\s+.*\b(?:del|erase|rd|rmdir)\b',
     "Windows cmd destructive delete"),
    (r'\b(?:powershell|pwsh)(?:\.exe)?\b(?:\s+-\S+)*\s+(?:-(?:command|c)\s+)?'
     r'["\']?(?:remove-item|rmdir|erase|del|rd|ri|rm)\b',
     "Windows PowerShell destructive delete"),
    (r'\b(?:powershell|pwsh)(?:\.exe)?\b.*\s-(?:encodedcommand|enc|e)\b',
     "PowerShell encoded command execution"),
    (r'\bchmod\s+(-[^\s]*\s+)*(777|666|o\+[rwx]*w|a\+[rwx]*w)\b',
     "world/other-writable permissions"),
    (r'\bchmod\s+--recursive\b.*(777|666|o\+[rwx]*w|a\+[rwx]*w)',
     "recursive world/other-writable (long flag)"),
    (r'\bchown\s+(-[^\s]*)?R\s+root', "recursive chown to root"),
    (r'\bchown\s+--recur[a-z]*\b.*root', "recursive chown to root (long flag)"),
    (r'\bmkfs\b', "format filesystem"),
    (r'\bdd\s+.*if=', "disk copy"),
    (r'>\s*/dev/sd', "write to block device"),
    (r'\bDROP\s+(TABLE|DATABASE)\b', "SQL DROP"),
    (r'\bDELETE\s+FROM\b(?![^\n]*\bWHERE\b)', "SQL DELETE without WHERE"),
    (r'\bTRUNCATE\s+(TABLE)?\s*\w', "SQL TRUNCATE"),
    (rf'>\s*{_SYSTEM_CONFIG_PATH}', "overwrite system config"),
    (r'\bsystemctl\s+(-[^\s]+\s+)*(stop|restart|disable|mask)\b',
     "stop/restart system service"),
    (r'\bkill\s+-9\s+-1\b', "kill all processes"),
    (r'\bpkill\s+-9\b', "force kill processes"),
    (r'\bkillall\s+(-[^\s]*\s+)*-(9|KILL|SIGKILL)\b',
     "force kill processes (killall -KILL)"),
    (r'\bkillall\s+(-[^\s]*\s+)*-s\s+(KILL|SIGKILL|9)\b',
     "force kill processes (killall -s KILL)"),
    (r'\bkillall\s+(-[^\s]*\s+)*-r\b', "kill processes by regex (killall -r)"),
    (r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:', "fork bomb"),
    (r'\b(curl|wget)\b.*\|\s*(?:[/\w]*/)?(?:ba)?sh(?:\s|$|-c)',
     "pipe remote content to shell"),
    (r'\b(bash|sh|zsh|ksh)\s+<\s*<?\s*\(\s*(curl|wget)\b',
     "execute remote script via process substitution"),
    (r'(?:\beval\b|\bsource\b|\.)\s*(?:\$\(\s*|`\s*)(?:curl|wget)\b',
     "execute remote content via command substitution"),
    (r'\b(base64|base32|base16)\s+(?:-[dD]|--decode)\b.*\|\s*\b(bash|sh|zsh|ksh|dash)\b',
     "pipe decoded content to shell (possible command obfuscation)"),
    (r'\bxxd\s+-r\b.*\|\s*\b(bash|sh|zsh|ksh|dash)\b',
     "pipe xxd-decoded content to shell (possible command obfuscation)"),
    (r'\becho\b[^|]*\|\s*\btr\b[^|]*\|\s*\b(bash|sh|zsh|ksh|dash)\b',
     "pipe tr-transformed output to shell (possible command obfuscation)"),
    (r'\bopenssl\b.*\b(?:base64|enc)\b[^|]*\s+-[dD]\b[^|]*\|\s*\b(bash|sh|zsh|ksh|dash)\b',
     "pipe openssl-decoded content to shell (possible command obfuscation)"),
    (rf'\btee\b.*["\']?{_SENSITIVE_WRITE_TARGET}',
     "overwrite system file via tee"),
    (rf'>>?\s*["\']?{_SENSITIVE_WRITE_TARGET}',
     "overwrite system file via redirection"),
    (rf'\btee\b.*["\']?{_PROJECT_SENSITIVE_WRITE_TARGET}["\']?{_WRITE_TARGET_BOUNDARY}',
     "overwrite project env/config via tee"),
    (rf'>>?\s*["\']?{_PROJECT_SENSITIVE_WRITE_TARGET}["\']?{_WRITE_TARGET_BOUNDARY}',
     "overwrite project env/config via redirection"),
    (r'\bxargs\s+.*\brm\b', "xargs with rm"),
    (r'\bfind\b.*-exec(?:dir)?\s+(/\S*/)?rm\b', "find -exec/-execdir rm"),
    (r'\bfind\b.*-delete\b', "find -delete"),
    (r'\bgit\s+reset\s+--h(?:a(?:r(?:d)?)?)?\b',
     "git reset --hard (destroys uncommitted changes)"),
    (r'\bgit\s+push\b.*--forc[a-z]*\b',
     "git force push (rewrites remote history)"),
    (r'\bgit\s+push\b.*-f\b',
     "git force push short flag (rewrites remote history)"),
    (r'\bgit\s+clean\s+-[^\s]*f',
     "git clean with force (deletes untracked files)"),
    (r'\bgit\s+branch\s+-D\b', "git branch force delete"),
    (r'\bgit\s+branch\b[^;|&\n]*?(?:-d\b|--delete\b)[^;|&\n]*?(?:-f\b|--force\b)',
     "git branch force delete (long flags)"),
    (r'\bgit\s+branch\b[^;|&\n]*?(?:-f\b|--force\b)[^;|&\n]*?(?:-d\b|--delete\b)',
     "git branch force delete (long flags, force-first)"),
    (r'\bchmod\s+\+x\b.*[;&|]+\s*\./',
     "chmod +x followed by immediate execution"),
    (r'\bsudo\b[^;|&\n]*?\s+(?:-s\b|--st[a-z]*\b|-a\b|--a[a-z]*\b)',
     "sudo with privilege flag (stdin/askpass/shell/list)"),
    (r'\bsudo\b[^;|&\n]*?\s+-[a-z]*[sa][a-z]*\b',
     "sudo with combined-flag privilege escalation"),
    (r'\b(bash|sh|zsh|ksh)\s+<<', "shell execution via heredoc"),
    (rf'\b(cp|mv|install)\b.*\s{_SYSTEM_CONFIG_PATH}',
     "copy/move file into system config path"),
    (rf'\b(cp|mv|install)\b.*\s["\']?{_PROJECT_SENSITIVE_WRITE_TARGET}["\']?{_COMMAND_TAIL}',
     "overwrite project env/config file"),
    (rf'\b(cp|mv|install)\b.*\s["\']?{_SENSITIVE_WRITE_TARGET}[^\s"\']*["\']?{_COMMAND_TAIL}',
     "copy/move file into sensitive credential/SSH/shell-rc path"),
    (rf'\bsed\s+-[^\s]*i.*(?:{_USER_SENSITIVE_WRITE_TARGET})[^\s"\']*',
     "in-place edit of sensitive credential/SSH/shell-rc path"),
    (rf'\bsed\s+--in-place\b.*(?:{_USER_SENSITIVE_WRITE_TARGET})[^\s"\']*',
     "in-place edit of sensitive credential/SSH/shell-rc path (long flag)"),
    (rf'\b(?:perl|ruby)\b.*(?:^|\s)-[^\s]*i\b.*(?:{_USER_SENSITIVE_WRITE_TARGET})[^\s"\']*',
     "in-place edit of sensitive credential/SSH/shell-rc path (perl/ruby)"),
    (rf'\bsed\s+-[^\s]*i.*\s{_SYSTEM_CONFIG_PATH}',
     "in-place edit of system config"),
    (rf'\bsed\s+--in-place\b.*\s{_SYSTEM_CONFIG_PATH}',
     "in-place edit of system config (long flag)"),
    (rf'\bsed\s+-[^\s]*i.*(?:{_SPIRIT_CONFIG_PATH}|{_SPIRIT_ENV_PATH})',
     "in-place edit of Spirit config/env"),
    (rf'\bsed\s+--in-place\b.*(?:{_SPIRIT_CONFIG_PATH}|{_SPIRIT_ENV_PATH})',
     "in-place edit of Spirit config/env (long flag)"),
    (rf'\b(?:perl|ruby)\b.*(?:^|\s)-[^\s]*i\b.*(?:{_SPIRIT_CONFIG_PATH}|{_SPIRIT_ENV_PATH})',
     "in-place edit of Spirit config/env (perl/ruby)"),
]

DANGEROUS_PATTERNS_COMPILED = [
    (re.compile(pattern, _RE_FLAGS), description)
    for pattern, description in DANGEROUS_PATTERNS
]

# ============================================================================
# 命令规范化（反混淆）
# ============================================================================

# 解析器限制
_MAX_DETECTION_COMMAND_CHARS = 128_000
_MAX_SEPARATOR_FREE_COMMAND_CHARS = 4_096
_MAX_DETECTION_SEGMENTS = 25_000
_PARSER_LIMIT_DESCRIPTION = "command parser limit exceeded"


def _command_parser_limit_exceeded(command: str) -> bool:
    """限制所有解析器工作，防止 DoS。"""
    if len(command) > _MAX_DETECTION_COMMAND_CHARS:
        return True
    if (
        len(command) > _MAX_SEPARATOR_FREE_COMMAND_CHARS
        and not any(char in command for char in ";&|\n")
    ):
        return True
    separators = 0
    for char in command:
        if char in ";&|\n":
            separators += 1
            if separators >= _MAX_DETECTION_SEGMENTS:
                return True
    return False


def _rewrite_resolved_user_home(command: str) -> str:
    """将绝对用户主目录重写为 ~/ 形式。"""
    try:
        home = os.path.expanduser("~")
        candidates = [home, os.path.realpath(home), os.environ.get("HOME", "")]
    except Exception:
        return command
    for path in sorted((p for p in candidates if p), key=len, reverse=True):
        if not path:
            continue
        parts = [c for c in re.split(r"[/\\]+", path) if c]
        if len(parts) < 2:
            continue
        body = r"[/\\]+".join(re.escape(c) for c in parts)
        pattern = re.compile(r"[/\\]*" + body + r"([/\\][^/\\\s\"'`;|&<>()]*)+")
        command = pattern.sub(
            lambda m: "~" + m.group(0)[len(path):].replace("\\", "/"),
            command,
        )
    return command


def _normalize_command_for_detection(command: str) -> str:
    """规范化命令字符串以进行模式匹配（反混淆）。

    处理：
    1. 剥离 ANSI 转义序列
    2. 剥离 null 字节
    3. Unicode NFKC 归一化（全角拉丁 → 半角）
    4. 折叠 shell 行续符（反斜杠+换行）
    5. 折叠绝对主目录路径到 ~/ 形式
    6. 剥离 shell 反斜杠转义（r\\m → rm）
    7. 剥离空字符串字面量（r''m → rm）
    8. 折叠 $IFS / ${IFS} 为空格
    """
    # 1. 剥离 ANSI 转义序列
    command = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07', '', command)
    # 2. 剥离 null 字节
    command = command.replace('\x00', '')
    # 3. Unicode NFKC 归一化
    command = unicodedata.normalize('NFKC', command)
    # 4. 折叠 shell 行续符
    command = re.sub(r'\\\r?\n', '', command)
    # 5. 折叠绝对主目录路径
    command = _rewrite_resolved_user_home(command)
    # 6. 剥离 shell 反斜杠转义
    command = re.sub(r'\\([^\n])', r'\1', command)
    # 7. 剥离空字符串字面量
    command = re.sub(r"''|\"\"", '', command)
    # 8. 折叠 $IFS / ${IFS}
    command = re.sub(r'\$\{IFS\b[^}]*\}|\$IFS\b', ' ', command)
    return command


# ============================================================================
# 检测入口
# ============================================================================

def detect_hardline_command(command: str) -> Tuple[bool, Optional[str]]:
    """检查命令是否匹配 HARDLINE 阻止列表（永远不可绕过）。

    Returns:
        (is_hardline, description) or (False, None)
    """
    if _command_parser_limit_exceeded(command):
        return (True, _PARSER_LIMIT_DESCRIPTION)
    normalized = _normalize_command_for_detection(command)
    for command_variant in _command_detection_variants(command):
        variant_lower = command_variant.lower()
        for pattern_re, description in HARDLINE_PATTERNS_COMPILED:
            if pattern_re.search(variant_lower):
                return (True, description)
    return (False, None)


def detect_dangerous_pattern(command: str) -> Tuple[bool, Optional[str]]:
    """检查命令是否匹配危险模式。

    Returns:
        (is_dangerous, description) or (False, None)
    """
    if _command_parser_limit_exceeded(command):
        return (True, _PARSER_LIMIT_DESCRIPTION)
    normalized = _normalize_command_for_detection(command)
    for command_variant in _command_detection_variants(command):
        variant_lower = command_variant.lower()
        for pattern_re, description in DANGEROUS_PATTERNS_COMPILED:
            if pattern_re.search(variant_lower):
                return (True, description)
    return (False, None)


def _command_detection_variants(command: str) -> List[str]:
    """生成命令的多个变体用于检测（原始 + 规范化）。"""
    variants = [command]
    normalized = _normalize_command_for_detection(command)
    if normalized != command:
        variants.append(normalized)
    return variants


# ============================================================================
# 用户自定义拒绝规则（approvals.deny）
# ============================================================================

def _match_user_deny_rule(command: str) -> Optional[str]:
    """匹配用户定义的拒绝规则（config.yaml 中的 approvals.deny）。"""
    try:
        deny_patterns = _get_approval_config().get("deny") or []
    except Exception:
        return None
    if not deny_patterns:
        return None
    globs = [p.strip() for p in deny_patterns if isinstance(p, str) and p.strip()]
    if not globs:
        return None
    for command_variant in _command_detection_variants(command):
        candidate = command_variant.lower().strip()
        for pattern in globs:
            if fnmatch.fnmatchcase(candidate, pattern.lower()):
                return pattern
    return None


# ============================================================================
# 审批配置
# ============================================================================

_approval_config_cache: Dict[str, Any] = {}
_approval_config_mtime: float = 0.0


def _get_approval_config() -> Dict[str, Any]:
    """加载审批配置（带 mtime 缓存）。"""
    global _approval_config_cache, _approval_config_mtime
    config_path = SPIRIT_HOME / "config.yaml"
    try:
        mtime = config_path.stat().st_mtime
    except OSError:
        return _approval_config_cache
    if mtime > _approval_config_mtime:
        try:
            import yaml
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            _approval_config_cache = cfg.get("approvals", {}) or {}
            _approval_config_mtime = mtime
        except Exception as exc:
            logger.debug("Could not load approval config: %s", exc)
    return _approval_config_cache


# ============================================================================
# YOLO 模式检测
# ============================================================================

def is_yolo_mode() -> bool:
    """检查是否处于 YOLO 模式（跳过审批）。"""
    if env_var_enabled("SPIRIT_YOLO"):
        return True
    try:
        cfg = _get_approval_config()
        if is_truthy_value(cfg.get("yolo", False)):
            return True
    except Exception:
        pass
    return False


# ============================================================================
# 审批模式
# ============================================================================

def get_approval_mode() -> str:
    """获取当前审批模式。

    Returns:
        "interactive" | "auto" | "off" | "cron"
    """
    try:
        cfg = _get_approval_config()
        mode = cfg.get("mode", "interactive")
        if isinstance(mode, str) and mode in ("interactive", "auto", "off", "cron"):
            return mode
    except Exception:
        pass
    if env_var_enabled("SPIRIT_CRON_SESSION"):
        return "cron"
    return "interactive"


# ============================================================================
# 会话级永久白名单
# ============================================================================

_session_allowlist: Dict[str, Dict[str, float]] = {}
_session_allowlist_lock = threading.Lock()


def add_session_approval(pattern_key: str, session_key: Optional[str] = None) -> None:
    """将模式添加到会话级白名单。"""
    sk = session_key or get_current_session_key()
    with _session_allowlist_lock:
        if sk not in _session_allowlist:
            _session_allowlist[sk] = {}
        _session_allowlist[sk][pattern_key] = time.time()


def is_session_approved(pattern_key: str, session_key: Optional[str] = None) -> bool:
    """检查模式是否在会话白名单中。"""
    sk = session_key or get_current_session_key()
    with _session_allowlist_lock:
        session_patterns = _session_allowlist.get(sk, {})
        return pattern_key in session_patterns


def clear_session_approvals(session_key: Optional[str] = None) -> None:
    """清除会话级白名单。"""
    sk = session_key or get_current_session_key()
    with _session_allowlist_lock:
        _session_allowlist.pop(sk, None)


# ============================================================================
# 永久白名单（持久化到 config.yaml）
# ============================================================================

def get_permanent_allowlist() -> List[str]:
    """获取永久白名单（config.yaml 中的 approvals.command_allowlist）。"""
    try:
        cfg = _get_approval_config()
        allowlist = cfg.get("command_allowlist") or []
        return [str(item) for item in allowlist if isinstance(item, str)]
    except Exception:
        return []


def add_permanent_approval(pattern_key: str) -> None:
    """添加永久白名单条目。"""
    import yaml
    config_path = SPIRIT_HOME / "config.yaml"
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        else:
            cfg = {}
        approvals = cfg.setdefault("approvals", {})
        allowlist = approvals.setdefault("command_allowlist", [])
        if pattern_key not in allowlist:
            allowlist.append(pattern_key)
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        global _approval_config_mtime
        _approval_config_mtime = 0.0  # 强制重新加载
    except Exception as exc:
        logger.error("Failed to add permanent approval: %s", exc)


# ============================================================================
# 主审批入口
# ============================================================================

def check_command_approval(command: str) -> Dict[str, Any]:
    """检查命令是否需要审批。

    审批优先级：
    1. HARDLINE → 无条件阻止（不可绕过）
    2. sudo stdin 防护 → 阻止密码猜测
    3. 用户 deny 规则 → 阻止
    4. YOLO 模式 → 放行（除 HARDLINE）
    5. 审批模式 off → 放行
    6. 会话白名单 → 放行
    7. 永久白名单 → 放行
    8. 危险模式匹配 → 需要审批

    Returns:
        {
            "approved": bool,
            "message": str,  # 阻止原因或放行说明
            "pattern_key": str | None,  # 匹配的模式键
            "requires_user_approval": bool,  # 是否需要用户交互
        }
    """
    # 1. HARDLINE — 无条件阻止
    is_hardline, hardline_desc = detect_hardline_command(command)
    if is_hardline:
        return {
            "approved": False,
            "hardline": True,
            "message": (
                f"BLOCKED (hardline): {hardline_desc}. "
                "This command is on the unconditional blocklist and cannot "
                "be executed — not even with YOLO mode or approvals.mode=off. "
                "Run it yourself in a terminal outside the agent."
            ),
            "pattern_key": hardline_desc,
            "requires_user_approval": False,
        }

    # 2. sudo stdin 防护
    is_sudo_guard, sudo_desc = _check_sudo_stdin_guard(command)
    if is_sudo_guard:
        return {
            "approved": False,
            "message": (
                f"BLOCKED: {sudo_desc}. "
                "Do not pipe passwords to 'sudo -S' — this is a brute-force "
                "attack vector. Set SUDO_PASSWORD in your .env file if the "
                "agent needs passwordless sudo, or run the sudo command "
                "manually in your own terminal."
            ),
            "pattern_key": sudo_desc,
            "requires_user_approval": False,
        }

    # 3. 用户 deny 规则
    deny_pattern = _match_user_deny_rule(command)
    if deny_pattern:
        return {
            "approved": False,
            "user_deny": True,
            "message": (
                f"BLOCKED: this command matches the user-defined deny rule "
                f"'{deny_pattern}'. It cannot be executed — not even with "
                "YOLO mode or approvals.mode=off."
            ),
            "pattern_key": deny_pattern,
            "requires_user_approval": False,
        }

    # 4. YOLO 模式 → 放行
    if is_yolo_mode():
        return {
            "approved": True,
            "message": "YOLO mode: all non-hardline commands auto-approved.",
            "pattern_key": None,
            "requires_user_approval": False,
        }

    # 5. 审批模式 off → 放行
    mode = get_approval_mode()
    if mode == "off":
        return {
            "approved": True,
            "message": "Approval mode is off.",
            "pattern_key": None,
            "requires_user_approval": False,
        }

    # 6. 检测危险模式
    is_dangerous, danger_desc = detect_dangerous_pattern(command)
    if not is_dangerous:
        return {
            "approved": True,
            "message": "No dangerous pattern detected.",
            "pattern_key": None,
            "requires_user_approval": False,
        }

    # 7. 会话白名单
    if is_session_approve(danger_desc):
        return {
            "approved": True,
            "message": f"Session-approved: {danger_desc}",
            "pattern_key": danger_desc,
            "requires_user_approval": False,
        }

    # 8. 永久白名单
    permanent = get_permanent_allowlist()
    if danger_desc in permanent:
        return {
            "approved": True,
            "message": f"Permanently approved: {danger_desc}",
            "pattern_key": danger_desc,
            "requires_user_approval": False,
        }

    # 9. 需要用户审批
    return {
        "approved": False,
        "message": (
            f"DANGEROUS: {danger_desc}. "
            "This command requires user approval before execution."
        ),
        "pattern_key": danger_desc,
        "requires_user_approval": True,
    }


def is_session_approve(pattern_key: str) -> bool:
    """检查模式是否在会话白名单中（别名兼容）。"""
    return is_session_approved(pattern_key)


# ============================================================================
# 审批结果辅助函数
# ============================================================================

def hardline_block_result(description: str) -> Dict[str, Any]:
    """构建 HARDLINE 阻止结果。"""
    return {
        "approved": False,
        "hardline": True,
        "message": (
            f"BLOCKED (hardline): {description}. "
            "This command is on the unconditional blocklist and cannot "
            "be executed via the agent — not even with YOLO, "
            "approvals.mode=off, or cron approve mode."
        ),
    }


def user_deny_block_result(pattern: str) -> Dict[str, Any]:
    """构建用户 deny 阻止结果。"""
    return {
        "approved": False,
        "user_deny": True,
        "message": (
            f"BLOCKED: this command matches the user-defined deny rule "
            f"'{pattern}'. It cannot be executed — not even with YOLO."
        ),
    }


def sudo_stdin_block_result(description: str) -> Dict[str, Any]:
    """构建 sudo stdin 阻止结果。"""
    return {
        "approved": False,
        "message": (
            f"BLOCKED: {description}. "
            "Do not pipe passwords to 'sudo -S'."
        ),
    }
