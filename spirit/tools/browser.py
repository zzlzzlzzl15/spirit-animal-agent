"""浏览器自动化工具 — Spirit Agent。

参考 Hermes browser_tool.py 设计：
- 通过 agent-browser CLI 进行浏览器自动化
- 支持导航、截图、点击、输入、滚动
- 使用无障碍树（accessibility tree）获取页面快照
- 会话隔离
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

from spirit.tools.registry import registry

logger = logging.getLogger(__name__)

# agent-browser 可执行文件路径
_browser_bin: Optional[str] = None
_browser_lock = threading.Lock()

# 活跃会话
_sessions: Dict[str, Dict[str, Any]] = {}


def _find_browser_binary() -> Optional[str]:
    """查找 agent-browser 可执行文件。"""
    global _browser_bin
    if _browser_bin is not None:
        return _browser_bin
    with _browser_lock:
        _browser_bin = shutil.which("agent-browser")
        return _browser_bin


def _run_browser_cmd(args: list, timeout: float = 60,
                     env: Optional[Dict] = None) -> Dict[str, Any]:
    """运行 agent-browser 命令。"""
    browser_bin = _find_browser_binary()
    if not browser_bin:
        return {
            "success": False,
            "error": "agent-browser 未安装。运行 `npm install -g agent-browser` 安装。",
        }

    cmd = [browser_bin] + args
    merged_env = {**os.environ, **(env or {})}

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=merged_env,
            stdin=subprocess.DEVNULL,
        )

        if result.returncode != 0:
            return {
                "success": False,
                "error": f"agent-browser 错误: {result.stderr[:500]}",
            }

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"success": True, "output": result.stdout.strip()}

    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"命令超时 ({timeout}s)"}
    except FileNotFoundError:
        return {"success": False, "error": "agent-browser 未找到"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ============================================================================
# Schema 定义
# ============================================================================

BROWSER_NAVIGATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "browser_navigate",
        "description": "在浏览器中导航到 URL。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要导航到的 URL"},
                "session_id": {
                    "type": "string",
                    "description": "浏览器会话 ID（可选，自动创建）",
                },
            },
            "required": ["url"],
        },
    },
}


def _handle_browser_navigate(args: Dict[str, Any], **kwargs) -> str:
    url = args.get("url", "")
    session_id = args.get("session_id") or f"session_{int(time.time())}"
    if not url:
        return json.dumps({"error": "url 不能为空"})

    result = _run_browser_cmd(["navigate", url, "--session", session_id])
    if result.get("success"):
        _sessions[session_id] = {"url": url, "created_at": time.time()}
    return json.dumps(result, ensure_ascii=False)


BROWSER_SNAPSHOT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "browser_snapshot",
        "description": "获取当前页面的无障碍树快照（文本表示）。",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "浏览器会话 ID"},
            },
            "required": ["session_id"],
        },
    },
}


def _handle_browser_snapshot(args: Dict[str, Any], **kwargs) -> str:
    session_id = args.get("session_id", "")
    if not session_id:
        return json.dumps({"error": "session_id 不能为空"})
    result = _run_browser_cmd(["snapshot", "--session", session_id])
    return json.dumps(result, ensure_ascii=False)


BROWSER_CLICK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "browser_click",
        "description": "点击页面元素（通过 ref 选择器，如 @e5）。",
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "元素引用（如 @e5）"},
                "session_id": {"type": "string", "description": "浏览器会话 ID"},
            },
            "required": ["ref", "session_id"],
        },
    },
}


def _handle_browser_click(args: Dict[str, Any], **kwargs) -> str:
    ref = args.get("ref", "")
    session_id = args.get("session_id", "")
    if not ref or not session_id:
        return json.dumps({"error": "ref 和 session_id 不能为空"})
    result = _run_browser_cmd(["click", ref, "--session", session_id])
    return json.dumps(result, ensure_ascii=False)


BROWSER_TYPE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "browser_type",
        "description": "在输入框中输入文本。",
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "输入框引用"},
                "text": {"type": "string", "description": "要输入的文本"},
                "session_id": {"type": "string", "description": "浏览器会话 ID"},
            },
            "required": ["ref", "text", "session_id"],
        },
    },
}


def _handle_browser_type(args: Dict[str, Any], **kwargs) -> str:
    ref = args.get("ref", "")
    text = args.get("text", "")
    session_id = args.get("session_id", "")
    if not ref or not session_id:
        return json.dumps({"error": "ref 和 session_id 不能为空"})
    result = _run_browser_cmd(
        ["type", ref, "--text", text, "--session", session_id]
    )
    return json.dumps(result, ensure_ascii=False)


BROWSER_SCROLL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "browser_scroll",
        "description": "滚动页面。",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["up", "down"],
                    "description": "滚动方向",
                },
                "amount": {
                    "type": "integer",
                    "description": "滚动像素量（默认 500）",
                },
                "session_id": {"type": "string", "description": "浏览器会话 ID"},
            },
            "required": ["direction", "session_id"],
        },
    },
}


def _handle_browser_scroll(args: Dict[str, Any], **kwargs) -> str:
    direction = args.get("direction", "down")
    amount = args.get("amount", 500)
    session_id = args.get("session_id", "")
    if not session_id:
        return json.dumps({"error": "session_id 不能为空"})
    result = _run_browser_cmd(
        ["scroll", direction, "--amount", str(amount), "--session", session_id]
    )
    return json.dumps(result, ensure_ascii=False)


BROWSER_CLOSE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "browser_close",
        "description": "关闭浏览器会话。",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "浏览器会话 ID"},
            },
            "required": ["session_id"],
        },
    },
}


def _handle_browser_close(args: Dict[str, Any], **kwargs) -> str:
    session_id = args.get("session_id", "")
    if not session_id:
        return json.dumps({"error": "session_id 不能为空"})
    result = _run_browser_cmd(["close", "--session", session_id])
    _sessions.pop(session_id, None)
    return json.dumps(result, ensure_ascii=False)


# ============================================================================
# 注册所有浏览器工具
# ============================================================================

def check_browser_requirements() -> bool:
    return _find_browser_binary() is not None


registry.register(
    name="browser_navigate",
    toolset="browser",
    schema=BROWSER_NAVIGATE_SCHEMA,
    handler=_handle_browser_navigate,
    check_fn=check_browser_requirements,
    emoji="🌐",
)

registry.register(
    name="browser_snapshot",
    toolset="browser",
    schema=BROWSER_SNAPSHOT_SCHEMA,
    handler=_handle_browser_snapshot,
    check_fn=check_browser_requirements,
    emoji="📸",
)

registry.register(
    name="browser_click",
    toolset="browser",
    schema=BROWSER_CLICK_SCHEMA,
    handler=_handle_browser_click,
    check_fn=check_browser_requirements,
    emoji="👆",
)

registry.register(
    name="browser_type",
    toolset="browser",
    schema=BROWSER_TYPE_SCHEMA,
    handler=_handle_browser_type,
    check_fn=check_browser_requirements,
    emoji="⌨️",
)

registry.register(
    name="browser_scroll",
    toolset="browser",
    schema=BROWSER_SCROLL_SCHEMA,
    handler=_handle_browser_scroll,
    check_fn=check_browser_requirements,
    emoji="📜",
)

registry.register(
    name="browser_close",
    toolset="browser",
    schema=BROWSER_CLOSE_SCHEMA,
    handler=_handle_browser_close,
    check_fn=check_browser_requirements,
    emoji="❌",
)


# ============================================================================
# CDPSupervisor — CDP 持久监控（移植自 browser_supervisor.py）
# ============================================================================

import asyncio
from dataclasses import dataclass

# 对话框策略
from spirit.config import get_config_value

DIALOG_POLICY_MUST_RESPOND = "must_respond"
DIALOG_POLICY_AUTO_DISMISS = "auto_dismiss"
DIALOG_POLICY_AUTO_ACCEPT = "auto_accept"

_VALID_POLICIES = frozenset({
    DIALOG_POLICY_MUST_RESPOND, DIALOG_POLICY_AUTO_DISMISS, DIALOG_POLICY_AUTO_ACCEPT,
})
DEFAULT_DIALOG_POLICY = DIALOG_POLICY_MUST_RESPOND
DEFAULT_DIALOG_TIMEOUT_S = get_config_value("timeouts.browser_dialog", 300.0)
FRAME_TREE_MAX_ENTRIES = get_config_value("browser.frame_tree_max_entries", 30)
FRAME_TREE_MAX_OOPIF_DEPTH = get_config_value("browser.frame_tree_max_oopif_depth", 2)
CONSOLE_HISTORY_MAX = get_config_value("browser.console_history_max", 50)
RECENT_DIALOGS_MAX = get_config_value("browser.recent_dialogs_max", 20)
DIALOG_BRIDGE_HOST = "spirit-dialog-bridge.invalid"
DIALOG_BRIDGE_URL_PATTERN = f"http://{DIALOG_BRIDGE_HOST}/*"

# 注入到每个 frame 的对话框桥接脚本
_DIALOG_BRIDGE_SCRIPT = r"""
(() => {
  if (window.__spiritDialogBridgeInstalled) return;
  window.__spiritDialogBridgeInstalled = true;
  const ENDPOINT = "http://spirit-dialog-bridge.invalid/";
  function ask(kind, message, defaultPrompt) {
    try {
      const xhr = new XMLHttpRequest();
      const params = new URLSearchParams({
        kind: String(kind || ""),
        message: String(message == null ? "" : message),
        default_prompt: String(defaultPrompt == null ? "" : defaultPrompt),
      });
      xhr.open("GET", ENDPOINT + "?" + params.toString(), false);
      xhr.send(null);
      if (xhr.status !== 200) return null;
      const body = xhr.responseText || "";
      let parsed;
      try { parsed = JSON.parse(body); } catch (e) { return null; }
      if (kind === "alert") return undefined;
      if (kind === "confirm") return Boolean(parsed && parsed.accept);
      if (kind === "prompt") {
        if (!parsed || !parsed.accept) return null;
        return parsed.prompt_text == null ? "" : String(parsed.prompt_text);
      }
      return null;
    } catch (e) {
      return null;
    }
  }
  const realAlert   = window.alert;
  const realConfirm = window.confirm;
  const realPrompt  = window.prompt;
  window.alert   = function(message) { ask("alert",   message, ""); };
  window.confirm = function(message) {
    const r = ask("confirm", message, "");
    return r === null ? false : Boolean(r);
  };
  window.prompt  = function(message, def) {
    const r = ask("prompt", message, def == null ? "" : def);
    return r === null ? null : String(r);
  };
})();
"""


@dataclass
class PendingDialog:
    """当前打开的 JS 对话框。"""
    id: str
    type: str  # "alert" | "confirm" | "prompt" | "beforeunload"
    message: str
    default_prompt: str
    opened_at: float
    cdp_session_id: str
    frame_id: Optional[str] = None
    bridge_request_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "type": self.type,
            "message": self.message[:200],
            "default_prompt": self.default_prompt[:100],
            "opened_at": self.opened_at,
            "frame_id": self.frame_id,
        }


@dataclass
class DialogRecord:
    """已处理的对话框历史记录。"""
    id: str
    type: str
    message: str
    opened_at: float
    closed_at: float
    closed_by: str  # "agent" | "auto_policy" | "remote" | "watchdog"
    frame_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "type": self.type,
            "message": self.message[:200],
            "opened_at": self.opened_at, "closed_at": self.closed_at,
            "closed_by": self.closed_by, "frame_id": self.frame_id,
        }


@dataclass
class FrameInfo:
    """页面 frame 树中的一个 frame。"""
    frame_id: str
    url: str
    origin: str
    parent_frame_id: Optional[str]
    is_oopif: bool
    cdp_session_id: Optional[str] = None
    name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "frame_id": self.frame_id, "url": self.url,
            "origin": self.origin, "is_oopif": self.is_oopif,
        }
        if self.cdp_session_id:
            d["session_id"] = self.cdp_session_id
        if self.parent_frame_id:
            d["parent_frame_id"] = self.parent_frame_id
        if self.name:
            d["name"] = self.name
        return d


@dataclass
class ConsoleEvent:
    """控制台/异常事件。"""
    ts: float
    level: str  # "log" | "error" | "warning" | "exception"
    text: str
    url: Optional[str] = None


@dataclass(frozen=True)
class SupervisorSnapshot:
    """Supervisor 状态的只读快照。"""
    pending_dialogs: tuple
    recent_dialogs: tuple
    frame_tree: Dict[str, Any]
    console_errors: tuple
    active: bool
    cdp_url: str
    task_id: str

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "pending_dialogs": [d.to_dict() for d in self.pending_dialogs],
            "frame_tree": self.frame_tree,
        }
        if self.recent_dialogs:
            out["recent_dialogs"] = [d.to_dict() for d in self.recent_dialogs]
        return out


class CDPSupervisor:
    """CDP 持久监控器（每个 task_id + cdp_url 对一个实例）。

    生命周期：
      * start() — 启动守护线程，连接 WebSocket，附加到页面目标
      * snapshot() — 同步、线程安全，从工具处理器调用
      * respond_to_dialog() — 同步桥接，在 supervisor 循环上调度协程
      * stop() — 取消任务，关闭 WebSocket，加入线程
    """

    def __init__(
        self,
        task_id: str,
        cdp_url: str,
        *,
        dialog_policy: str = DEFAULT_DIALOG_POLICY,
        dialog_timeout_s: float = DEFAULT_DIALOG_TIMEOUT_S,
    ):
        if dialog_policy not in _VALID_POLICIES:
            raise ValueError(f"Invalid dialog_policy {dialog_policy!r}")
        self.task_id = task_id
        self.cdp_url = cdp_url
        self.dialog_policy = dialog_policy
        self.dialog_timeout_s = float(dialog_timeout_s)

        self._state_lock = threading.Lock()
        self._pending_dialogs: Dict[str, PendingDialog] = {}
        self._recent_dialogs: List[DialogRecord] = []
        self._frames: Dict[str, FrameInfo] = {}
        self._console_events: List[ConsoleEvent] = []
        self._active = False

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready_event = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._stop_requested = False

        self._next_call_id = 1
        self._pending_calls: Dict[int, asyncio.Future] = {}
        self._ws = None
        self._page_session_id: Optional[str] = None
        self._child_sessions: Dict[str, Dict[str, Any]] = {}
        self._dialog_watchdogs: Dict[str, asyncio.TimerHandle] = {}
        self._dialog_seq = 0

    def start(self, timeout: float = 15.0) -> None:
        """启动后台循环并等待连接完成。"""
        if self._thread and self._thread.is_alive():
            return
        self._ready_event.clear()
        self._start_error = None
        self._stop_requested = False
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"cdp-supervisor-{self.task_id}",
            daemon=True,
        )
        self._thread.start()
        if not self._ready_event.wait(timeout=timeout):
            self.stop()
            raise TimeoutError(
                f"CDP supervisor did not attach within {timeout}s"
            )
        if self._start_error is not None:
            err = self._start_error
            self.stop()
            raise RuntimeError(f"CDP supervisor failed to start: {err}") from None

    def stop(self, timeout: float = 5.0) -> None:
        """取消监控任务并加入线程。"""
        self._stop_requested = True
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._request_stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        with self._state_lock:
            self._active = False

    def snapshot(self) -> SupervisorSnapshot:
        """获取当前状态快照（同步、线程安全）。"""
        with self._state_lock:
            return SupervisorSnapshot(
                pending_dialogs=tuple(self._pending_dialogs.values()),
                recent_dialogs=tuple(self._recent_dialogs[-RECENT_DIALOGS_MAX:]),
                frame_tree=self._build_frame_tree(),
                console_errors=tuple(self._console_events[-CONSOLE_HISTORY_MAX:]),
                active=self._active,
                cdp_url=self.cdp_url,
                task_id=self.task_id,
            )

    def respond_to_dialog(
        self, dialog_id: str, action: str, *, text: str = "",
    ) -> bool:
        """响应一个待处理的对话框（同步桥接到 CDP 循环）。"""
        loop = self._loop
        if loop is None or not loop.is_running():
            return False
        future = asyncio.run_coroutine_threadsafe(
            self._handle_dialog_response(dialog_id, action, text=text),
            loop,
        )
        try:
            return future.result(timeout=10.0)
        except Exception as exc:
            logger.warning("Dialog response failed: %s", exc)
            return False

    # ── 内部实现 ──────────────────────────────────────────────────────────

    def _thread_main(self) -> None:
        """Supervisor 后台线程主入口。"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        except Exception as exc:
            if not self._stop_requested:
                self._start_error = exc
        finally:
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            self._loop.close()
            self._loop = None

    async def _run(self) -> None:
        """主异步循环 — 连接 WebSocket 并处理 CDP 事件。"""
        try:
            import websockets
            async with websockets.connect(
                self.cdp_url, max_size=get_config_value("browser.websocket_max_size", 10 * 1024 * 1024),
            ) as ws:
                self._ws = ws
                # 附加到第一个页面目标
                targets = await self._send_cdp("Target.getTargets")
                page_targets = [
                    t for t in (targets.get("targetInfos") or [])
                    if t.get("type") == "page"
                ]
                if not page_targets:
                    raise RuntimeError("No page targets found")
                target_id = page_targets[0]["targetId"]
                attach_result = await self._send_cdp(
                    "Target.attachToTarget",
                    {"targetId": target_id, "flatten": True},
                )
                self._page_session_id = attach_result.get("sessionId")
                # 启用必要域
                await self._send_cdp_session(self._page_session_id, "Page.enable")
                await self._send_cdp_session(self._page_session_id, "Runtime.enable")
                await self._send_cdp_session(self._page_session_id, "Log.enable")
                # 注入对话框桥接脚本
                await self._send_cdp_session(
                    self._page_session_id,
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": _DIALOG_BRIDGE_SCRIPT},
                )
                # 自动附加到子目标
                await self._send_cdp(
                    "Target.setAutoAttach",
                    {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
                )
                with self._state_lock:
                    self._active = True
                self._ready_event.set()
                # 事件循环
                async for raw in ws:
                    if self._stop_requested:
                        break
                    try:
                        event = json.loads(raw)
                        await self._handle_cdp_event(event)
                    except Exception as exc:
                        logger.debug("CDP event handling error: %s", exc)
        except Exception as exc:
            if not self._stop_requested:
                self._start_error = exc
                self._ready_event.set()  # 解除 start() 的等待

    async def _handle_cdp_event(self, event: Dict[str, Any]) -> None:
        """处理一个 CDP 事件。"""
        method = event.get("method", "")
        params = event.get("params", {})
        session_id = event.get("sessionId", "")

        if method == "Page.javascriptDialogOpening":
            await self._on_dialog_opening(params, session_id)
        elif method == "Page.javascriptDialogClosed":
            await self._on_dialog_closed(params, session_id)
        elif method == "Page.frameNavigated":
            frame = params.get("frame", {})
            self._update_frame(frame, session_id)
        elif method in ("Runtime.consoleAPICalled", "Log.entryAdded"):
            self._record_console_event(method, params)

    async def _on_dialog_opening(self, params: Dict, session_id: str) -> None:
        """对话框打开事件。"""
        self._dialog_seq += 1
        dialog_id = f"dlg_{self._dialog_seq}"
        dialog = PendingDialog(
            id=dialog_id,
            type=params.get("type", "alert"),
            message=params.get("message", ""),
            default_prompt=params.get("defaultPrompt", ""),
            opened_at=time.time(),
            cdp_session_id=session_id,
            frame_id=params.get("frameId"),
        )
        with self._state_lock:
            self._pending_dialogs[dialog_id] = dialog
        # 自动策略处理
        if self.dialog_policy == DIALOG_POLICY_AUTO_DISMISS:
            await self._handle_dialog_response(dialog_id, "dismiss")
        elif self.dialog_policy == DIALOG_POLICY_AUTO_ACCEPT:
            await self._handle_dialog_response(dialog_id, "accept")

    async def _on_dialog_closed(self, params: Dict, session_id: str) -> None:
        """对话框关闭事件。"""
        with self._state_lock:
            for did, dialog in list(self._pending_dialogs.items()):
                if dialog.cdp_session_id == session_id:
                    record = DialogRecord(
                        id=did, type=dialog.type, message=dialog.message,
                        opened_at=dialog.opened_at, closed_at=time.time(),
                        closed_by="remote", frame_id=dialog.frame_id,
                    )
                    self._recent_dialogs.append(record)
                    del self._pending_dialogs[did]
                    break
            # 限制历史记录大小
            if len(self._recent_dialogs) > RECENT_DIALOGS_MAX * 2:
                self._recent_dialogs = self._recent_dialogs[-RECENT_DIALOGS_MAX:]

    async def _handle_dialog_response(
        self, dialog_id: str, action: str, *, text: str = "",
    ) -> bool:
        """实际发送对话框响应到 CDP。"""
        with self._state_lock:
            dialog = self._pending_dialogs.get(dialog_id)
        if not dialog:
            return False
        accept = action == "accept"
        try:
            await self._send_cdp(
                "Page.handleJavaScriptDialog",
                {"accept": accept, "promptText": text},
            )
            with self._state_lock:
                record = DialogRecord(
                    id=dialog_id, type=dialog.type, message=dialog.message,
                    opened_at=dialog.opened_at, closed_at=time.time(),
                    closed_by="agent", frame_id=dialog.frame_id,
                )
                self._recent_dialogs.append(record)
                self._pending_dialogs.pop(dialog_id, None)
            return True
        except Exception as exc:
            logger.warning("Dialog response CDP call failed: %s", exc)
            return False

    def _update_frame(self, frame: Dict, session_id: str) -> None:
        """更新 frame 树。"""
        frame_id = frame.get("id", "")
        if not frame_id:
            return
        from urllib.parse import urlparse as _urlparse
        parsed = _urlparse(frame.get("url", ""))
        info = FrameInfo(
            frame_id=frame_id,
            url=frame.get("url", ""),
            origin=f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else "",
            parent_frame_id=frame.get("parentId"),
            is_oopif=frame.get("type") == "iframe" and bool(session_id),
            cdp_session_id=session_id if frame.get("type") == "iframe" else None,
            name=frame.get("name", ""),
        )
        with self._state_lock:
            self._frames[frame_id] = info
            if len(self._frames) > FRAME_TREE_MAX_ENTRIES:
                # 保留根 frame 和最近的
                root_ids = {fid for fid, f in self._frames.items() if not f.parent_frame_id}
                self._frames = {
                    fid: f for fid, f in self._frames.items()
                    if fid in root_ids or f.is_oopif
                }

    def _record_console_event(self, method: str, params: Dict) -> None:
        """记录控制台事件。"""
        if method == "Runtime.consoleAPICalled":
            args = params.get("args", [])
            text = " ".join(a.get("value", str(a.get("description", ""))) for a in args)
            level = params.get("type", "log")
        else:  # Log.entryAdded
            entry = params.get("entry", {})
            text = entry.get("text", "")
            level = entry.get("level", "log")
        event = ConsoleEvent(ts=time.time(), level=level, text=text[:500])
        with self._state_lock:
            self._console_events.append(event)
            if len(self._console_events) > CONSOLE_HISTORY_MAX * 2:
                self._console_events = self._console_events[-CONSOLE_HISTORY_MAX:]

    def _build_frame_tree(self) -> Dict[str, Any]:
        """构建 frame 树字典。"""
        with self._state_lock:
            frames = dict(self._frames)
        roots = {fid: f for fid, f in frames.items() if not f.parent_frame_id}
        result: Dict[str, Any] = {}
        for fid, frame in roots.items():
            node = frame.to_dict()
            children = [
                f.to_dict() for f in frames.values() if f.parent_frame_id == fid
            ]
            if children:
                node["children"] = children
            result[fid] = node
        return result

    async def _send_cdp(self, method: str, params: Optional[Dict] = None) -> Dict:
        """发送 CDP 命令（在 supervisor 循环上）。"""
        call_id = self._next_call_id
        self._next_call_id += 1
        msg = {"id": call_id, "method": method}
        if params:
            msg["params"] = params
        future = self._loop.create_future()
        self._pending_calls[call_id] = future
        await self._ws.send(json.dumps(msg))
        return await asyncio.wait_for(future, timeout=30.0)

    async def _send_cdp_session(
        self, session_id: str, method: str, params: Optional[Dict] = None,
    ) -> Dict:
        """发送会话级 CDP 命令。"""
        call_id = self._next_call_id
        self._next_call_id += 1
        msg = {"id": call_id, "method": method, "sessionId": session_id}
        if params:
            msg["params"] = params
        future = self._loop.create_future()
        self._pending_calls[call_id] = future
        await self._ws.send(json.dumps(msg))
        return await asyncio.wait_for(future, timeout=30.0)

    def _request_stop(self) -> None:
        """从外部线程请求停止。"""
        self._stop_requested = True
        if self._ws:
            asyncio.ensure_future(self._ws.close())


# ============================================================================
# SupervisorRegistry — Supervisor 实例管理
# ============================================================================

_supervisors: Dict[str, CDPSupervisor] = {}
_supervisor_lock = threading.Lock()


def get_or_start_supervisor(
    task_id: str, cdp_url: str, **kwargs,
) -> CDPSupervisor:
    """获取或启动一个 CDPSupervisor。"""
    key = f"{task_id}:{cdp_url}"
    with _supervisor_lock:
        sup = _supervisors.get(key)
        if sup and sup._active:
            return sup
        sup = CDPSupervisor(task_id, cdp_url, **kwargs)
        sup.start()
        _supervisors[key] = sup
        return sup


def stop_all_supervisors() -> None:
    """停止所有 supervisor。"""
    with _supervisor_lock:
        for sup in _supervisors.values():
            try:
                sup.stop()
            except Exception:
                pass
        _supervisors.clear()


# ============================================================================
# Camofox 反检测浏览器后端（移植自 browser_camofox.py）
# ============================================================================

_camofox_vnc_url: Optional[str] = None
_camofox_vnc_checked = False
_cached_cmd_timeout: int = 30
_cmd_timeout_resolved = False


def _camofox_auth_headers() -> Dict[str, str]:
    """返回 Camofox 认证头。"""
    key = os.environ.get("CAMOFOX_API_KEY", "").strip()
    if key:
        return {"Authorization": f"Bearer {key}"}
    return {}


def get_camofox_url() -> str:
    """返回配置的 Camofox 服务器 URL。"""
    return os.environ.get("CAMOFOX_URL", "").rstrip("/")


def is_camofox_mode() -> bool:
    """检查是否处于 Camofox 模式（无 CDP 覆盖时）。"""
    if os.environ.get("BROWSER_CDP_URL", "").strip():
        return False
    return bool(get_camofox_url())


def check_camofox_available() -> bool:
    """验证 Camofox 服务器是否可达。"""
    global _camofox_vnc_url, _camofox_vnc_checked
    url = get_camofox_url()
    if not url:
        return False
    try:
        import requests
        resp = requests.get(f"{url}/health", timeout=5)
        if resp.status_code == 200 and not _camofox_vnc_checked:
            try:
                data = resp.json()
                vnc_port = data.get("vncPort")
                if isinstance(vnc_port, int) and 1 <= vnc_port <= 65535:
                    parsed = urlparse(url)
                    host = parsed.hostname or "localhost"
                    _camofox_vnc_url = f"http://{host}:{vnc_port}"
            except (ValueError, KeyError):
                pass
            _camofox_vnc_checked = True
        return True
    except Exception:
        return False


def get_camofox_vnc_url() -> Optional[str]:
    """返回 Camofox VNC 查看器 URL。"""
    if not _camofox_vnc_checked:
        check_camofox_available()
    return _camofox_vnc_url


def camofox_navigate(url: str, *, session_id: str = "default") -> Dict[str, Any]:
    """通过 Camofox REST API 导航。"""
    base = get_camofox_url()
    if not base:
        return {"success": False, "error": "Camofox not configured"}
    try:
        import requests
        resp = requests.post(
            f"{base}/navigate",
            json={"url": url, "session": session_id},
            headers=_camofox_auth_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return {"success": False, "error": f"Camofox navigate failed: {exc}"}


def camofox_snapshot(*, session_id: str = "default") -> Dict[str, Any]:
    """通过 Camofox REST API 获取无障碍树快照。"""
    base = get_camofox_url()
    if not base:
        return {"success": False, "error": "Camofox not configured"}
    try:
        import requests
        resp = requests.get(
            f"{base}/snapshot",
            params={"session": session_id},
            headers=_camofox_auth_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return {"success": False, "error": f"Camofox snapshot failed: {exc}"}


def camofox_click(ref: str, *, session_id: str = "default") -> Dict[str, Any]:
    """通过 Camofox REST API 点击元素。"""
    base = get_camofox_url()
    if not base:
        return {"success": False, "error": "Camofox not configured"}
    try:
        import requests
        resp = requests.post(
            f"{base}/click",
            json={"ref": ref, "session": session_id},
            headers=_camofox_auth_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return {"success": False, "error": f"Camofox click failed: {exc}"}


def camofox_type(ref: str, text: str, *, session_id: str = "default") -> Dict[str, Any]:
    """通过 Camofox REST API 输入文本。"""
    base = get_camofox_url()
    if not base:
        return {"success": False, "error": "Camofox not configured"}
    try:
        import requests
        resp = requests.post(
            f"{base}/type",
            json={"ref": ref, "text": text, "session": session_id},
            headers=_camofox_auth_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return {"success": False, "error": f"Camofox type failed: {exc}"}


def camofox_close(*, session_id: str = "default") -> Dict[str, Any]:
    """关闭 Camofox 会话。"""
    base = get_camofox_url()
    if not base:
        return {"success": False, "error": "Camofox not configured"}
    try:
        import requests
        resp = requests.post(
            f"{base}/close",
            json={"session": session_id},
            headers=_camofox_auth_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        return {"success": True}
    except Exception as exc:
        return {"success": False, "error": f"Camofox close failed: {exc}"}
