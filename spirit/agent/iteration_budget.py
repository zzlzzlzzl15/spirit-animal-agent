"""Iteration budget management for conversation loop.

This module provides the IterationBudget class that tracks and controls
the number of API calls in a conversation turn, preventing infinite loops
and managing resource consumption.

Reference: agent/iteration_budget.py (Hermes Agent)
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class IterationBudget:
    """迭代预算管理
    
    职责:
    - 跟踪已使用的迭代次数
    - 控制最大迭代次数防止无限循环
    - 支持预算退还（用于压缩后重试）
    
    使用场景:
    - 每次 API 调用前 consume()
    - 上下文压缩后 refund()（因为压缩减少了 token，值得重试）
    """
    
    max_total: int = 50              # 最大迭代次数
    used: int = 0                    # 已使用次数
    remaining: int = field(init=False)  # 剩余次数（自动计算）
    
    def __post_init__(self):
        """初始化剩余次数"""
        self.remaining = self.max_total
    
    def consume(self) -> bool:
        """消耗一次迭代，返回是否还有剩余
        
        Returns:
            bool: True 如果还有剩余预算，False 如果已耗尽
            
        Example:
            >>> budget = IterationBudget(max_total=3)
            >>> budget.consume()  # True, remaining=2
            >>> budget.consume()  # True, remaining=1
            >>> budget.consume()  # True, remaining=0
            >>> budget.consume()  # False, remaining=0
        """
        if self.remaining <= 0:
            logger.debug("Iteration budget exhausted (used=%d/%d)", 
                        self.used, self.max_total)
            return False
        
        self.used += 1
        self.remaining -= 1
        logger.debug(
            "Iteration consumed: used=%d/%d, remaining=%d",
            self.used, self.max_total, self.remaining
        )
        return True
    
    def refund(self) -> bool:
        """退还一次迭代（用于压缩后重试）
        
        Returns:
            bool: True 如果成功退还，False 如果没有可退还的迭代
            
        Example:
            >>> budget = IterationBudget(max_total=3)
            >>> budget.consume()  # used=1, remaining=2
            >>> budget.consume()  # used=2, remaining=1
            >>> budget.refund()   # True, used=1, remaining=2
            >>> budget.refund()   # True, used=0, remaining=3
            >>> budget.refund()   # False, used=0, remaining=3 (nothing to refund)
        """
        if self.used <= 0:
            logger.debug("No iterations to refund (used=0)")
            return False
        
        self.used -= 1
        self.remaining += 1
        logger.debug(
            "Iteration refunded: used=%d/%d, remaining=%d",
            self.used, self.max_total, self.remaining
        )
        return True
    
    def reset(self):
        """重置预算到初始状态"""
        self.used = 0
        self.remaining = self.max_total
        logger.debug("Iteration budget reset: max_total=%d", self.max_total)
    
    @property
    def utilization(self) -> float:
        """预算利用率（0.0 - 1.0）"""
        if self.max_total <= 0:
            return 0.0
        return self.used / self.max_total
    
    def __repr__(self) -> str:
        return f"IterationBudget(used={self.used}, remaining={self.remaining}, max={self.max_total})"


def create_budget_from_config(config: dict) -> IterationBudget:
    """从配置字典创建预算对象
    
    Args:
        config: 配置字典，包含 'max_iterations' 键
        
    Returns:
        IterationBudget: 配置的预算对象
        
    Example:
        >>> config = {'max_iterations': 100}
        >>> budget = create_budget_from_config(config)
        >>> budget.max_total
        100
    """
    max_iterations = config.get('max_iterations', 50)
    return IterationBudget(max_total=max_iterations)


if __name__ == "__main__":
    # 简单测试
    import sys
    
    print("Testing IterationBudget...")
    
    # Test 1: Basic consume
    budget = IterationBudget(max_total=3)
    assert budget.consume() == True
    assert budget.consume() == True
    assert budget.consume() == True
    assert budget.consume() == False
    print("[OK] Test 1 passed: Basic consume")
    
    # Test 2: Refund
    budget = IterationBudget(max_total=3)
    budget.consume()
    budget.consume()
    assert budget.refund() == True
    assert budget.used == 1
    assert budget.remaining == 2
    print("[OK] Test 2 passed: Refund")
    
    # Test 3: Reset
    budget = IterationBudget(max_total=3)
    budget.consume()
    budget.consume()
    budget.reset()
    assert budget.used == 0
    assert budget.remaining == 3
    print("[OK] Test 3 passed: Reset")
    
    # Test 4: Utilization
    budget = IterationBudget(max_total=10)
    budget.consume()
    budget.consume()
    assert abs(budget.utilization - 0.2) < 0.01
    print("[OK] Test 4 passed: Utilization")
    
    print("\n[SUCCESS] All tests passed!")
    sys.exit(0)
