"""网关生命周期管理。

参考 Hermes 的 gateway/run.py：
- GatewayRunner: 管理所有平台适配器的启动/停止
- Agent 缓存管理（LRU + 空闲 TTL）
- 优雅关闭
- 状态监控
"""

import asyncio
import logging
import signal
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from spirit.gateway.config import GatewayConfig, Platform, PlatformConfig, load_gateway_config
from spirit.gateway.session import SessionContext, SessionSource, SessionStore, build_session_context_prompt
from spirit.gateway.platform_registry import PlatformRegistry, platform_registry
from spirit.gateway.delivery import DeliveryRouter, DeliveryResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 网关状态
# ---------------------------------------------------------------------------

class GatewayState(str, Enum):
    """网关运行状态。"""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Agent 缓存
# ---------------------------------------------------------------------------

class AgentCache:
    """Agent 实例缓存 — LRU + 空闲 TTL。

    参考 Hermes 的 agent cache 机制：
    - 每个会话一个 Agent 实例
    - LRU 淘汰防止无限增长
    - 空闲超时自动清理
    """

    def __init__(self, max_size: int = 32, idle_ttl_seconds: float = 3600.0):
        self.max_size = max_size
        self.idle_ttl_seconds = idle_ttl_seconds

        # session_id -> (agent, last_access_time)
        self._cache: OrderedDict[str, tuple] = OrderedDict()

    def get(self, session_id: str) -> Optional[Any]:
        """获取缓存的 Agent。"""
        if session_id not in self._cache:
            return None

        agent, last_access = self._cache[session_id]

        # 检查 TTL
        if time.time() - last_access > self.idle_ttl_seconds:
            self._evict(session_id)
            return None

        # 移动到末尾（最近使用）
        self._cache.move_to_end(session_id)
        self._cache[session_id] = (agent, time.time())
        return agent

    def put(self, session_id: str, agent: Any):
        """缓存 Agent。"""
        if session_id in self._cache:
            self._cache.move_to_end(session_id)
        self._cache[session_id] = (agent, time.time())

        # LRU 淘汰
        while len(self._cache) > self.max_size:
            evicted_id, _ = self._cache.popitem(last=False)
            logger.debug("Agent 缓存 LRU 淘汰: %s", evicted_id[:8])

    def remove(self, session_id: str) -> Optional[Any]:
        """移除缓存的 Agent。"""
        entry = self._cache.pop(session_id, None)
        if entry:
            return entry[0]
        return None

    def _evict(self, session_id: str):
        """淘汰指定会话。"""
        self._cache.pop(session_id, None)

    def clear(self):
        """清空缓存。"""
        self._cache.clear()

    def cleanup_expired(self) -> List[str]:
        """清理过期条目，返回被清理的 session_id 列表。"""
        now = time.time()
        expired = [
            sid for sid, (_, last_access) in self._cache.items()
            if now - last_access > self.idle_ttl_seconds
        ]
        for sid in expired:
            self._evict(sid)
        return expired

    @property
    def size(self) -> int:
        return len(self._cache)

    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计。"""
        return {
            "size": self.size,
            "max_size": self.max_size,
            "idle_ttl_seconds": self.idle_ttl_seconds,
        }


# ---------------------------------------------------------------------------
# 网关运行器
# ---------------------------------------------------------------------------

class GatewayRunner:
    """网关生命周期管理器。

    参考 Hermes 的 GatewayRunner：
    - 启动/停止所有已配置的平台适配器
    - 管理 Agent 缓存
    - 处理消息路由
    - 优雅关闭

    Usage:
        runner = GatewayRunner(config)
        await runner.start()
        # ... 处理消息 ...
        await runner.stop()
    """

    def __init__(
        self,
        config: GatewayConfig = None,
        registry: PlatformRegistry = None,
    ):
        """初始化网关运行器。

        Args:
            config: 网关配置（None=从默认路径加载）
            registry: 平台注册表（None=使用全局注册表）
        """
        self.config = config or load_gateway_config()
        self.registry = registry or platform_registry

        # 状态
        self._state = GatewayState.STOPPED
        self._start_time: Optional[datetime] = None

        # 组件
        self.session_store = SessionStore(
            max_size=self.config.agent_cache_size,
            idle_ttl_seconds=self.config.agent_idle_ttl_seconds,
            persist_dir=self.config.sessions_dir,
        )
        self.agent_cache = AgentCache(
            max_size=self.config.agent_cache_size,
            idle_ttl_seconds=self.config.agent_idle_ttl_seconds,
        )
        self.delivery_router = DeliveryRouter(self.config)

        # 活跃适配器
        self._adapters: Dict[Platform, Any] = {}

        # 后台任务
        self._tasks: List[asyncio.Task] = []
        self._cleanup_task: Optional[asyncio.Task] = None

        # 消息处理回调（由外部设置）
        self._on_message: Optional[Callable] = None
        self._on_status: Optional[Callable] = None

    @property
    def state(self) -> GatewayState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state == GatewayState.RUNNING

    def set_message_handler(self, handler: Callable):
        """设置消息处理回调。

        回调签名: async def handler(source: SessionSource, message: str, context: SessionContext) -> str
        """
        self._on_message = handler

    def set_status_handler(self, handler: Callable):
        """设置状态通知回调。"""
        self._on_status = handler

    async def start(self) -> bool:
        """启动网关 — 连接所有已配置的平台。

        Returns:
            True 如果至少有一个平台成功连接
        """
        if self._state == GatewayState.RUNNING:
            logger.warning("网关已在运行中")
            return True

        self._state = GatewayState.STARTING
        logger.info("正在启动网关...")

        connected_count = 0

        for platform in self.config.get_enabled_platforms():
            try:
                platform_config = self.config.get_platform_config(platform)
                adapter = self.registry.create_adapter(platform, platform_config)

                # 连接
                if hasattr(adapter, "connect"):
                    success = await adapter.connect()
                    if not success:
                        logger.error("平台 %s 连接失败", platform.value)
                        continue

                self._adapters[platform] = adapter
                self.delivery_router.set_adapter(platform, adapter)
                connected_count += 1
                logger.info("平台 %s 已连接", platform.value)

            except Exception as e:
                logger.error("启动平台 %s 失败: %s", platform.value, e)

        if connected_count == 0:
            self._state = GatewayState.ERROR
            logger.error("没有平台成功连接")
            return False

        # 启动后台清理任务
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())

        self._state = GatewayState.RUNNING
        self._start_time = datetime.now()
        logger.info("网关已启动，%d 个平台已连接", connected_count)
        return True

    async def stop(self):
        """停止网关 — 断开所有平台连接。"""
        if self._state != GatewayState.RUNNING:
            return

        self._state = GatewayState.STOPPING
        logger.info("正在停止网关...")

        # 取消后台任务
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        for task in self._tasks:
            task.cancel()

        # 断开所有适配器
        for platform, adapter in self._adapters.items():
            try:
                if hasattr(adapter, "disconnect"):
                    timeout = self.config.graceful_shutdown_timeout
                    await asyncio.wait_for(adapter.disconnect(), timeout=timeout)
                logger.info("平台 %s 已断开", platform.value)
            except asyncio.TimeoutError:
                logger.warning("平台 %s 断开超时", platform.value)
            except Exception as e:
                logger.error("断开平台 %s 失败: %s", platform.value, e)

        self._adapters.clear()
        self.agent_cache.clear()

        self._state = GatewayState.STOPPED
        logger.info("网关已停止")

    async def handle_message(
        self,
        source: SessionSource,
        message: str,
    ) -> str:
        """处理来自平台的消息。

        这是网关的核心入口点：
        1. 获取/创建会话上下文
        2. 获取/创建 Agent
        3. 调用 Agent 处理消息
        4. 返回响应

        Args:
            source: 消息来源
            message: 用户消息

        Returns:
            Agent 响应文本
        """
        if not self.is_running:
            return "网关未运行"

        # 获取平台配置
        platform_config = self.config.get_platform_config(source.platform)

        # 用户权限检查
        if platform_config and not platform_config.is_user_allowed(source.user_id):
            return "您没有被授权使用此服务。"

        # 获取/创建会话
        ctx = self.session_store.get_or_create(source, platform_config)

        # 获取/创建 Agent
        agent = self._get_or_create_agent(ctx)

        # 注入会话上下文到系统提示词
        context_prompt = build_session_context_prompt(
            source=source,
            session_id=ctx.session_id,
        )
        agent.set_ephemeral_system_prompt(context_prompt)

        # 调用消息处理器
        if self._on_message:
            try:
                response = await self._on_message(source, message, ctx)
            except Exception as e:
                logger.error("消息处理失败: %s", e)
                response = f"处理消息时出错: {e}"
        else:
            # 默认处理：直接调用 Agent
            try:
                response = agent.chat(message)
            except Exception as e:
                logger.error("Agent 调用失败: %s", e)
                response = f"Agent 调用失败: {e}"

        # 更新会话统计
        ctx.increment_message(estimated_tokens=len(message) // 4 + len(response) // 4)

        return response

    def _get_or_create_agent(self, ctx: SessionContext) -> Any:
        """获取或创建 Agent 实例。"""
        agent = self.agent_cache.get(ctx.session_id)
        if agent is not None:
            return agent

        # 创建新 Agent
        from spirit.agent.agent import SpiritAgent, AgentConfig

        platform_config = self.config.get_platform_config(ctx.source.platform)
        config = AgentConfig(
            session_id=ctx.session_id,
            platform=ctx.source.platform.value,
        )

        agent = SpiritAgent(config)
        self.agent_cache.put(ctx.session_id, agent)
        return agent

    async def _periodic_cleanup(self):
        """定期清理过期会话和 Agent 缓存。"""
        while self.is_running:
            try:
                from spirit.config import get_config_value
                await asyncio.sleep(get_config_value("gateway.cleanup_interval", 300))

                # 清理过期 Agent
                expired = self.agent_cache.cleanup_expired()
                if expired:
                    logger.debug("清理了 %d 个过期 Agent", len(expired))

                # 清理过期会话
                self.session_store.clear_expired()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("清理任务出错: %s", e)

    def get_status(self) -> Dict[str, Any]:
        """获取网关状态摘要。"""
        uptime = None
        if self._start_time:
            uptime_seconds = (datetime.now() - self._start_time).total_seconds()
            uptime = f"{int(uptime_seconds // 3600)}h {(uptime_seconds % 3600) // 60:.0f}m"

        return {
            "state": self._state.value,
            "uptime": uptime,
            "connected_platforms": [p.value for p in self._adapters.keys()],
            "active_sessions": self.session_store.size,
            "cached_agents": self.agent_cache.size,
            "dead_targets": self.delivery_router.dead_targets,
        }
