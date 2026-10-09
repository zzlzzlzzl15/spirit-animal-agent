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
import time
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

    # Tool Search（Progressive Tool Disclosure，Hermes 同款）
    # 开关开启 + 工具总数 > eager_threshold 时，对 LLM 只暴露 3 个元工具
    # （tool_search / tool_describe / tool_call）+ 在 system prompt 注入精简 manifest。
    # 关闭 / 工具数少时走原有路径（直接发所有 schema），保持向后兼容。
    tool_search_enabled: bool = False
    tool_search_eager_threshold: int = 15  # ≤ 此数直接发所有 schema（不用 bridge）
    tool_search_manifest_budget_pct: float = 5.0  # manifest 占 context 比例

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
            # 嵌套格式: agent.tool_search.*
            ts = agent_cfg.get("tool_search", {})
            if isinstance(ts, dict):
                if "enabled" in ts:
                    config.tool_search_enabled = bool(ts["enabled"])
                if ts.get("eager_threshold") is not None:
                    config.tool_search_eager_threshold = int(ts["eager_threshold"])
                if ts.get("manifest_budget_pct") is not None:
                    config.tool_search_manifest_budget_pct = float(ts["manifest_budget_pct"])

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
                     "compression_threshold", "streaming_enabled",
                     "tool_search_enabled", "tool_search_eager_threshold",
                     "tool_search_manifest_budget_pct"):
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
        self.context_length = config.context_length  # Tool Search manifest / 压缩器都要用

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

        # ── 会话级 token 用量累加（对齐 Hermes session_*_tokens 语义）──
        # 状态栏 / debug / 计费统计共用。conversation_loop 在每次 LLM 响应成功后
        # 调用 += 累加。初始 0 表示"还没产生过 API 调用"，与 _api_call_count 配套。
        # 字段语义对齐 Hermes：session_input_tokens / session_output_tokens /
        # session_total_tokens / session_api_calls。
        self.session_input_tokens: int = 0     # 本会话累计输入（prompt）token
        self.session_output_tokens: int = 0    # 本会话累计输出（completion）token
        self.session_total_tokens: int = 0     # 本会话累计总 token（输入+输出）
        self.session_api_calls: int = 0        # 本会话成功 API 调用次数

        # 工作区上下文（由 VSCode 扩展推送，CLI 模式下为 None）
        self.workspace_context: Any = None

        # OpenAI 客户端（延迟初始化）
        self._client = None

        # 工具注册表（延迟初始化）
        self._registry = None

        # 目标管理器（Ralph Loop 持久目标 — 延迟初始化，Phase 4）
        self._goal_manager = None

        # 会话数据库（延迟初始化）
        self._session_db = None

        # 自动初始化会话数据库
        try:
            from spirit.storage.session_db import SessionDB
            self._session_db = SessionDB()
            logger.info("会话数据库已初始化")
        except Exception as e:
            logger.warning("会话数据库初始化失败 (非致命): %s", e)

        # ── 首启 session 幂等创建（v0.2.8） ──────────────────────
        # 之前：SpiritAgent.__init__ 只设内存 session_id，conversation_loop
        # 也不调 create_session——用户首次启动 spirit 跑任务、退出，db 里
        # 根本没记录这条任务，导致 "之前最新的五条" 永远不包含刚跑完的任务
        # （仅包含 /new 主动创建的会话）。
        # 修复：spirit 启动时幂等确保 session_id 已在 db 里。
        #   - 新启动：create_session 插一行
        #   - 恢复（/resume [n] 或 config.session_id 显式传）：get_session
        #     返回非空、跳过
        #   - 失败：非致命（log warning 继续运行）
        if self._session_db and self.session_id:
            try:
                if not self._session_db.get_session(self.session_id):
                    self._session_db.create_session(
                        session_id=self.session_id,
                        source=self.platform,
                        model=self.model,
                    )
                    logger.info(
                        "会话记录已创建 (首启): %s", self.session_id[:8]
                    )
            except Exception as _create_exc:  # noqa: BLE001
                logger.warning(
                    "自动创建会话记录失败 (非致命): %s", _create_exc
                )

        # ── 会话开始时间戳 (v0.2.10) ───────────────────
        # 状态栏显示 "since HH:MM:SS" 是用户查看累计用量的更新时间错。
        # 不存 db：进程重启后重置，重启后从 db.started_at 补。
        self._session_started_at = time.time()

        # 钩子系统（在 __init__ 统一装配：CLI/桌面 launcher/gateway 都是
        # 直接构造 SpiritAgent，不走 agent_init.create_agent；失败非致命）
        self.hook_manager = None
        try:
            from spirit.hooks.hook_manager import HookManager
            from spirit.hooks.lifecycle_hooks import register_lifecycle_hooks
            from spirit.hooks.tool_hooks import register_tool_hooks
            from spirit.hooks.task_hooks import register_task_hooks
            from spirit.hooks.conversation_hooks import register_conversation_hooks
            from spirit.hooks.memora_auto_save import register_auto_save_hook

            _hm = HookManager()
            register_lifecycle_hooks(_hm, self)
            register_tool_hooks(_hm, self)
            register_task_hooks(_hm, self)
            register_conversation_hooks(_hm, self)
            register_auto_save_hook(_hm)
            self.hook_manager = _hm
            logger.info("钩子系统已初始化")
        except Exception as e:
            logger.warning("钩子系统初始化失败 (非致命): %s", e)

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

        # Tool Search（Progressive Tool Disclosure）
        self.tool_search_enabled = config.tool_search_enabled
        self.tool_search_eager_threshold = config.tool_search_eager_threshold
        self.tool_search_manifest_budget_pct = config.tool_search_manifest_budget_pct
        # bridge 句柄（lazy 导入避免循环依赖 spirit.tools.tool_search → spirit.tools.registry）
        self._bridge_handlers: Optional[Dict[str, Callable]] = None
        self._bridge_tool_names: Optional[set] = None

        # 临时系统提示词
        self._ephemeral_system_prompt = None

        # 额外 API 参数（provider 特定）
        self._extra_api_params: Dict[str, Any] = {}

        # MoA 参考展示钩子（provider=="moa" 时传给 MoAClient）。
        # 签名：callback(event, **kwargs)，event ∈ {"moa.reference", "moa.aggregating"}。
        # CLI/前端可设置以在聚合器行动前渲染每个参考模型的输出块。
        self._moa_reference_callback: Optional[Callable] = None

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
        """OpenAI 兼容客户端（延迟初始化）。

        ``provider == "moa"`` 时返回 :class:`MoAClient` facade（其
        ``.chat.completions.create`` 拦截为 MoA 流程：并行跑参考模型 → 注入
        guidance → 调聚合器），``self.model`` 即预设名。facade 缓存在 ``_client``，
        使其 turn 域参考缓存跨一轮内的多次 ``create()``（每工具迭代一次）持久；
        ``set_model`` 重置 ``_client`` 时会重建。
        """
        if self._client is None:
            if str(self.provider or "").strip().lower() == "moa":
                from spirit.moa.moa_loop import MoAClient
                self._client = MoAClient(
                    self.model,
                    reference_callback=getattr(self, "_moa_reference_callback", None),
                )
            else:
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
    def goal_manager(self):
        """当前会话的目标管理器（Ralph Loop，延迟初始化）。

        绑定到 ``self.session_id``，持久化走 SessionDB 的 ``state_meta`` 表（因此
        ``/resume`` 能重新捡起未完成的目标）。judge 的 LLM 调用通过
        ``build_agent_llm_caller(self)`` 从本 Agent 的 client 构造闭包——``goals.enabled``
        关闭时 llm_caller 为 None，judge 直接 fail-open 返回 continue（不自主续传）。
        """
        if self._goal_manager is None:
            from spirit.goals import (
                GoalManager,
                SessionDBGoalStore,
                build_agent_llm_caller,
            )
            from spirit.config import get_config_value

            enabled = bool(get_config_value("goals.enabled", True))
            store = SessionDBGoalStore(db=self._session_db)
            caller = build_agent_llm_caller(self) if enabled else None
            self._goal_manager = GoalManager(
                self.session_id,
                default_max_turns=get_config_value("goals.max_turns", 20),
                store=store,
                llm_caller=caller,
            )
        return self._goal_manager

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

        v0.2.9: 包装 try/finally，在所有 return 路径 / 异常路径上
        调 self.close_session(reason) 令 db 的 ended_at / end_reason
        总是有值。与 Hermes session_persistence 三层终止对齐。
        """
        from spirit.agent.conversation_loop import run_conversation
        _close_reason = "task_complete"
        try:
            result = run_conversation(self, user_message, **kwargs)
            _close_reason = self._infer_close_reason(result)
            return result
        except KeyboardInterrupt:
            _close_reason = "user_interrupt"
            raise
        except BaseException as exc:  # noqa: BLE001 - 捕获一切上报
            _close_reason = "task_failed"
            logger.warning(
                "对话循环异常 (%s)，会话标记为 task_failed", exc,
            )
            raise
        finally:
            try:
                self.close_session(reason=_close_reason)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "close_session finally 异常 (非致命)",
                    exc_info=True,
                )

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
        notice_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """对话接口 — 返回完整响应（含工具调用）。

        Args:
            message: 用户消息
            stream_callback: 可选的流式文本增量回调（集成在主循环内）
            notice_callback: 可选的过程通知回调（fallback 切换等；缺省复用
                stream_callback）。桌面链路用它把通知与流式增量分通道推送，
                避免通知占用 streamBuffer 吞掉轮末错误结论的兜底渲染。

        Returns:
            dict: {
                'response': str - 最终文本响应,
                'tool_calls': list - 本轮的工具调用列表,
                'usage': dict - token 使用统计,
                'iterations': int - 迭代次数
            }
        """
        result = self.run_conversation(
            message,
            stream_callback=stream_callback,
            notice_callback=notice_callback,
        )
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

    def _emit_hook(self, event: str, **kwargs) -> None:
        """触发钩子事件（钩子系统未启用时静默跳过，异常由 HookManager 隔离）。"""
        hook_manager = getattr(self, "hook_manager", None)
        if hook_manager is not None:
            hook_manager.emit(event, **kwargs)

    def clear_interrupt(self):
        """清除中断标志。"""
        self._interrupt_requested = False

    def reset_session(self):
        """重置会话状态（Soft End + New Session）。
        
        这是三层终止机制中的第一层：仅清空内存状态，
        数据库会话保持活跃，Agent 实例继续运行。
        用于 /new 命令或上下文压缩触发。
        """
        # 0. 钩子：会话结束（handler 会把内存消息持久化到 session_db，
        #    必须在清空 _messages 之前触发；add_message 不做逐条落库，无重复风险）
        self._emit_hook("on_session_end", session_id=self.session_id)

        # 1. Soft End: 结束当前会话（标记为 'compression' 或 'new_session'）
        old_session_id = self.session_id  # 记住旧 id，用于持久目标迁移
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
        # 目标管理器绑定了旧 session_id —— 丢弃，让 goal_manager property 按新 id 重建。
        self._goal_manager = None

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

        # 6. 迁移持久目标到新会话（Ralph Loop 目标不应在会话轮换边界静默死亡）
        if old_session_id and old_session_id != self.session_id:
            try:
                from spirit.goals import SessionDBGoalStore, migrate_goal_to_session
                migrated = migrate_goal_to_session(
                    old_session_id,
                    self.session_id,
                    store=SessionDBGoalStore(db=self._session_db),
                    reason="reset_session",
                )
                if migrated:
                    logger.info(
                        "持久目标已迁移到新会话: %s -> %s",
                        old_session_id[:8], self.session_id[:8],
                    )
            except Exception as e:
                logger.warning("目标迁移失败 (非致命): %s", e)

        logger.info("会话已重置 (Soft End): %s", self.session_id[:8])

        # 7. 钩子：会话重置 + 新会话开始（新 id 无历史，handler 加载为空 no-op）
        self._emit_hook("on_session_reset", old_session_id=old_session_id)
        self._emit_hook("on_session_start", session_id=self.session_id)

    def close_session(self, reason: str = "task_complete") -> None:
        """结束当前会话记录（会话终止入口，与 Hermes 三层终止对齐）。

        与 ``shutdown_memory_provider`` 的区别：
        - 本方法**只**负责 db session 结束（不关闭 compressor），
          可被多次调用、幂等（end_session 本身 First Reason Wins）。
        - ``shutdown_memory_provider`` 同时关 compressor，走 Medium End。
        - ``close`` 走 Hard End（全资源释放）。

        调用场景：
        - conversation_loop 5 个 return 路径前
        - cli/main.py chat() 退出循环
        - 信号处理（SIGTERM / SIGBREAK）
        - 超时（max_iterations / api_error / retry_exhausted）

        设计原则：
        - **幂等**：可被多次调用，end_session SQL 写 WHERE ended_at IS NULL
          保证 First Reason Wins（不覆盖）
        - **异常隔离**：db 失败只 log warning、不抛错
        - **可选 reason**：默认 ``task_complete``，按调用方语义覆盖

        reason 可选值（与 end_session reason 字段对齐）:
            task_complete / user_interrupt / max_iterations /
            api_error / retry_exhausted / guardrail_halt /
            cli_exit / agent_close / signal_term / signal_break
        """
        if not (self.session_id and self._session_db):
            return
        try:
            self._session_db.end_session(self.session_id, reason=reason)
            logger.info(
                "会话已结束 (v0.2.9 close_session): %s reason=%s",
                self.session_id[:8], reason,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "close_session 结束会话失败 (非致命): %s", e,
            )

    @staticmethod
    def _infer_close_reason(result) -> str:
        """根据 conversation_loop 返回的 ConversationResult 推断关闭 reason。

        原理：conversation_loop 8 个 return 路径的 response 前缀都是
        固定格式（[已中断] / [达到最大迭代次数 N] / [API 错误: ...] /
        [API 调用失败，已重试 N 次] / [工具循环停止: ...]），用 startswith
        判定即可。其它 = task_complete（含 line 681 fallback 兜底总结、
        line 761 正常完成、line 762 模型文本回复）。
        """
        try:
            response = (getattr(result, "response", "") or "").strip()
        except Exception:  # noqa: BLE001
            return "task_complete"
        if response.startswith("[已中断]"):
            return "user_interrupt"
        if response.startswith("[达到最大迭代次数 "):
            return "max_iterations"
        if response.startswith("[API 错误"):
            return "api_error"
        if response.startswith("[API 调用失败"):
            return "retry_exhausted"
        if response.startswith("[工具循环停止"):
            return "guardrail_halt"
        return "task_complete"

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
        # 0. 钩子：Agent 销毁（handler 会在 db 关闭前持久化当前消息）
        self._emit_hook("on_agent_destroy", session_id=self.session_id)

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
        """获取当前可用的工具定义（OpenAI 格式）。

        Tool Search 路径（agent.tool_search.enabled=True 且工具总数 > eager_threshold）：
            返回 3 个 bridge schema（tool_search / tool_describe / tool_call）；
            原始工具 schema 走 system prompt 里注入的精简 manifest 透露。
        Eager 路径：返回所有工具 schema（与原行为一致），避免不必要的多跳开销。
        """
        definitions = self.registry.get_definitions(
            enabled_toolsets=self.enabled_toolsets,
            disabled_toolsets=self.disabled_toolsets,
        )
        if not self.tool_search_enabled:
            return definitions
        if len(definitions) <= self.tool_search_eager_threshold:
            return definitions
        # Defer 模式：只发 3 个 bridge，外加 manifest 拼到 system prompt
        from spirit.tools.tool_search import ALL_BRIDGE_SCHEMAS
        return list(ALL_BRIDGE_SCHEMAS)

    def _ensure_bridge_loaded(self) -> None:
        """Lazy 加载 bridge 句柄（避免循环依赖）。"""
        if self._bridge_handlers is None:
            from spirit.tools.tool_search import BRIDGE_HANDLERS, BRIDGE_TOOL_NAMES
            self._bridge_handlers = dict(BRIDGE_HANDLERS)
            self._bridge_tool_names = set(BRIDGE_TOOL_NAMES)

    def _is_bridge_tool(self, name: str) -> bool:
        """判断是否为 Tool Search 桥接工具。"""
        self._ensure_bridge_loaded()
        return name in (self._bridge_tool_names or set())

    def invoke_tool(self, name: str, args: Dict[str, Any]) -> str:
        """调用工具。

        Tool Search 启用时：先检查 name 是否是 bridge（tool_search/tool_describe/tool_call），
        是则走桥接 handler，不走 registry.dispatch。其它情况透传到原 dispatcher。
        这样 bridge 永远能被 LLM 调用，同时 60+ 原生工具的可调用能力也保留。
        """
        # 回调
        if self.on_tool_start:
            try:
                self.on_tool_start(name, args)
            except Exception:
                pass

        if self.tool_search_enabled and self._is_bridge_tool(name):
            self._ensure_bridge_loaded()
            handler = (self._bridge_handlers or {}).get(name)
            if handler is None:
                result = json.dumps(
                    {"error": f"未知的 bridge 工具: {name}"},
                    ensure_ascii=False,
                )
            else:
                try:
                    result = handler(args, agent=self)
                except Exception as exc:
                    logger.warning("bridge 工具 %s 执行失败: %s", name, exc, exc_info=True)
                    result = json.dumps(
                        {"error": f"bridge 工具执行失败: {type(exc).__name__}: {str(exc)[:500]}"},
                        ensure_ascii=False,
                    )
        else:
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
    
        系统提示词采用“一次构建，永久缓存”架构:
        - 仅在首次调用时构建
        - 后续轮次直接返回缓存，确保 prompt cache 前缀稳定
        - 只有 reset_session() 或 switch_model() 才会清除缓存
    
        Tool Search 启用且走 defer 模式（返回的是 bridge 而非全部 schema）时，
        会在 prompt 末尾追加一份精简 manifest（name + toolset + 一句话描述），
        作为 LLM 跳查工具名的索引。manifest 按 token 预算（默认 5% context）降级。
        """
        if self._cached_system_prompt:
            return self._cached_system_prompt
    
        from spirit.agent.prompt_builder import build_system_prompt
    
        extra_context = None
        if self.tool_search_enabled:
            try:
                # 计算实际生效的 definitions：与 get_tool_definitions 同样的门槛
                definitions = self.registry.get_definitions(
                    enabled_toolsets=self.enabled_toolsets,
                    disabled_toolsets=self.disabled_toolsets,
                )
                if len(definitions) > self.tool_search_eager_threshold:
                    from spirit.tools.tool_search import build_tool_manifest
                    extra_context = build_tool_manifest(
                        budget_pct=self.tool_search_manifest_budget_pct,
                        context_length=self.context_length,
                    )
            except Exception as exc:  # noqa: BLE001 - manifest 生成失败不影响主路径
                logger.debug("tool manifest 生成失败（非致命）: %s", exc)
    
        prompt = build_system_prompt(
            agent=self,
            custom_system_message=self.config.system_prompt or None,
            extra_context=extra_context,
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
