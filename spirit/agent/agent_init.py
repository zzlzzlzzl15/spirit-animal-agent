"""Agent 初始化 — SpiritAgent 完整初始化流程。

参考 Hermes 的 agent_init.py (2200行)，实现：
- 配置加载与验证（委托给 spirit.config 集中式配置系统）
- 模块初始化（钩子、记忆、任务、Transport）
- 工具注册
- 会话恢复
- 健康检查

初始化流程：
1. 加载配置（spirit.config.load_config → YAML + 环境变量 + 参数）
2. 初始化钩子管理器
3. 初始化记忆管理器
4. 初始化任务管理器
5. 创建 Transport
6. 注册工具
7. 恢复会话（可选）
8. 触发初始化钩子
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from spirit.agent.agent import AgentConfig, SpiritAgent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 配置加载（委托给 spirit.config）
# ---------------------------------------------------------------------------

def load_config(
    config_path: str = None,
    **overrides,
) -> AgentConfig:
    """加载 Agent 配置。

    委托给 spirit.config.load_config() 集中式配置系统。
    优先级：参数 > 环境变量 > YAML 文件 > DEFAULT_CONFIG

    Args:
        config_path: 配置文件路径（默认 ~/.spirit/config.yaml）
        **overrides: 参数覆盖

    Returns:
        AgentConfig 对象
    """
    from spirit.config import load_config as _load_config

    raw_config = _load_config(config_path=config_path, **overrides)
    return AgentConfig.from_dict(raw_config)


# ---------------------------------------------------------------------------
# Agent 初始化
# ---------------------------------------------------------------------------

def initialize_agent(
    config: AgentConfig = None,
    config_path: str = None,
    enable_hooks: bool = True,
    enable_memory: bool = True,
    enable_tasks: bool = True,
    enable_transport: bool = True,
    resume_session: str = None,
    **kwargs,
) -> SpiritAgent:
    """完整初始化 SpiritAgent。

    Args:
        config: 配置对象（不提供则自动加载）
        config_path: 配置文件路径
        enable_hooks: 启用钩子系统
        enable_memory: 启用记忆管理器
        enable_tasks: 启用任务管理器
        enable_transport: 启用 Transport
        resume_session: 恢复的会话 ID
        **kwargs: 覆盖配置

    Returns:
        初始化完成的 SpiritAgent
    """
    # 1. 加载配置
    if config is None:
        config = load_config(config_path=config_path, **kwargs)

    logger.info("初始化 SpiritAgent: model=%s, provider=%s", config.model, config.provider)

    # 2. 创建 Agent
    agent = SpiritAgent(config=config)

    # 3. 初始化钩子系统
    if enable_hooks:
        _init_hooks(agent)

    # 4. 初始化记忆管理器
    if enable_memory:
        _init_memory(agent)

    # 5. 初始化任务管理器
    if enable_tasks:
        _init_tasks(agent)

    # 6. 初始化 Transport
    if enable_transport:
        _init_transport(agent)

    # 7. 恢复会话
    if resume_session:
        _resume_session(agent, resume_session)

    # 8. 触发初始化钩子
    if enable_hooks and hasattr(agent, "hook_manager"):
        agent.hook_manager.emit(
            "on_agent_init",
            model=agent.model,
            provider=agent.provider,
            session_id=agent.session_id,
        )

    logger.info("SpiritAgent 初始化完成: session=%s", agent.session_id[:8])
    return agent


def _init_hooks(agent: SpiritAgent) -> None:
    """初始化钩子系统。"""
    from spirit.hooks.hook_manager import HookManager
    from spirit.hooks.lifecycle_hooks import register_lifecycle_hooks
    from spirit.hooks.tool_hooks import register_tool_hooks
    from spirit.hooks.task_hooks import register_task_hooks
    from spirit.hooks.conversation_hooks import register_conversation_hooks

    hook_manager = HookManager()
    agent.hook_manager = hook_manager

    # 注册默认钩子
    register_lifecycle_hooks(hook_manager, agent)
    register_tool_hooks(hook_manager, agent)
    register_task_hooks(hook_manager, agent)
    register_conversation_hooks(hook_manager, agent)

    logger.debug("钩子系统已初始化")


def _init_memory(agent: SpiritAgent) -> None:
    """初始化记忆管理器。"""
    from spirit.agent.memory_manager import MemoryManager

    memory_manager = MemoryManager()
    agent.memory_manager = memory_manager

    logger.debug("记忆管理器已初始化: %s", memory_manager.db_path)


def _init_tasks(agent: SpiritAgent) -> None:
    """初始化任务管理器。"""
    from spirit.task.manager import TaskManager

    hook_manager = getattr(agent, "hook_manager", None)
    task_manager = TaskManager(hook_manager=hook_manager)
    agent.task_manager = task_manager

    logger.debug("任务管理器已初始化: %s", task_manager.db_path)


def _init_transport(agent: SpiritAgent) -> None:
    """初始化 Transport。"""
    try:
        from spirit.agent.transports import TransportConfig, create_transport

        transport_config = TransportConfig(
            provider=agent.provider,
            api_key=agent.api_key,
            base_url=agent.base_url,
            model=agent.model,
        )
        transport = create_transport(transport_config)
        agent.transport = transport

        logger.debug("Transport 已初始化: %s", transport.provider_name)
    except Exception as e:
        logger.warning("Transport 初始化失败: %s（将使用默认客户端）", e)


def _resume_session(agent: SpiritAgent, session_id: str) -> None:
    """恢复会话。"""
    session_db = getattr(agent, "_session_db", None)
    if session_db:
        try:
            history = session_db.get_messages(session_id)
            if history:
                for msg in history:
                    agent.add_message(msg["role"], msg["content"])
                agent.session_id = session_id
                logger.info("会话已恢复: %s (%d 条消息)", session_id[:8], len(history))
        except Exception as e:
            logger.warning("会话恢复失败: %s", e)


# ---------------------------------------------------------------------------
# 快速初始化
# ---------------------------------------------------------------------------

def quick_init(
    model: str = None,
    api_key: str = None,
    provider: str = None,
    **kwargs,
) -> SpiritAgent:
    """快速初始化 Agent（用于测试和简单场景）。

    不提供参数时自动从配置文件和环境变量加载。

    Args:
        model: 模型名称（不提供则从配置加载）
        api_key: API 密钥（不提供则从环境变量读取）
        provider: Provider 名称（不提供则从配置加载）
        **kwargs: 其他配置

    Returns:
        初始化完成的 SpiritAgent
    """
    # 构建显式覆盖（仅传入了值的参数）
    overrides = {}
    if model:
        overrides["model"] = model
    if api_key:
        overrides["api_key"] = api_key
    if provider:
        overrides["provider"] = provider
    overrides.update(kwargs)

    # 通过 load_config 自动加载 YAML + 环境变量 + 覆盖
    config = load_config(**overrides)

    return initialize_agent(config=config)


__all__ = [
    "load_config",
    "initialize_agent",
    "quick_init",
]
