"""Spirit Agent 核心模块 — 智能引擎。

导出：
- SpiritAgent: 核心 Agent 类
- AgentConfig: 配置数据类
- ConversationResult: 对话结果
- error_handler: 错误分类 + 自适应重试
- tool_guardrails: 工具调用护栏
- context_compressor: 上下文压缩引擎
- streaming: 流式输出
- prompt_builder: 动态系统提示词
- tool_executor: 并发工具执行
"""

from spirit.agent.agent import SpiritAgent, AgentConfig, ConversationResult

__all__ = [
    "SpiritAgent",
    "AgentConfig",
    "ConversationResult",
]
