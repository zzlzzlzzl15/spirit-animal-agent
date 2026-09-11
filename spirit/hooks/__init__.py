"""Spirit Agent 钩子系统 — 统一事件驱动架构。

设计借鉴 Hermes 的防御性编程理念：
- 钩子失败不影响主流程（异常隔离）
- 每个事件支持多处理器 + 优先级排序
- 全局监听器可捕获所有事件
"""

from spirit.hooks.hook_manager import HookManager

__all__ = ["HookManager"]
