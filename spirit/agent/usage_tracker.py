"""Usage tracking for Spirit Agent.

Tracks token consumption, API calls, and estimated costs across sessions.
Designed as an auxiliary task that doesn't block the main conversation loop.

Reference: agent/account_usage.py, agent/billing_usage.py (Hermes Agent)
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class UsageStats:
    """会话级别的用量统计。
    
    Attributes:
        session_id: 当前会话 ID
        total_tokens: 总 token 数
        prompt_tokens: 输入 token 数
        completion_tokens: 输出 token 数
        api_calls: API 调用次数
        estimated_cost_usd: 预估费用（USD）
        started_at: 会话开始时间戳
        last_updated: 最后更新时间戳
    """
    session_id: str = ""
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    api_calls: int = 0
    estimated_cost_usd: float = 0.0
    started_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)
    
    def update_from_response(self, usage_data: Dict):
        """从 LLM 响应中更新用量统计。
        
        Args:
            usage_data: OpenAI 格式的 usage 对象字典
                {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150
                }
        """
        if not usage_data:
            return
        
        self.prompt_tokens += usage_data.get("prompt_tokens", 0)
        self.completion_tokens += usage_data.get("completion_tokens", 0)
        self.total_tokens += usage_data.get("total_tokens", 0)
        self.api_calls += 1
        self.last_updated = time.time()
        
        # 估算费用（默认按 GPT-4 价格：$0.03/1K input, $0.06/1K output）
        input_cost = (self.prompt_tokens / 1000) * 0.03
        output_cost = (self.completion_tokens / 1000) * 0.06
        self.estimated_cost_usd = input_cost + output_cost
        
        logger.debug(
            "用量更新: session=%s, tokens=%d, cost=$%.4f",
            self.session_id[:8] if self.session_id else "N/A",
            self.total_tokens,
            self.estimated_cost_usd,
        )
    
    def reset(self):
        """重置统计（用于新会话）。"""
        self.total_tokens = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.api_calls = 0
        self.estimated_cost_usd = 0.0
        self.started_at = time.time()
        self.last_updated = time.time()
    
    def to_dict(self) -> Dict:
        """转换为字典格式。"""
        return {
            "session_id": self.session_id,
            "total_tokens": self.total_tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "api_calls": self.api_calls,
            "estimated_cost_usd": round(self.estimated_cost_usd, 4),
            "duration_seconds": round(time.time() - self.started_at, 2),
        }


class UsageTracker:
    """用量追踪器 — 管理会话级别的用量统计。
    
    设计原则：
    - 辅助任务：失败不影响主对话循环
    - 内存存储：轻量级，不依赖数据库
    - 自动清理：会话结束时自动归档
    """
    
    def __init__(self):
        self._current_stats: Optional[UsageStats] = None
        self._session_history: Dict[str, UsageStats] = {}
    
    def start_session(self, session_id: str):
        """开始新会话的用量追踪。
        
        Args:
            session_id: 会话 ID
        """
        # 归档旧会话
        if self._current_stats and self._current_stats.session_id:
            self._session_history[self._current_stats.session_id] = self._current_stats
        
        # 创建新统计
        self._current_stats = UsageStats(session_id=session_id)
        logger.info("用量追踪已开始: session=%s", session_id[:8])
    
    def end_session(self, session_id: str):
        """结束会话的用量追踪。
        
        Args:
            session_id: 会话 ID
        """
        if self._current_stats and self._current_stats.session_id == session_id:
            self._session_history[session_id] = self._current_stats
            logger.info(
                "用量追踪已结束: session=%s, total_tokens=%d, cost=$%.4f",
                session_id[:8],
                self._current_stats.total_tokens,
                self._current_stats.estimated_cost_usd,
            )
            self._current_stats = None
    
    def update(self, usage_data: Dict):
        """更新当前会话的用量统计。
        
        Args:
            usage_data: LLM 响应的 usage 字段
        """
        if not self._current_stats:
            logger.warning("用量追踪未启动，忽略更新")
            return
        
        try:
            self._current_stats.update_from_response(usage_data)
        except Exception as e:
            # 辅助任务失败不中断主流程
            logger.warning("用量更新失败 (非致命): %s", e)
    
    def get_current_stats(self) -> Optional[Dict]:
        """获取当前会话的用量统计。
        
        Returns:
            用量统计字典，或 None（如果未启动）
        """
        if not self._current_stats:
            return None
        return self._current_stats.to_dict()
    
    def get_session_stats(self, session_id: str) -> Optional[Dict]:
        """获取历史会话的用量统计。
        
        Args:
            session_id: 会话 ID
            
        Returns:
            用量统计字典，或 None（如果不存在）
        """
        stats = self._session_history.get(session_id)
        return stats.to_dict() if stats else None
    
    def get_total_usage(self) -> Dict:
        """获取所有会话的总用量。
        
        Returns:
            汇总统计字典
        """
        total_tokens = sum(s.total_tokens for s in self._session_history.values())
        total_cost = sum(s.estimated_cost_usd for s in self._session_history.values())
        total_calls = sum(s.api_calls for s in self._session_history.values())
        
        # 加上当前会话
        if self._current_stats:
            total_tokens += self._current_stats.total_tokens
            total_cost += self._current_stats.estimated_cost_usd
            total_calls += self._current_stats.api_calls
        
        return {
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 4),
            "total_api_calls": total_calls,
            "tracked_sessions": len(self._session_history) + (1 if self._current_stats else 0),
        }
    
    def reset(self):
        """重置所有统计。"""
        self._current_stats = None
        self._session_history.clear()
        logger.info("用量统计已重置")


# 全局单例
_global_tracker = UsageTracker()


def get_usage_tracker() -> UsageTracker:
    """获取全局用量追踪器实例。"""
    return _global_tracker


__all__ = ["UsageTracker", "UsageStats", "get_usage_tracker"]
