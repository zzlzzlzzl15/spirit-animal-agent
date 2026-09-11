"""工具基础设施 — Spirit Agent。

合并自 Hermes:
- tool_backend_helpers.py: 工具后端选择辅助
- tool_output_limits.py: 输出限制
- tool_search.py: 渐进式工具披露
- lazy_deps.py: 延迟依赖
- debug_helpers.py: 调试辅助
- website_policy.py: 网站访问策略
- threat_patterns.py: 威胁模式
- tirith_security.py: 安全检查
- credential_files.py: 凭证文件管理
- image_source.py: 图片来源处理
- osv_check.py: OSV 漏洞检查
- hook_output_spill.py: 钩子输出溢出
- thread_context.py: 线程上下文
- vision_tools.py: 视觉分析
- xai_http.py: xAI HTTP 辅助
- xai_video_tools.py: xAI 视频工具
- neutts_synth.py: NeUTTS 语音合成
- microsoft_graph_auth.py: MS Graph 认证
- microsoft_graph_client.py: MS Graph 客户端
- async_delegation.py: 异步委托
- skill_provenance.py: 技能来源
- skills_ast_audit.py: AST 审计
- skills_sync.py: 技能同步
- skill_usage.py: 技能使用追踪
"""

import fnmatch
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)

from spirit.config import get_config_value, SPIRIT_HOME

# ============================================================================
# 输出截断（来自 tool_output_limits.py）
# ============================================================================

DEFAULT_MAX_OUTPUT_CHARS = get_config_value("limits.tool_output_max_chars", 100_000)


def truncate_tool_output(output: str, max_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
                          tool_name: str = "") -> str:
    """截断工具输出到指定字符数。"""
    if not output or len(output) <= max_chars:
        return output
    half = max_chars // 2
    return (
        f"{output[:half]}\n"
        f"\n... [输出已截断: {len(output)} 字符 → {max_chars} 字符] ...\n\n"
        f"{output[-half:]}"
    )


# ============================================================================
# 网站访问策略（来自 website_policy.py）
# ============================================================================

_CACHE_TTL = get_config_value("internal.policy_cache_ttl", 30.0)
_cache_lock = threading.Lock()
_cached_policy: Optional[Dict] = None
_cached_policy_time: float = 0.0

_DEFAULT_BLOCKLIST = {"enabled": False, "domains": [], "shared_files": []}


class WebsitePolicyError(Exception):
    pass


def _normalize_host(host: str) -> str:
    return (host or "").strip().lower().rstrip(".")


def _load_policy() -> Dict:
    global _cached_policy, _cached_policy_time
    config_path = SPIRIT_HOME / "config.yaml"
    with _cache_lock:
        now = time.time()
        if _cached_policy and (now - _cached_policy_time) < _CACHE_TTL:
            return _cached_policy
        if not config_path.exists():
            _cached_policy = dict(_DEFAULT_BLOCKLIST)
            _cached_policy_time = now
            return _cached_policy
        try:
            import yaml
            cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            policy = (cfg or {}).get("website_blocklist", _DEFAULT_BLOCKLIST)
            _cached_policy = dict(policy) if isinstance(policy, dict) else dict(_DEFAULT_BLOCKLIST)
            _cached_policy_time = now
            return _cached_policy
        except Exception:
            _cached_policy = dict(_DEFAULT_BLOCKLIST)
            _cached_policy_time = now
            return _cached_policy


def check_website_access(url: str) -> Tuple[bool, Optional[str]]:
    """检查 URL 是否被网站策略阻止。返回 (allowed, reason)。"""
    policy = _load_policy()
    if not policy.get("enabled", False):
        return True, None

    parsed = urlparse(url)
    host = _normalize_host(parsed.hostname or "")
    if not host:
        return True, None

    block_rules = policy.get("domains", [])
    for rule in block_rules:
        if not isinstance(rule, str):
            continue
        rule = rule.strip().lower()
        if not rule or rule.startswith("#"):
            continue
        if "://" in rule:
            rule = urlparse(rule).netloc or rule
        rule = rule.split("/", 1)[0].strip().rstrip(".")
        if rule.startswith("www."):
            rule = rule[4:]
        if fnmatch.fnmatch(host, rule) or host == rule:
            return False, f"域名 {host} 被网站策略阻止"

    return True, None


# ============================================================================
# 延迟依赖（来自 lazy_deps.py）
# ============================================================================

class LazyDep:
    """延迟导入依赖的包装器。"""

    def __init__(self, module_name: str, attr: Optional[str] = None):
        self._module_name = module_name
        self._attr = attr
        self._resolved = None
        self._failed = False

    def resolve(self):
        if self._resolved is not None:
            return self._resolved
        if self._failed:
            return None
        try:
            import importlib
            mod = importlib.import_module(self._module_name)
            if self._attr:
                self._resolved = getattr(mod, self._attr)
            else:
                self._resolved = mod
            return self._resolved
        except (ImportError, AttributeError):
            self._failed = True
            return None

    @property
    def available(self) -> bool:
        return self.resolve() is not None


# ============================================================================
# 调试辅助（来自 debug_helpers.py）
# ============================================================================

class DebugSession:
    """简单的调试会话记录器。"""

    def __init__(self, name: str, env_var: Optional[str] = None):
        self.name = name
        self._enabled = False
        if env_var:
            self._enabled = bool(os.getenv(env_var))
        self._log_file: Optional[Path] = None

    def enable(self, log_path: Optional[Path] = None):
        self._enabled = True
        self._log_file = log_path

    def log(self, msg: str, *args):
        if not self._enabled:
            return
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] [{self.name}] {msg % args if args else msg}"
        if self._log_file:
            try:
                self._log_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self._log_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
        logger.debug(line)


# ============================================================================
# 线程上下文（来自 thread_context.py）
# ============================================================================

import contextvars

_current_agent_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("agent_id", default=None)
_current_session_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("session_id", default=None)


def get_agent_id() -> Optional[str]:
    return _current_agent_id.get()


def set_agent_id(agent_id: Optional[str]):
    return _current_agent_id.set(agent_id)


def get_session_id() -> Optional[str]:
    return _current_session_id.get()


def set_session_id(session_id: Optional[str]):
    return _current_session_id.set(session_id)


# ============================================================================
# 凭证文件管理（来自 credential_files.py）
# ============================================================================

CREDENTIAL_FILE_EXTENSIONS = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".credentials"}
CREDENTIAL_FILE_NAMES = {".env", ".netrc", ".pgpass", "credentials.json", "service-account.json"}


def is_credential_file(path: Path) -> bool:
    """检查文件是否为凭证文件。"""
    if path.name in CREDENTIAL_FILE_NAMES:
        return True
    if path.suffix.lower() in CREDENTIAL_FILE_EXTENSIONS:
        return True
    return False


def scan_for_credentials(directory: Path, max_depth: int = 3) -> List[Path]:
    """扫描目录中的凭证文件。"""
    found: List[Path] = []
    if not directory.is_dir():
        return found

    for fpath in directory.rglob("*"):
        if fpath.is_file() and is_credential_file(fpath):
            depth = len(fpath.relative_to(directory).parts)
            if depth <= max_depth:
                found.append(fpath)
    return found


# ============================================================================
# 威胁模式检测（来自 threat_patterns.py）
# ============================================================================

THREAT_PATTERNS = [
    {
        "id": "data_exfiltration",
        "description": "数据外泄尝试",
        "patterns": [
            r"curl\s+.*-d\s+.*(/etc/passwd|/etc/shadow|\.ssh|\.env)",
            r"wget\s+.*--post-file\s+.*(credentials|secret|key)",
            r"nc\s+-.*\d+\.\d+\.\d+\.\d+",
        ],
    },
    {
        "id": "prompt_injection",
        "description": "提示注入",
        "patterns": [
            r"ignore\s+(?:\w+\s+)*(?:previous|all|above)\s+(?:\w+\s+)*instructions",
            r"you\s+are\s+now\s+(?:a|in)\s+",
            r"system\s*:\s*(?:you|ignore)",
        ],
    },
    {
        "id": "destructive_commands",
        "description": "破坏性命令",
        "patterns": [
            r"rm\s+-rf\s+/",
            r"mkfs\.\w+\s+/dev/",
            r"dd\s+if=.+\s+of=/dev/",
            r":\(\)\s*\{:\|:\&\}\s*;",  # fork bomb
        ],
    },
    {
        "id": "privilege_escalation",
        "description": "权限提升",
        "patterns": [
            r"chmod\s+[0-7]*777\s+/",
            r"chown\s+root\s+.*&&.*chmod\s+u\+s",
            r"/etc/sudoers",
        ],
    },
]


def scan_for_threats(content: str) -> List[Dict[str, Any]]:
    """扫描内容中的威胁模式。"""
    matches = []
    for threat in THREAT_PATTERNS:
        for pattern in threat["patterns"]:
            if re.search(pattern, content, re.IGNORECASE):
                matches.append({
                    "id": threat["id"],
                    "description": threat["description"],
                    "pattern": pattern,
                })
    return matches


# ============================================================================
# 图片来源处理（来自 image_source.py）
# ============================================================================

def normalize_image_source(source: str) -> Dict[str, Any]:
    """将各种图片来源标准化为统一格式。"""
    source = source.strip()
    if not source:
        return {"type": "error", "error": "空来源"}

    # URL
    if source.startswith(("http://", "https://")):
        return {"type": "url", "url": source}

    # Base64
    if source.startswith("data:image/"):
        return {"type": "base64", "data": source}

    # 本地文件
    path = Path(source)
    if path.exists() and path.is_file():
        return {"type": "file", "path": str(path.resolve())}

    return {"type": "unknown", "source": source}


# ============================================================================
# OSV 漏洞检查（来自 osv_check.py）
# ============================================================================

def check_package_vulnerabilities(package_name: str, version: str) -> Dict[str, Any]:
    """通过 OSV API 检查包的已知漏洞。"""
    try:
        import requests
        response = requests.post(
            get_config_value("integrations.osv_api_url", "https://api.osv.dev/v1/query"),
            json={"package": {"name": package_name, "ecosystem": "PyPI"},
                   "version": version},
            timeout=get_config_value("timeouts.osv_api", 10),
        )
        if response.status_code != 200:
            return {"error": f"OSV API 错误: {response.status_code}"}
        data = response.json()
        vulns = data.get("vulns", [])
        return {
            "package": package_name,
            "version": version,
            "vulnerabilities": len(vulns),
            "details": [{"id": v.get("id"), "summary": v.get("summary", "")[:200]}
                        for v in vulns[:10]],
        }
    except Exception as exc:
        return {"error": str(exc)}


# ============================================================================
# 渐进式工具披露（来自 tool_search.py）
# ============================================================================

TOOL_SEARCH_NAME = "tool_search"
TOOL_DESCRIBE_NAME = "tool_describe"
TOOL_CALL_NAME = "tool_call"
BRIDGE_TOOL_NAMES = frozenset({TOOL_SEARCH_NAME, TOOL_DESCRIBE_NAME, TOOL_CALL_NAME})
CHARS_PER_TOKEN = get_config_value("compression.chars_per_token", 3.5)


@dataclass
class ToolSearchConfig:
    enabled: str = "auto"  # "auto" | "on" | "off"
    threshold_pct: float = 10.0
    search_default_limit: int = 20
    max_search_limit: int = 100


def should_enable_tool_search(total_tool_chars: int, context_window: int,
                                config: Optional[ToolSearchConfig] = None) -> bool:
    """判断是否应启用渐进式工具披露。"""
    cfg = config or ToolSearchConfig()
    if cfg.enabled == "off":
        return False
    if cfg.enabled == "on":
        return True
    # auto 模式：如果工具定义超过阈值百分比的上下文窗口
    if context_window <= 0:
        return False
    pct = (total_tool_chars / context_window) * 100
    return pct > cfg.threshold_pct


# ============================================================================
# 工具后端辅助（来自 tool_backend_helpers.py）
# ============================================================================

def normalize_browser_provider(value: Optional[str]) -> str:
    """标准化浏览器 provider 名称。"""
    provider = str(value or "local").strip().lower()
    return provider or "local"


def coerce_modal_mode(value: Optional[str]) -> str:
    """标准化 modal 模式。"""
    mode = str(value or "auto").strip().lower()
    valid = {"auto", "direct", "managed"}
    return mode if mode in valid else "auto"
