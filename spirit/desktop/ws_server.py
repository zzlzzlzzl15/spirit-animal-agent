"""WebSocket 桥接服务器 — Python 后端 ↔ Electron 前端实时通信。

架构：
- 异步 WebSocket 服务器（websockets 库）
- PetEngine 作为核心状态源
- 前端连接后可调用命令、接收状态推送
- 支持多客户端同时连接

协议（JSON-RPC 风格）：
    请求: {"type": "command", "action": "get_status", "id": "req-1"}
    响应: {"type": "response", "action": "get_status", "id": "req-1", "data": {...}}
    推送: {"type": "event", "event": "state_change", "data": {...}}
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, Callable, Dict, Optional, Set

from spirit.desktop.pet_engine import PetEngine
from spirit.desktop import system_status
from spirit.desktop.voice_engine import VoiceEngine, VoiceConfig

logger = logging.getLogger(__name__)

# 超时自动续跑时注入的续跑指令：让 Agent 从断点继续完成未完成任务，
# 而非重头再来或直接结束（用户要求：超时不能就算了，得继续做完）。
_RESUME_CONTINUATION_PROMPT = (
    "[系统自动续跑] 上一轮因请求超时被中断，任务尚未完成。"
    "请查看上面的对话历史，定位你中断在哪个步骤，从该处继续完成剩余工作："
    "不要重做已完成的步骤，不要复述背景，不要向我确认，直接执行并给出最终结论。"
)

# 尝试导入 websockets（可选依赖）
try:
    import websockets
    import websockets.server
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False


# ---------------------------------------------------------------------------
# 命令处理器注册
# ---------------------------------------------------------------------------

class CommandRegistry:
    """命令注册表 — 将 action 字符串映射到处理函数。"""

    def __init__(self):
        self._handlers: Dict[str, Callable] = {}

    def register(self, action: str, handler: Callable) -> None:
        """注册命令处理器。

        handler 签名: async def handler(ws_client, data: dict) -> dict
        """
        self._handlers[action] = handler

    def get(self, action: str) -> Optional[Callable]:
        return self._handlers.get(action)

    def list_actions(self) -> list:
        return list(self._handlers.keys())


# ---------------------------------------------------------------------------
# WebSocket 客户端包装
# ---------------------------------------------------------------------------

class WSClient:
    """单个 WebSocket 客户端连接。"""

    def __init__(self, ws, server: "WSServer"):
        self.ws = ws
        self.server = server
        self.connected_at = time.time()
        self.id = f"client-{id(self):x}"

    async def send_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """向客户端推送事件。"""
        try:
            msg = json.dumps({
                "type": "event",
                "event": event_type,
                "data": data,
                "timestamp": time.time(),
            }, ensure_ascii=False)
            await self.ws.send(msg)
        except Exception as exc:
            logger.debug("推送事件失败 (%s): %s", self.id, exc)

    async def send_response(
        self, action: str, data: Dict[str, Any], req_id: str = ""
    ) -> None:
        """向客户端发送命令响应。"""
        try:
            msg = json.dumps({
                "type": "response",
                "action": action,
                "id": req_id,
                "data": data,
                "timestamp": time.time(),
            }, ensure_ascii=False)
            await self.ws.send(msg)
        except Exception as exc:
            logger.debug("发送响应失败 (%s): %s", self.id, exc)


# ---------------------------------------------------------------------------
# WebSocket 服务器
# ---------------------------------------------------------------------------

class WSServer:
    """WebSocket 桥接服务器。

    管理客户端连接、命令分发、状态推送。
    """

    def __init__(
        self,
        engine: PetEngine,
        *,
        host: str = "127.0.0.1",
        port: int = 9877,
        agent=None,
    ):
        """初始化服务器。

        Args:
            engine: PetEngine 实例
            host: 绑定地址（默认 localhost）
            port: 绑定端口
            agent: SpiritAgent 实例（可选，用于系统状态采集）
        """
        self.engine = engine
        self.host = host
        self.port = port
        self.agent = agent

        # 语音引擎（延迟初始化）
        self._voice_engine: Optional[VoiceEngine] = None

        # 客户端管理
        self._clients: Set[WSClient] = set()

        # 命令注册表
        self.commands = CommandRegistry()
        self._register_builtin_commands()

        # 引擎事件订阅
        self.engine.on_event(self._on_engine_event)

        # 服务器引用
        self._server = None
        self._running = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动 WebSocket 服务器。"""
        if not HAS_WEBSOCKETS:
            raise RuntimeError(
                "websockets 库未安装。请运行: pip install websockets"
            )

        self._running = True
        self._server = await websockets.server.serve(
            self._handle_client,
            self.host,
            self.port,
        )
        logger.info("WebSocket 服务器已启动: ws://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        """停止服务器。"""
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        # 断开所有客户端
        for client in list(self._clients):
            try:
                await client.ws.close()
            except Exception:
                pass
        self._clients.clear()
        logger.info("WebSocket 服务器已停止")

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()

    # ------------------------------------------------------------------
    # 客户端管理
    # ------------------------------------------------------------------

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def broadcast(self, event_type: str, data: Dict[str, Any]) -> None:
        """向所有连接的客户端广播事件。"""
        for client in list(self._clients):
            await client.send_event(event_type, data)

    # ------------------------------------------------------------------
    # 内部处理
    # ------------------------------------------------------------------

    async def _handle_client(self, ws) -> None:
        """处理单个客户端连接。"""
        client = WSClient(ws, self)
        self._clients.add(client)
        logger.info("客户端已连接: %s (总计: %d)", client.id, len(self._clients))

        try:
            # 发送初始状态同步
            await client.send_event("init", self.engine.get_full_status())

            # 消息循环
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await client.send_event("error", {"message": "无效的 JSON"})
                    continue

                await self._dispatch(client, msg)

        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as exc:
            logger.warning("客户端异常 (%s): %s", client.id, exc)
        finally:
            self._clients.discard(client)
            logger.info("客户端已断开: %s (剩余: %d)", client.id, len(self._clients))

    async def _dispatch(self, client: WSClient, msg: Dict[str, Any]) -> None:
        """分发客户端消息。"""
        msg_type = msg.get("type", "")
        action = msg.get("action", "")
        req_id = msg.get("id", "")
        data = msg.get("data", {})

        if msg_type != "command":
            await client.send_event("error", {
                "message": f"未知消息类型: {msg_type}"
            })
            return

        handler = self.commands.get(action)
        if handler is None:
            await client.send_response(action, {
                "error": f"未知命令: {action}",
                "available": self.commands.list_actions(),
            }, req_id)
            return

        try:
            result = await handler(client, data)
            await client.send_response(action, result or {}, req_id)
        except Exception as exc:
            logger.warning("命令执行失败 (%s): %s", action, exc)
            await client.send_response(action, {
                "error": str(exc),
            }, req_id)

    # ------------------------------------------------------------------
    # 引擎事件 → 客户端广播
    # ------------------------------------------------------------------

    def _on_engine_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """PetEngine 事件回调 → 异步广播到所有客户端。"""
        if not self._clients:
            return

        # 创建异步任务广播
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.broadcast(event_type, payload))
        except RuntimeError:
            # 没有运行中的事件循环，忽略
            pass

    # ------------------------------------------------------------------
    # 内置命令
    # ------------------------------------------------------------------

    def _register_builtin_commands(self) -> None:
        """注册内置命令。"""
        reg = self.commands.register

        # ── 宠物状态 ──────────────────────────────────────────

        async def cmd_get_status(client, data):
            """获取完整状态（PetEngine + Agent）。"""
            result = self.engine.get_full_status()
            # 补充 Agent 信息
            if self.agent is not None:
                agent_status = self.agent.get_status()
                result.update(agent_status)
                result["running"] = True
            else:
                result["model"] = ""
                result["provider"] = ""
                result["session_id"] = ""
                result["message_count"] = 0
                result["tool_count"] = 0
                result["api_call_count"] = 0
                result["running"] = False
            return result
        reg("get_status", cmd_get_status)

        async def cmd_get_pet_info(client, data):
            return self.engine.get_pet_info()
        reg("get_pet_info", cmd_get_pet_info)

        async def cmd_list_pets(client, data):
            return {"pets": self.engine.list_pets()}
        reg("list_pets", cmd_list_pets)

        async def cmd_switch_pet(client, data):
            slug = data.get("slug", "")
            ok = self.engine.switch_pet(slug)
            return {"success": ok, "slug": slug}
        reg("switch_pet", cmd_switch_pet)

        # ── 系统状态 ──────────────────────────────────────────

        async def cmd_get_system_status(client, data):
            return system_status.get_full_status(self.agent)
        reg("get_system_status", cmd_get_system_status)

        async def cmd_get_cpu(client, data):
            return system_status.get_cpu_info()
        reg("get_cpu", cmd_get_cpu)

        async def cmd_get_memory(client, data):
            return system_status.get_memory_info()
        reg("get_memory", cmd_get_memory)

        # ── 偏好设置 ──────────────────────────────────────────

        async def cmd_set_scale(client, data):
            scale = float(data.get("scale", 1.0))
            self.engine.set_scale(scale)
            return {"scale": self.engine.preferences.scale}
        reg("set_scale", cmd_set_scale)

        async def cmd_set_position(client, data):
            x = int(data.get("x", -1))
            y = int(data.get("y", -1))
            self.engine.set_position(x, y)
            return {"pos_x": x, "pos_y": y}
        reg("set_position", cmd_set_position)

        # ── Agent 交互 ────────────────────────────────────────

        async def cmd_execute_command(client, data):
            """执行 CLI 命令（通过 Agent）。"""
            cmd = data.get("command", "")
            if not cmd:
                return {"error": "缺少 command 参数"}
            # 占位：实际执行需要通过 Agent 的工具系统
            return {
                "command": cmd,
                "status": "pending",
                "message": "命令执行功能待集成",
            }
        reg("execute_command", cmd_execute_command)

        async def cmd_search_knowledge(client, data):
            """知识库搜索。"""
            query = data.get("query", "")
            if not query:
                return {"error": "缺少 query 参数"}
            # 占位：实际搜索需要通过 Memora API
            return {
                "query": query,
                "results": [],
                "message": "知识库搜索功能待集成",
            }
        reg("search_knowledge", cmd_search_knowledge)

        async def cmd_chat(client, data):
            """对话接口 — 支持工具进度推送和流式响应。"""
            message = data.get("message", "")
            if not message:
                return {"error": "缺少 message 参数"}
            if self.agent is None:
                return {"error": "Agent 未连接"}

            # 新对话开始前清除历史中断标志：上一轮超时/中断留下的
            # _interrupt_requested 若不清除，conversation_loop 会在本轮
            # 开头立即返回 "[已中断]"，表现为 Agent 假死——之后每条
            # 消息都秒回 [已中断]，看起来像崩溃。
            self.agent.clear_interrupt()

            # 从配置读取超时时间（默认 1800 秒 = 30 分钟；
            # 多工具聚合的长任务 5 分钟远远不够，过早超时会误杀正常任务）
            from spirit.config import get_config_value
            chat_timeout = get_config_value("timeouts.chat_request", 1800.0)
            # 超时自动续跑（超时≠结束）：中断当前轮后注入续跑指令重进对话循环，
            # 继续完成未完成的任务；轮数封顶防无限续跑烧 token。
            auto_resume = bool(get_config_value("chat.auto_resume_on_timeout", True))
            max_resumes = int(get_config_value("chat.max_auto_resumes", 3))
            resume_grace = float(get_config_value("chat.resume_grace_seconds", 120))
            
            try:
                # 注册临时回调用于进度推送
                original_on_tool_start = self.agent.on_tool_start
                original_on_tool_complete = self.agent.on_tool_complete
                original_on_stream = self.agent.on_stream_delta

                async def _push_tool_start(name, args):
                    try:
                        await client.send_event("tool_start", {
                            "tool": name,
                            "args_preview": str(args)[:200] if args else "",
                        })
                    except Exception:
                        pass

                async def _push_tool_complete(name, result):
                    try:
                        result_preview = str(result)[:300] if result else ""
                        await client.send_event("tool_complete", {
                            "tool": name,
                            "result_preview": result_preview,
                        })
                    except Exception:
                        pass

                async def _push_stream_delta(text):
                    try:
                        # 主循环已门控/清理过文本，这里直接转发
                        if text:  # 只发送非空文本
                            await client.send_event("stream_delta", {"text": text})
                    except Exception:
                        pass

                # 设置回调（工具在执行器线程中运行，必须用
                # run_coroutine_threadsafe 线程安全地调度到事件循环）
                loop = asyncio.get_event_loop()

                def _push_tool_start_sync(name, args):
                    try:
                        asyncio.run_coroutine_threadsafe(
                            _push_tool_start(name, args), loop
                        )
                    except Exception:
                        pass

                def _push_tool_complete_sync(name, result):
                    try:
                        asyncio.run_coroutine_threadsafe(
                            _push_tool_complete(name, result), loop
                        )
                    except Exception:
                        pass

                self.agent.on_tool_start = _push_tool_start_sync
                self.agent.on_tool_complete = _push_tool_complete_sync

                # 单一路径：流式作为传输层集成在主循环内（chat + stream_callback）
                deltas_sent = {"v": False}

                def _sync_delta_callback(text):
                    """同步回调 → 线程安全推送，并阻塞等待发送完成。

                    必须 .result() 等待：否则主循环返回后
                    chat_complete/命令响应可能先于 delta 到达前端，
                    导致最终文本乱序或丢失。
                    """
                    deltas_sent["v"] = True
                    try:
                        asyncio.run_coroutine_threadsafe(
                            _push_stream_delta(text), loop
                        ).result(timeout=10)
                    except Exception:
                        pass

                def _dispatch(text):
                    """把一轮对话派发到执行器线程（流式/非流式单一路径）。"""
                    if self.agent.config.streaming_enabled:
                        return loop.run_in_executor(
                            None, self.agent.chat, text, _sync_delta_callback
                        )
                    # 非流式模式：等待完整响应
                    return loop.run_in_executor(None, self.agent.chat, text)

                # ── 超时自动续跑主循环 ───────────────────────
                # 正常完成 → break 走收尾；超时 → 中断当前轮 + 等旧循环退出 +
                # 注入续跑指令重进循环，直到完成或续跑轮数封顶。
                current_message = message
                resumes_used = 0
                while True:
                    future = _dispatch(current_message)
                    try:
                        response = await asyncio.wait_for(future, timeout=chat_timeout)
                        break
                    except asyncio.TimeoutError:
                        logger.warning("聊天请求超时 (%.1f 秒)，中断 Agent", chat_timeout)
                        self.agent.interrupt()
                        if not auto_resume or resumes_used >= max_resumes:
                            reason = (
                                f"请求超时（{chat_timeout:.0f} 秒），自动续跑"
                                f"{resumes_used} 轮后仍未完成，已中断"
                                if resumes_used else
                                f"请求超时（{chat_timeout:.0f} 秒），已中断处理"
                            )
                            return {"error": reason, "timeout": True}
                        resumes_used += 1
                        logger.warning(
                            "超时自动续跑第 %d/%d 轮：注入续跑指令重进对话循环",
                            resumes_used, max_resumes,
                        )
                        await _push_stream_delta(
                            f"\n⏳ 请求超时（{chat_timeout:.0f} 秒），自动续跑未完成"
                            f"任务（第 {resumes_used}/{max_resumes} 轮）…\n"
                        )
                        # 旧对话循环见到中断标志才会退出（可能正卡在 LLM 调用/
                        # 工具调用里），必须等它落定再开新一轮，避免两个 chat
                        # 并行改会话消息。用轮询而非 wait_for/shield：嵌套
                        # wait_for 在同一任务上的取消记账会污染后续轮（3.12
                        # 曾致 CancelledError 逸出），轮询免疫且语义直观。
                        deadline = loop.time() + resume_grace
                        while not future.done() and loop.time() < deadline:
                            await asyncio.sleep(0.2)
                        if not future.done():
                            logger.warning(
                                "旧对话循环 %ss 宽限后仍未退出，放弃自动续跑",
                                resume_grace,
                            )
                            return {
                                "error": (
                                    f"请求超时且旧任务 {resume_grace:.0f} 秒内未退出，"
                                    "已放弃自动续跑"
                                ),
                                "timeout": True,
                            }
                        # 旧循环已落定（[已中断]/异常/结果均可），会话状态可用
                        self.agent.clear_interrupt()
                        current_message = _RESUME_CONTINUATION_PROMPT
                    except asyncio.CancelledError:
                        # 外部取消（断连/关停）或 asyncio 取消记账异常：
                        # 不续跑、立即收敛，避免孤儿任务与状态污染
                        logger.warning("对话循环被取消（CancelledError），不续跑直接收敛")
                        if self.agent:
                            self.agent.interrupt()
                        return {
                            "error": f"请求被取消（超时或连接断开），已中断处理",
                            "timeout": True,
                        }

                # 恢复原始回调
                self.agent.on_tool_start = original_on_tool_start
                self.agent.on_tool_complete = original_on_tool_complete

                # 清理响应中的 <think> 标签和工具调用格式
                from spirit.cli.main_enhanced import _clean_think_tags, _format_tool_calls_display
                
                # response 现在是 dict: {'response': str, 'tool_calls': list, ...}
                if isinstance(response, dict):
                    raw_response = response.get('response', '')
                    tool_calls = response.get('tool_calls', [])
                else:
                    # 兼容旧版本（流式模式可能返回字符串）
                    raw_response = response
                    tool_calls = []
                
                # 清理文本响应
                cleaned_response = _clean_think_tags(raw_response) if raw_response else ''
                
                # 格式化工具调用（如果有）
                formatted_tools = _format_tool_calls_display(tool_calls) if tool_calls else ''
                
                # 发送完成事件：chat_complete 始终携带清理后的最终文本，
                # 作为可靠兕底渲染源（前端仅在流式增量未送达时渲染它，防双重输出）
                logger.info(
                    "chat 完成: deltas_sent=%s, response_len=%d, tools=%d",
                    deltas_sent["v"], len(cleaned_response), len(tool_calls),
                )
                await client.send_event("chat_complete", {
                    "response": cleaned_response,
                    "tool_calls_formatted": formatted_tools,
                    "deltas_sent": deltas_sent["v"],
                })

                return {
                    "response": cleaned_response,
                    "tool_calls": tool_calls,
                    "tool_calls_formatted": formatted_tools,
                }
            except asyncio.TimeoutError:
                # 防御分支：超时已在续跑循环内逐轮处理，到这里属意外路径
                logger.warning("聊天请求超时（意外路径, %.1f 秒）", chat_timeout)
                if self.agent:
                    self.agent.interrupt()
                return {
                    "error": f"请求超时（{chat_timeout:.0f} 秒），已中断处理",
                    "timeout": True,
                }
            except Exception as exc:
                return {"error": str(exc)}
            finally:
                # 恢复原始回调（覆盖正常/超时续跑封顶/异常全部退出路径）
                self.agent.on_tool_start = original_on_tool_start
                self.agent.on_tool_complete = original_on_tool_complete
        reg("chat", cmd_chat)

        async def cmd_transcribe_audio(client, data):
            """语音识别：接收 base64 音频，返回文本。"""
            audio_b64 = data.get("audio_base64", "")
            mime_type = data.get("mime_type", "audio/webm")
            if not audio_b64:
                return {"error": "缺少 audio_base64 参数"}
            try:
                audio_bytes = base64.b64decode(audio_b64)
                # 从 mime type 推断格式
                fmt = "webm"
                if "wav" in mime_type:
                    fmt = "wav"
                elif "mp3" in mime_type or "mpeg" in mime_type:
                    fmt = "mp3"
                elif "ogg" in mime_type:
                    fmt = "ogg"
                # 延迟初始化语音引擎
                if self._voice_engine is None:
                    self._voice_engine = VoiceEngine(VoiceConfig.from_env())
                result = await self._voice_engine.transcribe(audio_bytes, format=fmt)
                if result.error:
                    return {"error": result.error}
                if result.is_empty:
                    return {"text": "", "message": "未识别到内容"}
                return {"text": result.text, "language": result.language}
            except Exception as exc:
                logger.warning("语音识别失败: %s", exc)
                return {"error": str(exc)}
        reg("transcribe_audio", cmd_transcribe_audio)

        async def cmd_synthesize_speech(client, data):
            """语音合成（TTS）：接收文本，返回 base64 音频供前端播放。"""
            text = data.get("text", "")
            if not text or not text.strip():
                return {"error": "缺少 text 参数"}
            try:
                # 延迟初始化语音引擎
                if self._voice_engine is None:
                    self._voice_engine = VoiceEngine(VoiceConfig.from_env())
                result = await self._voice_engine.synthesize(text)
                if result.error:
                    return {"error": result.error}
                if not result.is_success:
                    return {"error": "语音合成失败（无音频数据）"}
                return {
                    "audio_base64": base64.b64encode(result.audio_data).decode("ascii"),
                    "format": result.format,
                    "voice": result.voice,
                    "duration_ms": result.duration_ms,
                }
            except Exception as exc:
                logger.warning("语音合成失败: %s", exc)
                return {"error": str(exc)}
        reg("synthesize_speech", cmd_synthesize_speech)

        async def cmd_voice_backends(client, data):
            """返回可用的语音后端（STT/TTS）供前端展示。"""
            try:
                if self._voice_engine is None:
                    self._voice_engine = VoiceEngine(VoiceConfig.from_env())
                return {"backends": self._voice_engine.get_available_backends()}
            except Exception as exc:
                return {"backends": {"stt": [], "tts": []}, "error": str(exc)}
        reg("voice_backends", cmd_voice_backends)

        async def cmd_voice_chat(client, data):
            """一站式语音问答：STT → Agent → TTS（listen_and_respond 全管线）。

            等价于 transcribe_audio + chat + synthesize_speech 三段式的单命令版本，
            适合唤醒词常驻监听等场景：前端一次往返即可拿到文本与语音回复。
            """
            audio_b64 = data.get("audio_base64", "")
            if not audio_b64:
                return {"error": "缺少 audio_base64 参数"}
            try:
                audio_bytes = base64.b64decode(audio_b64)
            except Exception as exc:
                return {"error": f"音频解码失败: {exc}"}

            try:
                if self._voice_engine is None:
                    self._voice_engine = VoiceEngine(VoiceConfig.from_env())

                # 注入 Agent 对话回调（同步 chat 放线程池，避免阻塞事件循环）
                agent_chat = None
                if self.agent is not None:
                    loop = asyncio.get_running_loop()

                    async def agent_chat(text: str) -> str:
                        result = await loop.run_in_executor(None, self.agent.chat, text)
                        if isinstance(result, dict):
                            return result.get("response", "")
                        return str(result or "")

                from spirit.config import get_config_value
                timeout = get_config_value("timeouts.chat_request", 300.0)
                out = await asyncio.wait_for(
                    self._voice_engine.listen_and_respond(audio_bytes, agent_chat=agent_chat),
                    timeout=timeout,
                )

                payload = {
                    "text": out.get("text", ""),
                    "response": out.get("response", ""),
                    "error": out.get("error", ""),
                }
                audio = out.get("audio") or b""
                if audio:
                    payload["audio_base64"] = base64.b64encode(audio).decode("ascii")
                    payload["format"] = out.get("format", "")
                return payload
            except asyncio.TimeoutError:
                logger.warning("语音问答超时")
                return {"error": "语音问答超时，已中断处理", "timeout": True}
            except Exception as exc:
                logger.warning("语音问答失败: %s", exc)
                return {"error": str(exc)}
        reg("voice_chat", cmd_voice_chat)

        # ── 状态控制 ──────────────────────────────────────────

        async def cmd_set_state(client, data):
            """强制设置宠物状态（调试用）。"""
            from spirit.desktop.pet_constants import PetState
            state_name = data.get("state", "idle")
            try:
                state = PetState(state_name)
                self.engine.set_state(state)
                return {"state": state.value}
            except ValueError:
                return {
                    "error": f"无效状态: {state_name}",
                    "valid": [s.value for s in PetState],
                }
        reg("set_state", cmd_set_state)

        async def cmd_idle(client, data):
            """回到空闲状态。"""
            self.engine.idle()
            return {"state": "idle"}
        reg("idle", cmd_idle)

        # ── 配置管理（参考 Hermes config 命令设计）──────────────

        async def cmd_get_config(client, data):
            """返回当前 Agent 配置。"""
            if self.agent is None:
                return {"config": {"error": "Agent 未连接"}}
            try:
                cfg = self.agent.config
                return {
                    "config": {
                        "model": cfg.model or "未配置",
                        "provider": cfg.provider or "auto",
                        "base_url": cfg.base_url or "默认",
                        "max_iterations": cfg.max_iterations,
                        "context_length": cfg.context_length,
                        "compression_enabled": cfg.compression_enabled,
                        "compression_threshold": cfg.compression_threshold,
                        "streaming_enabled": cfg.streaming_enabled,
                        "session_id": self.agent.session_id,
                        "platform": cfg.platform,
                    }
                }
            except Exception as exc:
                return {"config": {}, "error": str(exc)}
        reg("get_config", cmd_get_config)

        async def cmd_set_config(client, data):
            """设置配置项。"""
            key = data.get("key", "")
            value = data.get("value", "")
            if not key:
                return {"error": "缺少 key 参数"}
            if self.agent is None:
                return {"error": "Agent 未连接"}
            try:
                cfg = self.agent.config
                # 类型安全转换
                int_keys = {"max_iterations", "context_length"}
                float_keys = {"compression_threshold"}
                bool_keys = {"compression_enabled", "streaming_enabled"}
                if key in int_keys:
                    value = int(value)
                elif key in float_keys:
                    value = float(value)
                elif key in bool_keys:
                    value = value.lower() in ("true", "1", "yes", "on")
                # 特殊处理：model/provider 需要同时更新 agent 属性
                if key == "model":
                    cfg.model = value
                    self.agent.model = value
                    self.agent._cached_system_prompt = None
                elif key == "provider":
                    cfg.provider = value
                    self.agent.provider = value
                else:
                    setattr(cfg, key, value)
                return {"key": key, "value": value, "message": f"已设置 {key} = {value}"}
            except Exception as exc:
                return {"error": str(exc)}
        reg("set_config", cmd_set_config)

        async def cmd_get_config_value(client, data):
            """获取单个配置值。"""
            key = data.get("key", "")
            if not key:
                return {"error": "缺少 key 参数"}
            if self.agent is None:
                return {"error": "Agent 未连接"}
            try:
                cfg = self.agent.config
                value = getattr(cfg, key, None)
                if value is None and hasattr(self.agent, key):
                    value = getattr(self.agent, key, None)
                return {"key": key, "value": str(value) if value is not None else "未设置"}
            except Exception as exc:
                return {"error": str(exc)}
        reg("get_config_value", cmd_get_config_value)

        # ── 会话管理 ──────────────────────────────────────────

        async def cmd_new_session(client, data):
            """开始新会话。"""
            if self.agent is None:
                return {"error": "Agent 未连接"}
            try:
                self.agent.reset_session()
                return {"message": "新会话已开始", "session_id": self.agent.session_id}
            except Exception as exc:
                return {"error": str(exc)}
        reg("new_session", cmd_new_session)

        async def cmd_get_history(client, data):
            """获取对话历史。"""
            if self.agent is None:
                return {"messages": [], "error": "Agent 未连接"}
            try:
                count = int(data.get("count", 10))
                msgs = self.agent.messages
                # 取最近 N 条（排除 system 消息）
                user_assistant = [m for m in msgs if m.get("role") in ("user", "assistant")]
                recent = user_assistant[-count:] if len(user_assistant) > count else user_assistant
                result = []
                for m in recent:
                    content = m.get("content", "")
                    if isinstance(content, list):
                        # 多模态消息，提取文本
                        text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                        content = " ".join(text_parts)
                    result.append({"role": m.get("role", ""), "content": str(content)[:500]})
                return {"messages": result, "total": len(user_assistant)}
            except Exception as exc:
                return {"messages": [], "error": str(exc)}
        reg("get_history", cmd_get_history)

        async def cmd_compress_context(client, data):
            """压缩对话上下文。"""
            if self.agent is None:
                return {"error": "Agent 未连接"}
            try:
                compressor = self.agent.context_compressor
                if compressor is None:
                    return {"message": "上下文压缩未启用"}
                # 估算当前 token 数（粗略：字符数 / 4）
                messages = self.agent.messages
                if len(messages) < 4:
                    return {"message": "对话太短，无需压缩"}
                estimated_tokens = sum(
                    len(str(m.get("content", ""))) for m in messages
                ) // 4
                compressed = compressor.compress(messages, estimated_tokens)
                if compressed and len(compressed) < len(messages):
                    self.agent._messages = compressed
                    return {"message": f"已压缩: {len(messages)} → {len(compressed)} 条消息"}
                return {"message": "压缩未触发（未达到阈值）"}
            except Exception as exc:
                return {"error": str(exc)}
        reg("compress_context", cmd_compress_context)

        # ── 目标系统（Ralph Loop — 持久目标自主循环，Phase 4）──────

        async def cmd_goal(client, data):
            """目标状态管理（不驱动循环，立即返回）。

            data: {"args": "</goal 之后的整段参数>"}，如 "status" / "pause" /
            "Migrate auth to JWT"。设完/恢复目标时结果带 ``kick_off``，前端可据此
            发下一条 chat 或调 ``goal_run`` 驱动自主循环。
            """
            if self.agent is None:
                return {"ok": False, "error": "Agent 未连接"}
            try:
                from spirit.goals import handle_goal_command
                args = data.get("args", "") or data.get("command", "")
                return handle_goal_command(self.agent, args)
            except Exception as exc:
                logger.warning("goal 命令失败: %s", exc)
                return {"ok": False, "error": str(exc)}
        reg("goal", cmd_goal)

        async def cmd_subgoal(client, data):
            """子目标管理。data: {"args": "<text> | remove <n> | clear"}。"""
            if self.agent is None:
                return {"ok": False, "error": "Agent 未连接"}
            try:
                from spirit.goals import handle_subgoal_command
                args = data.get("args", "") or data.get("command", "")
                return handle_subgoal_command(self.agent, args)
            except Exception as exc:
                logger.warning("subgoal 命令失败: %s", exc)
                return {"ok": False, "error": str(exc)}
        reg("subgoal", cmd_subgoal)

        async def cmd_profile(client, data):
            """多实例 Profile 隔离管理（/profile 子命令，Phase 4.2）。

            data: {"args": "</profile 之后的整段参数>"}，如 "list" / "current" /
            "create work --clone-config" / "use work" / "delete work" /
            "rename work job" / "describe work" / "info work"。多为本地快速操作；
            describe 走一次辅助 LLM side call，故在 executor 中运行不卡事件循环。
            结果 dict 带 ok/action/message/lines/active/restart_hint，前端自行渲染。
            """
            args = data.get("args", "") or data.get("command", "")
            from spirit.config import get_config_value

            loop = asyncio.get_event_loop()

            def _run():
                from spirit.profile import handle_profile_command
                return handle_profile_command(self.agent, args)

            timeout = float(get_config_value("timeouts.chat_request", 300.0))
            try:
                future = loop.run_in_executor(None, _run)
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("profile 命令超时 (%.0f 秒)", timeout)
                return {"ok": False, "error": f"profile 命令超时（{timeout:.0f} 秒）", "timeout": True}
            except Exception as exc:
                logger.warning("profile 命令失败: %s", exc)
                return {"ok": False, "error": str(exc)}
        reg("profile", cmd_profile)

        async def cmd_moa(client, data):
            """MoA（Mixture of Agents）预设管理 + 一次性 prompt。

            data: {"args": "</moa 之后的整段参数>"}，如 "list" / "use review" /
            "off" / "帮我审这段代码"（一次性）。list/use/off 为纯状态流转；一次性
            模式会阻塞跑一轮 MoA（多参考 + 聚合器），故在 executor 中运行（与
            cmd_goal_run 同构），不卡事件循环。结果 dict 带 ok/action/message/lines/
            status_line/response（一次性），前端自行渲染。
            """
            if self.agent is None:
                return {"ok": False, "error": "Agent 未连接"}
            from spirit.config import get_config_value

            args = data.get("args", "") or data.get("command", "")
            loop = asyncio.get_event_loop()

            def _run():
                from spirit.moa import handle_moa_command
                return handle_moa_command(self.agent, args)

            timeout = float(get_config_value("timeouts.chat_request", 300.0))
            try:
                future = loop.run_in_executor(None, _run)
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("moa 命令超时 (%.0f 秒)，中断 Agent", timeout)
                if self.agent:
                    self.agent.interrupt()
                return {"ok": False, "error": f"MoA 命令超时（{timeout:.0f} 秒），已中断", "timeout": True}
            except Exception as exc:
                logger.warning("moa 命令失败: %s", exc)
                return {"ok": False, "error": str(exc)}
        reg("moa", cmd_moa)

        async def cmd_skill(client, data):
            """技能中心管理（/skill 子命令）。

            data: {"args": "</skill 之后的整段参数>"}，如 "list" / "reload" /
            "browse <query>" / "install <id>" / "uninstall <name>" / "scan [name]" /
            "bundles" / "info <name>" / "audit"。多为本地快速操作；install 可能触网，
            故在 executor 中运行，不卡事件循环。结果 dict 带 ok/action/message/lines/
            status_line，前端自行渲染。技能调用（/<skill-name>）走 chat 路径，不在此。
            """
            args = data.get("args", "") or data.get("command", "")
            loop = asyncio.get_event_loop()

            def _run():
                from spirit.skills_hub import handle_skill_command
                return handle_skill_command(self.agent, args)

            from spirit.config import get_config_value
            timeout = float(get_config_value("timeouts.chat_request", 300.0))
            try:
                future = loop.run_in_executor(None, _run)
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("skill 命令超时 (%.0f 秒)", timeout)
                return {"ok": False, "error": f"技能命令超时（{timeout:.0f} 秒）", "timeout": True}
            except Exception as exc:
                logger.warning("skill 命令失败: %s", exc)
                return {"ok": False, "error": str(exc)}
        reg("skill", cmd_skill)

        async def cmd_goal_run(client, data):
            """驱动 Ralph Loop：跑第一轮 + 自主续传直到 done/paused/waiting/预算耗尽。

            data: {"input": "<触发轮输入，缺省用当前目标文本>"}。每轮响应通过
            ``goal_turn`` 事件实时广播；循环结束后返回 outcome 汇总。在 executor 中
            运行（阻塞式 agent.chat），与 cmd_chat 同构，不卡事件循环。
            """
            if self.agent is None:
                return {"ok": False, "error": "Agent 未连接"}
            from spirit.config import get_config_value
            # 目标循环可能跑很多轮 —— 用 chat 超时 × 预算上限作为总兜底。
            per_turn = float(get_config_value("timeouts.chat_request", 300.0))
            budget = int(get_config_value("goals.max_turns", 20))
            total_timeout = per_turn * max(1, budget)

            loop = asyncio.get_event_loop()

            def _broadcast_turn(text, idx):
                try:
                    asyncio.run_coroutine_threadsafe(
                        client.send_event("goal_turn", {"turn": idx, "response": text}), loop
                    )
                except Exception:
                    pass

            def _broadcast_decision(decision):
                try:
                    asyncio.run_coroutine_threadsafe(
                        client.send_event("goal_decision", {
                            "verdict": decision.get("verdict", ""),
                            "status": decision.get("status", ""),
                            "message": decision.get("message", ""),
                        }), loop
                    )
                except Exception:
                    pass

            def _drive():
                from spirit.goals import run_goal_turn_loop
                first_input = data.get("input", "")
                if not first_input:
                    mgr = getattr(self.agent, "goal_manager", None)
                    first_input = mgr.state.goal if (mgr and mgr.state) else ""
                return run_goal_turn_loop(
                    self.agent,
                    first_input,
                    on_turn=_broadcast_turn,
                    on_decision=_broadcast_decision,
                )

            try:
                future = loop.run_in_executor(None, _drive)
                result = await asyncio.wait_for(future, timeout=total_timeout)
                return {"ok": True, **result}
            except asyncio.TimeoutError:
                logger.warning("goal_run 超时 (%.0f 秒)，中断 Agent", total_timeout)
                if self.agent:
                    self.agent.interrupt()
                return {"ok": False, "error": f"目标循环超时（{total_timeout:.0f} 秒），已中断", "timeout": True}
            except Exception as exc:
                logger.warning("goal_run 失败: %s", exc)
                return {"ok": False, "error": str(exc)}
        reg("goal_run", cmd_goal_run)

        # ── 工具与信息 ────────────────────────────────────────

        async def cmd_list_tools(client, data):
            """列出可用工具。"""
            if self.agent is None:
                return {"tools": [], "error": "Agent 未连接"}
            try:
                tool_defs = self.agent.get_tool_definitions()
                tools = [{"name": t.get("name", ""), "description": t.get("description", "")}
                         for t in tool_defs]
                return {"tools": tools, "count": len(tools)}
            except Exception as exc:
                return {"tools": [], "error": str(exc)}
        reg("list_tools", cmd_list_tools)

        async def cmd_get_version(client, data):
            """返回版本信息。"""
            import sys
            import platform
            return {
                "version": "0.1.0",
                "python": platform.python_version(),
                "platform": platform.system(),
                "arch": platform.machine(),
            }
        reg("get_version", cmd_get_version)

        # ── 心跳 ──────────────────────────────────────────────

        async def cmd_ping(client, data):
            return {"pong": True, "timestamp": time.time()}
        reg("ping", cmd_ping)


# ---------------------------------------------------------------------------
# 便捷启动
# ---------------------------------------------------------------------------

async def run_ws_server(
    engine: PetEngine = None,
    *,
    host: str = "127.0.0.1",
    port: int = 9877,
    agent=None,
) -> WSServer:
    """创建并启动 WebSocket 服务器。

    返回运行中的 WSServer 实例。
    """
    if engine is None:
        engine = PetEngine()
    server = WSServer(engine, host=host, port=port, agent=agent)
    await server.start()
    return server


__all__ = [
    "WSServer",
    "WSClient",
    "CommandRegistry",
    "run_ws_server",
]
