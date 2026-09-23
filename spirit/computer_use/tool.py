"""``computer_use`` 工具派发层 — Spirit Agent（Phase 4.5）。

对标 Hermes ``tools/computer_use/tool.py``：把「安全校验 → 审批门 → 后端派发 → 响应塑形」
这条链从具体平台驱动里剥离出来，只吃 :class:`~spirit.computer_use.backend.ComputerUseBackend`
抽象接口，故全部可离线单测（对着 :class:`~spirit.computer_use.noop_backend.NoopBackend`
断言）。

可测试 seam（对齐 Hermes 的 env-swappable backend + approval callback 设计）：

- **后端选择**：``SPIRIT_COMPUTER_USE_BACKEND`` 选后端（默认 ``noop``）；:func:`set_backend`
  直接注入实例；:func:`set_backend_factory` 注册具名后端工厂（真实驱动接入点）。
- **审批回调**：:func:`set_approval_callback` 注册 ``(action, args, summary) -> verdict``
  回调（``approve_once`` / ``approve_session`` / ``always_approve`` / ``deny``），带会话级
  自动批准状态机；无回调时默认放行（网关审批在更外层经通用工具审批基础设施处理）。

安全边界（逐条对标 Hermes）：硬阻止破坏性组合键与危险输入文本在审批**之前**拦截；只读
动作（capture/wait/list_*）不经审批；改变状态的动作经审批门。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from spirit.computer_use.backend import (
    ActionResult,
    CaptureResult,
    ComputerUseBackend,
    UIElement,
)
from spirit.computer_use.noop_backend import NoopBackend
from spirit.computer_use.safety import (
    DESTRUCTIVE_ACTIONS,
    blocked_key_combo,
    blocked_type_pattern,
)
from spirit.config import get_config_value

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 审批 & 会话状态
# ---------------------------------------------------------------------------

_approval_callback: Optional[Callable[[str, Dict[str, Any], str], str]] = None
_session_auto_approve = False
_always_allow: set = set()


def set_approval_callback(cb: Optional[Callable[[str, Dict[str, Any], str], str]]) -> None:
    """注册 computer_use 审批回调（供 CLI / 桌面端）。

    回调签名 ``(action, args, summary) -> verdict``，verdict ∈
    ``{"approve_once", "approve_session", "always_approve", "deny"}``。
    """
    global _approval_callback
    _approval_callback = cb


def get_approval_callback() -> Optional[Callable[[str, Dict[str, Any], str], str]]:
    return _approval_callback


def clear_approval_callback() -> None:
    global _approval_callback
    _approval_callback = None


def reset_approval_state() -> None:
    """清会话级审批状态（自动批准 + always-allow 集合）。测试隔离用。"""
    global _session_auto_approve, _always_allow
    _session_auto_approve = False
    _always_allow = set()


# ---------------------------------------------------------------------------
# 后端选择（env-swappable + 可注入，测试 seam）
# ---------------------------------------------------------------------------

_backend_lock = threading.Lock()
_backend: Optional[ComputerUseBackend] = None
_backend_factories: Dict[str, Callable[[], ComputerUseBackend]] = {}


def set_backend(backend: Optional[ComputerUseBackend]) -> None:
    """直接注入后端实例（None 清除）；注入的实例会被 ``start()``。测试首选 seam。"""
    global _backend
    with _backend_lock:
        if _backend is not None:
            try:
                _backend.stop()
            except Exception:
                pass
        _backend = backend
        if _backend is not None and not getattr(_backend, "started", False):
            try:
                _backend.start()
            except Exception:
                _backend = None
                raise


def set_backend_factory(name: str, factory: Optional[Callable[[], ComputerUseBackend]]) -> None:
    """注册 / 注销一个具名后端工厂（``factory() -> ComputerUseBackend``）。

    真实桌面驱动（如 cua-driver 后端）的接入点：注册后设
    ``SPIRIT_COMPUTER_USE_BACKEND=<name>`` 即启用。
    """
    if factory is None:
        _backend_factories.pop(name, None)
    else:
        _backend_factories[name] = factory


def _default_backend_name() -> str:
    """后端名解析：env ``SPIRIT_COMPUTER_USE_BACKEND`` → config ``computer_use.backend`` → ``noop``。"""
    env = os.environ.get("SPIRIT_COMPUTER_USE_BACKEND", "").strip().lower()
    if env:
        return env
    return str(get_config_value("computer_use.backend", "noop") or "noop").strip().lower() or "noop"


def _get_backend() -> ComputerUseBackend:
    """返回（并惰性实例化 + 缓存）当前后端。

    解析顺序：已注入实例 → ``SPIRIT_COMPUTER_USE_BACKEND`` 选具名工厂（``noop`` 内置）→
    未知名抛 ``RuntimeError``。start() 失败的后端不被缓存（下次干净重试）。
    """
    global _backend
    with _backend_lock:
        if _backend is not None:
            return _backend
        name = _default_backend_name()
        if name in {"noop", ""}:
            _backend = NoopBackend()
        elif name in _backend_factories:
            _backend = _backend_factories[name]()
        else:
            raise RuntimeError(f"未知 SPIRIT_COMPUTER_USE_BACKEND={name!r}")
        try:
            _backend.start()
        except Exception:
            _backend = None
            raise
        return _backend


def reset_backend_for_tests() -> None:
    """测试助手——拆掉缓存后端并重置会话审批状态。"""
    global _backend
    with _backend_lock:
        if _backend is not None:
            try:
                _backend.stop()
            except Exception:
                pass
        _backend = None
    reset_approval_state()


# ---------------------------------------------------------------------------
# 审批门
# ---------------------------------------------------------------------------

def _request_approval(action: str, args: Dict[str, Any]) -> Optional[str]:
    """批准返回 None；拒绝返回 JSON 错误串。"""
    global _session_auto_approve
    if _session_auto_approve:
        return None
    if action in _always_allow:
        return None
    cb = _approval_callback
    if cb is None:
        # 未接 CLI 审批——默认放行（网关审批在更外层经通用工具审批基础设施处理）。
        return None
    summary = summarize_action(action, args)
    try:
        verdict = cb(action, args, summary)
    except Exception as exc:
        logger.warning("computer_use 审批回调失败: %s", exc)
        verdict = "deny"
    if verdict == "approve_once":
        return None
    if verdict in {"approve_session", "always_approve"}:
        _always_allow.add(action)
        if verdict == "always_approve":
            _session_auto_approve = True
        return None
    return json.dumps({"error": "denied by user", "action": action}, ensure_ascii=False)


def summarize_action(action: str, args: Dict[str, Any]) -> str:
    """给审批面板看的一行人类可读动作摘要。"""
    if action in {"click", "double_click", "right_click", "middle_click"}:
        if args.get("element") is not None:
            return f"{action} element #{args['element']}"
        coord = args.get("coordinate")
        if coord:
            return f"{action} at {tuple(coord)}"
        return action
    if action == "drag":
        src = args.get("from_element") or args.get("from_coordinate")
        dst = args.get("to_element") or args.get("to_coordinate")
        return f"drag {src} → {dst}"
    if action == "scroll":
        return f"scroll {args.get('direction', '?')} x{args.get('amount', 3)}"
    if action == "type":
        text = args.get("text", "")
        return f"type {text[:60]!r}" + ("..." if len(text) > 60 else "")
    if action == "key":
        return f"key {args.get('keys', '')!r}"
    if action == "focus_app":
        return f"focus {args.get('app', '')!r}" + (" (raise)" if args.get("raise_window") else "")
    return action


# ---------------------------------------------------------------------------
# 响应塑形
# ---------------------------------------------------------------------------

_DEFAULT_MAX_ELEMENTS = int(get_config_value("computer_use.max_elements", 100))
_MAX_ALLOWED_MAX_ELEMENTS = int(get_config_value("computer_use.max_elements_ceiling", 1000))


def _coerce_max_elements(value: Any) -> int:
    """把调用方给的 ``max_elements`` 钳制到 ``[1, 1000]``（缺省 100，非法→100）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ELEMENTS
    if n < 1:
        return 1
    return min(n, _MAX_ALLOWED_MAX_ELEMENTS)


def _element_to_dict(e: UIElement) -> Dict[str, Any]:
    x, y, w, h = e.bounds
    return {
        "index": e.index, "role": e.role, "label": e.label,
        "bounds": {"x": x, "y": y, "w": w, "h": h},
        "center": list(e.center()), "app": e.app,
    }


def _format_elements(elements: List[UIElement]) -> List[str]:
    """把元素渲染成人类可读的索引行（供 summary）。"""
    lines: List[str] = []
    for e in elements:
        label = f" {e.label!r}" if e.label else ""
        lines.append(f"  [{e.index}] {e.role}{label} @ {e.center()}")
    return lines


def _capture_payload(cap: CaptureResult, max_elements: int = _DEFAULT_MAX_ELEMENTS) -> Dict[str, Any]:
    """AX / 文本路径的捕获 payload（总是 dict；含截断说明）。"""
    total = len(cap.elements)
    visible = cap.elements[:max_elements]
    truncated = max(0, total - len(visible))
    summary_lines = [
        f"capture mode={cap.mode} {cap.width}x{cap.height}"
        + (f" app={cap.app}" if cap.app else "")
        + (f" window={cap.window_title!r}" if cap.window_title else ""),
        f"{total} interactable element(s):",
    ]
    summary_lines.extend(_format_elements(visible))
    if truncated:
        summary_lines.append(
            f"  (response truncated to {len(visible)} of {total} elements; "
            f"raise max_elements or pass app= to narrow)"
        )
    payload: Dict[str, Any] = {
        "mode": cap.mode, "width": cap.width, "height": cap.height,
        "app": cap.app, "window_title": cap.window_title,
        "elements": [_element_to_dict(e) for e in visible],
        "total_elements": total, "summary": "\n".join(summary_lines),
    }
    if truncated:
        payload["truncated_elements"] = truncated
    return payload


def _capture_response(cap: CaptureResult, max_elements: int = _DEFAULT_MAX_ELEMENTS) -> Any:
    """捕获动作的响应：有图且非 ax → 多模态信封 dict；否则 AX/文本 JSON 串。"""
    if cap.png_b64 and cap.mode != "ax":
        mime = cap.image_mime_type
        if not mime:
            prefix = cap.png_b64[:8]
            mime = "image/jpeg" if prefix.startswith("/9j/") else "image/png"
        summary = _capture_payload(cap, max_elements)["summary"]
        return {
            "_multimodal": True,
            "content": [
                {"type": "text", "text": summary},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{cap.png_b64}"}},
            ],
            "text_summary": summary,
            "meta": {"mode": cap.mode, "width": cap.width, "height": cap.height,
                     "elements": len(cap.elements), "png_bytes": cap.png_bytes_len},
        }
    return json.dumps(_capture_payload(cap, max_elements), ensure_ascii=False)


def _text_response(res: ActionResult) -> str:
    payload: Dict[str, Any] = {"ok": res.ok, "action": res.action}
    if res.message:
        payload["message"] = res.message
    if res.meta:
        payload["meta"] = res.meta
    return json.dumps(payload, ensure_ascii=False)


def _maybe_follow_capture(backend: ComputerUseBackend, res: ActionResult, capture_after: bool) -> str:
    """动作结果 JSON；``capture_after`` 且动作成功时附一次后续捕获的 AX payload。"""
    payload: Dict[str, Any] = {"ok": res.ok, "action": res.action}
    if res.message:
        payload["message"] = res.message
    if res.meta:
        payload["meta"] = res.meta
    if capture_after and res.ok:
        payload["capture"] = _capture_payload(backend.capture())
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 派发
# ---------------------------------------------------------------------------

def _dispatch(backend: ComputerUseBackend, action: str, args: Dict[str, Any]) -> Any:
    capture_after = bool(args.get("capture_after"))

    if action == "capture":
        mode = str(args.get("mode") or get_config_value("computer_use.default_capture_mode", "som"))
        if mode not in {"som", "vision", "ax"}:
            return json.dumps({"error": f"bad mode {mode!r}; use som|vision|ax"}, ensure_ascii=False)
        capture_kwargs: Dict[str, Any] = {"mode": mode, "app": args.get("app")}
        if args.get("pid") is not None or args.get("window_id") is not None:
            capture_kwargs.update({"pid": args.get("pid"), "window_id": args.get("window_id")})
        cap = backend.capture(**capture_kwargs)
        return _capture_response(cap, max_elements=_coerce_max_elements(args.get("max_elements")))

    if action == "wait":
        return _text_response(backend.wait(float(args.get("seconds", 1.0))))

    if action == "list_apps":
        apps = backend.list_apps()
        return json.dumps({"apps": apps, "count": len(apps)}, ensure_ascii=False)

    if action == "list_windows":
        windows = backend.list_windows()
        return json.dumps({"windows": windows, "count": len(windows)}, ensure_ascii=False)

    if action == "focus_app":
        app = args.get("app")
        if not app:
            return json.dumps({"error": "focus_app requires `app`"}, ensure_ascii=False)
        return _maybe_follow_capture(
            backend, backend.focus_app(app, raise_window=bool(args.get("raise_window"))), capture_after
        )

    if action in {"click", "double_click", "right_click", "middle_click"}:
        button = args.get("button")
        click_count = 1
        if action == "double_click":
            click_count = 2
        elif action == "right_click":
            button = "right"
        elif action == "middle_click":
            button = "middle"
        else:
            button = button or "left"
        coord = args.get("coordinate") or (None, None)
        x, y = (coord[0], coord[1]) if coord and coord[0] is not None else (None, None)
        res = backend.click(
            element=args.get("element"), x=x, y=y, button=button or "left",
            click_count=click_count, modifiers=args.get("modifiers"),
        )
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "drag":
        has_elements = args.get("from_element") is not None and args.get("to_element") is not None
        has_coords = args.get("from_coordinate") and args.get("to_coordinate")
        if not has_elements and not has_coords:
            return json.dumps(
                {"error": "drag requires from_coordinate/to_coordinate or from_element/to_element"},
                ensure_ascii=False,
            )
        res = backend.drag(
            from_element=args.get("from_element"), to_element=args.get("to_element"),
            from_xy=tuple(args["from_coordinate"]) if args.get("from_coordinate") else None,
            to_xy=tuple(args["to_coordinate"]) if args.get("to_coordinate") else None,
            button=args.get("button", "left"), modifiers=args.get("modifiers"),
        )
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "scroll":
        coord = args.get("coordinate") or (None, None)
        res = backend.scroll(
            direction=args.get("direction", "down"), amount=int(args.get("amount", 3)),
            element=args.get("element"),
            x=coord[0] if coord and coord[0] is not None else None,
            y=coord[1] if coord and coord[1] is not None else None,
            modifiers=args.get("modifiers"),
        )
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "type":
        return _maybe_follow_capture(backend, backend.type_text(args.get("text", "")), capture_after)

    if action == "key":
        return _maybe_follow_capture(backend, backend.key(args.get("keys", "")), capture_after)

    if action == "set_value":
        value = args.get("value")
        if value is None:
            return json.dumps({"error": "set_value requires `value`"}, ensure_ascii=False)
        res = backend.set_value(value=str(value), element=args.get("element"))
        return _maybe_follow_capture(backend, res, capture_after)

    return json.dumps({"error": f"unknown action {action!r}"}, ensure_ascii=False)


def handle_computer_use(args: Dict[str, Any], **kwargs: Any) -> Any:
    """主入口——由 ``tools.registry`` 派发。返回 JSON 串或多模态 dict。

    流程：动作校验 → 硬安全拦截（危险输入 / 破坏性组合键）→ 审批门（仅改变状态的动作）→
    后端派发。任何后端不可用 / 派发异常都折叠成 JSON 错误串（不抛给 Agent 循环）。
    """
    action = (args.get("action") or "").strip().lower()
    if not action:
        return json.dumps({"error": "missing `action`"}, ensure_ascii=False)

    # 安全：审批提示之前先校验动作。
    if action == "type":
        pat = blocked_type_pattern(args.get("text", ""))
        if pat:
            return json.dumps({
                "error": f"blocked pattern in type text: {pat!r}",
                "hint": "危险 shell 模式不能经 computer_use 输入。",
            }, ensure_ascii=False)

    if action == "key":
        blocked = blocked_key_combo(args.get("keys", ""))
        if blocked is not None:
            return json.dumps({
                "error": f"blocked key combo: {sorted(blocked)}",
                "hint": "破坏性系统快捷键被硬阻止。",
            }, ensure_ascii=False)

    # 审批门（仅改变用户可见状态的动作；computer_use.approval_required 关闭时跳过）。
    if action in DESTRUCTIVE_ACTIONS and get_config_value("computer_use.approval_required", True):
        err = _request_approval(action, args)
        if err is not None:
            return err

    try:
        backend = _get_backend()
    except Exception as exc:
        return json.dumps({
            "error": f"computer_use backend unavailable: {exc}",
            "hint": "若无桌面控制后端，工具以 noop 降级（动作只记录、不生效）。",
        }, ensure_ascii=False)

    try:
        return _dispatch(backend, action, args)
    except Exception as exc:
        logger.exception("computer_use %s 失败", action)
        return json.dumps({"error": f"{action} failed: {exc}"}, ensure_ascii=False)


def check_computer_use_requirements() -> bool:
    """工具 check_fn：后端可实例化且 ``is_available()`` 为真时返回 True。

    noop 后端恒可用（安全降级）；真实后端在驱动缺失 / 未就绪时返回 False，工具面隐藏。
    ``computer_use.enabled`` 关闭时直接返回 False（全局 kill-switch）。
    """
    if not get_config_value("computer_use.enabled", True):
        return False
    try:
        return bool(_get_backend().is_available())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 工具注册
# ---------------------------------------------------------------------------
#
# 注册在发现 shim ``spirit/tools/computer_use_tool.py`` 里完成（对标 Hermes
# ``tools/computer_use_tool.py``）：本子包只放实现，shim 承担 ``registry.register``
# 以便 ``discover_tools()`` 扁平扫描 ``spirit/tools/*.py`` 时发现。测试直接调
# ``handle_computer_use``，无需注册。


__all__ = [
    "handle_computer_use",
    "check_computer_use_requirements",
    "set_approval_callback",
    "get_approval_callback",
    "clear_approval_callback",
    "reset_approval_state",
    "set_backend",
    "set_backend_factory",
    "reset_backend_for_tests",
    "summarize_action",
]
