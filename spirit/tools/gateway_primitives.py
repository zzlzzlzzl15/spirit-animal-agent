"""网关原语 — Spirit Agent。

移植自 Hermes:
- clarify_gateway.py（316 行）: 网关侧阻塞式澄清队列
- slash_confirm.py（168 行）: 斜杠命令确认（按钮 UI + 文本回退）
- hook_output_spill.py（237 行）: 钩子输出溢出到磁盘
- managed_tool_gateway.py（151 行）: Nous 托管工具网关
- microsoft_graph_auth.py（202 行）: MS Graph 设备码/交互式认证
- microsoft_graph_client.py（358 行）: MS Graph 异步 REST 客户端
- openrouter_client.py（25 行）: OpenRouter 懒加载客户端
- neutts_synth.py（83 行）: NeuTTS 语音合成辅助
- tirith_security.py（726 行）: tirith 二进制安全扫描器
"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

from spirit.config import SPIRIT_HOME, get_config_value

# ============================================================================
# clarify_gateway — 网关侧阻塞式澄清队列
# ============================================================================


@dataclass
class _ClarifyEntry:
    """一个待处理的澄清请求。"""
    clarify_id: str
    session_key: str
    question: str
    choices: Optional[List[str]]
    event: threading.Event = field(default_factory=threading.Event)
    response: Optional[str] = None
    awaiting_text: bool = False

    def signature(self) -> Dict[str, object]:
        return {
            "clarify_id": self.clarify_id,
            "session_key": self.session_key,
            "question": self.question,
            "choices": list(self.choices) if self.choices else None,
        }


_clarify_lock = threading.RLock()
_clarify_entries: Dict[str, _ClarifyEntry] = {}
_clarify_session_index: Dict[str, List[str]] = {}


def clarify_register(
    clarify_id: str,
    session_key: str,
    question: str,
    choices: Optional[List[str]],
) -> _ClarifyEntry:
    """注册一个待处理的澄清请求。"""
    entry = _ClarifyEntry(
        clarify_id=clarify_id,
        session_key=session_key,
        question=question,
        choices=list(choices) if choices else None,
        awaiting_text=not bool(choices),
    )
    with _clarify_lock:
        _clarify_entries[clarify_id] = entry
        _clarify_session_index.setdefault(session_key, []).append(clarify_id)
    return entry


def clarify_wait_for_response(
    clarify_id: str, timeout: float = 300.0,
) -> Optional[str]:
    """阻塞等待用户回复。"""
    with _clarify_lock:
        entry = _clarify_entries.get(clarify_id)
    if not entry:
        return None
    if entry.event.wait(timeout=timeout):
        return entry.response
    return None


def resolve_gateway_clarify(clarify_id: str, response: str) -> bool:
    """解析一个澄清请求（由网关适配器调用）。"""
    with _clarify_lock:
        entry = _clarify_entries.get(clarify_id)
    if not entry:
        return False
    entry.response = response
    entry.event.set()
    return True


def clarify_get_pending(session_key: str) -> Optional[_ClarifyEntry]:
    """获取会话的最新待处理澄清。"""
    with _clarify_lock:
        ids = _clarify_session_index.get(session_key, [])
        for cid in reversed(ids):
            entry = _clarify_entries.get(cid)
            if entry and not entry.event.is_set():
                return entry
    return None


def clarify_clear_session(session_key: str) -> None:
    """清除会话的所有待澄清。"""
    with _clarify_lock:
        ids = _clarify_session_index.pop(session_key, [])
        for cid in ids:
            entry = _clarify_entries.pop(cid, None)
            if entry:
                entry.response = None
                entry.event.set()


# ============================================================================
# slash_confirm — 斜杠命令确认
# ============================================================================

_confirm_pending: Dict[str, Dict[str, Any]] = {}
_confirm_lock = threading.RLock()
CONFIRM_DEFAULT_TIMEOUT = get_config_value("timeouts.gateway_confirm", 300)


def confirm_register(
    session_key: str,
    confirm_id: str,
    command: str,
    handler: Callable[[str], Awaitable[Optional[str]]],
) -> None:
    """注册一个待确认的斜杠命令。"""
    with _confirm_lock:
        _confirm_pending[session_key] = {
            "confirm_id": confirm_id,
            "command": command,
            "handler": handler,
            "created_at": time.time(),
        }


def confirm_get_pending(session_key: str) -> Optional[Dict[str, Any]]:
    """获取会话的待确认。"""
    with _confirm_lock:
        entry = _confirm_pending.get(session_key)
        return dict(entry) if entry else None


def confirm_clear(session_key: str) -> None:
    """清除会话的待确认。"""
    with _confirm_lock:
        _confirm_pending.pop(session_key, None)


def confirm_clear_if_stale(
    session_key: str, timeout: float = CONFIRM_DEFAULT_TIMEOUT,
) -> bool:
    """如果超时就清除。"""
    with _confirm_lock:
        entry = _confirm_pending.get(session_key)
        if not entry:
            return False
        if time.time() - float(entry.get("created_at", 0) or 0) > timeout:
            _confirm_pending.pop(session_key, None)
            return True
        return False


async def confirm_resolve(
    session_key: str, confirm_id: str, choice: str,
) -> Optional[str]:
    """解析确认（由适配器回调）。"""
    with _confirm_lock:
        entry = _confirm_pending.get(session_key)
        if not entry or entry["confirm_id"] != confirm_id:
            return None
        handler = entry["handler"]
        _confirm_pending.pop(session_key, None)
    try:
        return await handler(choice)
    except Exception as exc:
        logger.error("Confirm handler failed: %s", exc)
        return str(exc)


# ============================================================================
# hook_output_spill — 钩子输出溢出到磁盘
# ============================================================================

SPILL_DEFAULT_MAX_CHARS = get_config_value("hook_outputs.spill_max_chars", 10_000)
SPILL_DEFAULT_PREVIEW_HEAD = get_config_value("hook_outputs.spill_preview_head", 500)
SPILL_DEFAULT_PREVIEW_TAIL = get_config_value("hook_outputs.spill_preview_tail", 500)


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return default
    return iv if iv > 0 else default


def get_spill_config() -> Dict[str, Any]:
    """返回钩子输出溢出配置。"""
    section: Dict[str, Any] = {}
    try:
        import yaml
        config_path = SPIRIT_HOME / "config.yaml"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            hooks = cfg.get("hooks") if isinstance(cfg, dict) else None
            if isinstance(hooks, dict):
                sub = hooks.get("output_spill")
                if isinstance(sub, dict):
                    section = sub
    except Exception:
        section = {}

    enabled_raw = section.get("enabled", True)
    enabled = bool(enabled_raw) if enabled_raw is not None else True

    directory = section.get("directory")
    if directory is not None and not isinstance(directory, str):
        directory = None

    return {
        "enabled": enabled,
        "max_chars": _coerce_positive_int(
            section.get("max_chars", SPILL_DEFAULT_MAX_CHARS),
            SPILL_DEFAULT_MAX_CHARS,
        ),
        "preview_head": _coerce_positive_int(
            section.get("preview_head", SPILL_DEFAULT_PREVIEW_HEAD),
            SPILL_DEFAULT_PREVIEW_HEAD,
        ),
        "preview_tail": _coerce_non_negative_int(
            section.get("preview_tail", SPILL_DEFAULT_PREVIEW_TAIL),
            SPILL_DEFAULT_PREVIEW_TAIL,
        ),
        "directory": directory or str(SPIRIT_HOME / "hook_outputs"),
    }


def _coerce_non_negative_int(value: Any, default: int) -> int:
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return default
    return iv if iv >= 0 else default


def spill_hook_output(
    content: str,
    *,
    session_key: str = "default",
    label: str = "hook",
) -> str:
    """如果内容超过预算，溢出到磁盘并返回预览占位符。

    Returns:
        原始内容（如果未溢出）或带路径的预览文本。
    """
    cfg = get_spill_config()
    if not cfg["enabled"]:
        return content
    max_chars = cfg["max_chars"]
    if len(content) <= max_chars:
        return content

    spill_dir = Path(cfg["directory"])
    try:
        spill_dir.mkdir(parents=True, exist_ok=True)
        spill_path = spill_dir / f"{session_key}_{label}_{uuid.uuid4().hex[:8]}.txt"
        spill_path.write_text(content, encoding="utf-8")
    except Exception as exc:
        logger.warning("Hook output spill failed: %s", exc)
        # 降级：截断
        return content[:max_chars] + f"\n\n[... truncated {len(content) - max_chars} chars, disk write failed]"

    head = content[:cfg["preview_head"]]
    tail = content[-cfg["preview_tail"]:] if cfg["preview_tail"] > 0 else ""
    preview = (
        f"{head}\n"
        f"\n[... {len(content) - cfg['preview_head'] - cfg['preview_tail']} chars "
        f"spilled to {spill_path} ...]\n"
        f"{tail}"
    )
    return preview


# ============================================================================
# managed_tool_gateway — 托管工具网关客户端
# ============================================================================


class ManagedToolGatewayClient:
    """Nous 托管工具网关的 HTTP 客户端。

    用于在托管模式下调用远程工具（如代码执行、文件操作等）。
    """

    def __init__(self, gateway_url: str, api_key: str = ""):
        self.gateway_url = gateway_url.rstrip("/")
        self.api_key = api_key or os.environ.get("SPIRIT_GATEWAY_KEY", "")

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def call_tool(self, tool_name: str, arguments: Dict[str, Any], timeout: float = 120.0) -> Dict[str, Any]:
        """调用远程工具。"""
        try:
            import requests
            resp = requests.post(
                f"{self.gateway_url}/tools/{tool_name}",
                json=arguments,
                headers=self._headers(),
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            return {"error": f"Gateway call failed: {exc}"}

    def health_check(self) -> bool:
        """检查网关健康状态。"""
        try:
            import requests
            resp = requests.get(f"{self.gateway_url}/health", timeout=get_config_value("timeouts.health_check", 5))
            return resp.status_code == 200
        except Exception:
            return False


def get_managed_gateway_client() -> Optional[ManagedToolGatewayClient]:
    """如果配置了托管网关，返回客户端实例。"""
    url = os.environ.get("SPIRIT_GATEWAY_URL", "").strip()
    if not url:
        return None
    return ManagedToolGatewayClient(url)


# ============================================================================
# microsoft_graph_auth — MS Graph 认证
# ============================================================================


@dataclass
class GraphCredentials:
    """MS Graph 认证凭据。"""
    client_id: str
    tenant_id: str = "common"
    client_secret: str = ""
    device_code: str = ""

    @classmethod
    def from_env(cls) -> "GraphCredentials":
        return cls(
            client_id=os.environ.get("MS_GRAPH_CLIENT_ID", ""),
            tenant_id=os.environ.get("MS_GRAPH_TENANT_ID", "common"),
            client_secret=os.environ.get("MS_GRAPH_CLIENT_SECRET", ""),
        )


class MicrosoftGraphTokenProvider:
    """MS Graph 令牌提供者（设备码流 / 客户端凭据流）。"""

    def __init__(self, credentials: GraphCredentials):
        self.credentials = credentials
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._lock = threading.Lock()

    async def get_token(self) -> str:
        """获取有效的访问令牌。"""
        with self._lock:
            if self._token and time.time() < self._token_expiry - 60:
                return self._token

        if self.credentials.client_secret:
            return await self._client_credentials_flow()
        return await self._device_code_flow()

    async def _client_credentials_flow(self) -> str:
        """客户端凭据流。"""
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"https://login.microsoftonline.com/{self.credentials.tenant_id}/oauth2/v2.0/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.credentials.client_id,
                        "client_secret": self.credentials.client_secret,
                        "scope": "https://graph.microsoft.com/.default",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                self._token = data["access_token"]
                self._token_expiry = time.time() + data.get("expires_in", 3600)
                return self._token
        except Exception as exc:
            raise RuntimeError(f"MS Graph client credentials flow failed: {exc}")

    async def _device_code_flow(self) -> str:
        """设备码流（交互式）。"""
        # 简化实现 — 实际需要用户交互
        raise NotImplementedError(
            "Device code flow requires interactive user authentication. "
            "Use client credentials flow for headless environments."
        )


# ============================================================================
# microsoft_graph_client — MS Graph 异步客户端
# ============================================================================

DEFAULT_GRAPH_BASE_URL = get_config_value("integrations.graph_base_url", "https://graph.microsoft.com/v1.0")


class MicrosoftGraphClientError(RuntimeError):
    """Graph 客户端基础异常。"""
    pass


class MicrosoftGraphAPIError(MicrosoftGraphClientError):
    """Graph API 请求失败。"""

    def __init__(
        self, status_code: int, method: str, url: str, message: str,
        *, retry_after_seconds: Optional[float] = None, payload: Any = None,
    ):
        self.status_code = status_code
        self.method = method
        self.url = url
        self.retry_after_seconds = retry_after_seconds
        self.payload = payload
        super().__init__(
            f"Microsoft Graph API error {status_code} for {method} {url}: {message}"
        )


class MicrosoftGraphClient:
    """最小化异步 MS Graph 客户端（支持重试和分页）。"""

    def __init__(
        self,
        token_provider: MicrosoftGraphTokenProvider,
        *,
        base_url: str = DEFAULT_GRAPH_BASE_URL,
        timeout: float = None,
        max_retries: int = None,
        user_agent: str = "Spirit-Agent/graph-client",
    ):
        self.token_provider = token_provider
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout if timeout is not None else get_config_value("timeouts.graph_client", 60.0)
        self.max_retries = max(0, int(max_retries if max_retries is not None else get_config_value("timeouts.graph_max_retries", 3)))
        self.user_agent = user_agent

    @classmethod
    def from_env(cls, **kwargs) -> "MicrosoftGraphClient":
        credentials = GraphCredentials.from_env()
        provider = MicrosoftGraphTokenProvider(credentials)
        return cls(provider, **kwargs)

    async def get_json(self, path: str, *, params: Optional[dict] = None,
                       headers: Optional[dict] = None) -> Any:
        response = await self._request("GET", path, params=params, headers=headers)
        return self._decode_json(response)

    async def post_json(self, path: str, *, json_body: Any = None,
                        headers: Optional[dict] = None) -> Any:
        response = await self._request("POST", path, json_body=json_body, headers=headers)
        return self._decode_json(response)

    async def patch_json(self, path: str, *, json_body: Any = None,
                         headers: Optional[dict] = None) -> Any:
        response = await self._request("PATCH", path, json_body=json_body, headers=headers)
        return self._decode_json(response)

    async def delete(self, path: str, *, headers: Optional[dict] = None) -> None:
        await self._request("DELETE", path, headers=headers)

    async def _request(
        self, method: str, path: str, *,
        params: Optional[dict] = None, json_body: Any = None,
        headers: Optional[dict] = None,
    ):
        import httpx
        token = await self.token_provider.get_token()
        url = f"{self.base_url}/{path.lstrip('/')}"
        req_headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": self.user_agent,
            **(headers or {}),
        }
        last_exc = None
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.request(
                        method, url, params=params, json=json_body, headers=req_headers,
                    )
                    if resp.status_code < 400:
                        return resp
                    if resp.status_code == 429 and attempt < self.max_retries:
                        retry_after = float(resp.headers.get("Retry-After", "5"))
                        await asyncio.sleep(min(retry_after, 60))
                        continue
                    if resp.status_code >= 500 and attempt < self.max_retries:
                        await asyncio.sleep(min(2 ** attempt, 30))
                        continue
                    raise MicrosoftGraphAPIError(
                        resp.status_code, method, url,
                        resp.text[:500],
                    )
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(min(2 ** attempt, 30))
                    continue
                raise MicrosoftGraphClientError(f"Request failed after {self.max_retries} retries: {exc}")
        raise MicrosoftGraphClientError(f"Request failed: {last_exc}")

    def _decode_json(self, response) -> Any:
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except Exception as exc:
            raise MicrosoftGraphClientError(f"Failed to decode JSON response: {exc}")

    async def paginate(self, path: str, *, params: Optional[dict] = None) -> List[Any]:
        """分页获取所有结果。"""
        items = []
        url = path
        while url:
            data = await self.get_json(url, params=params)
            items.extend(data.get("value", []))
            url = data.get("@odata.nextLink", "")
            params = None  # nextLink 已包含参数
        return items


# ============================================================================
# openrouter_client — OpenRouter 懒加载客户端
# ============================================================================


def get_openrouter_client():
    """懒加载 OpenAI 兼容的 OpenRouter 客户端。"""
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from openai import OpenAI
        return OpenAI(
            base_url=get_config_value("llm.base_url", "https://openrouter.ai/api/v1") or "https://openrouter.ai/api/v1",
            api_key=api_key,
        )
    except ImportError:
        logger.debug("openai package not installed; OpenRouter unavailable")
        return None


# ============================================================================
# neutts_synth — NeuTTS 语音合成辅助
# ============================================================================


def synth_neutts_audio(text: str, *, voice: str = "default", output_path: Optional[str] = None) -> Optional[Path]:
    """使用 NeuTTS 合成语音。

    Returns:
        音频文件路径，或 None（如果 NeuTTS 不可用）。
    """
    neutts_bin = shutil.which("neutts") or os.environ.get("NEUTTS_BIN", "")
    if not neutts_bin:
        return None
    out = output_path or str(SPIRIT_HOME / "tts_output.wav")
    try:
        subprocess.run(
            [neutts_bin, "--voice", voice, "--output", out, text],
            capture_output=True, timeout=get_config_value("timeouts.tts_subprocess", 30), check=True,
        )
        return Path(out)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("NeuTTS synthesis failed: %s", exc)
        return None


# ============================================================================
# tirith_security — tirith 二进制安全扫描器
# ============================================================================

_tirith_resolved_path: Optional[str] = None
_tirith_install_failed = False
_tirith_install_failure_reason = ""
_tirith_crash_count = 0
_tirith_circuit_open = False
_TIRITH_CRASH_LIMIT = 3
_tirith_warned_messages: set = set()
_tirith_warned_lock = threading.Lock()
_tirith_install_lock = threading.Lock()
_tirith_install_thread: Optional[threading.Thread] = None
_TIRITH_MARKER_TTL = get_config_value("internal.tirith_marker_ttl", 86400)  # 24 小时


def _tirith_warn_once(key: str, message: str, *args) -> None:
    with _tirith_warned_lock:
        if key in _tirith_warned_messages:
            return
        _tirith_warned_messages.add(key)
    logger.warning(message, *args)


def _tirith_load_security_config() -> Dict[str, Any]:
    """加载安全扫描配置。"""
    defaults = {
        "tirith_enabled": True,
        "tirith_path": "tirith",
        "tirith_timeout": 5,
        "tirith_fail_open": True,
    }
    try:
        import yaml
        config_path = SPIRIT_HOME / "config.yaml"
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            sec = cfg.get("security", {}) or {}
        else:
            sec = {}
    except Exception:
        sec = {}

    return {
        "tirith_enabled": sec.get("tirith_enabled", defaults["tirith_enabled"]),
        "tirith_path": os.getenv("TIRITH_BIN", sec.get("tirith_path", defaults["tirith_path"])),
        "tirith_timeout": int(sec.get("tirith_timeout", defaults["tirith_timeout"])),
        "tirith_fail_open": sec.get("tirith_fail_open", defaults["tirith_fail_open"]),
    }


def _tirith_resolve_path() -> Optional[str]:
    """解析 tirith 二进制路径。"""
    global _tirith_resolved_path
    if _tirith_resolved_path is not None:
        return _tirith_resolved_path if isinstance(_tirith_resolved_path, str) else None
    cfg = _tirith_load_security_config()
    path = cfg["tirith_path"]
    resolved = shutil.which(path)
    if resolved:
        _tirith_resolved_path = resolved
        return resolved
    _tirith_resolved_path = False
    return None


def _tirith_record_crash() -> None:
    """记录崩溃并触发熔断器。"""
    global _tirith_crash_count, _tirith_circuit_open
    _tirith_crash_count += 1
    if _tirith_crash_count >= _TIRITH_CRASH_LIMIT:
        _tirith_circuit_open = True
        logger.warning(
            "tirith circuit breaker opened after %d consecutive failures",
            _tirith_crash_count,
        )


def check_command_security(command: str) -> Dict[str, Any]:
    """使用 tirith 检查命令安全性。

    Returns:
        {
            "safe": bool,
            "scanner": "tirith" | "none",
            "details": str,
        }
    """
    global _tirith_crash_count, _tirith_circuit_open
    cfg = _tirith_load_security_config()
    if not cfg["tirith_enabled"]:
        return {"safe": True, "scanner": "none", "details": "tirith disabled"}

    if _tirith_circuit_open:
        if cfg["tirith_fail_open"]:
            return {"safe": True, "scanner": "tirith", "details": "circuit breaker open, fail-open"}
        return {"safe": False, "scanner": "tirith", "details": "circuit breaker open, fail-closed"}

    tirith_path = _tirith_resolve_path()
    if not tirith_path:
        if cfg["tirith_fail_open"]:
            _tirith_warn_once("tirith_missing", "tirith binary not found, fail-open")
            return {"safe": True, "scanner": "none", "details": "tirith not installed, fail-open"}
        return {"safe": False, "scanner": "tirith", "details": "tirith not installed, fail-closed"}

    try:
        result = subprocess.run(
            [tirith_path, "check", command],
            capture_output=True, text=True,
            timeout=cfg["tirith_timeout"],
        )
        # 重置崩溃计数
        _tirith_crash_count = 0
        _tirith_circuit_open = False

        if result.returncode == 0:
            return {"safe": True, "scanner": "tirith", "details": result.stdout.strip() or "OK"}
        else:
            return {
                "safe": False, "scanner": "tirith",
                "details": result.stderr.strip() or result.stdout.strip() or "blocked by tirith",
            }
    except subprocess.TimeoutExpired:
        _tirith_record_crash()
        if cfg["tirith_fail_open"]:
            return {"safe": True, "scanner": "tirith", "details": "timeout, fail-open"}
        return {"safe": False, "scanner": "tirith", "details": "timeout, fail-closed"}
    except Exception as exc:
        _tirith_record_crash()
        _tirith_warn_once("tirith_spawn", "tirith spawn failed: %s", exc)
        if cfg["tirith_fail_open"]:
            return {"safe": True, "scanner": "tirith", "details": f"error, fail-open: {exc}"}
        return {"safe": False, "scanner": "tirith", "details": f"error, fail-closed: {exc}"}
