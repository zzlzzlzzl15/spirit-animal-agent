"""SpiritAgent — 核心 Agent 类（完整智能版）。

参考 Hermes 的 AIAgent 转发器架构设计：
- 状态容器：持有所有核心状态
- 调度中心：将具体逻辑委托给子模块
- 延迟导入策略加快启动速度

集成的智能模块：
- error_handler: 错误分类 + 自适应重试
- tool_guardrails: 工具调用护栏（防死循环）
- context_compressor: 上下文自动压缩
- streaming: 流式输出
- prompt_builder: 动态系统提示词
- tool_executor: 并发工具执行
- iteration_budget: 迭代预算控制
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 配置数据类
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    """Agent 配置。

    默认值全部留空/安全默认 — 实际配置通过以下优先级加载：
    1. 参数覆盖（initialize_agent 的 kwargs）
    2. 环境变量（SPIRIT_MODEL / SPIRIT_PROVIDER 等）
    3. YAML 配置文件（~/.spirit/config.yaml）
    4. DEFAULT_CONFIG（spirit/config.py）

    不要在此处硬编码具体的模型/URL — 那是配置文件的职责。
    """

    # LLM 连接（留空 — 由配置文件或环境变量提供）
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    provider: str = "auto"

    # 工具
    enabled_toolsets: Optional[List[str]] = None
    disabled_toolsets: Optional[List[str]] = None

    # 控制
    max_iterations: int = 90
    system_prompt: str = ""

    # 会话
    session_id: str = ""
    platform: str = "cli"

    # 上下文压缩
    compression_enabled: bool = True
    context_length: int = 128_000
    compression_threshold: float = 0.75

    # 流式输出
    streaming_enabled: bool = False

    # Prompt caching（Anthropic 风格 cache_control）
    prompt_caching_enabled: bool = True   # 是否启用 prompt caching
    cache_ttl: str = "5m"                 # 缓存有效期: '5m' 或 '1h'

    # 回调（事件总线简化版）
    on_tool_start: Optional[Callable] = None
    on_tool_complete: Optional[Callable] = None
    on_stream_delta: Optional[Callable] = None
    on_status: Optional[Callable] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentConfig":
        """从配置字典创建 AgentConfig。

        支持嵌套格式（如 {"llm": {"model": "gpt-4o"}}）
        和扁平格式（如 {"model": "gpt-4o"}）。
        """
        config = cls()

        # 嵌套格式: llm.agent
        llm = data.get("llm", {})
        if isinstance(llm, dict):
            if llm.get("model"):
                config.model = llm["model"]
            if llm.get("api_key"):
                config.api_key = llm["api_key"]
            if llm.get("base_url"):
                config.base_url = llm["base_url"]
            if llm.get("provider"):
                config.provider = llm["provider"]
            if llm.get("context_length"):
                config.context_length = int(llm["context_length"])

        # 嵌套格式: agent.*
        agent_cfg = data.get("agent", {})
        if isinstance(agent_cfg, dict):
            if agent_cfg.get("max_iterations"):
                config.max_iterations = int(agent_cfg["max_iterations"])

        # 嵌套格式: compression.*
        comp = data.get("compression", {})
        if isinstance(comp, dict):
            if "enabled" in comp:
                config.compression_enabled = bool(comp["enabled"])
            if comp.get("threshold"):
                config.compression_threshold = float(comp["threshold"])

        # 嵌套格式: streaming.*
        stream = data.get("streaming", {})
        if isinstance(stream, dict):
            if "enabled" in stream:
                config.streaming_enabled = bool(stream["enabled"])

        # 嵌套格式: prompt_caching.*
        pc = data.get("prompt_caching", {})
        if isinstance(pc, dict):
            if "enabled" in pc:
                config.prompt_caching_enabled = bool(pc["enabled"])
            if pc.get("ttl"):
                config.cache_ttl = str(pc["ttl"])

        # 扁平格式 兼容（直接 model=xxx）
        for key in ("model", "api_key", "base_url", "provider",
                     "max_iterations", "session_id", "platform",
                     "system_prompt", "context_length", "compression_enabled",
                     "compression_threshold", "streaming_enabled"):
            if key in data and data[key] is not None:
                setattr(config, key, data[key])

        return config


# ---------------------------------------------------------------------------
# 对话结果
# ---------------------------------------------------------------------------

@dataclass
class ConversationResult:
    """对话循环的结果。"""

    response: str
    messages: List[Dict[str, Any]]
    usage: Dict[str, int] = field(default_factory=dict)
    iterations: int = 0
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)  # 本轮的工具调用列表


# ---------------------------------------------------------------------------
# SpiritAgent 类
# ---------------------------------------------------------------------------

class SpiritAgent:
    """Spirit Agent 核心 — 状态容器 + 调度中心。

    设计参考 Hermes 的 AIAgent：
    - 持有所有状态（client, tools, messages, session...）
    - 大部分方法委托给子模块
    - 延迟导入策略加快启动速度
    """

    def __init__(self, config: AgentConfig = None, **kwargs):
        """初始化 Agent。

        Args:
            config: Agent 配置对象
            **kwargs: 覆盖 config 中的字段
        """
        # 合并配置
        if config is None:
            config = AgentConfig()
        for key, value in kwargs.items():
            if hasattr(config, key):
                setattr(config, key, value)
        self.config = config

        # 核心状态
        self.model = config.model
        self.api_key = config.api_key
        self.base_url = config.base_url
        self.provider = config.provider
        self.max_iterations = config.max_iterations
        self.session_id = config.session_id or str(uuid.uuid4())
        self.platform = config.platform

        # 工具集过滤
        self.enabled_toolsets = config.enabled_toolsets
        self.disabled_toolsets = config.disabled_toolsets

        # 回调
        self.on_tool_start = config.on_tool_start
        self.on_tool_complete = config.on_tool_complete
        self.on_stream_delta = config.on_stream_delta
        self.on_status = config.on_status

        # 运行时状态
        self._interrupt_requested = False
        self._api_call_count = 0
        self._messages: List[Dict[str, Any]] = []

        # 工作区上下文（由 VSCode 扩展推送，CLI 模式下为 None）
        self.workspace_context: Any = None

        # OpenAI 客户端（延迟初始化）
        self._client = None

        # 工具注册表（延迟初始化）
        self._registry = None

        # 会话数据库（延迟初始化）
        self._session_db = None
        
        # 自动初始化会话数据库
        try:
            from spirit.storage.session_db import SessionDB
            self._session_db = SessionDB()
            logger.info("会话数据库已初始化")
        except Exception as e:
            logger.warning("会话数据库初始化失败 (非致命): %s", e)

        # ── 智能模块 ────────────────────────────────────────────

        # 上下文压缩器
        self._context_compressor = None
        if config.compression_enabled:
            from spirit.agent.context_compressor import ContextCompressor
            self._context_compressor = ContextCompressor(
                context_length=config.context_length,
                threshold_percent=config.compression_threshold,
            )

        # 缓存的系统提示词
        self._cached_system_prompt: Optional[str] = None

        # Prompt caching 配置
        self._use_prompt_caching = config.prompt_caching_enabled
        self._cache_ttl = config.cache_ttl

        # 临时系统提示词
        self._ephemeral_system_prompt = None

        # 额外 API 参数（provider 特定）
        self._extra_api_params: Dict[str, Any] = {}

        logger.info(
            "SpiritAgent 初始化: model=%s, session=%s, compression=%s, prompt_cache=%s",
            self.model,
            self.session_id[:8],
            "on" if self._context_compressor else "off",
            "on" if self._use_prompt_caching else "off",
        )

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def client(self):
        """OpenAI 兼容客户端（延迟初始化）。"""
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
            )
        return self._client

    @property
    def registry(self):
        """工具注册表（延迟初始化 + 自动发现）。"""
        if self._registry is None:
            from spirit.tools.registry import registry, discover_tools
            discover_tools()  # 触发工具自注册
            self._registry = registry
        return self._registry

    @property
    def messages(self) -> List[Dict[str, Any]]:
        """当前对话消息列表。"""
        return self._messages

    @property
    def context_compressor(self):
        """上下文压缩器。"""
        return self._context_compressor

    # ------------------------------------------------------------------
    # 核心方法
    # ------------------------------------------------------------------

    def run_conversation(self, user_message: str, **kwargs) -> ConversationResult:
        """执行对话循环 — 委托给 conversation_loop。

        集成了：
        - 错误分类 + 自适应重试
        - 工具调用护栏
        - 上下文自动压缩
        - 并发工具执行
        - 迭代预算控制
        - TurnContext per-turn 初始化
        """
        from spirit.agent.conversation_loop import run_conversation
        return run_conversation(self, user_message, **kwargs)

    def prepare_turn_context(self, user_message: str):
        """准备本轮对话的上下文（per-turn initialization）。
        
        参考 Hermes 的 build_turn_context()，执行以下操作：
        1. 消息清理与消毒
        2. 会话恢复检查
        3. 工作区上下文更新
        4. 初始化工具懒加载标志
        
        Args:
            user_message: 用户输入消息
            
        Returns:
            TurnContext: 包含本轮所需的所有上下文数据
        """
        from spirit.agent.turn_context import build_turn_context
        
        turn_ctx = build_turn_context(
            agent=self,
            user_message=user_message,
            system_message=None,
            conversation_history=None,
            task_id=None,
        )
        
        logger.debug(
            "Turn context prepared: session=%s, messages=%d",
            self.session_id[:8],
            len(turn_ctx.messages),
        )
        
        return turn_ctx

    def chat(
        self,
        message: str,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """对话接口 — 返回完整响应（含工具调用）。

        Args:
            message: 用户消息
            stream_callback: 可选的流式文本增量回调（集成在主循环内）

        Returns:
            dict: {
                'response': str - 最终文本响应,
                'tool_calls': list - 本轮的工具调用列表,
                'usage': dict - token 使用统计,
                'iterations': int - 迭代次数
            }
        """
        result = self.run_conversation(message, stream_callback=stream_callback)
        return {
            'response': result.response,
            'tool_calls': result.tool_calls,
            'usage': result.usage,
            'iterations': result.iterations,
        }

    def chat_stream(self, message: str, delta_callback: Callable = None):
        """流式对话接口 — 委托给统一主循环（chat）。

        历史遗留的独立流式管道已移除：流式增量现在作为传输层
        集成在 run_conversation 主循环内（带门控），保证工具调用
        解析、循环处理、总结与非流式路径完全一致。

        Args:
            message: 用户消息
            delta_callback: 每个文本增量回调 (text_delta: str) -> None

        Returns:
            dict: 与 chat() 相同的完整响应字典
        """
        return self.chat(message, stream_callback=delta_callback)

    def interrupt(self):
        """请求中断当前操作。"""
        self._interrupt_requested = True
        logger.info("收到中断请求")

    def clear_interrupt(self):
        """清除中断标志。"""
        self._interrupt_requested = False

    def reset_session(self):
        """重置会话状态（Soft End + New Session）。
        
        这是三层终止机制中的第一层：仅清空内存状态，
        数据库会话保持活跃，Agent 实例继续运行。
        用于 /new 命令或上下文压缩触发。
        """
        # 1. Soft End: 结束当前会话（标记为 'compression' 或 'new_session'）
        if self.session_id and self._session_db:
            try:
                self._session_db.end_session(self.session_id, reason="new_session")
            except Exception as e:
                logger.warning("结束会话失败 (非致命): %s", e)
        
        # 2. 清空内存状态
        self._messages.clear()
        self._api_call_count = 0
        self._interrupt_requested = False
        self._cached_system_prompt = None
        self.session_id = str(uuid.uuid4())

        # 3. 重置压缩器
        if self._context_compressor:
            from spirit.agent.context_compressor import SessionLifecycleHooks
            hooks = SessionLifecycleHooks(self._context_compressor)
            hooks.on_session_reset()
        
        # 4. 创建新会话记录
        if self._session_db:
            try:
                self._session_db.create_session(
                    session_id=self.session_id,
                    source=self.platform,
                    model=self.model,
                )
            except Exception as e:
                logger.warning("创建新会话记录失败 (非致命): %s", e)
        
        # 5. 启动用量追踪
        try:
            from spirit.agent.usage_tracker import get_usage_tracker
            tracker = get_usage_tracker()
            tracker.start_session(self.session_id)
        except Exception as e:
            logger.warning("用量追踪启动失败 (非致命): %s", e)

        logger.info("会话已重置 (Soft End): %s", self.session_id[:8])

    def shutdown_memory_provider(self):
        """关闭记忆提供者（Medium End）。
        
        这是三层终止机制中的第二层：关闭记忆/上下文提供者，
        但 Agent 实例仍可重用。用于 CLI 退出或 /reset 命令。
        """
        # 1. Soft End: 结束当前会话
        if self.session_id and self._session_db:
            try:
                self._session_db.end_session(self.session_id, reason="cli_exit")
            except Exception as e:
                logger.warning("结束会话失败 (非致命): %s", e)
        
        # 2. 关闭压缩器
        if self._context_compressor:
            try:
                self._context_compressor.shutdown()
                logger.info("上下文压缩器已关闭")
            except Exception as e:
                logger.warning("关闭压缩器失败 (非致命): %s", e)
        
        logger.info("记忆提供者已关闭 (Medium End)")

    def close(self):
        """完全关闭 Agent（Hard End）。
        
        这是三层终止机制中的第三层：杀死所有资源，
        标记数据库会话结束。用于真正终止 Agent。
        """
        # 1. Hard End: 结束会话
        if self.session_id and self._session_db:
            try:
                self._session_db.end_session(self.session_id, reason="agent_close")
            except Exception as e:
                logger.warning("结束会话失败 (非致命): %s", e)
        
        # 2. 关闭客户端
        if self._client:
            try:
                # OpenAI client 没有显式 close 方法，但我们可以清理引用
                self._client = None
                logger.debug("OpenAI 客户端已清理")
            except Exception as e:
                logger.warning("清理客户端失败 (非致命): %s", e)
        
        # 3. 关闭数据库连接
        if self._session_db:
            try:
                self._session_db.close()
                logger.info("数据库连接已关闭")
            except Exception as e:
                logger.warning("关闭数据库失败 (非致命): %s", e)
        
        # 4. 清空状态
        self._messages.clear()
        self._interrupt_requested = False
        
        logger.info("Agent 已完全关闭 (Hard End)")

    def resume_session(self, session_id: str) -> bool:
        """恢复历史会话（Resume Mode）。
        
        Args:
            session_id: 要恢复的会话 ID
            
        Returns:
            bool: 是否成功恢复
        """
        if not self._session_db:
            logger.error("会话数据库未初始化")
            return False
        
        # 1. 检查会话是否存在
        session = self._session_db.get_session(session_id)
        if not session:
            logger.error("会话不存在: %s", session_id[:8])
            return False
        
        # 2. 结束当前会话
        if self.session_id:
            try:
                self._session_db.end_session(self.session_id, reason="resumed_other")
            except Exception as e:
                logger.warning("结束当前会话失败 (非致命): %s", e)
        
        # 3. 重新打开目标会话
        success = self._session_db.reopen_session(session_id)
        if not success:
            logger.error("无法重新打开会话: %s", session_id[:8])
            return False
        
        # 4. 加载历史消息
        messages = self._session_db.get_messages(session_id)
        self._messages = [
            {
                "role": msg["role"],
                "content": msg.get("content", ""),
                **({"tool_calls": json.loads(msg["tool_calls"])} if msg.get("tool_calls") else {}),
            }
            for msg in messages
        ]
        
        # 5. 切换会话 ID
        old_session_id = self.session_id
        self.session_id = session_id
        
        # 6. 重置计数器
        self._api_call_count = 0
        self._cached_system_prompt = None  # 强制重建系统提示词
        
        logger.info(
            "会话已恢复 (Resume): %s → %s (%d 条消息)",
            old_session_id[:8] if old_session_id else "None",
            session_id[:8],
            len(self._messages),
        )
        
        return True

    def branch_session(self, branch_name: str = None) -> str:
        """创建会话分支（Branch Mode）。
        
        Args:
            branch_name: 分支名称（可选）
            
        Returns:
            str: 新会话 ID
        """
        if not self._session_db:
            raise RuntimeError("会话数据库未初始化")
        
        # 1. 创建分支（复制当前会话的所有消息）
        new_session_id = self._session_db.branch_session(
            parent_session_id=self.session_id,
            branch_name=branch_name,
        )
        
        # 2. 切换到新会话
        old_session_id = self.session_id
        self.session_id = new_session_id
        
        # 3. 重置计数器（但保留消息历史）
        self._api_call_count = 0
        self._cached_system_prompt = None
        
        logger.info(
            "会话已分支 (Branch): %s → %s (%s)",
            old_session_id[:8],
            new_session_id[:8],
            branch_name or "auto",
        )
        
        return new_session_id

    # ------------------------------------------------------------------
    # 工具相关
    # ------------------------------------------------------------------

    def get_tool_definitions(self) -> List[dict]:
        """获取当前可用的工具定义（OpenAI 格式）。"""
        return self.registry.get_definitions(
            enabled_toolsets=self.enabled_toolsets,
            disabled_toolsets=self.disabled_toolsets,
        )

    def invoke_tool(self, name: str, args: Dict[str, Any]) -> str:
        """调用工具。"""
        # 回调
        if self.on_tool_start:
            try:
                self.on_tool_start(name, args)
            except Exception:
                pass

        result = self.registry.dispatch(name, args)

        # 回调
        if self.on_tool_complete:
            try:
                self.on_tool_complete(name, result)
            except Exception:
                pass

        return result

    # ------------------------------------------------------------------
    # 消息管理
    # ------------------------------------------------------------------

    def add_message(self, role: str, content: str, **kwargs):
        """添加消息到对话历史。"""
        msg = {"role": role, "content": content}
        msg.update(kwargs)
        self._messages.append(msg)

    def get_system_prompt(self) -> str:
        """获取系统提示词（动态构建 + 缓存）。

        系统提示词采用"一次构建，永久缓存"架构:
        - 仅在首次调用时构建
        - 后续轮次直接返回缓存，确保 prompt cache 前缀稳定
        - 只有 reset_session() 或 switch_model() 才会清除缓存
        """
        if self._cached_system_prompt:
            return self._cached_system_prompt

        from spirit.agent.prompt_builder import build_system_prompt

        prompt = build_system_prompt(
            agent=self,
            custom_system_message=self.config.system_prompt or None,
        )

        self._cached_system_prompt = prompt
        return prompt

    def set_ephemeral_system_prompt(self, text: str):
        """设置临时系统提示词（仅当前轮有效）。"""
        from spirit.agent.prompt_builder import EphemeralSystemPrompt
        if self._ephemeral_system_prompt is None:
            self._ephemeral_system_prompt = EphemeralSystemPrompt()
        self._ephemeral_system_prompt.set(text)

    # ------------------------------------------------------------------
    # 模型切换
    # ------------------------------------------------------------------

    def switch_model(self, model: str, base_url: str = None, api_key: str = None):
        """运行时切换模型。"""
        self.model = model
        if base_url:
            self.base_url = base_url
        if api_key:
            self.api_key = api_key
        # 重建客户端
        self._client = None
        # 清除缓存的系统提示词
        self._cached_system_prompt = None
        logger.info("模型已切换: %s", model)

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """获取当前状态摘要。"""
        return {
            "session_id": self.session_id,
            "model": self.model,
            "provider": self.provider,
            "platform": self.platform,
            "api_call_count": self._api_call_count,
            "message_count": len(self._messages),
            "tool_count": len(self.registry.get_tool_names()),
            "compression_enabled": self._context_compressor is not None,
            "prompt_caching_enabled": self._use_prompt_caching,
            "cache_ttl": self._cache_ttl,
        }
