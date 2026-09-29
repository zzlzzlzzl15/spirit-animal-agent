"""桌宠 WS 桥接 —— Spirit Agent Phase 7.C（HITL 询问 ↔ 桌面宠物）。

把 :class:`~spirit.evolution.hitl.HumanInTheLoop` 的同步 ``ask`` seam 桥接到桌面宠物的
WebSocket 通道，实现"该问就问用户（气泡弹窗），没回答就 fail-open 自主决策"：

- **询问**：``ask(query, timeout)`` 向所有 WS 客户端广播 ``autonomy_decision`` 事件
  （桌宠渲染层据此弹气泡/状态窗），随后在**线程安全队列**上阻塞等待答复；
  超时/无客户端/无通道 → 返回 ``None``，由 HITL 上游 fail-open 自主决策（不卡死）；
- **答复**：渲染层回发 ``autonomy_answer`` 命令 → 注册的 handler 调
  :meth:`DesktopBridge.submit_answer` 把文本投入队列，唤醒阻塞的 ``ask``；
- **启停/状态**：``autonomy_start`` / ``autonomy_stop`` / ``autonomy_status`` 命令，
  作用于注入的 :class:`~spirit.autonomy.explorer.AutonomyLoop` 控制器。

**注入式可测试**：``send`` 可注入任意同步可调用（测试用假发送器收集广播）；
真实部署经 :func:`bridge_from_ws` 把 ``send`` 绑到 ``WSServer.broadcast``
（跨线程经 ``run_coroutine_threadsafe`` 调度到事件循环）。核心逻辑同步、离线、确定性。
"""

from __future__ import annotations

import asyncio
import logging
import queue
from typing import Any, Callable, Dict, Optional

from spirit.evolution.hitl import DEFAULT_TIMEOUT, HumanQuery

logger = logging.getLogger(__name__)

# 广播事件名 / 命令名（渲染层据此弹窗与回发）
EVENT_DECISION = "autonomy_decision"
CMD_ANSWER = "autonomy_answer"
CMD_STATUS = "autonomy_status"
CMD_START = "autonomy_start"
CMD_STOP = "autonomy_stop"

SendFn = Callable[[str, Dict[str, Any]], Any]


class DesktopBridge:
    """HITL 同步 ask ↔ 桌宠 WS 异步广播/答复 的桥接器。"""

    def __init__(
        self,
        *,
        send: Optional[SendFn] = None,
        server: Any = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        timeout: float = DEFAULT_TIMEOUT,
        controller: Any = None,
    ) -> None:
        self._send = send or (_broadcast_sender(server, loop) if server is not None else None)
        self.timeout = float(timeout)
        self.controller = controller
        self._answers: "queue.Queue[str]" = queue.Queue()
        self._seq = 0

    # -- HITL ask seam ----------------------------------------------------
    def ask(self, query: HumanQuery, timeout: Optional[float] = None) -> Optional[str]:
        """广播决策请求并阻塞等待用户答复；超时/无通道 → None（上游 fail-open）。"""
        wait = self.timeout if timeout is None else float(timeout)
        if self._send is None:
            logger.debug("autonomy.bridge: 无 WS 通道 → 返回 None（fail-open）")
            return None

        self._seq += 1
        payload = {
            "id": self._seq,
            "question": query.question,
            "options": list(query.options or []),
            "context": query.context,
            "is_fork": bool(query.is_fork),
            "timeout": wait,
        }
        # 先清掉上一轮残留答复，再广播；避免广播后新到的答复被误清
        self._drain()
        try:
            self._send(EVENT_DECISION, payload)
        except Exception as exc:
            logger.warning("autonomy.bridge: 广播决策失败 → fail-open: %s", exc)
            return None

        try:
            return self._answers.get(timeout=wait)
        except queue.Empty:
            logger.debug("autonomy.bridge: 等待答复超时 → None（fail-open）")
            return None

    def submit_answer(self, text: Any) -> bool:
        """由 WS handler 调用：把用户答复投入队列唤醒 ask。"""
        value = str(text or "").strip()
        if not value:
            return False
        self._answers.put(value)
        return True

    def _drain(self) -> None:
        while True:
            try:
                self._answers.get_nowait()
            except queue.Empty:
                return

    # -- WS 命令 handler（供 CommandRegistry 注册）------------------------
    def handlers(self) -> Dict[str, Callable]:
        bridge = self

        async def on_answer(ws_client, data: Dict[str, Any]) -> Dict[str, Any]:
            ok = bridge.submit_answer((data or {}).get("text", ""))
            return {"ok": ok}

        async def on_status(ws_client, data: Dict[str, Any]) -> Dict[str, Any]:
            ctrl = bridge.controller
            return {"state": ctrl.state() if ctrl is not None and hasattr(ctrl, "state") else {}}

        async def on_start(ws_client, data: Dict[str, Any]) -> Dict[str, Any]:
            ctrl = bridge.controller
            if ctrl is None or not hasattr(ctrl, "start"):
                return {"ok": False, "error": "no controller"}
            ctrl.start()
            return {"ok": True}

        async def on_stop(ws_client, data: Dict[str, Any]) -> Dict[str, Any]:
            ctrl = bridge.controller
            if ctrl is None or not hasattr(ctrl, "stop"):
                return {"ok": False, "error": "no controller"}
            ctrl.stop()
            return {"ok": True}

        return {
            CMD_ANSWER: on_answer,
            CMD_STATUS: on_status,
            CMD_START: on_start,
            CMD_STOP: on_stop,
        }

    def attach(self, server: Any) -> None:
        """把 autonomy 命令注册到 ``WSServer.commands``。"""
        for action, handler in self.handlers().items():
            server.commands.register(action, handler)


def _broadcast_sender(server: Any, loop: Optional[asyncio.AbstractEventLoop]) -> Optional[SendFn]:
    """构造同步 send：跨线程把 ``server.broadcast`` 调度到事件循环。"""
    if server is None:
        return None

    def send(event_type: str, data: Dict[str, Any]) -> Any:
        coro = server.broadcast(event_type, data)
        if loop is not None and loop.is_running():
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=5.0)
        # 无运行中的循环（测试/离线）：直接丢弃协程并 fail-soft
        coro.close()
        return None

    return send


def bridge_from_ws(server: Any, loop: Optional[asyncio.AbstractEventLoop] = None,
                   *, timeout: float = DEFAULT_TIMEOUT, controller: Any = None) -> DesktopBridge:
    """便捷构造：把桥接绑定到真实 ``WSServer`` + 事件循环。"""
    return DesktopBridge(server=server, loop=loop, timeout=timeout, controller=controller)


__all__ = [
    "DesktopBridge",
    "bridge_from_ws",
    "EVENT_DECISION",
    "CMD_ANSWER",
    "CMD_STATUS",
    "CMD_START",
    "CMD_STOP",
]
